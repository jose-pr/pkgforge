"""The payload every package format writes: members read from the build root,
and the tar archive that holds them."""

from __future__ import annotations

import os
import posixpath
import stat
import tarfile
import time
import typing
from pathlib import Path

from .._buildroot import under_root
from ..entry import DEFAULT, FileEntries, FileEntry, FileKind, FileType, _file_type
from ..exceptions import DumpError, UsageError
from ._compress import Sink, compressor

#: Longest user or group name a tar header holds; GNU headers cut a longer one.
_NAME_BYTES = 32
_GHOST = FileKind.Ghost.value
_BLOCK = 512
_ZEROS = bytes(_BLOCK)
#: Bytes read from a file per write.
_CHUNK = 1 << 20


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
    #: The DB path of the earlier member this regular file is a hardlink to
    #: (the same inode, mode, owners and capability), else ``None``.
    link: typing.Optional[str] = None


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


def _same_file(first: Member, other: Member) -> bool:
    """Whether ``other`` may be a hardlink to ``first``: a link shares one
    inode, so it can carry neither another mode or owner nor a capability the
    first lacks."""
    assert first.entry is not None and other.entry is not None
    return (first.mode, first.owner, first.group, first.entry["caps"]) == (
        other.mode,
        other.owner,
        other.group,
        other.entry["caps"],
    )


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
    root:root directory. Regular files that share an inode, mode, owners and
    capability become one file plus :attr:`Member.link` members that name it.

    Raises:
        UsageError: a recorded path is missing from ``buildroot``, is of
            another type than recorded, or has a symlinked parent that leads
            outside ``buildroot``.
        DumpError: a user or group name is longer than a tar header holds.
    """
    recorded = {path for path, _entry in entries}
    result: typing.Dict[str, Member] = {}
    inodes: typing.Dict[str, typing.Tuple[int, int]] = {}
    real_root = os.path.realpath(buildroot)
    for path, entry in entries:
        if entry["kind"] == _GHOST or path == "/":
            continue
        local = under_root(buildroot, real_root, path)
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
        if wanted == FileType.File and st.st_nlink > 1:
            inodes[path] = (st.st_dev, st.st_ino)
    first: typing.Dict[typing.Tuple[int, int], Member] = {}
    for path in sorted(inodes):
        item = result[path]
        target = first.setdefault(inodes[path], item)
        if target is not item and _same_file(target, item):
            result[path] = item._replace(link=target.path)
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
    its target), a hardlink adds nothing, every other object counts 1, and so
    does the package root."""
    total = 1
    for item in items:
        if item.link is not None:
            continue
        if item.type == FileType.File:
            total += -(-item.size // 1024)
        elif item.type == FileType.Symlink:
            total += -(
                -len((item.target or "").encode("utf-8", "surrogateescape")) // 1024
            )
        else:
            total += 1
    return total


def payload_bytes(items: typing.Iterable[Member]) -> int:
    """The bytes ``items`` occupy: each regular file's size plus the length of
    each symlink's target; a hardlink adds nothing."""
    total = 0
    for item in items:
        if item.link is not None:
            continue
        if item.type == FileType.File:
            total += item.size
        elif item.type == FileType.Symlink:
            total += len((item.target or "").encode("utf-8", "surrogateescape"))
    return total


class _Record(typing.NamedTuple):
    """One tar entry: its header bytes, then ``size`` bytes of content (from a
    file, or bytes) padded to a block."""

    header: bytes
    name: str
    size: int
    content: typing.Union[bytes, Path, None]


def _records(
    items: typing.Iterable[typing.Union[Member, Blob]],
    *,
    format: int,
    prefix: str,
    root: bool,
    pax: typing.Optional[typing.Callable[[Member], typing.Dict[str, str]]],
    owners: typing.Optional[typing.Callable[[Member], typing.Tuple[str, str]]],
    mtime: int,
) -> typing.Iterator[_Record]:
    def put(info: tarfile.TarInfo, content: typing.Union[bytes, Path, None]) -> _Record:
        size = info.size if info.isreg() else 0
        return _Record(
            info.tobuf(format, "utf-8", "surrogateescape"), info.name, size, content
        )

    def header(name: str, kind: bytes, mode: int, when: int) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.mode = mode
        info.mtime = when
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        return info

    if root:
        yield put(header("./", tarfile.DIRTYPE, 0o755, mtime), None)
    for item in items:
        if isinstance(item, Blob):
            info = header(prefix + item.name, tarfile.REGTYPE, item.mode, mtime)
            info.size = len(item.data)
            yield put(info, item.data)
            continue
        kind = {
            FileType.File: tarfile.REGTYPE,
            FileType.Directory: tarfile.DIRTYPE,
            FileType.Symlink: tarfile.SYMTYPE,
        }[item.type]
        if item.link is not None:
            kind = tarfile.LNKTYPE
        info = header(prefix + item.path.lstrip("/"), kind, item.mode, item.mtime)
        info.uname, info.gname = owners(item) if owners else (item.owner, item.group)
        info.size = 0 if item.link is not None else item.size
        if item.target is not None:
            info.linkname = item.target
        elif item.link is not None:
            info.linkname = prefix + item.link.lstrip("/")
        if pax is not None:
            info.pax_headers = pax(item)
        yield put(info, item.local)


def _write_record(out: Sink, record: _Record) -> None:
    """Write ``record`` to ``out``: its header, then its content and padding,
    a chunk at a time. A file up to one chunk goes out in a single write, which
    is what keeps a payload of many small files cheap.

    Raises:
        OSError: the file is not ``record.size`` bytes long any more.
    """
    size = record.size
    if not size:
        out.write(record.header)
        return
    padding = _ZEROS[: -size % _BLOCK]
    if isinstance(record.content, bytes):
        out.write(record.header + record.content + padding)
        return
    assert record.content is not None
    changed = OSError(f"{record.name}: changed while the package was built")
    with open(record.content, "rb") as handle:
        if size <= _CHUNK:
            data = handle.read(size + 1)
            if len(data) != size:
                raise changed
            out.write(record.header + data + padding)
            return
        out.write(record.header)
        remaining = size
        while remaining:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                raise changed
            out.write(chunk)
            remaining -= len(chunk)
        if handle.read(1):
            raise changed
    out.write(padding)


def write_tar(
    out: Sink,
    items: typing.Iterable[typing.Union[Member, Blob]],
    *,
    format: int,
    prefix: str = "",
    root: bool = False,
    pax: typing.Optional[typing.Callable[[Member], typing.Dict[str, str]]] = None,
    owners: typing.Optional[typing.Callable[[Member], typing.Tuple[str, str]]] = None,
    eof: bool = True,
    mtime: int = 0,
) -> None:
    """Write a tar archive of ``items`` in ``format`` (``tarfile.GNU_FORMAT``,
    ``PAX_FORMAT`` or ``USTAR_FORMAT``) to ``out``, reading each file in
    chunks.

    Every entry has uid and gid 0. A :class:`Member` takes its names from
    ``owners(member)`` when given, else from itself, and ``pax(member)`` adds
    pax records to its header; a :class:`Blob` is root:root. A member with
    :attr:`Member.link` is a hardlink to that member. Names are ``prefix`` plus
    the DB path without its leading ``/`` (a blob's own name). ``root`` writes a
    ``./`` directory first. ``mtime`` is the time of the ``./`` entry and of
    every blob. ``eof=False`` leaves out the end-of-archive blocks, for a stream
    another archive continues.

    Raises:
        OSError: a file changed size while it was read.
    """
    for record in _records(
        items,
        format=format,
        prefix=prefix,
        root=root,
        pax=pax,
        owners=owners,
        mtime=mtime,
    ):
        _write_record(out, record)
    if eof:
        out.write(b"\0" * (2 * _BLOCK))


def tar_size(
    items: typing.Iterable[typing.Union[Member, Blob]],
    *,
    format: int,
    prefix: str = "",
    root: bool = False,
    pax: typing.Optional[typing.Callable[[Member], typing.Dict[str, str]]] = None,
    owners: typing.Optional[typing.Callable[[Member], typing.Tuple[str, str]]] = None,
    eof: bool = True,
    mtime: int = 0,
) -> int:
    """The length of what :func:`write_tar` writes for the same arguments,
    without reading any file (``pax`` is still called per member)."""
    total = 2 * _BLOCK if eof else 0
    for record in _records(
        items,
        format=format,
        prefix=prefix,
        root=root,
        pax=pax,
        owners=owners,
        mtime=mtime,
    ):
        total += len(record.header) + record.size + -record.size % _BLOCK
    return total


def write_compressed_tar(
    out: Sink,
    kind: str,
    items: typing.Sequence[typing.Union[Member, Blob]],
    **options: typing.Any,
) -> None:
    """:func:`write_tar` of ``items`` into ``out`` compressed as ``kind`` (see
    :func:`~pkgforge.package._compress.compressor`). ``options`` are
    :func:`write_tar`'s keyword arguments."""
    size = tar_size(items, **options) if kind == "zst" else None
    with compressor(out, kind, size) as stream:
        write_tar(stream, items, **options)
