"""``symlink``: record a symbolic link that has no source."""

from __future__ import annotations

from ._common import OwnershipArgs, ProjectCmd

__all__ = ["Symlink"]


class Symlink(OwnershipArgs, ProjectCmd):
    """Create a symbolic link under the build root and record its entry.

    The target is stored as written and may name nothing. A symlink has no mode.
    """

    _parsername_ = "symlink"
    _logger_name_ = "pkgforge.symlink"

    parents: bool = False
    "create missing parent directories"
    ("--parents", "-p")
    target: str
    "what the link points to, stored as written"
    ("target",)
    link: str
    "the link's path under the build root"
    ("link",)

    def __call__(self) -> int:
        records = self._project().symlink(
            self.target,
            self.link,
            owner=self.owner,
            group=self.group,
            meta=self.meta,
            parents=self.parents,
        )
        self._emit(records)
        return 0
