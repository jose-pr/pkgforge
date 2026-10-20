"""``initdb`` subcommand: create/reset an empty file DB."""

from __future__ import annotations

from .command import PkgForgeCmd


class InitDb(PkgForgeCmd):
    """Create or reset (truncate) the file DB."""

    _parsername_ = "initdb"
    _logger_name_ = "pkgforge.initdb"

    def __call__(self):
        if self._no_file_db():
            self._logger_.warning(
                "no file DB (--db unset or '-'): nothing to initialize"
            )
            return
        self._project().init()


InitDb._register()
