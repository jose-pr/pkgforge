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
    PkgForgeError,
    UsageError,
    FileEntryArgs,
    entry_from_args,
    resolve_entry,
    _parse_filetype,
)
from .exclude import ExcludeArgs, PathMatch


class ScanCmd(FileEntryArgs, ExcludeArgs, PkgForgeCmd):
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
        # follow_final=False (unlike install's DESTINATION): the leaf is
        # never followed here, only realpath(parent) is checked, so a
        # symlink PATH pointing at a directory outside the root is recorded
        # as a symlink below, not refused. An escaping *ancestor* component
        # is still refused by _rootpath itself.
        scanpath = self._rootpath(self.path, follow_final=False)
        if not os.path.lexists(scanpath):
            raise UsageError(
                f"{self.path!r} does not exist under build root "
                f"{os.fspath(self.buildroot)!r}"
            )
        # True when PATH normalizes to the root itself ("/", or a plain
        # "."): PATH "/" must still be walked even when --buildroot itself
        # resolves through a symlink, so the symlink-leaf check below is
        # skipped for it.
        is_root = scanpath == Path(self.buildroot)
        db = self.loaddb() if self.missing else {}
        baseentry = entry_from_args(self, type=AUTO)
        filter = PathMatch(self.exclude, scanpath)
        self._logger_.info("Scanning %s", scanpath)

        recorded = 0

        def _scanfile(path: Path) -> bool:
            """Record ``path``'s entry (unless already known, under
            ``--missing``). Returns ``False`` only when ``-X`` excluded
            ``path`` -- the signal the walk below uses to prune an excluded
            directory's subtree, so it no longer descends into it and
            records its contents anyway (matching ``install``).
            """
            nonlocal recorded
            try:
                # meta=dict(self.meta): a (?meta:k=v) inline test then sees
                # this run's -O values, the same as it always has in dbdump
                # (scan/install build the entry from disk, which never
                # carries meta on its own).
                if self.exclude and filter.match(path, meta=dict(self.meta)):
                    self._logger_.debug("Excluding %s", path)
                    return False
                fspath = os.fspath(self.buildpath(path))
                if db.get(fspath) is None:
                    self._logger_.debug("Updating file entry for: %s", fspath)
                    self.add_entry(fspath, entry=resolve_entry(baseentry, path))
                    recorded += 1
                return True
            except TypeError as exc:
                # FileType.from_path raises TypeError for a fifo/socket. A
                # glob-only -X (e.g. '*.fifo') already skipped such a path
                # above without ever reaching here (PathMatch builds the
                # entry lazily); this is the path scan cannot record at all.
                raise PkgForgeError(
                    f"{path}: unsupported file type (not a file, directory "
                    "or symlink); exclude it with -X"
                ) from exc

        if not is_root and scanpath.is_symlink():
            # A symlink PATH (a link to a directory included) is recorded as
            # a single symlink entry, never followed -- otherwise its
            # target's contents would be recorded under the link's own
            # path, and an absolute target would walk the build HOST's
            # filesystem instead of the build root.
            _scanfile(scanpath)
        elif not scanpath.is_dir():
            _scanfile(scanpath)
        else:
            for top, dirs, files in os.walk(scanpath):
                # Prune in place: os.walk only descends into names still
                # left in `dirs` after this line runs.
                dirs[:] = [d for d in dirs if _scanfile(Path(top, d))]
                for file in files:
                    _scanfile(Path(top, file))

        self._logger_.info("Scanned %s: %d path(s) recorded", scanpath, recorded)


ScanCmd._register()
