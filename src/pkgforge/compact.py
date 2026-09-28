"""``compact`` subcommand: collapse the file DB's redundant history."""

from __future__ import annotations

from .common import PkgForgeCmd


class Compact(PkgForgeCmd):
    """Collapse the DB to one record per live path (drop superseded/removed)."""

    _parsername_ = "compact"
    _logger_name_ = "pkgforge.compact"

    def __call__(self):
        reason = self._no_db_reason()
        if reason:
            self._logger_.warning("%s: nothing to compact", reason)
            return
        if not self.db.exists():
            self._logger_.warning("DB %s does not exist; nothing to compact", self.db)
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
