"""``scan`` subcommand: walk a path and record file entries into the DB."""

from __future__ import annotations

import argparse
import os
import typing
from pathlib import Path

import duho

from .errors import PkgForgeError, UsageError
from .entry import (
    AUTO,
    DEFAULT,
    FileType,
    FileEntryArgs,
    entry_from_args,
    normalize_mode,
    _parse_filetype,
    _parse_mode,
    _file_type,
    _resolve_stat,
)
from .command import PkgForgeCmd
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
        filter = PathMatch(self.exclude, scanpath)
        self._logger_.info("Scanning %s", scanpath)

        # Precomputed ONCE (never per walked path, so the walk stays cheap):
        # the file DB's own directory (realpath) and the four basenames that
        # matter -- the DB file itself, and a sqlite backend's -journal/-wal/
        # -shm sidecars, which exist only transiently during a write but cost
        # nothing to also name here. `_scanfile` below only pays for a second
        # `realpath` call (to confirm it's the SAME directory, not merely a
        # same-named file elsewhere in the tree) on an actual basename hit.
        # `dbdump` OUTPUT files (rpm-files.txt, debian/) are not recognizable
        # here and are documented instead -- keep them outside --buildroot too.
        db_skip_dir = db_skip_names = None
        if not self._no_file_db():
            db_path = Path(self.db)
            db_skip_dir = os.path.realpath(db_path.parent)
            db_skip_names = {
                db_path.name + suffix for suffix in ("", "-journal", "-wal", "-shm")
            }
        warned_db_skip = False

        recorded = 0

        def _scanfile(path: Path) -> bool:
            """Record ``path``'s entry (unless already known, under
            ``--missing``). Returns ``False`` only when ``-X`` excluded
            ``path`` -- the signal the walk below uses to prune an excluded
            directory's subtree, so it no longer descends into it and
            records its contents anyway (matching ``install``).
            """
            nonlocal recorded, warned_db_skip
            try:
                if db_skip_names is not None and path.name in db_skip_names:
                    if os.path.realpath(path.parent) == db_skip_dir:
                        if warned_db_skip:
                            self._logger_.debug("Skipping the file DB %s", path)
                        else:
                            self._logger_.warning(
                                "Skipping the file DB %s found inside the "
                                "scanned tree; keep --db (and dbdump output) "
                                "outside --buildroot",
                                path,
                            )
                            warned_db_skip = True
                        return True
                # meta=dict(self.meta): a (?meta:k=v) inline test then sees
                # this run's -O values, the same as it always has in dbdump
                # (scan/install build the entry from disk, which never
                # carries meta on its own).
                if self.exclude and filter.match(path, meta=dict(self.meta)):
                    self._logger_.debug("Excluding %s", path)
                    return False
                fspath = os.fspath(self.buildpath(path))
                if db.get(fspath) is None:
                    st = path.lstat()
                    ftype = _file_type(path, st)
                    entry = _resolve_stat(bases[ftype], path, st, lookupval=AUTO)
                    self._logger_.debug("Updating file entry for: %s", fspath)
                    self.add_entry(fspath, entry=entry)
                    recorded += 1
                return True
            except TypeError as exc:
                # _file_type raises TypeError for a fifo/socket. A
                # glob-only -X (e.g. '*.fifo') already skipped such a path
                # above without ever reaching here (PathMatch builds the
                # entry lazily); this is the path scan cannot record at all.
                raise PkgForgeError(
                    f"{path}: unsupported file type (not a file, directory "
                    "or symlink); exclude it with -X"
                ) from exc

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
                _scanfile(scanpath)
            elif not scanpath.is_dir():
                _scanfile(scanpath)
            else:
                for top, dirs, files in os.walk(scanpath):
                    # Prune in place: os.walk only descends into names
                    # still left in `dirs` after this line runs.
                    dirs[:] = [d for d in dirs if _scanfile(Path(top, d))]
                    for file in files:
                        _scanfile(Path(top, file))

        self._logger_.info("Scanned %s: %d path(s) recorded", scanpath, recorded)

        if self.drop_stale:
            # Reload from disk regardless of --missing (the walk's own `db`
            # is {} without it): drop-stale reasons about the DB's current
            # state, not what the walk happened to see in memory.
            dropdb = self.loaddb()
            scan_buildpath = os.fspath(self.buildpath(scanpath))
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
