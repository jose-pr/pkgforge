"""``scan``: walk a path and record each file's entry."""

from __future__ import annotations

import typing

import duho

from ._common import EntryArgs, ExcludeArgs, ProjectCmd, _parse_mode

__all__ = ["ScanCmd"]


class ScanCmd(EntryArgs, ExcludeArgs, ProjectCmd):
    """Scan a path under the build root and record each file's entry in the DB.

    Each path is recorded with its own on-disk type, so there is no --type.
    """

    _parsername_ = "scan"
    _logger_name_ = "pkgforge.scan"

    missing: bool = False
    "only record entries absent from the DB, leaving existing ones untouched"
    ("--missing",)
    dir_mode: duho.Arg[
        typing.Optional[str], duho.Meta(type=_parse_mode, literal_value=True)
    ] = None
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

    def __call__(self) -> int:
        records = self._project().scan(
            self.path,
            mode=self.mode,
            owner=self.owner,
            group=self.group,
            meta=self.meta,
            dir_mode=self.dir_mode,
            missing=self.missing,
            drop_stale=self.drop_stale,
            exclude=self.exclude,
        )
        self._emit(records)
        return 0
