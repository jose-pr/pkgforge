"""The common command base and its file-DB plumbing.

Defines the CLI path parser (:func:`parsepath`), the common command base
(:class:`PkgForgeCmd`: the ``--db``/``--db-format``/``--buildroot`` options
every subcommand shares, and the bridge to :class:`~pkgforge.project.Project`
that does the work) that every subcommand extends, and the CLI root
(:class:`PkgForge`).

Storage is pluggable (see :mod:`pkgforge.db`: ``jsonl`` by default, plus
``yaml`` and ``sqlite``, and a third party can add its own by subclassing
:class:`~pkgforge.db.DbProvider`). On load, the last record for a path wins.
The backend is ``--db-format``, else the ``--db`` suffix, else ``jsonl``; a
file's content is never inspected, so one in another format fails to load.
"""

from __future__ import annotations

import os
import sys
import traceback
import typing
from pathlib import Path, PurePath

import duho
from duho import Cli, Cmd, LoggingArgs
from duho.text import BOOL_TRUE

from .db import jsonl_line
from .entry import FileEntry, FileRecords
from .exceptions import PkgForgeError, UsageError

if typing.TYPE_CHECKING:
    from .project import Project


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


def _refuse_mcp_stdin(what: str) -> None:
    """Raise :class:`UsageError` when ``what`` would be read from standard
    input inside an MCP tool call, where stdin is the protocol channel."""
    if duho.running_mode() == "mcp":
        raise UsageError(
            f"reading {what} from standard input is not available in an MCP tool call"
        )


def parsepath(path: str) -> typing.Optional[typing.Union[str, Path]]:
    """Parse a CLI path argument.

    ``"-"`` (stdin/stdout) is returned as-is; the empty string becomes
    ``None``; anything else becomes a :class:`~pathlib.Path`.
    """
    if path == "-":
        return "-"
    elif not path:
        return None
    else:
        return Path(path)


def _as_path(value: typing.Any, what: str) -> typing.Any:
    """``value`` as a :class:`~pathlib.Path`; ``"-"`` (stdin/stdout) and a falsy
    value (an unset field, left for the caller to refuse) pass through. Raises :class:`UsageError` for anything
    that is not path-like."""
    if not value or isinstance(value, Path) or value == "-":
        return value
    try:
        return Path(os.fspath(value))
    except TypeError:
        raise UsageError(
            f"{what} must be a str or path-like, not {type(value).__name__}"
        ) from None


def _env_path(value: str) -> typing.Optional[Path]:
    """Env-var/CLI value -> Path, or None if empty/unset (see PkgForgeCmd.db)."""
    return Path(value) if value else None


def _env_str(value: str) -> typing.Optional[str]:
    """Env-var/CLI value -> str, or None if empty/unset (PkgForgeCmd.db_format)."""
    return value if value else None


def _env_root(value: str) -> Path:
    """Env-var/CLI value -> Path, defaulting to '.' if empty (buildroot)."""
    return Path(value) if value else Path(".")


def _check_db_format(value: typing.Optional[str]) -> typing.Optional[str]:
    """Validate a ``db_format`` value against the registered providers,
    returning its canonical :attr:`~pkgforge.db.DbProvider.NAME`.

    A falsy value (``None`` or ``""``) means "from the suffix" and is never
    checked here -- ``open_db`` resolves it from the ``--db`` suffix. Anything else must already
    be a registered :class:`~pkgforge.db.DbProvider` subclass's ``NAME`` or
    ``ALIASES`` entry, checked eagerly so a typo fails before any file is
    staged, instead of surfacing as a traceback the first time the DB is
    touched.
    """
    if not value:
        return value
    # Function-local, like PkgForgeCmd._provider() below: DbProvider._registry
    # is read fresh on every call, so a subclass defined after this module is
    # imported (a third-party backend) is still recognized.
    from .db import DbProvider

    return DbProvider.lookup(value).NAME


