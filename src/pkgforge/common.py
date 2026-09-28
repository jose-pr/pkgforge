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

import argparse
import contextlib
import enum
import json
import logging
import os
import posixpath
import re
import stat
import typing
from pathlib import Path, PurePath, PurePosixPath

try:  # Unix-only; pkgforge targets Linux, but keep parsing/--help importable elsewhere.
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-Unix
    grp = pwd = None

import duho
from duho import Cli, Cmd, LoggingArgs

#: Sentinel meaning "resolve this field from the file on disk".
AUTO = "--"
#: Sentinel meaning "leave this field at the system/OS default (do not set it)".
DEFAULT = "-"


class PkgForgeError(Exception):
    """Base class for pkgforge's own runtime failures.

    Caught by :func:`pkgforge.main`'s error boundary: prints one
    ``pkgforge: error: ...`` line to stderr and exits 1. Python-API callers
    still see it raised normally.
    """


class UsageError(PkgForgeError, ValueError):
    """An argument-shaped mistake (a bad or missing value the caller gave).

    Caught by :func:`pkgforge.main`'s error boundary and mapped to exit 2,
    like an argparse usage error. Subclasses :class:`ValueError` so existing
    ``pytest.raises(ValueError, ...)`` tests, and any Python-API caller
    catching ``ValueError``, keep working unchanged.
    """


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
            raise TypeError(
                f"{path}: not a regular file, directory or symlink "
                "(missing or special file)"
            )


def mode_to_octal(mode: int) -> str:
    """Render a stat ``st_mode`` as a bare octal permission string (e.g. ``"644"``)."""
    return format(mode & 0o7777, "o")


def _normalize_field(value: typing.Union[str, typing.List[str]]) -> str:
    """Map argparse's Python 3.9 ``[]`` artifact to AUTO, and an explicit
    empty string to DEFAULT.

    On Python 3.9, argparse strips an attached ``--`` (``--mode=--``,
    ``-m--``, ``--owner=--``) to ``[]`` *before* any ``type=`` converter
    runs, so this has to be checked wherever such a value can land, not only
    inside a converter. Anything else passes through unchanged.
    """
    if value == []:
        return AUTO
    if value == "":
        return DEFAULT
    return value


def normalize_mode(value: typing.Union[str, int, typing.List[str]]) -> str:
    """Normalize a ``mode`` value to an octal permission string.

    An ``int`` is rendered via :func:`mode_to_octal`. ``-``/``""`` mean
    :data:`DEFAULT`; ``--``, ``auto`` (mode only -- ``owner``/``group`` get no
    such alias, since ``auto`` can be a real account name) or Python 3.9's
    stripped ``[]`` mean :data:`AUTO`. A string of 1-4 octal digits is
    normalized (``"0644"`` -> ``"644"``). Anything else raises
    :class:`UsageError`.
    """
    if isinstance(value, int):
        return mode_to_octal(value)
    value = _normalize_field(value)
    if value == AUTO or value == "auto":
        return AUTO
    if value == DEFAULT:
        return DEFAULT
    if isinstance(value, str) and re.fullmatch(r"[0-7]{1,4}", value):
        return mode_to_octal(int(value, 8))
    raise UsageError(
        f"invalid mode {value!r}: expected 1-4 octal digits, '-', '--' or 'auto'"
    )


def _parse_mode(text: str) -> str:
    """CLI ``type=`` converter for ``--mode``: an explicit empty value is
    rejected outright (unlike the Python API, where :func:`normalize_mode`
    treats ``""`` as :data:`DEFAULT`), and anything else is normalized via
    :func:`normalize_mode`, with :class:`UsageError` translated to argparse's
    own error type so a bad value exits 2 before anything is staged.
    """
    if text == "":
        raise argparse.ArgumentTypeError(
            "mode must not be empty; use '-' for the default, or '--'/'auto' "
            "to resolve from the staged file"
        )
    try:
        return normalize_mode(text)
    except UsageError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _or_default(value: str) -> str:
    """Treat a falsy field value (e.g. an empty string) as :data:`DEFAULT`."""
    return value if value else DEFAULT


