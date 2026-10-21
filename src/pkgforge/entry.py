"""File entry types: the on-disk *file DB* record and its metadata.

Defines the record (:class:`FileEntry`: mode / owner / group / type / kind /
caps / package / free-form ``meta``) and the functions that normalize, build, resolve and
apply one (:func:`normalize_mode`, :func:`entry_from_path`,
:func:`resolve_entry`, :func:`apply_entry`). ``mode`` is always stored as an **octal permission
string** (e.g. ``"644"``) so the DB round-trips cleanly and dumps (e.g.
``%attr(644,...)`` in an RPM spec) are correct.

See :mod:`pkgforge.project` for the API that reads/writes entries through a
file DB, and :mod:`pkgforge.db` for the pluggable storage backends.
"""

from __future__ import annotations

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

from .exceptions import UsageError

__all__ = [
    "AUTO",
    "BASE_GROUPS",
    "BASE_USERS",
    "DEFAULT",
    "FileEntries",
    "FileEntry",
    "FileKind",
    "FileRecords",
    "FileType",
    "StrPath",
    "apply_entry",
    "check_meta",
    "entry_from_path",
    "mode_to_octal",
    "normalize_caps",
    "normalize_kind",
    "normalize_mode",
    "normalize_name",
    "normalize_package",
    "resolve_entry",
]

#: Sentinel meaning "resolve this field from the file on disk".
AUTO = "--"
#: Sentinel meaning "leave this field at the system/OS default (do not set it)".
DEFAULT = "-"
#: A portable account name, or one of the :data:`DEFAULT`/:data:`AUTO` sentinels.
#: No ``%``, whitespace or control character can occur, so a recorded owner or
#: group is safe in an rpm ``%attr`` and a shell word.
_NAME_PATTERN = r"--?|[A-Za-z_][A-Za-z0-9_.-]*\$?"
#: 1-4 octal digits, or a sentinel.
_MODE_PATTERN = r"--?|[0-7]{1,4}"
#: Every C0 control character plus DEL.
_CONTROL_PATTERN = "[\x00-\x1f\x7f]"
#: One ``setcap`` clause in libcap's text form, lowercase and without spaces:
#: capability names, then one or more ``=``/``+``/``-`` runs of ``e``, ``i``,
#: ``p``. It is a single argument of ``setcap`` and of rpm's ``%caps``.
_CAPS_PATTERN = r"(?:all|cap_[a-z0-9_]+)(?:,(?:all|cap_[a-z0-9_]+))*(?:[=+-][eip]*)+"
#: A package name in Debian's grammar (valid for rpm too), or :data:`DEFAULT`.
_PACKAGE_PATTERN = r"-|[a-z0-9][a-z0-9+.-]+"

#: The user names every Debian system has: the static ``passwd.master`` of the
#: ``base-passwd`` package (3.6.9). An owner outside this set must be declared.
BASE_USERS: typing.FrozenSet[str] = frozenset(
    "root daemon bin sys sync games man lp mail news uucp proxy www-data "
    "backup list irc _apt nobody".split()
)
#: The group names every Debian system has: the static ``group.master`` of the
#: ``base-passwd`` package (3.6.9). A group outside this set must be declared.
BASE_GROUPS: typing.FrozenSet[str] = frozenset(
    "root daemon bin sys adm tty disk lp mail news uucp man proxy kmem dialout "
    "fax voice cdrom floppy tape sudo audio dip www-data backup operator list "
    "irc src shadow utmp video sasl plugdev staff games users nogroup".split()
)

_NAME_RE = re.compile(_NAME_PATTERN)
_CAPS_RE = re.compile(_CAPS_PATTERN)
_PACKAGE_RE = re.compile(_PACKAGE_PATTERN)


class FileType(str, enum.Enum):
    """The three on-disk kinds pkgforge stages/records: ``File``, ``Directory``,
    ``Symlink``. :meth:`from_path` looks one up from a file on disk."""

    File = "file"
    Directory = "directory"
    Symlink = "symlink"

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


