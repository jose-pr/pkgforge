"""``deb``: a Debian binary package written without dpkg-deb or debhelper."""

from __future__ import annotations

import hashlib
import shlex
import tarfile
import typing
from pathlib import Path

from .._sysfiles import SYSUSERS_DIR, TMPFILES_DIR, unit_names
from ..dbdump import _has_control, _reject_control, _write_files
from ..dbdump._debian import (
    _SUBSTVAR_RE,
    _description_lines,
    _install_time,
    _package_lines,
)
from ..entry import DEFAULT, FileType, _or_default
from ..exceptions import UsageError
from . import BuildContext, PackageFormat, PackagePayload
from ._compress import SUFFIXES, compress
from ._payload import Blob, Member, installed_kib, members, stamp, tar_bytes

#: The only dpkg-gencontrol variables a depends item may hold here.
_VERSION_VARS = ("${binary:Version}", "${source:Version}")
_SYSUSERS_DEPENDS = "systemd | systemd-standalone-sysusers | systemd-sysusers"
_SHEBANG = "#!/bin/sh\nset -e\n"
#: The guard under which a script talks to the running systemd.
_SYSTEMD_UP = '[ -z "$DPKG_ROOT" ] && [ "$1" = remove ] && [ -d /run/systemd/system ]'
_ROOT_OPTION = '${DPKG_ROOT:+--root="$DPKG_ROOT"}'
_AR_MAGIC = b"!<arch>\n"


def _ar_member(name: str, when: int, data: bytes) -> bytes:
    """One ar member: a 60-byte header, then ``data`` padded to an even length."""
    header = f"{name:<16}{when:<12}{0:<6}{0:<6}{0o100644:<8o}{len(data):<10}`\n"
    return header.encode("ascii") + data + (b"\n" if len(data) % 2 else b"")


def _owners(item: Member) -> typing.Tuple[str, str]:
    """The header names of ``item``: root for an account created at install
    time, which does not exist while dpkg unpacks (``postinst`` applies it)."""
    entry = item.entry
    if entry is None or _install_time(
        _or_default(entry["owner"]), _or_default(entry["group"])
    ):
        return "root", "root"
    return item.owner, item.group


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _depends(payload: PackagePayload, version: str, sysusers: bool, caps: bool) -> str:
    items = []
    if sysusers:
        items.append(_SYSUSERS_DEPENDS)
    if caps:
        items.append("libcap2-bin")
    where = (
        "[package.debian]"
        if payload.info is payload.main
        else f"[[packages]] {payload.name} debian"
    )
    for item in payload.info.debian.depends:
        for variable in _SUBSTVAR_RE.findall(item):
            if variable not in _VERSION_VARS:
                raise UsageError(
                    f"deb: {where} depends item {item!r} holds {variable}, "
                    "which only dpkg-gencontrol defines"
                )
        items.append(_SUBSTVAR_RE.sub(lambda _match: version, item))
    return ", ".join(items)


def _control(
    payload: PackagePayload, version: str, arch: str, size: int, depends: str
) -> str:
    main = payload.main
    info = payload.info
    fields = [f"Package: {payload.name}"]
    if info is not main:
        fields.append(f"Source: {main.name}")
    fields.extend((f"Version: {version}", f"Architecture: {arch}"))
    fields.append(f"Maintainer: {main.maintainer}")
    fields.append(f"Installed-Size: {size}")
    if depends:
        fields.append(f"Depends: {depends}")
    fields.append(f"Section: {info.debian.section or main.debian.section or 'misc'}")
    fields.append(
        f"Priority: {info.debian.priority or main.debian.priority or 'optional'}"
    )
    if main.homepage:
        fields.append(f"Homepage: {main.homepage}")
    return (
        "\n".join(fields)
        + "\n"
        + _description_lines(info.summary or "", info.description)
    )


def _postinst(
    main: str,
    units: typing.List[str],
    sysusers: bool,
    tmpfiles: bool,
    overrides: typing.List[str],
    setcaps: typing.List[str],
) -> typing.List[str]:
    lines = []
    if sysusers:
        lines.append(f"  systemd-sysusers {_ROOT_OPTION} {shlex.quote(main + '.conf')}")
    if tmpfiles:
        lines.append(
            f"  if command -v systemd-tmpfiles >/dev/null; then systemd-tmpfiles "
            f"{_ROOT_OPTION} --create {shlex.quote(main + '.conf')} || true; fi"
        )
    for unit in map(shlex.quote, units):
        lines.extend(
            (
                f"  deb-systemd-helper unmask {unit} >/dev/null || true",
                f"  if deb-systemd-helper --quiet was-enabled {unit}; then",
                f"    deb-systemd-helper enable {unit} >/dev/null || true",
                "  else",
                f"    deb-systemd-helper update-state {unit} >/dev/null || true",
                "  fi",
            )
        )
    if units:
        lines.extend(
            (
                "  if [ -d /run/systemd/system ]; then",
                "    systemctl --system daemon-reload >/dev/null || true",
                '    if [ -n "$2" ]; then _action=restart; else _action=start; fi',
                f"    deb-systemd-invoke $_action {' '.join(map(shlex.quote, units))}"
                " >/dev/null || true",
                "  fi",
            )
        )
    # chown drops file capabilities, so the overrides come first.
    lines.extend(overrides)
    lines.extend(setcaps)
    return ['if [ "$1" = configure ]; then', *lines, "fi"] if lines else []


