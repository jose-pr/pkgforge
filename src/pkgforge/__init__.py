"""pkgforge: stage files into a build root and record their install metadata.

Import order matters: importing the leaf command modules runs their
``_register()`` calls, which attach each command to the :class:`PkgForge`
root's subcommand tree.
"""

from __future__ import annotations

from .common import PkgForgeCmd, PkgForge, FileEntry, FileEntryArgs, FileType
from .db import DbProvider, open_db, register_provider
from . import compact, dbdump, initdb, install, scan

try:  # resolve the installed distribution version, if any
    from importlib.metadata import PackageNotFoundError, version as _version

    try:
        __version__ = _version("pkgforge")
    except PackageNotFoundError:  # not installed (running from a source checkout)
        __version__ = "0.0.0"
except ImportError:  # pragma: no cover - importlib.metadata always present on 3.9+
    __version__ = "0.0.0"

__all__ = [
    "PkgForgeCmd",
    "PkgForge",
    "DbProvider",
    "FileEntry",
    "FileEntryArgs",
    "FileType",
    "__version__",
    "main",
    "open_db",
    "register_provider",
    "compact",
    "dbdump",
    "initdb",
    "install",
    "scan",
]


def main(argv=None) -> int:
    """Console entry point: build the parser, dispatch the selected command."""
    import duho

    return duho.main(PkgForge, argv)
