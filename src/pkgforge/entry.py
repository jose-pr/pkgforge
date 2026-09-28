"""File entry types: the on-disk *file DB* record and its metadata.

Defines the record (:class:`FileEntry`: mode / owner / group / type /
free-form ``meta``), the CLI argument mixin that supplies those fields
(:class:`FileEntryArgs`), and the functions that build, resolve and apply an
entry (:func:`entry_from_args`, :func:`entry_from_path`, :func:`resolve_entry`,
:func:`apply_entry`). ``mode`` is always stored as an **octal permission
string** (e.g. ``"644"``) so the DB round-trips cleanly and dumps (e.g.
``%attr(644,...)`` in an RPM spec) are correct.

See :mod:`pkgforge.command` for the command base that reads/writes entries
through a file DB, and :mod:`pkgforge.db` for the pluggable storage backends.
"""

from __future__ import annotations

import argparse
import contextlib
import enum
import functools
import logging
import os
import re
import stat
import typing
from pathlib import Path

try:  # Unix-only; pkgforge targets Linux, but keep parsing/--help importable elsewhere.
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-Unix
    grp = pwd = None

import duho
from duho import Cmd

from .errors import UsageError

#: Sentinel meaning "resolve this field from the file on disk".
AUTO = "--"
#: Sentinel meaning "leave this field at the system/OS default (do not set it)".
DEFAULT = "-"


class FileType(str, enum.Enum):
    """The three on-disk kinds pkgforge stages/records: ``File``, ``Directory``,
    ``Symlink``. The sentinel member ``_AUTO`` (value :data:`AUTO`) means
    "determine from the file on disk"; :meth:`from_path` does that lookup."""

    File = "file"
    Directory = "directory"
    Symlink = "symlink"
    #: Placeholder meaning "determine the type from the file on disk".
    _AUTO = AUTO

    @classmethod
    def from_path(cls, path: Path) -> FileType:
        try:
            st = path.lstat()
        except FileNotFoundError:
            raise TypeError(
                f"{path}: not a regular file, directory or symlink "
                "(missing or special file)"
            ) from None
        return _file_type(path, st)


def _file_type(path: Path, st: os.stat_result) -> FileType:
    """Classify an already-``lstat``-ed ``path`` from ``st.st_mode`` alone --
    no extra syscall beyond the one ``lstat`` the caller already made."""
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        return FileType.Symlink
    elif stat.S_ISDIR(mode):
        return FileType.Directory
    elif stat.S_ISREG(mode):
        return FileType.File
    else:
        raise TypeError(
            f"{path}: not a regular file, directory or symlink "
            "(missing or special file)"
        )


@functools.lru_cache(maxsize=None)
def _user_name(uid: int) -> str:
    """``uid`` -> account name, memoized per process. :data:`DEFAULT` when
    ``pwd`` is unavailable (non-POSIX) or the uid has no passwd entry --
    that negative result is cached too, same as a real name."""
    if pwd is None:
        return DEFAULT
    with contextlib.suppress(KeyError):
        return pwd.getpwuid(uid).pw_name
    return DEFAULT


@functools.lru_cache(maxsize=None)
def _group_name(gid: int) -> str:
    """``gid`` -> group name, memoized per process. See :func:`_user_name`."""
    if grp is None:
        return DEFAULT
    with contextlib.suppress(KeyError):
        return grp.getgrgid(gid).gr_name
    return DEFAULT


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
    """CLI mixin supplying ``--mode/-m``, ``--group/-g``, ``--owner/-o``,
    ``--type/-t`` and ``-O/--meta KEY=VALUE`` -- the fields every leaf command
    that stages or records a :class:`FileEntry` shares."""

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
    """One DB record: ``mode`` (an octal permission **string**, e.g. ``"644"``,
    never a raw ``st_mode`` int), ``owner``, ``group``, ``type``, and a
    free-form ``meta`` string map. A plain dict at runtime (a ``TypedDict``
    carries no methods) -- use the module functions below, or the
    back-compat unbound aliases ``FileEntry.from_args``/``from_path``/
    ``resolve_for``/``apply``."""

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


def _resolve_stat(
    entry: FileEntry, path: Path, st: os.stat_result, lookupval: str = AUTO
) -> FileEntry:
    """Replace each of ``entry``'s ``mode``/``type``/``owner``/``group``
    fields equal to ``lookupval`` with the value from an already-``lstat``-ed
    ``st`` for ``path``. Shared by :func:`entry_from_path` (which resolves
    every field) and :func:`resolve_entry` (which resolves only the caller's
    AUTO-valued fields) -- ``mode``/``type`` come from ``st`` alone (no extra
    syscall); ``owner``/``group`` only pay for a (memoized) ``pwd``/``grp``
    lookup when the field actually needs resolving.
    """
    resolved: FileEntry = {**entry}
    if resolved["mode"] == lookupval:
        # Stored as an octal permission string so the DB round-trips and
        # dumps (e.g. %attr(644,...)) are correct; apply_entry() reads it
        # back via int(mode, 8).
        resolved["mode"] = mode_to_octal(st.st_mode)
    if resolved["type"] == lookupval:
        resolved["type"] = _file_type(path, st)
    if resolved["owner"] == lookupval:
        resolved["owner"] = _user_name(st.st_uid)
    if resolved["group"] == lookupval:
        resolved["group"] = _group_name(st.st_gid)
    return resolved


def entry_from_path(
    path: Path, meta: typing.Optional[typing.Dict[str, str]] = None
) -> FileEntry:
    """Build a :class:`FileEntry` by ``lstat``-ing a real path on disk."""
    base: FileEntry = {
        "mode": AUTO,
        "owner": AUTO,
        "group": AUTO,
        "type": AUTO,
        "meta": {} if meta is None else meta,
    }
    return _resolve_stat(base, path, path.lstat(), lookupval=AUTO)


def resolve_entry(
    entry: FileEntry, path: Path, lookupval: str = AUTO, **overwrite
) -> FileEntry:
    """Replace every field of ``entry`` equal to ``lookupval`` with the
    on-disk value -- a single ``lstat``, and a ``pwd``/``grp`` name lookup
    only for ``owner``/``group`` fields that actually equal ``lookupval``."""
    resolved = _resolve_stat(entry, path, path.lstat(), lookupval)
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