def _prerm(units: typing.List[str]) -> typing.List[str]:
    if not units:
        return []
    return [
        f"if {_SYSTEMD_UP}; then",
        f"  deb-systemd-invoke stop {' '.join(map(shlex.quote, units))}"
        " >/dev/null || true",
        "fi",
    ]


def _postrm(units: typing.List[str], purges: typing.List[str]) -> typing.List[str]:
    lines = []
    if units:
        lines.extend(
            (
                f"if {_SYSTEMD_UP}; then",
                "  systemctl --system daemon-reload >/dev/null || true",
                "fi",
            )
        )
    purge = []
    if units:
        purge.extend(
            (
                "  if [ -x /usr/bin/deb-systemd-helper ]; then",
                f"    deb-systemd-helper purge {' '.join(map(shlex.quote, units))}"
                " >/dev/null || true",
                "  fi",
            )
        )
    purge.extend(purges)
    if purge:
        lines.extend(('if [ "$1" = purge ]; then', *purge, "fi"))
    return lines


def _script(lines: typing.List[str]) -> bytes:
    return (_SHEBANG + "\n".join(lines) + "\n").encode("utf-8", "surrogateescape")


class DebFormat(PackageFormat):
    """``NAME_VERSION[-RELEASE]_ARCH.deb`` per package.

    The ar archive holds ``debian-binary``, ``control.tar`` and ``data.tar``,
    both GNU tar with a ``./`` root, as debhelper builds them. The control
    file, ``conffiles`` (regular files below ``/etc`` and config kinds
    elsewhere), ``md5sums`` and the maintainer scripts match what debhelper
    builds from the same configuration; ``postinst``, ``prerm`` and ``postrm``
    are written only with a command in them (sysusers, tmpfiles, units,
    ``dpkg-statoverride`` for an account created at install time, ``setcap``).
    """

    NAME = "deb"
    COMPRESSIONS = ("xz", "gz", "zst", "none")
    REQUIRED = ("version", "summary", "maintainer")
    SUB_REQUIRED = ("summary",)
    ALL_ARCH = "all"
    MACHINES = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armhf",
        "i686": "i386",
        "i386": "i386",
        "riscv64": "riscv64",
        "ppc64le": "ppc64el",
        "s390x": "s390x",
    }

    def build(
        self, payloads: typing.Sequence[PackagePayload], context: BuildContext
    ) -> typing.List[Path]:
        assert context.compression is not None
        when = stamp(context.epoch)
        files: typing.Dict[Path, bytes] = {}
        for payload in payloads:
            arch = self.arch_name(payload, context)
            main = payload.main
            version = f"{main.version}-{main.release}" if main.release else main.version
            assert version is not None  # REQUIRED
            data = self._deb(payload, context, version, arch, when)
            files[context.output / f"{payload.name}_{version}_{arch}.deb"] = data
        _write_files(files)
        return list(files)

    def _deb(
        self,
        payload: PackagePayload,
        context: BuildContext,
        version: str,
        arch: str,
        when: int,
    ) -> bytes:
        paths = [path for path, _entry in payload.entries]
        if _has_control(paths):
            for path in paths:
                _reject_control(path, "deb")
        items = members(payload.entries, context.buildroot, epoch=context.epoch)
        lines = _package_lines(payload.name, payload.entries)
        present = {item.path for item in items}
        main = payload.main.name
        sysusers = f"{SYSUSERS_DIR}/{main}.conf" in present
        tmpfiles = f"{TMPFILES_DIR}/{main}.conf" in present
        units = unit_names(payload.entries)
        conffiles = sorted(
            {
                item.path
                for item in items
                if item.type == FileType.File and item.path.startswith("/etc/")
            }.union(lines.conffiles)
        )
        listed = set(conffiles)
        sums = [
            f"{_md5(item.local)}  {item.path.lstrip('/')}\n"
            for item in items
            if item.type == FileType.File and item.path not in listed
        ]
        depends = _depends(
            payload,
            version,
            sysusers,
            any(
                item.entry is not None and item.entry["caps"] != DEFAULT
                for item in items
            ),
        )
        blobs = []
        if conffiles:
            text = "".join(f"{path}\n" for path in conffiles)
            blobs.append(
                Blob("conffiles", text.encode("utf-8", "surrogateescape"), 0o644)
            )
        control_text = _control(payload, version, arch, installed_kib(items), depends)
        blobs.append(Blob("control", control_text.encode("utf-8"), 0o644))
        if sums:
            blobs.append(
                Blob("md5sums", "".join(sums).encode("utf-8", "surrogateescape"), 0o644)
            )
        scripts = (
            (
                "postinst",
                _postinst(
                    main, units, sysusers, tmpfiles, lines.overrides, lines.setcaps
                ),
            ),
            ("postrm", _postrm(units, lines.purges)),
            ("prerm", _prerm(units)),
        )
        blobs.extend(Blob(name, _script(body), 0o755) for name, body in scripts if body)
        control = tar_bytes(
            blobs,
            format=tarfile.GNU_FORMAT,
            prefix="./",
            root=True,
            mtime=when,
        )
        payload_tar = tar_bytes(
            items,
            format=tarfile.GNU_FORMAT,
            prefix="./",
            root=True,
            owners=_owners,
            mtime=when,
        )
        assert context.compression is not None
        suffix = SUFFIXES[context.compression]
        return b"".join(
            (
                _AR_MAGIC,
                _ar_member("debian-binary", when, b"2.0\n"),
                _ar_member(
                    f"control.tar{suffix}", when, compress(control, context.compression)
                ),
                _ar_member(
                    f"data.tar{suffix}",
                    when,
                    compress(payload_tar, context.compression),
                ),
            )
        )
