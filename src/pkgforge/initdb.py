"""``initdb`` subcommand: create/reset an empty file DB."""

from __future__ import annotations

from .command import PkgForgeCmd


class InitDb(PkgForgeCmd):
    """Create or reset (truncate) the file DB."""

    _parsername_ = "initdb"
    _logger_name_ = "pkgforge.initdb"

    def __call__(self):
        reason = self._no_db_reason()
        if reason:
            self._logger_.warning("%s: nothing to initialize", reason)
            return
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.initdb()  # provider-specific empty DB (truncate file / reset table)
        self._logger_.info("Initialized empty DB at %s", self.db)


InitDb._register()