def _filetype(value: typing.Union[str, FileType]) -> typing.Union[FileType, str]:
    """Normalize a ``--type`` value.

    ``-`` means :data:`DEFAULT` (auto-detect from the source); ``--`` or
    :attr:`FileType._AUTO` means :data:`AUTO` (the explicit auto-detect
    sentinel). The value or member name of ``File``, ``Directory`` or
    ``Symlink``, matched case-insensitively, returns that member. Anything
    else -- including ``auto``/``_AUTO``, which name the sentinel member but
    are not a documented spelling of it -- raises :class:`UsageError`.
    """
    if value == DEFAULT:
        return DEFAULT
    if value == AUTO or value == FileType._AUTO:
        return AUTO
    if isinstance(value, FileType) and value != FileType._AUTO:
        return value
    if isinstance(value, str):
        for member in (FileType.File, FileType.Directory, FileType.Symlink):
            if value.lower() in (member.value, member.name.lower()):
                return member
    raise UsageError(f"invalid type {value!r} (choose from file, directory, symlink)")


def _parse_filetype(text: str) -> typing.Union[FileType, str]:
    """CLI ``type=`` converter for ``--type``: wraps :func:`_filetype`,
    translating :class:`UsageError` to argparse's own error type."""
    try:
        return _filetype(text)
    except UsageError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _key_value(text: str) -> typing.Dict[str, str]:
    """CLI ``type=`` converter for ``-O``/``--meta``: one ``KEY=VALUE`` pair
    as a single-entry dict (merged by :class:`duho.UpdateAction`). A value
    missing ``=`` raises argparse's own error type directly (naming what was
    given), instead of the opaque ``invalid <lambda> value`` a bare lambda
    converter reports.
    """
    if "=" not in text:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    key, value = text.split("=", maxsplit=1)
    return {key: value}


class FileEntryArgs(Cmd):
    mode: duho.Arg[str, duho.NS(type=_parse_mode)] = DEFAULT
    "permission mode: 1-4 octal digits, '-' (leave default), '--' or 'auto' (resolve from the staged file)"
    ("--mode", "-m")
    group: str = DEFAULT
    "group to record (default '-', the OS default)"
    ("--group", "-g")
    owner: str = DEFAULT
    "owner to record (default '-', the OS default)"
    ("--owner", "-o")
    type: duho.Arg[
        typing.Optional[FileType],
        duho.NS(type=_parse_filetype, metavar="{file,directory,symlink}"),
    ] = None
    "file, directory or symlink, in any case (auto-detected if unset, or given as '--')"
    ("--type", "-t")
    meta: duho.Arg[
        typing.Dict[str, str],
        duho.NS(
            action=duho.UpdateAction,
            type=_key_value,
            metavar="KEY=VALUE",
        ),
    ] = {}
    "extra metadata KEY=VALUE (repeatable)"
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
        "mode": normalize_mode(args.mode),
        "owner": _normalize_field(args.owner),
        "group": _normalize_field(args.group),
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
    """Apply ``entry``'s owner/group (if ``chown``) and then mode to ``path``.

    Order matters: on Linux, ``chown()`` of a regular file clears any
    setuid/setgid bit even when the owner doesn't change and even as root, so
    chown runs first and mode is applied last (or, when the entry leaves mode
    at ``usedefault``, the pre-chown special bits are restored). A symlink's
    mode is never set on disk -- Linux ignores it -- but it is still recorded
    in ``entry``.
    """
    mode = _or_default(entry["mode"])
    owner = _or_default(entry["owner"])
    group = _or_default(entry["group"])

    if mode == AUTO:
        raise UsageError(
            "entry mode is unresolved (AUTO); call resolve_entry before apply_entry"
        )

    is_symlink = stat.S_ISLNK(os.lstat(path).st_mode)
    restore_mode = None

    if chown and (owner != usedefault or group != usedefault):
        if pwd is None or grp is None:
            raise RuntimeError("chown requires the Unix pwd/grp modules")
        try:
            uid = -1 if owner == usedefault else pwd.getpwnam(owner).pw_uid
        except KeyError:
            raise UsageError(f"unknown owner {owner!r}") from None
        try:
            gid = -1 if group == usedefault else grp.getgrnam(group).gr_gid
        except KeyError:
            raise UsageError(f"unknown group {group!r}") from None
        if not is_symlink and mode == usedefault:
            # The entry isn't setting an explicit mode, so chown() below
            # would otherwise silently drop any setuid/setgid/sticky bit
            # already on disk; capture it here to restore below.
            current = stat.S_IMODE(os.lstat(path).st_mode)
            if current & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                restore_mode = current
        if logger:
            logger.debug("Setting owner/group for %s to %s:%s", path, uid, gid)
        os.chown(path, uid, gid, follow_symlinks=False)

    if mode != usedefault:
        if is_symlink:
            if logger:
                logger.debug("Not setting mode on symlink %s (Linux ignores it)", path)
        else:
            mode_int = int(mode, 8) if isinstance(mode, str) else mode
            if logger:
                logger.debug("Setting mode for %s to %o", path, mode_int)
            if os.chmod in os.supports_follow_symlinks:
                os.chmod(path, mode_int, follow_symlinks=False)
            else:
                # Plain chmod(2): every glibc supports this. Passing
                # follow_symlinks=False here is unsupported on Linux for ANY
                # path (not just symlinks) below glibc 2.32, and CPython
                # refuses it outright (NotImplementedError) regardless of
                # glibc version, since it never advertises the capability.
                os.chmod(path, mode_int)
    elif restore_mode is not None:
        if logger:
            logger.debug("Restoring mode %o for %s after chown", restore_mode, path)
        os.chmod(path, restore_mode)


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
    """Validate a ``db_format`` value against the registered providers.

    A falsy value (``None`` or ``""``) means "auto-detect" and is never
    checked here -- ``open_db`` still resolves it from the ``--db`` suffix or,
    for an existing file, by sniffing its content. Anything else must already
    be a registered provider name, checked eagerly so a typo fails before any
    file is staged, instead of surfacing as a traceback the first time the DB
    is touched.
    """
    if not value:
        return value
    # Function-local, like PkgForgeCmd._provider() below: db.py's PROVIDERS is
    # read fresh on every call, so a register_provider() call that runs after
    # this module is imported (a third-party backend) is still recognized.
    from .db import PROVIDERS

    if value not in PROVIDERS:
        raise UsageError(
            f"unknown db format {value!r}; choose from {', '.join(sorted(PROVIDERS))}"
        )
    return value


