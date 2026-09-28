"""Core types shared across pkgforge commands.

Defines the on-disk *file DB* record (:class:`FileEntry`) and its metadata
(mode / owner / group / type / free-form ``meta``), the CLI argument mixin that
supplies those fields (:class:`FileEntryArgs`), and the common command base
(:class:`PkgForgeCmd`) that every subcommand extends.

The file DB is an append-only **JSON Lines** log: one JSON object per line,
each carrying a build-relative ``path`` plus the entry's fields (or
``{"path": ..., "_removed": true}`` to mark a removal). On load the last record
for a path wins. ``mode`` is always stored as an **octal permission string**
(e.g. ``"644"``) so the DB round-trips cleanly and dumps (e.g. ``%attr(644,...)``
in an RPM spec) are correct.

Legacy DBs written in the older single-document YAML format are still read
transparently (auto-detected), and are upgraded to JSON Lines in place on the
next write.
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import os
import typing
from pathlib import Path

try:  # Unix-only; pkgforge targets Linux, but keep parsing/--help importable elsewhere.
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-Unix
    grp = pwd = None

import duho
from duho import Cli, Cmd, LoggingArgs

buildroot = os.environ.get("PKGFORGE_ROOT")
filedb = os.environ.get("PKGFORGE_DB")

#: Sentinel meaning "resolve this field from the file on disk".
AUTO = "--"
#: Sentinel meaning "leave this field at the system/OS default (do not set it)".
DEFAULT = "-"


def parsepath(path: str) -> typing.Optional[typing.Union[str, Path]]:
    """Parse a CLI path argument.

    ``"-"`` (stdin/stdout) and the empty string are preserved as-is; anything
    else becomes a :class:`~pathlib.Path`.
    """
    if path == "-":
        return "-"
    elif not path:
        return None
    else:
        return Path(path)


class FileType(str, enum.Enum):
    File = "file"
    Directory = "directory"
    Symlink = "symlink"
    #: Placeholder meaning "determine the type from the file on disk".
    _AUTO = AUTO

    @classmethod
    def from_path(cls, path: Path) -> FileType:
        if path.is_symlink():
            return cls.Symlink
        elif path.is_dir():
            return cls.Directory
        elif path.is_file():
            return cls.File
        else:
            raise TypeError(path)


def mode_to_octal(mode: int) -> str:
    """Render a stat ``st_mode`` as a bare octal permission string (e.g. ``"644"``)."""
    return format(mode & 0o7777, "o")


class FileEntryArgs(Cmd):
    mode: str = DEFAULT
    ("--mode", "-m")
    group: str = DEFAULT
    ("--group", "-g")
    owner: str = DEFAULT
    ("--owner", "-o")
    type: typing.Optional[FileType] = None
    ("--type", "-t")
    meta: duho.Arg[
        typing.Dict[str, str],
        duho.NS(
            action=duho.UpdateAction,
            type=lambda x: dict([x.split("=", maxsplit=1)]),
        ),
    ] = {}
    ("-O", "--meta")


class FileEntry(typing.TypedDict):
    mode: str
    owner: str
    group: str
    type: str
    meta: typing.Dict[str, str]


def entry_from_args(args: FileEntryArgs, **overwrite) -> FileEntry:
    """Build a :class:`FileEntry` from a parsed :class:`FileEntryArgs` mixin.

    ``type`` is converted via :class:`FileType` only when ``overwrite`` does
    not itself supply ``type`` -- a caller overwriting ``type`` (e.g. ``scan``
    passing ``type="--"``) never pays for, or risks, converting ``args.type``.
    """
    if "type" in overwrite:
        type_ = overwrite.pop("type")
    else:
        type_ = FileType(args.type) if args.type else args.type
    entry: FileEntry = {
        "mode": args.mode,
        "owner": args.owner,
        "group": args.group,
        "type": type_,
        "meta": dict(args.meta),
    }
    entry.update(overwrite)
    return entry


def entry_from_path(
    path: Path, meta: typing.Optional[typing.Dict[str, str]] = None
) -> FileEntry:
    """Build a :class:`FileEntry` by ``lstat``-ing a real path on disk."""
    stat = path.lstat()
    owner = group = DEFAULT
    if pwd is not None:
        with contextlib.suppress(KeyError):
            owner = pwd.getpwuid(stat.st_uid).pw_name
    if grp is not None:
        with contextlib.suppress(KeyError):
            group = grp.getgrgid(stat.st_gid).gr_name

    return {
        # Store mode as an octal permission string so the DB round-trips and
        # dumps (e.g. %attr(644,...)) are correct; apply_entry() reads it back
        # via int(mode, 8).
        "mode": mode_to_octal(stat.st_mode),
        "owner": owner,
        "group": group,
        "type": FileType.from_path(path),
        "meta": {} if meta is None else meta,
    }


def resolve_entry(
    entry: FileEntry, path: Path, lookupval: str = AUTO, **overwrite
) -> FileEntry:
    """Replace every field of ``entry`` equal to ``lookupval`` with the on-disk value."""
    resolved: FileEntry = {**entry}
    ondisk = entry_from_path(path)
    for k, v in resolved.items():
        if v == lookupval:
            resolved[k] = ondisk[k]
    resolved.update(overwrite)

    return resolved


def apply_entry(
    entry: FileEntry,
    path: Path,
    chown: bool = False,
    *,
    logger: typing.Optional[logging.Logger] = None,
    usedefault: str = DEFAULT,
) -> None:
    """Apply ``entry``'s mode (and, if ``chown``, owner/group) to ``path``."""
    mode = entry["mode"]
    owner = entry["owner"]
    group = entry["group"]

    if mode and mode != usedefault:
        mode = int(mode, 8) if isinstance(mode, str) else mode
        if logger:
            logger.debug("Setting mode for %s to %o", path, mode)
        os.chmod(path, mode, follow_symlinks=False)

    if chown and (owner != usedefault or group != usedefault):
        if pwd is None or grp is None:
            raise RuntimeError("chown requires the Unix pwd/grp modules")
        owner = -1 if owner == usedefault else pwd.getpwnam(owner).pw_uid
        group = -1 if group == usedefault else grp.getgrnam(group).gr_gid
        if logger:
            logger.debug("Setting owner/group for %s to %s:%s", path, owner, group)
        os.chown(path, owner, group, follow_symlinks=False)