class PkgForgeCmd(LoggingArgs, Cmd):
    """Common base for every pkgforge subcommand.

    Carries the three app-wide options (``--db``, ``--db-format`` and
    ``--buildroot``), the file-DB read/write helpers, and the build-root
    <-> local-path translation. Each leaf
    command subclasses this and implements ``__call__``; a leaf attaches itself
    to the :class:`PkgForge` root's subcommand tree via :meth:`_register`.
    """

    db: duho.Arg[
        typing.Optional[Path],
        duho.Meta(env="PKGFORGE_DB", type=_env_path, metavar="PATH"),
    ] = None
    "file DB path (env PKGFORGE_DB); '-' (or unset, for install/scan) writes records to stdout, and dbdump reads them from stdin"
    ("--db",)
    db_format: duho.Arg[
        typing.Optional[str],
        duho.Meta(env="PKGFORGE_DB_FORMAT", type=_env_str, metavar="FORMAT"),
    ] = None
    "storage backend: jsonl, yaml, sqlite, or a registered DbProvider subclass's NAME (env PKGFORGE_DB_FORMAT); else inferred from the --db suffix"
    ("--db-format",)
    buildroot: duho.Arg[
        Path, duho.Meta(env="PKGFORGE_ROOT", type=_env_root, metavar="DIR")
    ] = Path(".")
    "staging root that maps to '/' in the DB (env PKGFORGE_ROOT; default '.')"
    ("--buildroot", "-r")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.db = _as_path(self.db, "db")
        self.buildroot = _as_path(self.buildroot, "buildroot")
        # Checked after construction (so env/CLI/default fill-in has already
        # happened) rather than as a `type=` converter: a converter only runs
        # for a value that actually goes through argparse, so a direct
        # Python-API construction (PkgForgeCmd(db_format="toml")) would
        # otherwise bypass it entirely, same as the mode/type converters
        # above.
        self.db_format = _check_db_format(self.db_format)

    def _no_file_db(self) -> bool:
        """True when records go to stdout instead of a file (``--db`` unset or ``-``)."""
        return self.db is None or str(self.db) == "-"

    def _project(self) -> "Project":
        """The :class:`~pkgforge.project.Project` these options describe."""
        # Function-local: project imports dbdump, which imports this module.
        from .project import Project

        if not self.buildroot:
            raise UsageError("no build root configured (--buildroot/PKGFORGE_ROOT)")
        return Project(
            self.buildroot,
            None if self._no_file_db() else self.db,
            db_format=self.db_format or None,
        )

    def _emit(self, records: FileRecords) -> None:
        """Print ``records`` as JSON Lines when there is no file DB, so a
        pipeline can capture what would have been recorded."""
        if self._no_file_db():
            for path, entry in records:
                print(jsonl_line(path, entry), end="")

    def localpath(self, buildpath: typing.Union[str, os.PathLike]) -> Path:
        return self._project().localpath(os.fspath(buildpath))

    def buildpath(self, localpath: Path) -> Path:
        return Path(self._project().dbpath(localpath))

    def loaddb(self) -> typing.Dict[str, typing.Optional[FileEntry]]:
        return self._project().load()

    def compactdb(self) -> None:
        """Collapse the DB's redundant history (backend-specific; no-op if none)."""
        if not self._no_file_db():
            self._project().compact()

    def initdb(self) -> None:
        """Create or reset an empty DB (no-op for a stdout / unset DB)."""
        if not self._no_file_db():
            self._project().init()

    def _write_entry(
        self, buildpath: typing.Union[str, PurePath], entry: typing.Optional[FileEntry]
    ) -> None:
        # DB keys are POSIX text whatever the host: a Windows Path would
        # render its "/"-rooted value with backslashes.
        path = buildpath.as_posix() if isinstance(buildpath, PurePath) else buildpath
        self._emit([(path, self._project()._write(path, entry))])

    def add_entry(self, buildpath: typing.Union[str, PurePath], entry: FileEntry):
        self._write_entry(buildpath, entry)

    def remove_entry(self, buildpath: typing.Union[str, PurePath]):
        self._write_entry(buildpath, None)

    @classmethod
    def _register(cls):
        PkgForge._register_subcmd_(cls)


class PkgForge(PkgForgeCmd, Cli):
    """Stage files into a build root and record their intended install
    metadata (mode, owner, group, type) in a file DB. Dump that DB into
    packaging manifests (RPM %files, Debian install/dirs/fixperms).
    """

    # Extends PkgForgeCmd (the shared --db/--buildroot options and the DB
    # helpers) and duho.Cli (the app-root layer: --version, completion, and
    # the subcommand tree).
    _version_ = duho.AUTO
    _distribution_ = "pkgforge"
    _completion_ = True
    # Without this, duho falls back to the class name ("PkgForge") for the
    # prog name: usage, errors, --version and completion would all say
    # "PkgForge" instead of the invoked command, and shell completion (which
    # binds by exact, case-sensitive name) would never fire on bash/zsh/fish.
    _parsername_ = "pkgforge"
    # First match wins: a usage mistake (a PkgForgeError) is listed before
    # the class it belongs to.
    _errors_ = {
        UsageError: 2,
        PkgForgeError: 1,
        OSError: 1,
    }

    @staticmethod
    def _report_error_(message: str, code: int, error: BaseException) -> None:
        """Print one ``pkgforge: error:`` line, after the traceback when
        ``PKGFORGE_TRACEBACK`` is set to a true value."""
        if os.environ.get("PKGFORGE_TRACEBACK", "").strip().lower() in BOOL_TRUE:
            traceback.print_exception(type(error), error, error.__traceback__)
        print(f"pkgforge: error: {message}", file=sys.stderr)
