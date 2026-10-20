"""``mkdir``: record directories that have no source."""

from __future__ import annotations

import typing

import duho

from ._common import EntryArgs, ProjectCmd

__all__ = ["Mkdir"]


class Mkdir(EntryArgs, ProjectCmd):
    """Create directories under the build root and record their entries.

    Nothing is read from a source, so this is how a directory with no files of
    its own is packaged. Running it again changes nothing.
    """

    _parsername_ = "mkdir"
    _logger_name_ = "pkgforge.mkdir"

    parents: bool = False
    "create missing parent directories"
    ("--parents", "-p")
    paths: duho.Arg[typing.List[str], duho.Meta(nargs="+", metavar="DEST")] = []
    "directories to record under the build root"
    ("paths",)

    def __call__(self) -> int:
        records = self._project().mkdir(
            self.paths,
            mode=self.mode,
            owner=self.owner,
            group=self.group,
            meta=self.meta,
            parents=self.parents,
        )
        self._emit(records)
        return 0
