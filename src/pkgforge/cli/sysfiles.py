"""``sysfiles``: write the sysusers.d and tmpfiles.d files a package needs."""

from __future__ import annotations

from ._common import ProjectCmd

__all__ = ["Sysfiles"]


class Sysfiles(ProjectCmd):
    """Write the sysusers.d and tmpfiles.d files the package needs and record
    them."""

    _parsername_ = "sysfiles"
    _logger_name_ = "pkgforge.sysfiles"

    def __call__(self) -> int:
        self._emit(self._project().sysfiles())
        return 0
