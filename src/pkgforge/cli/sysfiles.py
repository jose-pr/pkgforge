"""``sysfiles``: write the sysusers.d and tmpfiles.d files a package needs."""

from __future__ import annotations

from ._common import ProjectCmd

__all__ = ["Sysfiles"]


class Sysfiles(ProjectCmd):
    """Write and record the sysusers.d and tmpfiles.d files the package needs."""

    _parsername_ = "sysfiles"
    _logger_name_ = "pkgforge.sysfiles"

    def __call__(self) -> int:
        self._emit(self._project().sysfiles())
        return 0
