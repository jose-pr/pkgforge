"""``initdb``: create or reset the file DB."""

from __future__ import annotations

from ._common import ProjectCmd

__all__ = ["InitDb"]


class InitDb(ProjectCmd):
    """Create or reset (truncate) the file DB."""

    _parsername_ = "initdb"
    _logger_name_ = "pkgforge.initdb"

    def __call__(self) -> int:
        project = self._project()  # checks --db-format even with no file DB
        if self._no_file_db():
            self._logger_.warning(
                "no file DB (--db unset or '-'): nothing to initialize"
            )
            return 0
        project.init()
        return 0
