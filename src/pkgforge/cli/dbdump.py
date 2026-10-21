"""``dbdump``: render the file DB into a packaging manifest."""

from __future__ import annotations

import os
import stat
import sys
import typing
from pathlib import Path

import duho

from ..db import Db, parse_jsonl
from ..dbdump import DumpFormat, dump
from ..entry import DEFAULT
from ..exceptions import DbError, UsageError
from ._common import ExcludeArgs, ProjectCmd, _open_stdout, _refuse_mcp_stdin

__all__ = ["DbDump"]


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
    dirs: duho.Arg[str, duho.Meta(choices=("all", "owned"))] = "all"
    (
        "directory entries to dump: all, or only those at or below an owned "
        "prefix (--own, or own in pkgforge.cfg)"
    )
    ("--dirs",)

    def _check_target(self) -> typing.Tuple[str, typing.Optional[Path]]:
        """The format's canonical name and the OUTPUT path (``None`` for
        stdout), after checking the OUTPUT fits the format.

        Runs before the DB is read, so a bad ``--format`` or OUTPUT exits 2
        without touching stdin.
        """
        fmt = DumpFormat.get(self.format)
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
        source does. ``--db-format`` is validated but not used: the input is
        always JSON Lines.
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
        project = self._project()  # checks --db-format even with --db -
        if str(self.db) == "-":
            own = project._dump_own(self.dirs)
            rendered = dump(
                self._stdin_db(), fmt, output, exclude=self.exclude, own=own
            )
        else:
            rendered = project.dump(fmt, output, exclude=self.exclude, dirs=self.dirs)
        if output is None:
            with _open_stdout() as out:
                if isinstance(rendered, bytes):
                    out.write(rendered)
                else:
                    for name, data in rendered.items():
                        out.write(f"# === {name} ===\n".encode())
                        out.write(data)
        return 0
