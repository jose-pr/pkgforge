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
    exclude: typing.Sequence[str],
) -> FileRecords:
    """Record every path below ``path`` (a DB path); see
    :meth:`pkgforge.project.Project.scan`."""
    if drop_stale and project.db is None:
        raise UsageError("--drop-stale needs a --db file (not unset or '-')")
    stmts = _statements(exclude)
    project._begin_operation()
    meta = dict(meta or {})
    base = _new_entry(mode=mode, owner=owner, group=group, type=AUTO, meta=meta)
    project._check_names("scan", base["owner"], base["group"])
    # follow_final=False: a symlink PATH pointing outside the root is
    # recorded as a symlink below, not refused; an escaping ancestor
    # component is still refused by _rootpath.
    scanpath = project._rootpath(path, follow_final=False)
    if not os.path.lexists(scanpath):
        raise UsageError(
            f"{path!r} does not exist under build root "
            f"{os.fspath(project.buildroot)!r}"
        )
    # PATH "/" (or ".") is walked even when --buildroot itself is a
    # symlink, so the symlink-leaf check below skips it.
    is_root = scanpath == Path(project.buildroot)
    db = project.load() if missing else {}
    # -m applies to regular files only; a directory takes --dir-mode, else
    # AUTO when -m is AUTO, else DEFAULT. A symlink's mode is always DEFAULT:
    # Linux ignores it and rpm/debian warn about or reject an explicit one.
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
        # Owned parents go to `records` first, then the entry itself.
        written = project._write(dbpath, entry, records)
        records.append((dbpath, written))

    # `dbdump` OUTPUT files (rpm-files.txt, debian/) are not recognizable
    # by TreeRecorder's DB-file skip and are documented instead -- keep
    # them outside --buildroot too.
    matcher = filter if stmts else None
    recorder = TreeRecorder(
        project, bases, matcher, meta, db, logger=_LOGGER, write=write
    )

    # Batched (a no-op except sqlite, which commits periodically), and
    # committed before --drop-stale's reload needs to see these writes.
    with project._batch():
        if not is_root and scanpath.is_symlink():
            # Never followed: an absolute target would walk the build
            # host's filesystem and record it under the link's path.
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
