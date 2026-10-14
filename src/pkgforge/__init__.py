"""pkgforge: stage files into a build root and record their install metadata.

Import order matters: importing the leaf command modules runs their
``_register()`` calls, which attach each command to the :class:`PkgForge`
root's subcommand tree.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version as _version

import duho

from .errors import PkgForgeError, UsageError
from .entry import (
    FileEntry,
    FileEntryArgs,
    FileType,
    apply_entry,
    entry_from_args,
    entry_from_path,
    normalize_mode,
    resolve_entry,
)
from .command import PkgForgeCmd, PkgForge, silence_broken_pipe
from .db import DbError, DbProvider, open_db
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
    "FileEntry",
    "FileEntryArgs",
    "FileType",
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
    :class:`~pkgforge.errors.UsageError` (an argument-shaped mistake), 1 for
    any other :class:`~pkgforge.errors.PkgForgeError`, ``OSError`` or
    ``subprocess.CalledProcessError``, and 1 silently for a closed output
    pipe (``BrokenPipeError``). Anything else propagates with its traceback,
    so a real bug stays visible. Set ``DUHO_TRACEBACK`` to a true value (duho's boolean tokens, e.g. ``1``;
    ``0``/``n``/``f`` mean off) to also print the traceback before that one line.

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
        silence_broken_pipe()
        return 1
