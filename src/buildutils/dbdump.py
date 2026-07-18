"""``dbdump`` subcommand: render the file DB into a packaging manifest."""

from __future__ import annotations

import json
import os
import sys
import typing
from pathlib import Path

import duho

from .common import BuildUtil, FileEntry
from .exclude import PathMatch, PathMatchStmt


class FormatDumper(typing.Protocol):
    def __call__(self, path: str, entry: FileEntry) -> bytes: ...


def rpmspecfile(path: str, entry: FileEntry) -> bytes:
    prefix = entry["meta"].get("rpmprefix") or ""
    if prefix:
        prefix += " "
    if entry["type"] == "directory":
        prefix += "%dir "

    return (
        f"{prefix}%attr({entry['mode']},{entry['owner']},{entry['group']}) "
        f"{json.dumps(path)}\n"
    ).encode()


DUMP_FORMATS: "typing.Dict[str, FormatDumper]" = {"rpmspecfiles": rpmspecfile}


class DbDump(BuildUtil):
    """Dump the file DB into a packaging manifest (e.g. an RPM file list)."""

    _parsername_ = "dbdump"

    exclude: duho.Arg[
        typing.List[PathMatchStmt],
        duho.NS(type=PathMatchStmt.parse, action="append"),
    ] = []
    ("--exclude", "-X")
    format: str
    ("--format", "-f")
    output: Path = Path("-")
    ("output",)

    def __call__(self):
        db = self.loaddb()
        dumper = DUMP_FORMATS[self.format]
        out = None
        filter = PathMatch(self.exclude)
        try:
            if str(self.output) == "-":
                out = os.fdopen(sys.stdout.fileno(), "wb", closefd=False)
            else:
                out = self.output.open("wb")

            for path, entry in db.items():
                if entry is None or (self.exclude and filter.match(Path(path), entry)):
                    continue
                out.write(dumper(path, entry))
        finally:
            if out:
                out.flush()
                if str(self.output) != "-":
                    out.close()


DbDump._register()
