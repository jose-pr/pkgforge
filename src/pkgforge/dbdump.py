"""``dbdump`` subcommand: render the file DB into packaging manifests.

Two shapes of format are supported:

* **per-entry** formats (``rpmspecfiles``) render one line per DB entry and are
  written to a single output stream (a file, or ``-`` for stdout);
* **multi-artifact** formats (``debian``) render several related files (an
  ``install`` path list plus a ``permissions`` manifest) into an output
  *directory* -- or, when the output is ``-``, concatenated to stdout under
  ``# === <name> ===`` section headers.
"""

from __future__ import annotations

import contextlib
import os
import sys
import typing
from pathlib import Path

from .common import (
    DEFAULT,
    FileType,
    PkgForgeCmd,
    PkgForgeError,
    FileEntry,
    UsageError,
    _or_default,
)
from .exclude import ExcludeArgs, PathMatch

#: An entry that survived filtering: (db-path, FileEntry).
Entries = typing.List[typing.Tuple[str, FileEntry]]


class DumpError(PkgForgeError, ValueError):
    """A DB entry cannot be rendered in the chosen format.

    Raised for a path (or, for ``debian``, a mode/owner/group) the target
    format's own tooling cannot represent -- a control character, or (rpm
    only) a ``%``. Caught by :func:`pkgforge.main`'s error boundary like any
    :class:`~pkgforge.common.PkgForgeError`: one stderr line, exit 1. Nothing
    is written to OUTPUT first: every entry is rendered before OUTPUT is
    opened, so this leaves no partial file.
    """


#: Every C0 control character (0x00-0x1f) plus DEL (0x7f): rpm and debhelper
#: both choke on these one way or another (a bare newline splits a line, a
#: literal DEL is simply illegal), and neither format has an escape for them.
_CONTROL_CHARS = frozenset(chr(c) for c in range(0x20)) | {"\x7f"}


def _reject_control(path: str, fmt: str) -> None:
    if any(c in _CONTROL_CHARS for c in path):
        raise DumpError(
            f"{fmt}: a DB entry's path contains a control character that "
            "cannot be represented in this format"
        )


class PerEntryDumper(typing.Protocol):
    """Render a single DB entry to one line of bytes."""

    def __call__(self, path: str, entry: FileEntry) -> bytes: ...


# --------------------------------------------------------------------------
# rpm (per-entry)
# --------------------------------------------------------------------------


def _rpm_quote(path: str) -> str:
    """Quote ``path`` for an rpm ``%files``/``%attr`` line (rpm 4.19+).

    rpm's ``%files -f`` parser macro-expands every line BEFORE it sees the
    quoting: on rpm 4.19+ no spelling of ``%`` is literal inside or outside
    double quotes, so a path containing one is refused outright rather than
    escaped (below 4.19, ``%%`` ran the doubled-percent expansion and
    ``%(cmd)`` ran ``cmd`` as a shell command -- there is no fix on that
    range, only refusal; see ``rpm4_quoted_globs.md``). Glob characters
    (``* ? [ ]``) are never escaped: rpm's own shell-globbing quoting rules
    make a quoted glob character match only the literal path anyway.
    """
    _reject_control(path, "rpmspecfiles")
    if "%" in path:
        raise DumpError(
            "rpmspecfiles: a DB entry's path contains '%', which rpm's "
            "%files parser expands as a macro on every rpm version; write "
            "this entry's %files line by hand"
        )
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def rpmspecfile(path: str, entry: FileEntry) -> bytes:
    prefix = entry["meta"].get("rpmprefix") or ""
    if prefix:
        prefix += " "
    if entry["type"] == FileType.Directory:
        prefix += "%dir "

    mode = _or_default(entry["mode"])
    owner = _or_default(entry["owner"])
    group = _or_default(entry["group"])

    quoted = _rpm_quote(path)
    return f"{prefix}%attr({mode},{owner},{group}) {quoted}\n".encode(
        "utf-8", "surrogateescape"
    )


# --------------------------------------------------------------------------
# debian (multi-artifact: install list + permissions manifest)
# --------------------------------------------------------------------------

#: Characters dh_install/dh_installdirs read as shell-glob syntax in a
#: SOURCE line, plus the backslash that escapes them; ``{``/``}`` are
#: included, so escaping them also makes a literal ``${`` read as literal
#: (``$\{``), with no separate ``$`` handling needed on the source side.
_DH_GLOB_CHARS = "\\*?[]{}"


