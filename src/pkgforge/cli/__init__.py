"""The ``pkgforge`` command line: the root, ``main`` and the error boundary.

Not library API: the commands are documented under "Command line" in the
shipped ``AGENTS.md``, and the library behind them is :mod:`pkgforge.project`.
"""

from __future__ import annotations

import os
import sys
import traceback
import typing

import duho
from duho import Cli, ConfigArgs
from duho.text import BOOL_TRUE

from ..exceptions import PkgForgeError, UsageError
from ._common import ProjectCmd, _load_config, _silence_broken_pipe
from .compact import Compact
from .dbdump import DbDump
from .initdb import InitDb
from .install import Install
from .mkdir import Mkdir
from .scan import ScanCmd
from .stage import Stage
from .symlink import Symlink
from .sysfiles import Sysfiles
from .verify import Verify

__all__ = ["PkgForge", "main"]


class PkgForge(ConfigArgs, ProjectCmd, Cli):
    """Stage files into a build root and record their intended install
    metadata (mode, owner, group, type) in a file DB. Dump that DB into
    packaging manifests (RPM %files, Debian install/dirs/fixperms).
    """

    # The project file: --config, else PKGFORGE_CONFIG (must exist; empty
    # means none), else pkgforge.cfg in the working directory when present.
    _config_ = "pkgforge.cfg"
    _config_env_ = "PKGFORGE_CONFIG"
    _config_loader_ = staticmethod(_load_config)

    _version_ = duho.AUTO
    _distribution_ = "pkgforge"
    _completion_ = True
    # Without a name duho uses the class name for usage, errors, --version and
    # completion, and shell completion binds by exact, case-sensitive name.
    _parsername_ = "pkgforge"
    _subcommands_ = (
        Install,
        ScanCmd,
        InitDb,
        Compact,
        DbDump,
        Stage,
        Mkdir,
        Symlink,
        Sysfiles,
        Verify,
    )
    # First match wins: a usage mistake is listed before the class it belongs to.
    _errors_ = {
        UsageError: 2,
        PkgForgeError: 1,
        OSError: 1,
    }

    @staticmethod
    def _report_error_(
        message: str,
        code: int,
        error: typing.Optional[BaseException],
        command: typing.Optional[object],
    ) -> None:
        """Print one ``pkgforge: error:`` line, after the traceback when
        ``PKGFORGE_TRACEBACK`` is true or the command ran with ``--debug``.

        ``command`` is ``None`` for an error raised while it was being built.
        """
        wanted = os.environ.get("PKGFORGE_TRACEBACK", "").strip().lower() in BOOL_TRUE
        if (wanted or getattr(command, "debug", False)) and error is not None:
            traceback.print_exception(type(error), error, error.__traceback__)
        print(f"pkgforge: error: {message}", file=sys.stderr)


def main(argv: typing.Optional[typing.Sequence[str]] = None) -> int:
    """Run the command line and return its exit status.

    Args:
        argv: The arguments after the program name; ``sys.argv[1:]`` when
            ``None``.

    Returns:
        0 on success. An error prints one ``pkgforge: error:`` line to stderr
        and returns 2 for a usage mistake (:class:`~pkgforge.exceptions.UsageError`,
        a bad configuration file included) or 1 for any other
        :class:`~pkgforge.exceptions.PkgForgeError` or ``OSError``; 1 silently
        for a closed output pipe; 130 for Ctrl-C.

    Raises:
        SystemExit: for ``--help``, ``--version`` and an error in the arguments
            themselves. Any other exception propagates with its traceback.

    ``--debug`` or ``PKGFORGE_TRACEBACK`` (``1``, ``true``, ``yes``, ``on``)
    prints the traceback before the error line.
    """
    try:
        rc = duho.run(PkgForge, None if argv is None else list(argv), exit="parse")
        # A broken downstream pipe (`pkgforge dbdump ... | head -1`) can
        # surface only on this final flush, after the command returned.
        sys.stdout.flush()
        return rc
    except BrokenPipeError:
        _silence_broken_pipe()
        return 1
    except tuple(PkgForge._errors_) as exc:
        # Choosing or reading the configuration file raises before duho
        # applies _errors_, so the same mapping is applied here.
        status = next(
            code for kind, code in PkgForge._errors_.items() if isinstance(exc, kind)
        )
        PkgForge._report_error_(str(exc), status, exc, None)
        return status
