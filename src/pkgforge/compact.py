"""``compact`` subcommand: collapse the file DB's redundant history."""

from __future__ import annotations

from .command import PkgForgeCmd
from .exceptions import UsageError


class Compact(PkgForgeCmd):
    """Collapse the DB to one record per live path (drop superseded/removed)."""

    _parsername_ = "compact"
    _logger_name_ = "pkgforge.compact"

    def __call__(self):
        if str(self.db) == "-":
            raise UsageError("compact rewrites a file; --db - has none")
        self._existing_db()
        before = self.loaddb()
        live = sum(1 for entry in before.values() if entry is not None)
        self.compactdb()
        self._logger_.info(
            "Compacted %s: %d live path(s), %d removal(s) dropped",
            self.db,
            live,
            len(before) - live,
        )


Compact._register()
