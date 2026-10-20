"""``Project.scan``: walk a path under the build root and record each file's
entry in the file DB."""

from __future__ import annotations

import logging
import os
import typing
from pathlib import Path

from ._tree import TreeRecorder
from .entry import (
    AUTO,
    DEFAULT,
    FileRecords,
    FileType,
    _new_entry,
    normalize_mode,
)
from .exceptions import UsageError
from .exclude import PathMatch, _statements

if typing.TYPE_CHECKING:
    from .project import Project

_LOGGER = logging.getLogger("pkgforge.scan")


def scan(
    project: "Project",
    path: str,
    *,
    mode: str,
    owner: str,
    group: str,
    meta: typing.Optional[typing.Mapping[str, str]],
    dir_mode: typing.Optional[str],
    missing: bool,
    drop_stale: bool,
    exclude: typing.Sequence[typing.Any],
) -> FileRecords:
    """Record every path below ``path`` (a DB path); see
    :meth:`pkgforge.project.Project.scan`."""
    if drop_stale and project.db is None:
        raise UsageError("--drop-stale needs a --db file (not unset or '-')")
    stmts = _statements(exclude)
    meta = dict(meta or {})
    # follow_final=False (unlike install's DESTINATION): the leaf is
    # never followed here, only realpath(parent) is checked, so a
    # symlink PATH pointing at a directory outside the root is recorded
    # as a symlink below, not refused. An escaping *ancestor* component
    # is still refused by _rootpath itself.
    scanpath = project._rootpath(path, follow_final=False)
    if not os.path.lexists(scanpath):
        raise UsageError(
            f"{path!r} does not exist under build root "
            f"{os.fspath(project.buildroot)!r}"
        )
    # True when PATH normalizes to the root itself ("/", or a plain
    # "."): PATH "/" must still be walked even when --buildroot itself
    # resolves through a symlink, so the symlink-leaf check below is
    # skipped for it.
    is_root = scanpath == Path(project.buildroot)
    db = project.load() if missing else {}
    base = _new_entry(mode=mode, owner=owner, group=group, type=AUTO, meta=meta)
    # -m/--mode applies to regular files only. A directory takes the
    # new --dir-mode instead: the explicit value if given; else AUTO
    # (read from disk) when --mode itself is AUTO, so --mode=-- still
    # reads directories from disk too; else DEFAULT ('-') -- an
    # explicit file mode is never inherited by directories. A symlink's
    # mode is always DEFAULT: Linux ignores it, and rpm/debian consumers
    # warn about (or reject) an explicit mode on one.
    if dir_mode is not None:
        dirmode = normalize_mode(dir_mode)
    elif base["mode"] == AUTO:
        dirmode = AUTO
    else:
        dirmode = DEFAULT
    bases = {
        FileType.File: base,
        FileType.Directory: {**base, "mode": dirmode},
        FileType.Symlink: {**base, "mode": DEFAULT},
    }
    # An absolute -X pattern is relative to PATH, in the walk and in
    # --drop-stale alike.
    filter = PathMatch(stmts, scanpath)
    _LOGGER.info("Scanning %s", scanpath)

    records: FileRecords = []

    def write(dbpath: str, entry: typing.Any) -> None:
        records.append((dbpath, project._write(dbpath, entry)))

    # `dbdump` OUTPUT files (rpm-files.txt, debian/) are not recognizable
    # by TreeRecorder's DB-file skip and are documented instead -- keep
    # them outside --buildroot too.
    matcher = filter if stmts else None
    recorder = TreeRecorder(
        project, bases, matcher, meta, db, logger=_LOGGER, write=write
    )

    # Batches the walk's own writes (a no-op except for sqlite, where it
    # holds one connection and commits periodically instead of once per
    # file); committed before --drop-stale's own reload below, which
    # needs a fresh load() to see what this walk just wrote.
    with project._batch():
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

    _LOGGER.info("Scanned %s: %d path(s) recorded", scanpath, recorder.recorded)

    if drop_stale:
        # Reload from disk regardless of `missing` (the walk's own `db`
        # is {} without it): drop-stale reasons about the DB's current
        # state, not what the walk happened to see in memory.
        dropdb = project.load()
        scan_dbpath = project.dbpath(scanpath)
        prefix = scan_dbpath if scan_dbpath == "/" else scan_dbpath + "/"
        dropped = 0
        with project._batch():
            for key, entry in dropdb.items():
                if entry is None or key == scan_dbpath or not key.startswith(prefix):
                    continue
                real_path = project.localpath(key)
                if stmts and filter.match(real_path, entry=entry):
                    _LOGGER.debug("Keeping excluded stale entry: %s", key)
                    continue
                if os.path.lexists(real_path):
                    continue
                _LOGGER.debug("Dropping stale entry for: %s", key)
                records.append((key, project._write(key, None)))
                dropped += 1
        _LOGGER.info(
            "Dropped %d stale entr%s below %s",
            dropped,
            "y" if dropped == 1 else "ies",
            scanpath,
        )
    return records
