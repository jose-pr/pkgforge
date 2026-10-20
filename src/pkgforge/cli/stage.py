"""``stage``: run the ``[[stage]]`` steps of ``pkgforge.cfg``."""

from __future__ import annotations

from ._common import ProjectCmd

__all__ = ["Stage"]


class Stage(ProjectCmd):
    """Run the [[stage]] steps of pkgforge.cfg: install each source, directory,
    file and link into the build root and record its entry."""

    _parsername_ = "stage"
    _logger_name_ = "pkgforge.stage"

    init: bool = False
    "reset the file DB before staging, as initdb does"
    ("--init",)

    def __call__(self) -> int:
        records = self._project().stage(init=self.init)
        self._emit(records)
        return 0
