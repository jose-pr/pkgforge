"""pkgforge: stage files into a build root and record their install metadata.

Import order matters: importing the leaf command modules runs their
``_register()`` calls, which attach each command to the :class:`PkgForge`
root's subcommand tree.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version as _version

import duho

from .exceptions import (
    DbError,
    DumpError,
    ExcludeSyntaxError,
    InvalidEntryError,
    PkgForgeError,
    UnsupportedOutputError,
    UsageError,
)
from .entry import (
    FileEntries,
    FileEntry,
    FileEntryArgs,
    FileRecords,
    FileType,
    StrPath,
    apply_entry,
    entry_from_args,
    entry_from_path,
    normalize_mode,
    resolve_entry,
)
from .command import PkgForgeCmd, PkgForge, _silence_broken_pipe
from .db import DbProvider, open_db
from .project import Project
from . import compact, dbdump, initdb, install, scan

try:  # resolve the installed distribution version, if any
    __version__ = _version("pkgforge")
except PackageNotFoundError:  # not installed (running from a source checkout)
    __version__ = "0.0.0"

__all__ = [
    "PkgForgeCmd",
    "PkgForge",
    "PkgForgeError",
    "DbError",
    "DbProvider",
    "DumpError",
    "ExcludeSyntaxError",
    "FileEntries",
    "FileEntry",
    "FileEntryArgs",
    "FileRecords",
    "FileType",
    "InvalidEntryError",
    "Project",
    "StrPath",
    "UnsupportedOutputError",
    "UsageError",
    "__version__",
    "apply_entry",
    "entry_from_args",
    "entry_from_path",
    "normalize_mode",
    "resolve_entry",
    "main",
    "open_db",
    "compact",
    "dbdump",
    "initdb",
    "install",
    "scan",
]


def main(argv=None) -> int:
    """Console entry point: build the parser, dispatch the selected command.

    Errors are reported by :class:`PkgForge`'s ``_errors_`` and
    ``_report_error_``: one ``pkgforge: error: <message>`` line to stderr and a
    plain exit code instead of a traceback. That is 2 for a
    :class:`~pkgforge.exceptions.UsageError` (an argument-shaped mistake), 1 for
    any other :class:`~pkgforge.exceptions.PkgForgeError`, ``OSError`` or
    ``subprocess.CalledProcessError``, and 1 silently for a closed output
    pipe (``BrokenPipeError``). 130 is Ctrl-C, silently. Anything else
    propagates with its traceback, so a real bug stays visible. Set
    ``PKGFORGE_TRACEBACK`` to a true value (``1``, ``true``, ``yes``, ``on``;
    ``0``/``n``/``f`` mean off) to also print the traceback before that one
    line.

    ``--help``, ``--version`` and a usage error from the parser raise
    ``SystemExit``; every other outcome is returned.

    Python-API callers that invoke a command directly (not through
    ``main()``) still get the raw exception -- this boundary only wraps the
    CLI entry point.
    """
    try:
        rc = duho.run(PkgForge, argv, exit="parse")
        # A broken downstream pipe (e.g. `pkgforge dbdump ... | head -1`) can
        # surface only here, on the final flush, after the command itself
        # already returned -- catch it inside the boundary, not as an
        # uncaught exception at interpreter shutdown.
        sys.stdout.flush()
        return rc
    except BrokenPipeError:
        _silence_broken_pipe()
        return 1
