"""``dbdump``: render the file DB into a packaging manifest."""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import typing
from pathlib import Path

from ..db import Db, parse_jsonl
from ..dbdump import DumpFormat, dump
from ..entry import DEFAULT
from ..exceptions import DbError, UsageError
from ._common import ExcludeArgs, ProjectCmd, _refuse_mcp_stdin

__all__ = ["DbDump"]


@contextlib.contextmanager
def _open_stdout() -> typing.Iterator[typing.Any]:
    """A binary stream onto the process's stdout, never closing it.

    Flushes ``sys.stdout`` and writes through its ``.buffer``, so output stays
    in order with text already printed and works where stdout has no file
    descriptor (pytest capture, ``redirect_stdout``). Without a ``.buffer`` the
    bytes are decoded with ``surrogateescape`` and written as text.
    """
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


class DbDump(ExcludeArgs, ProjectCmd):
    """Dump the file DB into a packaging manifest (rpmspecfiles or debian)."""

    _parsername_ = "dbdump"
    _logger_name_ = "pkgforge.dbdump"

    format: str
    (
        "output format: rpmspecfiles (aliases rpm, rpmspec; rpm 4.19+), "
        "rpmspecfiles-pre419 (alias rpm-pre419; rpm older than 4.19), "
        "debian (alias deb), or a registered DumpFormat subclass's NAME"
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

    def _check_target(self) -> typing.Tuple[str, typing.Optional[Path]]:
        """The format's canonical name and the OUTPUT path (``None`` for
        stdout), after checking the OUTPUT fits the format.

        Runs before the DB is read, so a bad ``--format`` or OUTPUT exits 2
        without touching stdin.
        """
        fmt = DumpFormat.lookup(self.format)
        if str(self.output) == DEFAULT:
            return fmt.NAME, None
        fmt().check_output(self.output)
        return fmt.NAME, self.output

    def _stdin_db(self) -> Db:
        """Read the DB as JSON Lines from stdin (``--db -``).

        Stdin must be a pipe, a socket or a regular file: a terminal, a closed
        stdin or a character device such as ``/dev/null`` raises
        :class:`UsageError`, so an inherited ``PKGFORGE_DB=-`` never dumps an
        empty manifest. A pipe that is never closed blocks, as any ``-``
        source does. ``--db-format`` is not consulted: the input is always
        JSON Lines.
        """
        _refuse_mcp_stdin("the file DB")
        if sys.stdin is None:
            raise UsageError("--db - reads the file DB from stdin, but stdin is closed")
        try:
            mode = os.fstat(sys.stdin.fileno()).st_mode
        except (OSError, ValueError, AttributeError):
            mode = None  # an in-memory stream: neither a terminal nor a device
        if mode is not None and not (
            stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISREG(mode)
        ):
            raise UsageError(
                "--db - reads the file DB from stdin, but stdin is a terminal "
                "or a device; pipe or redirect JSON Lines records"
            )
        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is not None:
            try:
                text = buffer.read().decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise DbError(f"<stdin>: {exc}") from exc
        else:
            text = sys.stdin.read().removeprefix("﻿")
        db = parse_jsonl(text, "<stdin>")
        if not db:
            self._logger_.warning("stdin held no DB records; dumping an empty manifest")
        return db

    def __call__(self) -> int:
        fmt, output = self._check_target()
        if str(self.db) == "-":
            rendered = dump(self._stdin_db(), fmt, output, exclude=self.exclude)
        else:
            rendered = self._project().dump(fmt, output, exclude=self.exclude)
        if output is None:
            with _open_stdout() as out:
                if isinstance(rendered, bytes):
                    out.write(rendered)
                else:
                    for name, data in rendered.items():
                        out.write(f"# === {name} ===\n".encode())
                        out.write(data)
        return 0
