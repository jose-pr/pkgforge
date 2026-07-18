"""``scan`` subcommand: walk a path and record file entries into the DB."""

from __future__ import annotations

import os
import typing
from pathlib import Path

import duho

from .common import BuildUtil, FileEntry, FileEntryArgs
from .exclude import PathMatch, PathMatchStmt


class ScanCmd(FileEntryArgs, BuildUtil):
    """Scan a path under the build root and record each file's entry in the DB."""

    _parsername_ = "scan"

    exclude: duho.Arg[
        typing.List[PathMatchStmt],
        duho.NS(type=PathMatchStmt.parse, action="append"),
    ] = []
    ("--exclude", "-X")
    missing: bool = False
    ("--missing",)
    path: str
    ("path",)

    def __call__(self):
        db = self.loaddb() if self.missing else {}
        baseentry = FileEntry.from_args(self, type="--")
        scanpath = self.buildroot / self.path.lstrip("/")
        filter = PathMatch(self.exclude, scanpath)
        self._logger_.info("Scanning %s", scanpath)

        def _scanfile(path: Path):
            if self.exclude and filter.match(path):
                self._logger_.debug("Excluding %s", path)
                return
            fspath = os.fspath(self.buildpath(path))
            if db.get(fspath) is None:
                self._logger_.info("Updating file entry for: %s", fspath)
                self.add_entry(fspath, entry=FileEntry.resolve_for(baseentry, path))

        if not scanpath.is_dir():
            _scanfile(scanpath)
        else:
            for top, dirs, files in os.walk(scanpath):
                for file in [*dirs, *files]:
                    _scanfile(Path(top, file))


ScanCmd._register()