class FileKind(str, enum.Enum):
    """How packaging treats an entry, whatever the package format.

    * ``File``: an ordinary payload file (the default);
    * ``Config``: a configuration file the package manager keeps when the
      administrator edited it and the package ships a new one;
    * ``ConfigNoReplace``: a configuration file whose edited copy stays in place,
      the package's new one is written beside it;
    * ``Doc``: documentation, which a minimal install may leave out;
    * ``License``: a license text;
    * ``Ghost``: a path the package owns but does not ship, created at run time.

    ``Config``, ``ConfigNoReplace``, ``Doc`` and ``License`` need a regular
    file; ``File`` and ``Ghost`` fit every type.
    """

    File = "file"
    Config = "config"
    ConfigNoReplace = "config-noreplace"
    Doc = "doc"
    License = "license"
    Ghost = "ghost"


_KINDS = tuple(member.value for member in FileKind)
_ANY_TYPE = frozenset(member.value for member in FileType)
_FILE_ONLY = frozenset((FileType.File.value,))
#: The entry types each kind fits, by kind value.
_KIND_TYPES: typing.Dict[str, typing.FrozenSet[str]] = {
    FileKind.File.value: _ANY_TYPE,
    FileKind.Config.value: _FILE_ONLY,
    FileKind.ConfigNoReplace.value: _FILE_ONLY,
    FileKind.Doc.value: _FILE_ONLY,
    FileKind.License.value: _FILE_ONLY,
    FileKind.Ghost.value: _ANY_TYPE,
}


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


def _normalize_field(value: str) -> str:
    """An explicit empty string is DEFAULT; anything else passes through."""
    return DEFAULT if value == "" else value


def normalize_mode(value: str) -> str:
    """Normalize a ``mode`` value to an octal permission string.

    ``-``/``""`` mean
    :data:`DEFAULT`; ``--`` and ``auto`` (mode only -- ``owner``/``group`` get
    no such alias, since ``auto`` can be a real account name) mean
    :data:`AUTO`. A string of 1-4 octal digits is
    normalized (``"0644"`` -> ``"644"``). Anything else raises
    :class:`UsageError`.
    """
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


def normalize_name(value: str, field: str) -> str:
    """Validate an ``owner``/``group`` value (``field`` names it in the error).

    Applies :func:`_normalize_field`, then requires the portable account-name pattern.
    Raises :class:`UsageError` otherwise.
    """
    value = _normalize_field(value)
    if not (isinstance(value, str) and _NAME_RE.fullmatch(value)):
        raise UsageError(
            f"invalid {field} {value!r}: expected an account name "
            "([A-Za-z_][A-Za-z0-9_.-]*), '-' or '--'"
        )
    return value


def normalize_kind(value: str) -> str:
    """Normalize a ``kind`` value: ``-``/``""`` mean :data:`DEFAULT` (unset),
    one of the :class:`FileKind` values is returned as text.
    Anything else raises :class:`UsageError`."""
    if isinstance(value, FileKind):
        return value.value
    value = _normalize_field(value)
    if value == DEFAULT or value in _KINDS:
        return value
    raise UsageError(f"invalid kind {value!r}: choose from {', '.join(_KINDS)}")


def normalize_caps(value: str) -> str:
    """Normalize a ``caps`` value: ``-``/``""`` mean :data:`DEFAULT` (none),
    otherwise one clause of libcap's text form, lowercase with no space
    (``cap_net_bind_service=ep``, ``cap_net_raw,cap_net_admin+ep``).
    Anything else raises :class:`UsageError`."""
    value = _normalize_field(value)
    if value == DEFAULT:
        return value
    if isinstance(value, str) and _CAPS_RE.fullmatch(value):
        return value
    raise UsageError(
        f"invalid caps {value!r}: expected one lowercase setcap clause "
        "without spaces, such as cap_net_bind_service=ep"
    )


def normalize_package(value: str) -> str:
    """Normalize a ``package`` value: ``-``/``""`` mean :data:`DEFAULT` (unset),
    otherwise a package name (``[a-z0-9][a-z0-9+.-]+``).
    Anything else raises :class:`UsageError`."""
    value = _normalize_field(value)
    if isinstance(value, str) and _PACKAGE_RE.fullmatch(value):
        return value
    raise UsageError(
        f"invalid package {value!r}: expected a package name "
        "([a-z0-9][a-z0-9+.-]+) or '-'"
    )


def _check_kind_caps(path: str, kind: str, caps: str, ftype: object) -> None:
    """Raise :class:`UsageError` unless the resolved ``kind`` fits ``ftype``
    and ``caps`` (when set) sits on a regular file that is not a ghost."""
    if isinstance(ftype, FileType):
        ftype = ftype.value
    if ftype not in _KIND_TYPES[kind]:
        raise UsageError(f"{path}: kind {kind} needs a regular file")
    if caps != DEFAULT and (ftype != FileType.File.value or kind == "ghost"):
        raise UsageError(f"{path}: caps need a regular file that is not a ghost")


