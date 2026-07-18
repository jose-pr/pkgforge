"""Core types shared across buildutils commands.

Defines the on-disk *file DB* record (:class:`FileEntry`) and its metadata
(mode / owner / group / type / free-form ``meta``), the CLI argument mixin that
supplies those fields (:class:`FileEntryArgs`), and the common command base
(:class:`BuildUtil`) that every subcommand extends.

The file DB is a YAML mapping of build-relative path -> :class:`FileEntry`
(or ``None`` to mark a removal). ``mode`` is always stored as an **octal
permission string** (e.g. ``"644"``) so the DB round-trips cleanly and dumps
(e.g. ``%attr(644,...)`` in an RPM spec) are correct.
"""

from __future__ import annotations

import enum
import logging
import os
import typing
from pathlib import Path

try:  # Unix-only; buildutils targets Linux, but keep parsing/--help importable elsewhere.
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-Unix
    grp = pwd = None

import duho
import yaml
from duho import Cli, Cmd, LoggingArgs

buildroot = os.environ.get("BUILDROOT")
filedb = os.environ.get("BUILDUTILS_DB")

#: Sentinel meaning "resolve this field from the file on disk".
AUTO = "--"
#: Sentinel meaning "leave this field at the system/OS default (do not set it)".
DEFAULT = "-"


def parsepath(path: str) -> "typing.Optional[typing.Union[str, Path]]":
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
    def from_path(cls, path: Path) -> "FileType":
        if path.is_symlink():
            return FileType.Symlink
        elif path.is_dir():
            return cls.Directory
        elif path.is_file():
            return cls.File
        else:
            raise TypeError(path)


yaml.SafeDumper.add_representer(FileType, lambda d, t: d.represent_str(t.value))


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
    type: "typing.Optional[FileType]" = None
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
    meta: "typing.Dict[str,str]"

    @classmethod
    def from_args(cls, args: "FileEntryArgs", **overwrite) -> "FileEntry":
        entry = {
            "mode": args.mode,
            "owner": args.owner,
            "group": args.group,
            "type": FileType(args.type) if args.type else args.type,
            "meta": dict(args.meta),
        }
        entry.update(overwrite)
        return entry

    @classmethod
    def from_path(cls, path: Path, meta: "typing.Dict[str, str]" = None) -> "FileEntry":
        stat = path.lstat()
        owner = group = DEFAULT
        if pwd is not None:
            try:
                owner = pwd.getpwuid(stat.st_uid).pw_name
            except KeyError:
                owner = DEFAULT
        if grp is not None:
            try:
                group = grp.getgrgid(stat.st_gid).gr_name
            except KeyError:
                group = DEFAULT

        return {
            # Store mode as an octal permission string so the DB round-trips and
            # dumps (e.g. %attr(644,...)) are correct; apply() reads it back via
            # int(mode, 8).
            "mode": mode_to_octal(stat.st_mode),
            "owner": owner,
            "group": group,
            "type": FileType.from_path(path),
            "meta": {} if meta is None else meta,
        }

    def resolve_for(self, path: Path, lookupval=AUTO, **overwrite) -> "FileEntry":
        resolved: FileEntry = {**self}
        ondisk = FileEntry.from_path(path)
        for k, v in resolved.items():
            if v == lookupval:
                resolved[k] = ondisk[k]
        resolved.update(overwrite)

        return resolved

    def apply(
        self,
        path: Path,
        chown=False,
        *,
        logger: logging.Logger = None,
        usedefault=DEFAULT,
    ):
        mode = self["mode"]
        owner = self["owner"]
        group = self["group"]

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


class BuildUtil(LoggingArgs, Cmd):
    """Common base for every buildutils subcommand.

    Carries the two app-wide options (``--db`` and ``--buildroot``), the file-DB
    read/write helpers, and the build-root <-> local-path translation. Each leaf
    command subclasses this and implements ``__call__``; a leaf attaches itself
    to the :class:`BuildUtils` root's subcommand tree via :meth:`_register`.
    """

    db: "typing.Optional[Path]" = Path(filedb) if filedb else None
    ("--db",)
    buildroot: "typing.Union[Path, str]" = Path(buildroot) if buildroot else Path(".")
    ("--buildroot", "-r")

    def localpath(self, buildpath: Path) -> Path:
        return Path(self.buildroot, *buildpath.parts[1:])

    def buildpath(self, localpath: Path) -> Path:
        return Path("/", localpath.relative_to(self.buildroot))

    def loaddb(self) -> "typing.Dict[str, typing.Optional[FileEntry]]":
        if self.db is None or str(self.db) == "-" or not self.db.exists():
            return {}
        return yaml.safe_load(self.db.read_text()) or {}

    def _write_entry(self, buildpath: Path, entry: "typing.Optional[FileEntry]"):
        record = yaml.safe_dump({os.fspath(buildpath): entry})
        if self.db is None or str(self.db) == "-":
            print(record, end="")
        else:
            with self.db.open("a") as file:
                file.write(record)

    def add_entry(self, buildpath: Path, entry: "FileEntry"):
        self._write_entry(buildpath, entry)

    def remove_entry(self, buildpath: Path):
        self._write_entry(buildpath, None)

    def __call__(self):
        raise NotImplementedError(self)

    @classmethod
    def _register(cls):
        BuildUtils._register_subcmd_(cls)


class BuildUtils(BuildUtil, Cli):
    """The buildutils application root (the ``buildutils`` command).

    Stages files into a build root and records their intended install
    metadata (mode / owner / group / type) in a YAML file DB, which can then
    be dumped into packaging manifests (e.g. an RPM file list).

    Extends :class:`BuildUtil` (for the shared ``--db``/``--buildroot`` options
    and the DB helpers) and :class:`~duho.Cli` (for the app-root layer:
    ``--version``, completion, and the subcommand tree).
    """

    _version_ = duho.AUTO
    _distribution_ = "buildutils"
    _completion_ = True

    def __call__(self):
        # No subcommand given: argparse (required subparsers) handles the error,
        # but keep a clear failure if reached directly.
        raise NotImplementedError(self)
