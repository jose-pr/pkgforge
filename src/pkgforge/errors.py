"""pkgforge's own exception types.

:class:`PkgForgeError` and :class:`UsageError` are caught by
:func:`pkgforge.main`'s error boundary; see its docstring for the exit-code
mapping. Python-API callers that invoke a command directly still see them
raised normally.
"""

from __future__ import annotations


class PkgForgeError(Exception):
    """Base class for pkgforge's own runtime failures.

    Caught by :func:`pkgforge.main`'s error boundary: prints one
    ``pkgforge: error: ...`` line to stderr and exits 1. Python-API callers
    still see it raised normally.
    """


class UsageError(PkgForgeError, ValueError):
    """An argument-shaped mistake (a bad or missing value the caller gave).

    Caught by :func:`pkgforge.main`'s error boundary and mapped to exit 2,
    like an argparse usage error. Subclasses :class:`ValueError` so existing
    ``pytest.raises(ValueError, ...)`` tests, and any Python-API caller
    catching ``ValueError``, keep working unchanged.
    """