# Runtime back-compat aliases: FileEntry values are plain dicts (it is a
# TypedDict), so these are called *unbound* through the class --
# ``FileEntry.apply(entry, path)``, never ``entry.apply(path)``. Assigning a
# plain function onto the class after its body (rather than defining it
# inside, which is what produced the historical "Invalid statement in
# TypedDict definition" mypy errors) means attribute access through the class
# returns the function itself, unbound, exactly as these callers expect
# (measured 2026-09-28, py3.9.25 and 3.14, duho 0.5.0).
setattr(FileEntry, "from_args", entry_from_args)
setattr(FileEntry, "from_path", entry_from_path)
setattr(FileEntry, "resolve_for", resolve_entry)
setattr(FileEntry, "apply", apply_entry)


class PkgForgeCmd(LoggingArgs, Cmd):
    """Common base for every pkgforge subcommand.

    Carries the two app-wide options (``--db`` and ``--buildroot``), the file-DB
    read/write helpers, and the build-root <-> local-path translation. Each leaf
    command subclasses this and implements ``__call__``; a leaf attaches itself
    to the :class:`PkgForge` root's subcommand tree via :meth:`_register`.
    """

    db: typing.Optional[Path] = Path(filedb) if filedb else None
    ("--db",)
    db_format: typing.Optional[str] = os.environ.get("PKGFORGE_DB_FORMAT")
    ("--db-format",)
    buildroot: typing.Union[Path, str] = Path(buildroot) if buildroot else Path(".")
    ("--buildroot", "-r")

    def localpath(self, buildpath: Path) -> Path:
        return Path(self.buildroot, *buildpath.parts[1:])

    def buildpath(self, localpath: Path) -> Path:
        return Path("/", localpath.relative_to(self.buildroot))

    def _no_file_db(self) -> bool:
        """True when there is no real DB file to operate on (unset / stdout)."""
        return self.db is None or str(self.db) == "-"

    def _provider(self, *, for_read: bool = False):
        """Resolve the DB storage provider for the configured --db/--db-format."""
        # Function-local: db.py imports common only under TYPE_CHECKING today,
        # so a module-top import here would be safe now, but db.py is expected
        # to import common's exceptions at runtime later; keeping this local
        # avoids introducing a cycle then.
        from .db import open_db

        return open_db(self.db, self.db_format, for_read=for_read)

    def loaddb(self) -> typing.Dict[str, typing.Optional[FileEntry]]:
        if self._no_file_db():
            return {}
        return self._provider(for_read=True).load()

    def compactdb(self) -> None:
        """Collapse the DB's redundant history (backend-specific; no-op if none)."""
        if self._no_file_db():
            return
        self._provider(for_read=True).compact()

    def initdb(self) -> None:
        """Create or reset an empty DB (no-op for a stdout / unset DB)."""
        if self._no_file_db():
            return
        self._provider().init()

    def _write_entry(self, buildpath: Path, entry: typing.Optional[FileEntry]):
        path = os.fspath(buildpath)
        if self._no_file_db():
            # No file: emit the record as a JSON Lines line to stdout.
            # Function-local for the same reason as _provider() above (Q2).
            from .db import _record

            print(json.dumps(_record(path, entry), sort_keys=True))
            return
        provider = self._provider(for_read=True)
        if entry is None:
            provider.remove(path)
        else:
            provider.add(path, entry)

    def add_entry(self, buildpath: Path, entry: FileEntry):
        self._write_entry(buildpath, entry)

    def remove_entry(self, buildpath: Path):
        self._write_entry(buildpath, None)

    @classmethod
    def _register(cls):
        PkgForge._register_subcmd_(cls)


class PkgForge(PkgForgeCmd, Cli):
    """The pkgforge application root (the ``pkgforge`` command).

    Stages files into a build root and records their intended install
    metadata (mode / owner / group / type) in a file DB (JSON Lines by
    default; YAML/SQLite backends), which can then
    be dumped into packaging manifests (e.g. an RPM file list).

    Extends :class:`PkgForgeCmd` (for the shared ``--db``/``--buildroot`` options
    and the DB helpers) and :class:`~duho.Cli` (for the app-root layer:
    ``--version``, completion, and the subcommand tree).
    """

    _version_ = duho.AUTO
    _distribution_ = "pkgforge"
    _completion_ = True