def _dh_src(rel: str) -> str:
    """Escape a debian ``install``/``dirs`` SOURCE (debhelper compat 13+).

    Backslash-escapes every glob character so the name matches only
    itself, and ``${Space}``-escapes a literal space; a leading ``#`` (the
    line's own first character) is backslash-escaped too, since dh_install
    treats a line starting with ``#`` as a comment. A bare ``$`` is left
    alone -- see :func:`_dh_dest`.
    """
    out = []
    for c in rel:
        if c in _DH_GLOB_CHARS:
            out.append("\\" + c)
        elif c == " ":
            out.append("${Space}")
        else:
            out.append(c)
    text = "".join(out)
    if text.startswith("#"):
        text = "\\" + text
    return text


def _dh_dest(rel: str) -> str:
    """Escape a debian ``install``/``dirs`` DESTINATION.

    dh_install never globs the destination (a backslash there is kept
    literally), so only a literal ``${`` (an unresolved compat-13 variable)
    and a space need handling; a bare ``$`` not followed by ``{`` stays
    literal, which also works on compat 12.
    """
    return rel.replace("${", "${Dollar}{").replace(" ", "${Space}")


def _debian_artifacts(entries: Entries) -> typing.Dict[str, bytes]:
    """Build Debian packaging artifacts from surviving DB entries.

    Returns a mapping of artifact filename -> bytes:

    * ``install`` -- ``dh_install``-style lines ``<src> <dest-dir>`` (the
      source is the build-root-relative path, debhelper-escaped; the
      destination is the entry's parent directory, escaped for ``$``/space
      only), one per non-directory entry;
    * ``permissions`` -- a pkgforge-specific ``<path> <mode> <owner>
      <group>`` manifest (unescaped: parse it right-to-left, since the path
      itself may contain spaces) for every entry that pins a non-default
      mode/owner/group.

    Every entry is validated (path free of control characters; mode/owner/
    group free of whitespace) before either artifact is built, so a
    :class:`DumpError` leaves no partial output.
    """
    for path, entry in entries:
        _reject_control(path, "debian")
        for field in ("mode", "owner", "group"):
            value = _or_default(entry[field])
            if any(c.isspace() for c in value):
                raise DumpError(
                    f"debian: a DB entry's {field} contains whitespace, "
                    "which the permissions format cannot represent"
                )

    install_lines: typing.List[str] = []
    perm_lines: typing.List[str] = []
    for path, entry in entries:
        rel = path.lstrip("/")
        mode = _or_default(entry["mode"])
        owner = _or_default(entry["owner"])
        group = _or_default(entry["group"])
        if entry["type"] != FileType.Directory:
            dest_dir = _dh_dest(os.path.dirname(rel))
            install_lines.append(f"{_dh_src(rel)} {dest_dir}".rstrip())
        if mode != DEFAULT or owner != DEFAULT or group != DEFAULT:
            perm_lines.append(f"{path} {mode} {owner} {group}")

    def _join(lines: typing.List[str]) -> bytes:
        text = "\n".join(lines) + "\n" if lines else ""
        return text.encode("utf-8", "surrogateescape")

    return {"install": _join(install_lines), "permissions": _join(perm_lines)}


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

#: Per-entry line formats: name -> dumper.
PER_ENTRY_FORMATS: typing.Dict[str, PerEntryDumper] = {"rpmspecfiles": rpmspecfile}

#: Multi-artifact formats: name -> (entries -> {filename: bytes}).
MULTI_ARTIFACT_FORMATS: typing.Dict[
    str, typing.Callable[[Entries], typing.Dict[str, bytes]]
] = {
    "debian": _debian_artifacts,
}


def dump_formats() -> typing.List[str]:
    """All known format names, sorted (for --help / error messages)."""
    return sorted([*PER_ENTRY_FORMATS, *MULTI_ARTIFACT_FORMATS])


