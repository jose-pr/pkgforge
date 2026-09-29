"""``dbdump`` subcommand: render the file DB into packaging manifests.

Two shapes of format are supported, each a :class:`DumpFormat` subclass:

* :class:`PerEntryFormat` (e.g. :class:`~pkgforge.dbdump.rpm.RpmSpecFiles`)
  renders one line per DB entry, written to a single output stream (a file,
  or ``-`` for stdout);
* :class:`MultiArtifactFormat` (e.g. :class:`~pkgforge.dbdump.debian.Debian`)
  renders several related files (e.g. an ``install`` path list plus a
  ``permissions`` manifest) into an output *directory* -- or, when the
  output is ``-``, concatenated to stdout under ``# === <name> ===`` section
  headers.

A third party adds a format by subclassing one of the two with its own
``NAME`` (and, optionally, ``ALIASES``) -- no registration call is needed,
subclassing alone registers it (see :mod:`pkgforge._registry`).
"""

from __future__ import annotations

import abc
import contextlib
import re
import sys
import typing
from pathlib import Path

from .._registry import Registered
from ..db import Db, DbError
from ..db.jsonl import _parse_jsonl
from ..entry import DEFAULT, FileEntry
from ..errors import PkgForgeError, UsageError
from ..command import PkgForgeCmd
from ..exclude import ExcludeArgs, PathMatch

#: An entry that survived filtering: (db-path, FileEntry).
Entries = typing.List[typing.Tuple[str, FileEntry]]


class DumpError(PkgForgeError, ValueError):
    """A DB entry cannot be rendered in the chosen format.

    Raised for a path (or, for ``debian``, a mode/owner/group) the target
    format's own tooling cannot represent -- a control character, or (rpm
    only) a ``%``. Caught by :func:`pkgforge.main`'s error boundary like any
    :class:`~pkgforge.errors.PkgForgeError`: one stderr line, exit 1. Nothing
    is written to OUTPUT first: every entry is rendered before OUTPUT is
    opened, so this leaves no partial file.
    """


class UnsupportedOutputError(UsageError, DumpError, NotImplementedError):
    """OUTPUT is the wrong shape for the chosen format: a file for a
    multi-artifact format, or an existing directory for a per-entry one.

    Subclasses :class:`~pkgforge.errors.UsageError` (exit 2, same as this
    replaces two separate raises with) and :class:`DumpError` (the format
    itself is refusing this OUTPUT, not a bad argument in general) and, on
    top of both, :class:`NotImplementedError` (this format cannot write
    this output shape) -- a valid MRO (measured, py3.14).
    """


#: Every C0 control character (0x00-0x1f) plus DEL (0x7f): rpm and debhelper
#: both choke on these one way or another (a bare newline splits a line, a
#: literal DEL is simply illegal), and neither format has an escape for them.
_CONTROL_RE = re.compile("[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s")


def _reject_control(path: str, fmt: str) -> None:
    if _CONTROL_RE.search(path):
        raise DumpError(
            f"{fmt}: a DB entry's path contains a control character that "
            "cannot be represented in this format"
        )


@contextlib.contextmanager
def _open_output(output: Path):
    """Open OUTPUT for writing: the real file for a path, or a stream onto
    the process's actual stdout for ``-``.

    Never wraps ``sys.stdout``'s raw file descriptor directly: that bypasses
    Python's own stdout buffer entirely, so it (a) raises
    ``io.UnsupportedOperation`` whenever ``sys.stdout`` isn't backed by a
    real file descriptor (``redirect_stdout``, embedding, pytest capture)
    and (b) writes out of order with text the caller already ``print()``-ed
    but hasn't flushed. Flushing ``sys.stdout`` first, then writing through
    its own ``.buffer`` (or, lacking one, decoding back to text with
    ``surrogateescape`` and writing through ``sys.stdout`` itself), keeps
    both cases correct. Never closes ``sys.stdout``.
    """
    if str(output) == DEFAULT:
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
        with output.open("wb") as f:
            yield f


class DumpFormat(Registered, abc.ABC):
    """A packaging-manifest format, selected by ``dbdump -f NAME``.

    A subclass registers by declaring its own ``NAME`` (plus, optionally,
    ``ALIASES``); see :class:`pkgforge._registry.Registered`. Subclass
    :class:`PerEntryFormat` or :class:`MultiArtifactFormat`, not this class
    directly -- they supply :meth:`check_output` and :meth:`dump` for their
    respective output shape.
    """

    NAME: typing.ClassVar[str] = ""
    ALIASES: typing.ClassVar[typing.Tuple[str, ...]] = ()
    _registry: typing.ClassVar[typing.Dict[str, type]] = {}
    _KIND = "format"

    @abc.abstractmethod
    def check_output(self, output: Path) -> None:
        """Raise :class:`UnsupportedOutputError` if ``output`` is the wrong
        shape for this format. Always returns for ``-`` (stdout)."""

    @abc.abstractmethod
    def dump(
        self,
        entries: Entries,
        output: Path,
        logger: typing.Optional[typing.Any] = None,
    ) -> None:
        """Check ``output``, render ``entries``, then write them to it."""


class PerEntryFormat(DumpFormat):
    """A format that renders one line per DB entry to a single stream."""

    @abc.abstractmethod
    def render_entry(self, path: str, entry: FileEntry) -> bytes:
        """Render one DB entry to its line of bytes."""

    def render(self, entries: Entries) -> bytes:
        return b"".join(self.render_entry(path, entry) for path, entry in entries)

    def check_output(self, output: Path) -> None:
        if str(output) == DEFAULT:
            return
        if output.is_dir():
            raise UnsupportedOutputError(
                f"{self.NAME} writes one file; OUTPUT is a directory: {output}"
            )

    def dump(
        self,
        entries: Entries,
        output: Path,
        logger: typing.Optional[typing.Any] = None,
    ) -> None:
        self.check_output(output)
        rendered = self.render(entries)
        with _open_output(output) as out:
            out.write(rendered)


