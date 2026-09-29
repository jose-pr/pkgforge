"""``scan`` subcommand: walk a path and record file entries into the DB."""

from __future__ import annotations

import argparse
import os
import typing
from pathlib import Path

import duho

from .errors import UsageError
from .entry import (
    AUTO,
    DEFAULT,
    FileType,
    FileEntryArgs,
    entry_from_args,
    normalize_mode,
    _parse_filetype,
    _parse_mode,
)
from ._tree import TreeRecorder
from .command import PkgForgeCmd
from .exclude import ExcludeArgs, PathMatch, log_unreachable


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
    dir_mode: duho.Arg[typing.Optional[str], duho.NS(type=_parse_mode)] = None
    (
        "mode for directory entries only -- 1-4 octal digits, '-', '--'/'auto' "
        "(resolve from the on-disk directory); default: '--' when --mode is "
        "'--', else '-'. -m/--mode never applies to a directory or a symlink"
    )
    ("--dir-mode",)
    drop_stale: bool = False
    (
        "after scanning, remove (tombstone) each DB entry below PATH whose "
        "file is gone from the build root; an entry matching -X is kept "
        "(protects a deliberately-absent entry, e.g. an RPM %ghost). "
        "Needs a --db file"
    )
    ("--drop-stale",)
    path: str
    "path under the build root to scan"
    ("path",)

    def __call__(self):
        if self.drop_stale and self._no_file_db():
            raise UsageError("--drop-stale needs a --db file (not unset or '-')")
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
        base = entry_from_args(self, type=AUTO)
        # -m/--mode applies to regular files only. A directory takes the
        # new --dir-mode instead: the explicit value if given; else AUTO
        # (read from disk) when --mode itself is AUTO, so --mode=-- still
        # reads directories from disk too; else DEFAULT ('-') -- an
        # explicit file mode is never inherited by directories. A symlink's
        # mode is always DEFAULT: Linux ignores it, and rpm/debian consumers
        # warn about (or reject) an explicit mode on one.
        if self.dir_mode is not None:
            # normalize_mode again: a direct Python-API construction (not
            # through the CLI's --dir-mode type= converter) can hand this a
            # raw, unnormalized value; normalize_mode is idempotent on an
            # already-normalized one.
            dirmode = normalize_mode(self.dir_mode)
        elif base["mode"] == AUTO:
            dirmode = AUTO
        else:
            dirmode = DEFAULT
        bases = {
            FileType.File: base,
            FileType.Directory: {**base, "mode": dirmode},
            FileType.Symlink: {**base, "mode": DEFAULT},
        }
        # installroot is the install path scanpath itself will have in the
        # DB (the same coordinate install/dbdump use); --drop-stale below
        # reuses this same string instead of recomputing it.
        filter = PathMatch(
            self.exclude, scanpath, installroot=self.buildpath(scanpath).as_posix()
        )
        log_unreachable(filter, self._logger_)
        self._logger_.info("Scanning %s", scanpath)

        # `dbdump` OUTPUT files (rpm-files.txt, debian/) are not recognizable
        # by TreeRecorder's DB-file skip and are documented instead -- keep
        # them outside --buildroot too.
        matcher = filter if self.exclude else None
        recorder = TreeRecorder(self, bases, matcher, dict(self.meta), db)

        # Batches the walk's own writes (a no-op except for sqlite, where it
        # holds one connection and commits periodically instead of once per
        # file); committed before --drop-stale's own reload below, which
        # needs a fresh load() to see what this walk just wrote.
        with self._db_batch():
            if not is_root and scanpath.is_symlink():
                # A symlink PATH (a link to a directory included) is
                # recorded as a single symlink entry, never followed --
                # otherwise its target's contents would be recorded under
                # the link's own path, and an absolute target would walk
                # the build HOST's filesystem instead of the build root.
                recorder.record(scanpath)
            elif not scanpath.is_dir():
                recorder.record(scanpath)
            else:
                recorder.walk(scanpath)

        self._logger_.info(
            "Scanned %s: %d path(s) recorded", scanpath, recorder.recorded
        )

        if self.drop_stale:
            # Reload from disk regardless of --missing (the walk's own `db`
            # is {} without it): drop-stale reasons about the DB's current
            # state, not what the walk happened to see in memory.
            dropdb = self.loaddb()
            scan_buildpath = filter.installroot
            prefix = scan_buildpath if scan_buildpath == "/" else scan_buildpath + "/"
            dropped = 0
            for key, entry in dropdb.items():
                if entry is None or key == scan_buildpath or not key.startswith(prefix):
                    continue
                real_path = self.localpath(key)
                if self.exclude and filter.match(real_path, entry=entry):
                    self._logger_.debug("Keeping excluded stale entry: %s", key)
                    continue
                if os.path.lexists(real_path):
                    continue
                self._logger_.debug("Dropping stale entry for: %s", key)
                self.remove_entry(key)
                dropped += 1
            self._logger_.info(
                "Dropped %d stale entr%s below %s",
                dropped,
                "y" if dropped == 1 else "ies",
                scanpath,
            )


ScanCmd._register()
