"""``arch``: a pacman package written without makepkg or fakeroot."""

from __future__ import annotations

import gzip
import hashlib
import re
import shlex
import tarfile
import typing
from pathlib import Path

from .._sysfiles import SYSUSERS_DIR, TMPFILES_DIR, created_accounts, sysusers_text
from ..config import ProjectConfig
from ..dbdump import _write_files
from ..entry import DEFAULT, FileType
from ..exceptions import UsageError
from . import BuildContext, PackageFormat, PackagePayload
from ._compress import SUFFIXES, compress
from ._payload import Blob, Member, members, payload_bytes, stamp, tar_bytes

_PKGREL_RE = re.compile(r"[0-9]+(\.[0-9]+)?")
_HEREDOC = "PKGFORGE_SYSUSERS"
#: Bytes an mtree path is written as ``\ooo`` for besides those outside
#: ``!``-``~``: the escape and the comment character.
_MTREE_QUOTED = frozenset(b"\\#")


def _escape(text: str) -> str:
    """``text`` as an mtree path: every byte outside ``0x21``-``0x7e``, and
    ``\\`` and ``#``, as a three-digit octal escape."""
    out = []
    for byte in text.encode("utf-8", "surrogateescape"):
        if 0x21 <= byte <= 0x7E and byte not in _MTREE_QUOTED:
            out.append(chr(byte))
        else:
            out.append(f"\\{byte:03o}")
    return "".join(out)


def _digests(path: Path) -> typing.Tuple[str, str]:
    md5, sha256 = hashlib.md5(), hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


def _mtree(
    blobs: typing.Sequence[Blob],
    items: typing.Sequence[Member],
    when: int,
) -> bytes:
    """The ``.MTREE`` of the package: gzip, mtime 0, of ``#mtree`` and one
    line per blob and payload member (owners are always 0:0)."""
    lines = ["#mtree"]
    for blob in blobs:
        lines.append(
            f"./{_escape(blob.name)} time={when}.0 mode={blob.mode:o} uid=0 gid=0 "
            f"type=file size={len(blob.data)} "
            f"md5digest={hashlib.md5(blob.data).hexdigest()} "
            f"sha256digest={hashlib.sha256(blob.data).hexdigest()}"
        )
    for item in items:
        head = f"./{_escape(item.path.lstrip('/'))} time={item.mtime}.0 mode={item.mode:o} uid=0 gid=0"
        if item.type == FileType.File:
            md5, sha256 = _digests(item.local)
            lines.append(
                f"{head} type=file size={item.size} md5digest={md5} "
                f"sha256digest={sha256}"
            )
        elif item.type == FileType.Symlink:
            lines.append(f"{head} type=link link={_escape(item.target or '')}")
        else:
            lines.append(f"{head} type=dir")
    return gzip.compress(("\n".join(lines) + "\n").encode("ascii"), 9, mtime=0)


class _Script(typing.NamedTuple):
    """What a package's ``.INSTALL`` does."""

    text: str
    #: Whether it creates accounts or runs tmpfiles, which need systemd.
    systemd: bool


def _install_script(
    payload: PackagePayload, items: typing.Sequence[Member], config: ProjectConfig
) -> typing.Optional[_Script]:
    """The ``.INSTALL`` of the package, or ``None`` when it has no command.

    pacman ignores the owner names of the tar, so every owner or group that is
    not root is applied here, after the accounts exist. ``chown`` drops a
    setuid or setgid bit and a file capability, hence ``chmod`` and ``setcap``
    come after it.
    """
    present = {item.path for item in items}
    main = payload.main.name
    users, groups = created_accounts(config)
    text = sysusers_text(config)
    accounts = bool(text) and (
        f"{SYSUSERS_DIR}/{main}.conf" in present
        or any(item.owner in users or item.group in groups for item in items)
    )
    tmpfiles = f"{TMPFILES_DIR}/{main}.conf" in present
    body = []
    if accounts:
        body.append(f"  systemd-sysusers - <<'{_HEREDOC}'")
        body.append(text + _HEREDOC)
    if tmpfiles:
        body.append(f"  systemd-tmpfiles --create {shlex.quote(main + '.conf')} || :")
    for item in items:
        word = shlex.quote(item.path)
        if item.owner != "root":
            body.append(
                f"  chown -h -- {shlex.quote(item.owner + ':' + item.group)} {word}"
            )
        elif item.group != "root":
            body.append(f"  chgrp -h -- {shlex.quote(item.group)} {word}")
        else:
            continue
        if item.type != FileType.Symlink and item.mode & 0o6000:
            body.append(f"  chmod -- {item.mode & 0o7777:04o} {word}")
    for item in items:
        if item.entry is not None and item.entry["caps"] != DEFAULT:
            body.append(
                f"  setcap {shlex.quote(item.entry['caps'])} {shlex.quote(item.path)}"
            )
    if not body:
        return None
    lines = ["post_install() {", *body, "}", "post_upgrade() {", "  post_install", "}"]
    return _Script("\n".join(lines) + "\n", accounts or tmpfiles)


