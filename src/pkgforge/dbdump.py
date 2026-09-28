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
import json
import os
import sys
import typing
from pathlib import Path

from .common import DEFAULT, FileType, PkgForgeCmd, FileEntry, UsageError, _or_default
from .exclude import ExcludeArgs, PathMatch

#: An entry that survived filtering: (db-path, FileEntry).
Entries = typing.List[typing.Tuple[str, FileEntry]]


class PerEntryDumper(typing.Protocol):
    """Render a single DB entry to one line of bytes."""

    def __call__(self, path: str, entry: FileEntry) -> bytes: ...


# --------------------------------------------------------------------------
# rpm (per-entry)
# --------------------------------------------------------------------------


def rpmspecfile(path: str, entry: FileEntry) -> bytes:
    prefix = entry["meta"].get("rpmprefix") or ""
    if prefix:
        prefix += " "
    if entry["type"] == FileType.Directory:
        prefix += "%dir "

    mode = _or_default(entry["mode"])
    owner = _or_default(entry["owner"])
    group = _or_default(entry["group"])

    return (f"{prefix}%attr({mode},{owner},{group}) {json.dumps(path)}\n").encode()


# --------------------------------------------------------------------------
# debian (multi-artifact: install list + permissions manifest)
# --------------------------------------------------------------------------


def _debian_artifacts(entries: Entries) -> typing.Dict[str, bytes]:
    """Build Debian packaging artifacts from surviving DB entries.

    Returns a mapping of artifact filename -> bytes:

    * ``install`` -- ``dh_install``-style lines ``<src>  <dest-dir>`` (the source
      is the build-root-relative path, the destination is the entry's parent
      directory), one per non-directory entry;
    * ``permissions`` -- ``<path> <mode> <owner> <group>`` lines
      (``dpkg-statoverride``-friendly) for every entry that pins a non-default
      mode/owner/group.
    """
    install_lines: typing.List[str] = []
    perm_lines: typing.List[str] = []
    for path, entry in entries:
        rel = path.lstrip("/")
        if entry["type"] != FileType.Directory:
            dest_dir = os.path.dirname(rel)
            install_lines.append(f"{rel} {dest_dir}".rstrip())
        mode = _or_default(entry["mode"])
        owner = _or_default(entry["owner"])
        group = _or_default(entry["group"])
        if mode != DEFAULT or owner != DEFAULT or group != DEFAULT:
            perm_lines.append(f"{path} {mode} {owner} {group}")

    def _join(lines: typing.List[str]) -> bytes:
        return ("\n".join(lines) + "\n" if lines else "").encode()

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
