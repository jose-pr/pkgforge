"""``initdb`` subcommand: create/reset an empty file DB."""

from __future__ import annotations

from .common import BuildUtil


class InitDb(BuildUtil):
    """Create or reset (truncate) the file DB."""

    _parsername_ = "initdb"

    def __call__(self):
        if self.db is None or str(self.db) == "-":
            return
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.initdb()  # provider-specific empty DB (truncate file / reset table)
        self._logger_.info("Initialized empty DB at %s", self.db)


InitDb._register()