def _pkgrel(release: typing.Optional[str]) -> str:
    if release is None:
        return "1"
    if not _PKGREL_RE.fullmatch(release):
        raise UsageError(f"arch: release {release!r} is not a pacman pkgrel (N or N.M)")
    return release


def _pkginfo(
    payload: PackagePayload,
    version: str,
    arch: str,
    when: int,
    size: int,
    depends: typing.Iterable[str],
) -> str:
    main, info = payload.main, payload.info
    lines = [
        "# Generated by pkgforge",
        f"pkgname = {payload.name}",
        f"pkgbase = {main.name}",
        f"xdata = pkgtype={'pkg' if info is main else 'split'}",
        f"pkgver = {version}",
        f"pkgdesc = {info.summary}",
    ]
    if main.homepage:
        lines.append(f"url = {main.homepage}")
    lines.append(f"builddate = {when}")
    lines.append(f"packager = {main.maintainer or 'Unknown Packager'}")
    lines.append(f"size = {size}")
    lines.append(f"arch = {arch}")
    if main.license:
        lines.append(f"license = {main.license}")
    lines.extend(f"depend = {item}" for item in dict.fromkeys(depends))
    return "\n".join(lines) + "\n"


class ArchFormat(PackageFormat):
    """``NAME-VERSION-PKGREL-ARCH.pkg.tar.{zst,xz,gz}`` per package.

    The tar is GNU, with ``.PKGINFO``, ``.MTREE`` and, when it has a command,
    ``.INSTALL`` ahead of the payload. pacman ignores the owner names of a
    tar, so every header is ``root:root`` and ``.INSTALL`` applies each other
    owner or group (creating the accounts first); ``pacman -Qkk`` reports an
    owner mismatch for those paths.
    """

    NAME = "arch"
    COMPRESSIONS = ("zst", "xz", "gz")
    REQUIRED = ("version", "summary")
    SUB_REQUIRED = ("summary",)
    ALL_ARCH = "any"
    MACHINES = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
        "armv7l": "armv7h",
        "i686": "i686",
        "i386": "i686",
        "riscv64": "riscv64",
    }

    def build(
        self, payloads: typing.Sequence[PackagePayload], context: BuildContext
    ) -> typing.List[Path]:
        assert context.compression is not None
        when = stamp(context.epoch)
        files: typing.Dict[Path, bytes] = {}
        for payload in payloads:
            arch = self.arch_name(payload, context)
            version = f"{payload.main.version}-{_pkgrel(payload.main.release)}"
            name = f"{payload.name}-{version}-{arch}.pkg.tar"
            data = self._package(payload, context, version, arch, when)
            files[context.output / (name + SUFFIXES[context.compression])] = data
        _write_files(files)
        return list(files)

    def _package(
        self,
        payload: PackagePayload,
        context: BuildContext,
        version: str,
        arch: str,
        when: int,
    ) -> bytes:
        assert context.compression is not None
        items = members(payload.entries, context.buildroot, epoch=context.epoch)
        script = _install_script(payload, items, context.config)
        depends = list(payload.info.pacman.depends)
        if script is not None and script.systemd:
            depends.append("systemd")
        if any(
            item.entry is not None and item.entry["caps"] != DEFAULT for item in items
        ):
            depends.append("libcap")
        pkginfo = Blob(
            ".PKGINFO",
            _pkginfo(
                payload, version, arch, when, payload_bytes(items), depends
            ).encode("utf-8", "surrogateescape"),
            0o644,
        )
        install = (
            []
            if script is None
            else [
                Blob(".INSTALL", script.text.encode("utf-8", "surrogateescape"), 0o644)
            ]
        )
        mtree = Blob(".MTREE", _mtree([pkginfo, *install], items, when), 0o644)
        data = tar_bytes(
            [pkginfo, mtree, *install, *items],
            format=tarfile.GNU_FORMAT,
            owners=lambda _item: ("root", "root"),
            mtime=when,
        )
        return compress(data, context.compression)
