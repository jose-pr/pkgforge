"""``scan`` subcommand: walk a path and record file entries into the DB."""

from __future__ import annotations

import argparse
import os
import typing
from pathlib import Path

import duho

from .common import (
    AUTO,
    FileType,
    PkgForgeCmd,
    FileEntryArgs,
    entry_from_args,
    resolve_entry,
    _parse_filetype,
)
from .exclude import PathMatch, PathMatchStmt


class ScanCmd(FileEntryArgs, PkgForgeCmd):
    """Scan a path under the build root and record each file's entry in the DB.

    ``--type/-t`` is hidden from ``--help`` and, unlike ``install``, never
    applied: scan always records each path's own on-disk type (a single type
    stamped over a whole tree would be nonsense). An explicit value logs a
    warning instead of silently doing nothing.
    """

    _parsername_ = "scan"
    _logger_name_ = "pkgforge.scan"

    type: duho.Arg[
        typing.Optional[FileType],
        duho.NS(type=_parse_filetype, help=argparse.SUPPRESS),
    ] = None
    ("--type", "-t")
    exclude: duho.Arg[
        typing.List[PathMatchStmt],
        duho.Append(PathMatchStmt.parse, metavar="STMT"),
    ] = []
    "exclude paths matching STMT (repeatable); see the exclude-pattern guide for the grammar"
    ("--exclude", "-X")
    missing: bool = False
    "only record entries absent from the DB, leaving existing ones untouched"
    ("--missing",)
    path: str
    "path under the build root to scan"
    ("path",)

    def __call__(self):
        if self.type not in (None, AUTO):
            self._logger_.warning(
                "scan records each path's on-disk type; --type is ignored"
            )
        scanpath = self._rootpath(self.path, follow_final=True)
        db = self.loaddb() if self.missing else {}
        baseentry = entry_from_args(self, type=AUTO)
        filter = PathMatch(self.exclude, scanpath)
        self._logger_.info("Scanning %s", scanpath)

        recorded = 0

        def _scanfile(path: Path):
            nonlocal recorded
            if self.exclude and filter.match(path):
                self._logger_.debug("Excluding %s", path)
                return
            fspath = os.fspath(self.buildpath(path))
            if db.get(fspath) is None:
                self._logger_.debug("Updating file entry for: %s", fspath)
                self.add_entry(fspath, entry=resolve_entry(baseentry, path))
                recorded += 1

        if not scanpath.is_dir():
            _scanfile(scanpath)
        else:
            for top, dirs, files in os.walk(scanpath):
                for file in [*dirs, *files]:
                    _scanfile(Path(top, file))

        self._logger_.info("Scanned %s: %d path(s) recorded", scanpath, recorded)


ScanCmd._register()
