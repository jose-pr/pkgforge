"""``compact`` subcommand: collapse the file DB's redundant history."""

from __future__ import annotations

from .common import PkgForgeCmd


class Compact(PkgForgeCmd):
    """Collapse the DB to one record per live path (drop superseded/removed)."""

    _parsername_ = "compact"

    def __call__(self):
        if self.db is None or str(self.db) == "-":
            self._logger_.info("No file DB to compact")
            return
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
