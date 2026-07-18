"""buildutils: stage files into a build root and record their install metadata.

Import order matters: importing the leaf command modules runs their
``_register()`` calls, which attach each command to the :class:`BuildUtils`
root's subcommand tree.
"""

from __future__ import annotations

from .common import BuildUtil, BuildUtils, FileEntry, FileEntryArgs, FileType
from . import dbdump, initdb, install, scan

__all__ = [
    "BuildUtil",
    "BuildUtils",
    "FileEntry",
    "FileEntryArgs",
    "FileType",
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
