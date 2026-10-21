"""The payload every package format writes: members read from the build root,
and the tar archive that holds them."""

from __future__ import annotations

import io
import os
import posixpath
import shutil
import stat
import tarfile
import time
import typing
from pathlib import Path

from ..entry import DEFAULT, FileEntries, FileEntry, FileKind, FileType, _file_type
from ..exceptions import DumpError, UsageError

#: Longest user or group name a tar header holds; GNU headers cut a longer one.
_NAME_BYTES = 32
_GHOST = FileKind.Ghost.value
_BLOCK = 512


class Member(typing.NamedTuple):
    """One path of a package payload."""

    #: The DB path (``/``-rooted POSIX text).
    path: str
    type: FileType
    #: Permission bits, setuid/setgid/sticky included.
    mode: int
    owner: str
    group: str
    #: Seconds since the epoch, at most ``SOURCE_DATE_EPOCH`` when it is set.
    mtime: int
    #: Bytes of a regular file, else 0.
    size: int
    #: The path in the build root.
    local: Path
    #: The link target of a symlink.
    target: typing.Optional[str]
    #: The recorded entry; ``None`` for a parent directory the package does
    #: not record.
    entry: typing.Optional[FileEntry]


class Blob(typing.NamedTuple):
    """A generated file of a package archive (control data, a script)."""

    name: str
    data: bytes
    mode: int


def stamp(epoch: typing.Optional[int]) -> int:
    """``epoch``, else the current time in whole seconds."""
    return epoch if epoch is not None else int(time.time())


def _account(path: str, what: str, name: str) -> str:
    """``name`` for a header: ``root`` for the unset ``-``."""
    if not name or name == DEFAULT:
        return "root"
    if len(name.encode("utf-8")) > _NAME_BYTES:
        raise DumpError(
            f"{path}: {what} {name!r} is longer than the {_NAME_BYTES} bytes "
            "a tar header holds"
        )
    return name


def _clamp(st: os.stat_result, epoch: typing.Optional[int]) -> int:
    seconds = int(st.st_mtime)
    return seconds if epoch is None else min(seconds, epoch)


def members(
    entries: FileEntries,
    buildroot: Path,
    *,
    epoch: typing.Optional[int],
    parents: bool = True,
) -> typing.List[Member]:
    """The payload of one package: its live, non-ghost ``entries`` read from
    ``buildroot``, sorted by DB path.

    A mode left unset (``-``) is the staged file's permission bits (``0o777``
    for a symlink) and an unset owner or group is ``root``. With ``parents``,
    each ancestor directory the package does not record is added as a 755
    root:root directory.

    Raises:
        UsageError: a recorded path is missing from ``buildroot`` or is of
            another type than recorded.
        DumpError: a user or group name is longer than a tar header holds.
    """
    recorded = {path for path, _entry in entries}
    result: typing.Dict[str, Member] = {}
    for path, entry in entries:
        if entry["kind"] == _GHOST or path == "/":
            continue
        local = buildroot / path.lstrip("/")
        wanted = FileType(entry["type"])
        try:
            st = local.lstat()
            found = _file_type(local, st).value
        except (FileNotFoundError, NotADirectoryError):
            raise UsageError(
                f"{path}: recorded but missing from the build root; "
                "run pkgforge verify"
            ) from None
        except TypeError:
            found = "special"
        if found != wanted.value:
            raise UsageError(
                f"{path}: recorded as {wanted.value}, but the build root holds "
                f"a {found}; run pkgforge verify"
            )
        symlink = wanted == FileType.Symlink
        if symlink:
            mode = 0o777
        elif entry["mode"] and entry["mode"] != DEFAULT:
            mode = int(entry["mode"], 8)
        else:
            mode = stat.S_IMODE(st.st_mode)
        result[path] = Member(
            path,
            wanted,
            mode,
            _account(path, "owner", entry["owner"]),
            _account(path, "group", entry["group"]),
            _clamp(st, epoch),
            st.st_size if wanted == FileType.File else 0,
            local,
            os.readlink(local) if symlink else None,
            entry,
        )
    if parents:
        for path in list(result):
            parent = posixpath.dirname(path)
            while parent != "/" and parent not in recorded and parent not in result:
                directory = buildroot / parent.lstrip("/")
                try:
                    mtime = _clamp(directory.lstat(), epoch)
                except OSError:
                    mtime = stamp(epoch)
                result[parent] = Member(
                    parent,
                    FileType.Directory,
                    0o755,
                    "root",
                    "root",
                    mtime,
                    0,
                    directory,
                    None,
                    None,
                )
                parent = posixpath.dirname(parent)
    return [result[path] for path in sorted(result)]


