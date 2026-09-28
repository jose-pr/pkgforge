"""pkgforge: stage files into a build root and record their install metadata.

Import order matters: importing the leaf command modules runs their
``_register()`` calls, which attach each command to the :class:`PkgForge`
root's subcommand tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from importlib.metadata import PackageNotFoundError, version as _version

import duho

from .common import (
    PkgForgeCmd,
    PkgForge,
    PkgForgeError,
    FileEntry,
    FileEntryArgs,
    FileType,
    UsageError,
    apply_entry,
    entry_from_args,
    entry_from_path,
    resolve_entry,
)
from .db import DbProvider, open_db, register_provider
from . import compact, dbdump, initdb, install, scan

try:  # resolve the installed distribution version, if any
    __version__ = _version("pkgforge")
except PackageNotFoundError:  # not installed (running from a source checkout)
    __version__ = "0.0.0"

__all__ = [
    "PkgForgeCmd",
    "PkgForge",
    "PkgForgeError",
    "DbProvider",
    "FileEntry",
    "FileEntryArgs",
    "FileType",
    "UsageError",
    "__version__",
    "apply_entry",
    "entry_from_args",
    "entry_from_path",
    "resolve_entry",
    "main",
    "open_db",
    "register_provider",
    "compact",
    "dbdump",
    "initdb",
    "install",
    "scan",
]

#: Truthy spellings for the DUHO_TRACEBACK opt-in (stripped, case-insensitive).
_TRACEBACK_TRUTHY = ("1", "true", "yes", "on", "y", "t")


def _traceback_opted_in() -> bool:
    return os.environ.get("DUHO_TRACEBACK", "").strip().lower() in _TRACEBACK_TRUTHY


def _print_error(exc: BaseException) -> None:
    if _traceback_opted_in():
        traceback.print_exc()
    print(f"pkgforge: error: {exc}", file=sys.stderr)


def _silence_broken_pipe() -> None:
    """Redirect stdout to ``os.devnull`` so a later implicit flush/close does
    not raise a second time (the recipe from the Python signal-handling docs).

    Best-effort: gives up quietly if stdout has no real file descriptor (e.g.
    a captured, non-fd stream in tests).
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


def main(argv=None) -> int:
    """Console entry point: build the parser, dispatch the selected command.

    This is pkgforge's only error boundary. It prints one
    ``pkgforge: error: <message>`` line to stderr and returns a plain exit
    code instead of a traceback: 2 for a :class:`~pkgforge.common.UsageError`
    (an argument-shaped mistake), 1 for any other
    :class:`~pkgforge.common.PkgForgeError`, ``OSError`` or
    ``subprocess.CalledProcessError``, and 1 silently for a closed output
    pipe (``BrokenPipeError``). Anything else propagates with its traceback,
    so a real bug stays visible. Set ``DUHO_TRACEBACK`` (any of ``1 true yes
    on y t``, case-insensitive, whitespace-stripped) to also print the
    traceback before that one line.

    Python-API callers that invoke a command directly (not through
    ``main()``) still get the raw exception -- this boundary only wraps the
    CLI entry point.
    """
    try:
        rc = duho.main(PkgForge, argv)
        # A broken downstream pipe (e.g. `pkgforge dbdump ... | head -1`) can
        # surface only here, on the final flush, after the command itself
        # already returned -- catch it inside the boundary, not as an
        # uncaught exception at interpreter shutdown.
        sys.stdout.flush()
        return rc
    except BrokenPipeError:
        _silence_broken_pipe()
        return 1
    except UsageError as exc:
        _print_error(exc)
        return 2
    except (PkgForgeError, OSError, subprocess.CalledProcessError) as exc:
        _print_error(exc)
        return 1
