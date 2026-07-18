"""buildutils: stage files into a build root and record their install metadata.

Import order matters: importing the leaf command modules runs their
``_register()`` calls, which attach each command to the :class:`BuildUtils`
root's subcommand tree.
"""

from __future__ import annotations

from .common import BuildUtil, BuildUtils, FileEntry, FileEntryArgs, FileType
from . import dbdump, initdb, install, scan

try:  # resolve the installed distribution version, if any
    from importlib.metadata import PackageNotFoundError, version as _version

    try:
        __version__ = _version("buildutils")
    except PackageNotFoundError:  # not installed (running from a source checkout)
        __version__ = "0.0.0"
except ImportError:  # pragma: no cover - importlib.metadata always present on 3.9+
    __version__ = "0.0.0"

__all__ = [
    "BuildUtil",
    "BuildUtils",
    "FileEntry",
    "FileEntryArgs",
    "FileType",
    "__version__",
    "main",
    "dbdump",
    "initdb",
    "install",
    "scan",
]


def main(argv=None) -> int:
    """Console entry point: build the parser, dispatch the selected command."""
    import duho

    return duho.main(BuildUtils, argv)
