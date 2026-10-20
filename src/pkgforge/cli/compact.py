"""``compact``: collapse the file DB's redundant history."""

from __future__ import annotations

from ..exceptions import UsageError
from ._common import ProjectCmd

__all__ = ["Compact"]


class Compact(ProjectCmd):
    """Collapse the DB to one record per live path (drop superseded/removed)."""

    _parsername_ = "compact"
    _logger_name_ = "pkgforge.compact"

    def __call__(self) -> int:
        if str(self.db) == "-":
            raise UsageError("compact rewrites a file; --db - has none")
        self._project().compact()
        return 0