class MultiArtifactFormat(DumpFormat):
    """A format that renders several named artifacts into a directory (or,
    for ``-``, concatenates them to stdout under section headers)."""

    @abc.abstractmethod
    def render(self, entries: Entries) -> typing.Dict[str, bytes]:
        """Render every artifact this format produces: ``{filename: bytes}``."""

    def check_output(self, output: Path) -> None:
        if str(output) == DEFAULT:
            return
        if output.exists() and not output.is_dir():
            raise UnsupportedOutputError(
                f"{self.NAME} writes several files; OUTPUT must be a "
                f"directory or '-', got existing file {output}"
            )

    def dump(
        self,
        entries: Entries,
        output: Path,
        logger: typing.Optional[typing.Any] = None,
    ) -> None:
        self.check_output(output)
        artifacts = self.render(entries)
        if str(output) == DEFAULT:
            with _open_output(output) as out:
                for name, data in artifacts.items():
                    out.write(f"# === {name} ===\n".encode())
                    out.write(data)
            return
        output.mkdir(parents=True, exist_ok=True)
        for name, data in artifacts.items():
            (output / name).write_bytes(data)
            if logger is not None:
                logger.info("Wrote %s", output / name)


class DbDump(ExcludeArgs, PkgForgeCmd):
    """Dump the file DB into a packaging manifest (rpmspecfiles or debian)."""

    _parsername_ = "dbdump"
    _logger_name_ = "pkgforge.dbdump"

    format: str
    (
        "output format: rpmspecfiles (aliases rpm, rpmspec), debian (alias "
        "deb), or a registered DumpFormat subclass's NAME"
    )
    ("--format", "-f")
    output: Path = Path("-")
    (
        "OUTPUT: a file for a per-entry format (e.g. rpmspecfiles), or a "
        "directory for a multi-artifact format (e.g. debian, created if "
        "missing); '-' for stdout (concatenated, sectioned, for a "
        "multi-artifact format)"
    )
    ("output",)
    stdin: bool = False
    (
        "read the file DB as JSON Lines from standard input (e.g. piped "
        "from install or scan run without --db) instead of --db"
    )
    ("--stdin",)

    def _check_target(self) -> DumpFormat:
        """Validate ``--format``/OUTPUT before anything reads the DB.

        Checked against the live registry at call time (never
        ``duho.Choice``, which would freeze the choices at import and reject
        a format a third party registers later): an unknown format, or an
        OUTPUT of the wrong shape for the chosen format, both exit 2 with one
        line naming the problem instead of surfacing whatever the DB load
        happens to raise first (e.g. a JSON parse error for an unrelated
        format typo). Returns the constructed format instance, its
        ``check_output`` already run.
        """
        cls = DumpFormat.lookup(self.format)
        self.format = cls.NAME
        instance = cls()
        instance.check_output(self.output)
        return instance

    def _file_db(self) -> Db:
        """Load the DB from ``--db``/``PKGFORGE_DB``, warning (never erroring)
        for every case the file DB itself already reads as empty for."""
        reason = self._no_db_reason()
        if reason:
            if str(self.db) == "-":
                reason += "; pass --stdin to read records from standard input"
            self._logger_.warning("%s; dumping an empty manifest", reason)
        elif not self.db.exists():
            self._logger_.warning(
                "DB %s does not exist; dumping an empty manifest", self.db
            )
        return self.loaddb()

    def _stdin_db(self) -> Db:
        """Read the DB as JSON Lines from stdin (``--stdin``).

        Runs only after :meth:`_check_target` already validated
        ``--format``/OUTPUT (see ``__call__``), so a bad one exits 2 without
        ever touching stdin. Never blocks unless explicitly told to: a
        closed or terminal stdin raises immediately; otherwise this reads to
        EOF, which *does* block on a pipe that is never closed -- that is
        the caller's responsibility, exactly as for an ``install -`` source.
        ``--db``/``--db-format`` are ignored here (only noted at DEBUG),
        never consulted.
        """
        if self.db is not None:
            self._logger_.debug("--stdin: ignoring --db %s", self.db)
        if sys.stdin is None:
            raise UsageError(
                "--stdin reads the file DB from stdin, but stdin is closed"
            )
        if sys.stdin.isatty():
            raise UsageError(
                "--stdin reads the file DB from stdin, but stdin is a "
                "terminal; pipe or redirect JSON Lines records"
            )
        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is not None:
            raw = buffer.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DbError(f"<stdin>: {exc}") from exc
        else:
            text = sys.stdin.read()
        db = _parse_jsonl(text, "<stdin>")
        if not db:
            self._logger_.warning("stdin held no DB records; dumping an empty manifest")
        return db

    def _surviving_entries(self) -> Entries:
        db = self._stdin_db() if self.stdin else self._file_db()
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

    def __call__(self):
        fmt = self._check_target()
        entries = self._surviving_entries()
        fmt.dump(entries, self.output, self._logger_)


# --------------------------------------------------------------------------
# Built-in formats: importing each module registers its format class.
# --------------------------------------------------------------------------

from .rpm import RpmSpecFiles  # noqa: E402
from .debian import Debian  # noqa: E402

DbDump._register()

__all__ = [
    "Debian",
    "DbDump",
    "DumpError",
    "DumpFormat",
    "Entries",
    "MultiArtifactFormat",
    "PerEntryFormat",
    "RpmSpecFiles",
    "UnsupportedOutputError",
]