class DbDump(ExcludeArgs, PkgForgeCmd):
    """Dump the file DB into a packaging manifest (rpmspecfiles or debian)."""

    _parsername_ = "dbdump"
    _logger_name_ = "pkgforge.dbdump"

    format: str
    "output format: one of the registered formats (built in: debian, rpmspecfiles)"
    ("--format", "-f")
    output: Path = Path("-")
    (
        "OUTPUT: a file for a per-entry format (e.g. rpmspecfiles), or a "
        "directory for a multi-artifact format (e.g. debian, created if "
        "missing); '-' for stdout (concatenated, sectioned, for a "
        "multi-artifact format)"
    )
    ("output",)

    def _check_target(self) -> None:
        """Validate ``--format``/OUTPUT before anything reads the DB.

        Checked against the live registries at call time (never
        ``duho.Choice``, which would freeze the choices at import and reject
        a format a third party registers later): an unknown format, or an
        OUTPUT of the wrong shape for the chosen format, both exit 2 with one
        line naming the problem instead of surfacing whatever the DB load
        happens to raise first (e.g. a JSON parse error for an unrelated
        format typo).
        """
        known = (
            self.format in PER_ENTRY_FORMATS or self.format in MULTI_ARTIFACT_FORMATS
        )
        if not known:
            raise UsageError(
                f"unknown format {self.format!r}; choose from {', '.join(dump_formats())}"
            )
        if str(self.output) == DEFAULT:
            return
        if self.format in MULTI_ARTIFACT_FORMATS:
            if self.output.exists() and not self.output.is_dir():
                raise UsageError(
                    f"{self.format} writes several files; OUTPUT must be a "
                    f"directory or '-', got existing file {self.output}"
                )
        elif self.output.is_dir():
            raise UsageError(
                f"{self.format} writes one file; OUTPUT is a directory: {self.output}"
            )

    def _surviving_entries(self) -> Entries:
        reason = self._no_db_reason()
        if reason:
            self._logger_.warning("%s; dumping an empty manifest", reason)
        elif not self.db.exists():
            self._logger_.warning(
                "DB %s does not exist; dumping an empty manifest", self.db
            )
        db = self.loaddb()
        matcher = PathMatch(self.exclude)
        entries: Entries = []
        for path, entry in db.items():
            if entry is None or (self.exclude and matcher.match(Path(path), entry)):
                continue
            entries.append((path, entry))
        if db and self.exclude and not entries:
            live = sum(1 for entry in db.values() if entry is not None)
            self._logger_.warning(
                "0 of %d entries survived --exclude filtering; dumping an "
                "empty manifest",
                live,
            )
        # Sorted by DB path (code-point order), not backend/insertion/readdir
        # order: neither rpm nor dh_install give the output order any
        # meaning, so this is what makes a staged tree give byte-identical
        # manifests on any filesystem and any backend.
        entries.sort(key=lambda pathentry: pathentry[0])
        return entries

    @contextlib.contextmanager
    def _open_output(self):
        """Open OUTPUT for writing: the real file for a path, or a stream
        onto the process's actual stdout for ``-``.

        Never opens a raw ``os.fdopen(sys.stdout.fileno(), ...)``: that
        bypasses Python's own stdout buffer entirely, so it (a) raises
        ``io.UnsupportedOperation`` whenever ``sys.stdout`` isn't backed by a
        real file descriptor (``redirect_stdout``, embedding, pytest capture)
        and (b) writes out of order with text the caller already ``print()``-ed
        but hasn't flushed. Flushing ``sys.stdout`` first, then writing
        through its own ``.buffer`` (or, lacking one, decoding back to text
        with ``surrogateescape`` and writing through ``sys.stdout`` itself),
        keeps both cases correct. Never closes ``sys.stdout``.
        """
        if str(self.output) == DEFAULT:
            sys.stdout.flush()
            buf = getattr(sys.stdout, "buffer", None)
            if buf is None:

                class _TextAdapter:
                    def write(self, data: bytes) -> None:
                        sys.stdout.write(data.decode("utf-8", "surrogateescape"))

                buf = _TextAdapter()
            try:
                yield buf
            finally:
                sys.stdout.flush()
        else:
            with self.output.open("wb") as f:
                yield f

    def __call__(self):
        self._check_target()
        entries = self._surviving_entries()

        if self.format in PER_ENTRY_FORMATS:
            dumper = PER_ENTRY_FORMATS[self.format]
            rendered = b"".join(dumper(path, entry) for path, entry in entries)
            with self._open_output() as out:
                out.write(rendered)
            return

        if self.format in MULTI_ARTIFACT_FORMATS:
            artifacts = MULTI_ARTIFACT_FORMATS[self.format](entries)
            if str(self.output) == DEFAULT:
                with self._open_output() as out:
                    for name, data in artifacts.items():
                        out.write(f"# === {name} ===\n".encode())
                        out.write(data)
            else:
                self.output.mkdir(parents=True, exist_ok=True)
                for name, data in artifacts.items():
                    (self.output / name).write_bytes(data)
                    self._logger_.info("Wrote %s", self.output / name)
            return


DbDump._register()