class PkgForgeCmd(LoggingArgs, Cmd):
    """Common base for every pkgforge subcommand.

    Carries the two app-wide options (``--db`` and ``--buildroot``), the file-DB
    read/write helpers, and the build-root <-> local-path translation. Each leaf
    command subclasses this and implements ``__call__``; a leaf attaches itself
    to the :class:`PkgForge` root's subcommand tree via :meth:`_register`.
    """

    db: duho.Arg[
        typing.Optional[Path],
        duho.NS(env="PKGFORGE_DB", type=_env_path, metavar="PATH"),
    ] = _env_path(os.environ.get("PKGFORGE_DB", ""))
    "file DB path (env PKGFORGE_DB); unset or '-' records to stdout"
    ("--db",)
    db_format: duho.Arg[
        typing.Optional[str],
        duho.NS(env="PKGFORGE_DB_FORMAT", type=_env_str, metavar="FORMAT"),
    ] = _env_str(os.environ.get("PKGFORGE_DB_FORMAT", ""))
    "storage backend: jsonl, yaml, sqlite, or a register_provider name (env PKGFORGE_DB_FORMAT); else inferred from the --db suffix"
    ("--db-format",)
    buildroot: duho.Arg[
        Path, duho.NS(env="PKGFORGE_ROOT", type=_env_root, metavar="DIR")
    ] = _env_root(os.environ.get("PKGFORGE_ROOT", ""))
    "staging root that maps to '/' in the DB (env PKGFORGE_ROOT; default '.')"
    ("--buildroot", "-r")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Checked after construction (so env/CLI/default fill-in has already
        # happened) rather than as a `type=` converter: a converter only runs
        # for a value that actually goes through argparse, so a direct
        # Python-API construction (PkgForgeCmd(db_format="toml")) would
        # otherwise bypass it entirely, same as the mode/type converters
        # above.
        self.db_format = _check_db_format(self.db_format)

    def localpath(self, buildpath: typing.Union[str, os.PathLike]) -> Path:
        p = PurePosixPath(os.fspath(buildpath))
        rel = p.relative_to("/") if p.is_absolute() else p
        return Path(self.buildroot, rel)

    def buildpath(self, localpath: Path) -> Path:
        return Path("/", localpath.relative_to(self.buildroot))

    def _rootpath(
        self, path: typing.Union[str, os.PathLike], *, follow_final: bool = False
    ) -> Path:
        """Resolve a ``/``-rooted DESTINATION/PATH to a path under
        ``--buildroot``, refusing one that would climb above it.

        A literal ``..`` that climbs above the root is refused -- checked on
        the root-relative form, since normalizing the ``/``-rooted form
        first (``posixpath.normpath("/../x")`` collapses to ``/``) would
        silently hide the escape instead of rejecting it. An in-root ``..``
        (``/usr/share/../lib/x``) is normalized in the returned path, never
        left for the caller to record verbatim.

        A symlinked path component is refused the same way: after building
        the local path, ``realpath`` of its parent (non-strict, so this runs
        before any mkdir/unlink -- it resolves the existing prefix without
        raising for a path that doesn't exist yet) must stay under the
        build root's own realpath. The leaf itself is checked too, but only
        when ``follow_final`` is true and it already names a directory --
        the case where the caller is about to walk or write *into* it, not
        merely replace it. A leaf that is a dangling symlink, or a symlink
        to a file, therefore has only its parent checked, and is recorded
        as the link itself rather than its (possibly out-of-root) target.

        Not a sandbox against a concurrent writer (no ``openat2``/TOCTOU
        guarantees) -- that isn't needed for a single build process, though
        ``install`` re-checks right before its own mkdir, since an earlier
        source in the same multi-source invocation can plant a new symlink
        after this method already ran for a later one.

        Also refuses an unusable ``--buildroot``: unset/empty (a falsy
        value, reachable only from a direct Python-API construction) always
        raises, and a *relative* build root (the ordinary cwd default)
        whose realpath is ``/`` raises unless it was spelled explicitly
        (``--buildroot /`` or ``PKGFORGE_ROOT=/``) -- otherwise an
        unattended run started from ``/`` with no build root configured
        would map straight onto the live filesystem, silently.
        """
        root = self.buildroot
        if not root:
            raise UsageError("no build root configured (--buildroot/PKGFORGE_ROOT)")
        root = Path(root)
        real_root = os.path.realpath(root)
        if not root.is_absolute() and real_root == os.path.realpath(os.sep):
            raise UsageError(
                f"build root {os.fspath(root)!r} resolves to '/'; pass "
                "--buildroot / (or set PKGFORGE_ROOT=/) to target the live "
                "filesystem"
            )

        # A PurePath (install's DESTINATION, always a Path) converts via its
        # own .as_posix(): stringifying it first (os.fspath) would render it
        # with the native separator, which is a backslash on Windows and
        # breaks PurePosixPath parsing -- self.buildpath() builds exactly
        # such a "/"-rooted Path for _stage()'s re-check. A plain str (scan's
        # PATH) is already posix-shaped text from the caller, so it goes
        # through PurePosixPath directly, as before.
        if isinstance(path, PurePath):
            posix_path = path.as_posix()
        else:
            posix_path = PurePosixPath(os.fspath(path)).as_posix()
        rel = posixpath.normpath(posix_path.lstrip("/") or ".")
        if rel == ".." or rel.startswith("../"):
            raise UsageError(f"{path}: resolves outside --buildroot")
        if rel == ".":
            return root

        local = Path(root, rel)
        check = local if (follow_final and os.path.isdir(local)) else local.parent
        if os.path.commonpath([real_root, os.path.realpath(check)]) != real_root:
            raise UsageError(f"{path}: a symlink leads outside --buildroot")
        return local

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

        # `self.db_format` can be "" from a direct Python-API construction
        # (e.g. PkgForgeCmd(db_format="")), which bypasses the `type=`
        # converter above entirely -- normalize it here too, so an empty
        # value means "auto-detect" everywhere, not only through argv/env.
        return open_db(self.db, self.db_format or None, for_read=for_read)

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
            # Function-local for the same reason as _provider() above.
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
    """Stage files into a build root and record their intended install
    metadata (mode, owner, group, type) in a file DB. Dump that DB into
    packaging manifests (RPM %files, Debian install/permissions).
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
