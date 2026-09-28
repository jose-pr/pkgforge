"""Tests for pkgforge's error model: ``PkgForgeError``/``UsageError`` and the
``pkgforge.main()`` error boundary.
"""

from __future__ import annotations

import pytest

import pkgforge
from pkgforge.common import PkgForge


def test_root_command_not_runnable():
    # Guard: PkgForgeCmd/PkgForge no longer override __call__ (F72) -- duho's
    # own Cmd base already raises NotImplementedError naming the class, so a
    # bare root command still fails loud if ever reached directly.
    with pytest.raises(NotImplementedError):
        PkgForge()()

    with pytest.raises(SystemExit) as excinfo:
        pkgforge.main([])
    assert excinfo.value.code == 2