def installed_kib(items: typing.Iterable[Member]) -> int:
    """The ``Installed-Size`` dpkg computes for ``items``, in KiB: each
    regular file and symlink rounds up to whole KiB (a symlink is as long as
    its target), every other object counts 1, and so does the package root."""
    total = 1
    for item in items:
        if item.type == FileType.File:
            total += -(-item.size // 1024)
        elif item.type == FileType.Symlink:
            total += -(
                -len((item.target or "").encode("utf-8", "surrogateescape")) // 1024
            )
        else:
            total += 1
    return total


def tar_bytes(
    items: typing.Iterable[typing.Union[Member, Blob]],
    *,
    format: int,
    prefix: str = "",
    root: bool = False,
    pax: typing.Optional[typing.Callable[[Member], typing.Dict[str, str]]] = None,
    owners: typing.Optional[typing.Callable[[Member], typing.Tuple[str, str]]] = None,
    eof: bool = True,
    mtime: int = 0,
) -> bytes:
    """A tar archive of ``items`` in ``format`` (``tarfile.GNU_FORMAT``,
    ``PAX_FORMAT`` or ``USTAR_FORMAT``).

    Every entry has uid and gid 0. A :class:`Member` takes its names from
    ``owners(member)`` when given, else from itself, and ``pax(member)`` adds
    pax records to its header; a :class:`Blob` is root:root. Names are
    ``prefix`` plus the DB path without its leading ``/`` (a blob's own name).
    ``root`` writes a ``./`` directory first. ``mtime`` is the time of the
    ``./`` entry and of every blob. ``eof=False`` leaves out the end-of-archive
    blocks, for a stream another archive continues.
    """
    out = io.BytesIO()

    def put(info: tarfile.TarInfo, content: typing.Union[bytes, Path, None]) -> None:
        out.write(info.tobuf(format, "utf-8", "surrogateescape"))
        if not info.isreg() or not info.size:
            return
        if isinstance(content, Path):
            with open(content, "rb") as handle:
                content = handle.read(info.size + 1)
        if content is None or len(content) != info.size:
            raise OSError(f"{info.name}: changed while the package was built")
        out.write(content)
        out.write(b"\0" * (-info.size % _BLOCK))

    def header(name: str, kind: bytes, mode: int, when: int) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.mode = mode
        info.mtime = when
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        return info

    if root:
        put(header("./", tarfile.DIRTYPE, 0o755, mtime), None)
    for item in items:
        if isinstance(item, Blob):
            info = header(prefix + item.name, tarfile.REGTYPE, item.mode, mtime)
            info.size = len(item.data)
            put(info, item.data)
            continue
        kind = {
            FileType.File: tarfile.REGTYPE,
            FileType.Directory: tarfile.DIRTYPE,
            FileType.Symlink: tarfile.SYMTYPE,
        }[item.type]
        info = header(prefix + item.path.lstrip("/"), kind, item.mode, item.mtime)
        info.uname, info.gname = owners(item) if owners else (item.owner, item.group)
        info.size = item.size
        if item.target is not None:
            info.linkname = item.target
        if pax is not None:
            info.pax_headers = pax(item)
        put(info, item.local)
    if eof:
        out.write(b"\0" * (2 * _BLOCK))
    return out.getvalue()
