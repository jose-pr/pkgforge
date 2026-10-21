"""``apk``: an Alpine package (apk v2) written without abuild."""

from __future__ import annotations

import hashlib
import re
import shlex
import tarfile
import typing
from pathlib import Path

from .._sysfiles import SYSUSERS_DIR, created_accounts
from ..config import ProjectConfig
from ..dbdump import _write_files
from ..entry import DEFAULT, FileKind, FileType
from ..exceptions import DumpError, UsageError
from . import BuildContext, PackageFormat, PackagePayload
from ._compress import compress
from ._payload import Blob, Member, members, payload_bytes, stamp, tar_bytes

_VERSION_RE = re.compile(
    r"[0-9]+(\.[0-9]+)*[a-z]?(_(alpha|beta|pre|rc|cvs|svn|git|hg|p)[0-9]*)*"
)
_RELEASE_RE = re.compile(r"[0-9]+")
_GHOST = FileKind.Ghost.value
_CHECKSUM = "APK-TOOLS.checksum.SHA1"
#: busybox ``adduser`` defaults for an account that sets no home or shell.
_HOME = "/"
_SHELL = "/sbin/nologin"
_QUIET = " 2>/dev/null || :"


def _sha1(member: Member) -> str:
    """The SHA-1 of a regular file's content, or of a symlink's target."""
    digest = hashlib.sha1()
    if member.type == FileType.Symlink:
        digest.update((member.target or "").encode("utf-8", "surrogateescape"))
    else:
        with open(member.local, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _checksum(member: Member) -> typing.Dict[str, str]:
    """The pax record apk-tools 3 requires on every file and symlink."""
    if member.type == FileType.Directory:
        return {}
    return {_CHECKSUM: _sha1(member)}


def _words(*words: str) -> str:
    return " ".join(shlex.quote(word) for word in words)


def _script(commands: typing.Sequence[str]) -> typing.Optional[bytes]:
    """A ``sh`` script that runs ``commands`` and succeeds, or ``None``."""
    if not commands:
        return None
    return ("#!/bin/sh\n" + "\n".join(commands) + "\nexit 0\n").encode("utf-8")


def _account_commands(
    items: typing.Sequence[Member], main: str, config: ProjectConfig
) -> typing.List[str]:
    """busybox commands that create the accounts ``config`` creates, when the
    package ships the sysusers file or holds an entry one of them owns."""
    users, groups = created_accounts(config)
    present = {item.path for item in items}
    if f"{SYSUSERS_DIR}/{main}.conf" not in present and not any(
        item.owner in users or item.group in groups for item in items
    ):
        return []
    commands = [
        _words("addgroup", "-S", group.name) + _QUIET
        for group in config.groups
        if group.create
    ]
    for user in config.users:
        if not user.create:
            continue
        commands.append(_words("addgroup", "-S", user.name) + _QUIET)
        adduser = ["adduser", "-S", "-D", "-H"]
        adduser += ["-h", user.home or _HOME, "-s", user.shell or _SHELL]
        adduser += ["-G", user.name]
        if user.description:
            adduser += ["-g", user.description]
        commands.append(_words(*adduser, user.name) + _QUIET)
        commands.extend(
            _words("addgroup", user.name, group) + _QUIET for group in user.groups
        )
    return commands


def _setup_commands(
    payload: PackagePayload, items: typing.Sequence[Member]
) -> typing.List[str]:
    """Commands that create each ghost directory with its recorded owner and
    mode (nothing creates them on Alpine, which has no tmpfiles), then set
    each file capability."""
    commands = []
    for path, entry in sorted(payload.entries, key=lambda pair: pair[0]):
        if entry["kind"] != _GHOST or entry["type"] != FileType.Directory:
            continue
        owner = entry["owner"] if entry["owner"] not in ("", DEFAULT) else "root"
        group = entry["group"] if entry["group"] not in ("", DEFAULT) else "root"
        command = (
            f"{_words('mkdir', '-p', '--', path)} && "
            f"{_words('chown', '--', f'{owner}:{group}', path)}"
        )
        if entry["mode"] != DEFAULT:
            command += f" && {_words('chmod', '--', format(int(entry['mode'], 8), '04o'), path)}"
        commands.append(command)
    for item in items:
        if item.entry is not None and item.entry["caps"] != DEFAULT:
            commands.append(_words("setcap", item.entry["caps"], item.path))
    return commands


def _version(payload: PackagePayload) -> str:
    version, release = payload.main.version or "", payload.main.release or "0"
    if not _VERSION_RE.fullmatch(version):
        raise UsageError(f"apk: version {version!r} is not an apk version")
    if not _RELEASE_RE.fullmatch(release):
        raise UsageError(f"apk: release {release!r} is not an apk release (digits)")
    return f"{version}-r{release}"


def _pkginfo(
    payload: PackagePayload,
    version: str,
    arch: str,
    when: int,
    size: int,
    depends: typing.Iterable[str],
    datahash: str,
) -> bytes:
    main, info = payload.main, payload.info
    lines = [
        "# Generated by pkgforge",
        f"pkgname = {payload.name}",
        f"pkgver = {version}",
        f"pkgdesc = {info.summary}",
    ]
    if main.homepage:
        lines.append(f"url = {main.homepage}")
    lines.append(f"builddate = {when}")
    lines.append(f"packager = {main.maintainer or 'Unknown'}")
    lines.append(f"size = {size}")
    lines.append(f"arch = {arch}")
    lines.append(f"origin = {main.name}")
    if main.maintainer:
        lines.append(f"maintainer = {main.maintainer}")
    if main.license:
        lines.append(f"license = {main.license}")
    lines.extend(f"depend = {item}" for item in dict.fromkeys(depends))
    lines.append(f"datahash = {datahash}")
    return ("\n".join(lines) + "\n").encode("utf-8")


class ApkFormat(PackageFormat):
    """``NAME-VERSION-rREL.apk`` per package: an apk v2 file, which apk-tools 2
    and 3 install with ``--allow-untrusted``.

    It is two gzip members back to back: the control tar (``.PKGINFO`` and the
    scripts, with no end-of-archive blocks) and the data tar (pax, owner names
    from the DB, and a ``APK-TOOLS.checksum.SHA1`` record on every file and
    symlink). ``.PKGINFO`` carries the ``datahash`` of the data member, which
    apk-tools 3 requires. apk v3 packages are not written.
    """

    NAME = "apk"
    COMPRESSIONS = ("gz",)
    REQUIRED = ("version", "summary")
    SUB_REQUIRED = ("summary",)
    ALL_ARCH = "noarch"
    MACHINES = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
        "armv7l": "armv7",
        "i686": "x86",
        "i386": "x86",
        "riscv64": "riscv64",
        "ppc64le": "ppc64le",
        "s390x": "s390x",
    }

    def build(
        self, payloads: typing.Sequence[PackagePayload], context: BuildContext
    ) -> typing.List[Path]:
        when = stamp(context.epoch)
        files: typing.Dict[Path, bytes] = {}
        for payload in payloads:
            arch = self.arch_name(payload, context)
            version = _version(payload)
            data = self._package(payload, context, version, arch, when)
            files[context.output / f"{payload.name}-{version}.apk"] = data
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
        items = members(payload.entries, context.buildroot, epoch=context.epoch)
        # Ghost directories are not members, but .post-install names them.
        texts = [(path, path) for path, _entry in payload.entries]
        texts.extend((item.path, item.target) for item in items if item.target)
        for path, text in texts:
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                raise DumpError(f"{path}: apk needs UTF-8 paths") from None
        data_gz = compress(
            tar_bytes(items, format=tarfile.PAX_FORMAT, pax=_checksum, mtime=when),
            "gz",
        )
        depends = list(payload.info.apk.depends)
        if any(
            item.entry is not None and item.entry["caps"] != DEFAULT for item in items
        ):
            depends.append("libcap-setcap")
        pre = _script(_account_commands(items, payload.main.name, context.config))
        post = _script(_setup_commands(payload, items))
        control: typing.List[Blob] = [
            Blob(
                ".PKGINFO",
                _pkginfo(
                    payload,
                    version,
                    arch,
                    when,
                    payload_bytes(items),
                    depends,
                    hashlib.sha256(data_gz).hexdigest(),
                ),
                0o644,
            )
        ]
        for scripts, names in (
            (pre, (".pre-install", ".pre-upgrade")),
            (post, (".post-install", ".post-upgrade")),
        ):
            if scripts is not None:
                control.extend(Blob(name, scripts, 0o755) for name in names)
        control_gz = compress(
            tar_bytes(control, format=tarfile.USTAR_FORMAT, eof=False, mtime=when),
            "gz",
        )
        return control_gz + data_gz