def check_meta(meta: typing.Mapping[str, str]) -> None:
    """Raise :class:`UsageError` if ``meta`` holds the key ``rpmprefix``: no
    dump reads it, and ``kind`` and ``caps`` say what it would."""
    if "rpmprefix" in meta:
        raise UsageError("meta key 'rpmprefix' is no longer read: use kind or caps")


def _or_default(value: str) -> str:
    """Treat a falsy field value (e.g. an empty string) as :data:`DEFAULT`."""
    return value if value else DEFAULT


def _is_auto(value: typing.Any) -> bool:
    """True for an unset type: :data:`DEFAULT` or :data:`AUTO`."""
    return value == DEFAULT or value == AUTO


def _filetype(value: typing.Union[str, FileType]) -> typing.Union[FileType, str]:
    """Normalize a ``--type`` value.

    ``-`` means :data:`DEFAULT` (auto-detect from the source); ``--`` means
    :data:`AUTO` (the explicit auto-detect sentinel). The value or member name of ``File``, ``Directory`` or
    ``Symlink``, matched case-insensitively, returns that member. Anything
    else -- including ``auto``, which is not a spelling of the sentinel --
    raises :class:`UsageError`.
    """
    if value == DEFAULT:
        return DEFAULT
    if value == AUTO:
        return AUTO
    if isinstance(value, FileType):
        return value
    if isinstance(value, str):
        for member in (FileType.File, FileType.Directory, FileType.Symlink):
            if value.lower() in (member.value, member.name.lower()):
                return member
    raise UsageError(f"invalid type {value!r} (choose from file, directory, symlink)")


class FileEntry(typing.TypedDict):
    """One DB record: ``mode`` (an octal permission **string**, e.g. ``"644"``,
    never a raw ``st_mode`` int), ``owner``, ``group``, ``type``, ``kind`` (a
    :class:`FileKind` value; ``"-"`` only before it is resolved), ``caps`` (one
    ``setcap`` clause, or ``"-"`` for none), ``package`` (the package the entry
    ships in; ``"-"`` only when the configuration has no ``[package]``) and a
    free-form ``meta`` string map. A plain dict at runtime (a ``TypedDict`` carries no methods) -- use
    the module functions below."""

    mode: str
    owner: str
    group: str
    type: str
    kind: str
    caps: str
    package: str
    meta: typing.Dict[str, str]


#: Entries that survived filtering: ``(db path, entry)`` pairs.
FileEntries = typing.List[typing.Tuple[str, FileEntry]]

#: What an operation wrote, in write order: ``(db path, entry)``, with ``None``
#: for a removal.
FileRecords = typing.List[typing.Tuple[str, typing.Optional[FileEntry]]]

#: A local filesystem path: text or any ``os.PathLike``.
StrPath = typing.Union[str, "os.PathLike[str]"]


def _new_entry(
    *,
    mode: str,
    owner: str,
    group: str,
    type: typing.Any,
    kind: str = DEFAULT,
    caps: str = DEFAULT,
    package: str = DEFAULT,
    meta: typing.Mapping[str, str],
) -> FileEntry:
    """A :class:`FileEntry` with ``mode``/``owner``/``group``/``kind``/``caps``/
    ``package``/``meta`` normalized and validated; ``type`` is stored as given
    (it may still be a sentinel to resolve from disk). A ``kind``, ``caps`` or
    ``package`` of ``"-"`` is resolved when the entry is recorded."""
    check_meta(meta)
    return {
        "mode": normalize_mode(mode),
        "owner": normalize_name(owner, "owner"),
        "group": normalize_name(group, "group"),
        "type": type,
        "kind": normalize_kind(kind),
        "caps": normalize_caps(caps),
        "package": normalize_package(package),
        "meta": dict(meta),
    }


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
        "kind": FileKind.File.value,
        "caps": DEFAULT,
        "package": DEFAULT,
        "meta": {} if meta is None else meta,
    }
    return _resolve_stat(base, path, path.lstat(), lookupval=AUTO)


def resolve_entry(
    entry: FileEntry, path: Path, lookupval: str = AUTO, **overwrite: typing.Any
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
            mode_int = int(mode, 8)
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
