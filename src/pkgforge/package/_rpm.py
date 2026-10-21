"""``rpm``: a package built by ``rpmbuild`` from the staged tree."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tarfile
import tempfile
import typing
from pathlib import Path

from .. import dbdump
from .._install._tool import _run_tool
from .._sysfiles import unit_names
from ..dbdump._rpmspec import _spec
from ..exceptions import PkgForgeError
from . import BuildContext, PackageFormat, PackagePayload
from ._payload import members, tar_bytes

_LOGGER = logging.getLogger("pkgforge.package")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")
_SYSTEMD_MACROS = "%{?systemd_post:1}"
#: The rpm release that reads the shipped sysusers.d file (see ``RPMSpec``).
_SYSUSERS_RPM = (4, 19)
_INSTALL = [
    'mkdir -p "%{buildroot}"\n'
    'tar -C "%{buildroot}" -xpf "%{pkgforge_payload}"\n'
    "mkdir -p %{_builddir}/pkgforge-files\n"
    'cp "%{pkgforge_lists}"/files-*.txt %{_builddir}/pkgforge-files/\n',
]


def _output(argv: typing.Sequence[str]) -> str:
    """What ``argv`` prints; empty when it cannot run or fails."""
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _eval(rpmbuild: str, expression: str) -> str:
    """``expression`` expanded by rpm."""
    return _output([rpmbuild, "--eval", expression])


def _rpm_version(rpmbuild: str) -> typing.Tuple[int, int]:
    found = _VERSION_RE.search(_output([rpmbuild, "--version"]))
    if found is None:
        raise PkgForgeError(f"package -f rpm: cannot read the version of {rpmbuild}")
    return int(found.group(1)), int(found.group(2))


class RpmFormat(PackageFormat):
    """``NAME-VERSION-RELEASE.ARCH.rpm`` per package, built by ``rpmbuild``.

    The files are the recorded, non-ghost entries of the build root, packed into
    a tar that the spec's ``%install`` unpacks, so rpm applies the DB's modes,
    owners and kinds through the ``%files`` lists ``dbdump -f rpmspecfiles``
    writes (the ``-pre419`` variants below rpm 4.19). The spec is the one
    ``dbdump -f rpmspec`` writes, with the unit scriptlets (they expand to
    nothing, with a warning, where rpm has no systemd macros). Needs
    ``rpmbuild`` on ``PATH`` and a ``summary`` per ``[[packages]]`` element;
    ``--compression`` is refused, since rpm owns its payload, and ``--arch`` is
    passed as ``rpmbuild --target``. The build happens in a temporary directory
    that is removed afterwards; the packages are moved into the output
    directory.
    """

    NAME = "rpm"
    REQUIRED = ("version", "summary", "license")
    SUB_REQUIRED = ("summary",)

    def build(
        self, payloads: typing.Sequence[PackagePayload], context: BuildContext
    ) -> typing.List[Path]:
        rpmbuild = shutil.which("rpmbuild")
        if rpmbuild is None:
            raise PkgForgeError("package -f rpm needs rpmbuild on PATH")
        legacy = _rpm_version(rpmbuild) < _SYSUSERS_RPM
        entries = [pair for payload in payloads for pair in payload.entries]
        if unit_names(entries) and _eval(rpmbuild, _SYSTEMD_MACROS) != "1":
            _LOGGER.warning(
                "rpm has no systemd macros: the unit scriptlets are left out "
                "(install systemd-rpm-macros)"
            )
        main = payloads[0].main
        top = Path(tempfile.mkdtemp(prefix="pkgforge-rpm-"))
        try:
            for name in ("SPECS", "RPMS", "BUILD", "BUILDROOT", "SOURCES", "SRPMS"):
                (top / name).mkdir()
            items = members(
                entries, context.buildroot, epoch=context.epoch, parents=False
            )
            (top / "payload.tar").write_bytes(
                tar_bytes(items, format=tarfile.GNU_FORMAT)
            )
            lists = top / "lists"
            lists.mkdir()
            dbdump.dump(
                dict(entries),
                "rpmspecfiles-pre419" if legacy else "rpmspecfiles",
                lists,
                config=context.config,
            )
            spec = _spec(
                context.config,
                entries,
                fmt=self.NAME,
                sysusers_pre=legacy,
                install=_INSTALL,
                guard=False,
            )
            spec_path = top / "SPECS" / f"{main.name}.spec"
            spec_path.write_bytes(spec.encode("utf-8"))
            argv = [
                rpmbuild,
                "-bb",
                "--define",
                f"_topdir {top}",
                "--define",
                f"pkgforge_payload {top / 'payload.tar'}",
                "--define",
                f"pkgforge_lists {lists}",
            ]
            if context.arch:
                argv += ["--target", context.arch]
            _run_tool([*argv, str(spec_path)], what="package -f rpm")
            return self._collect(payloads, top / "RPMS", context.output)
        finally:
            shutil.rmtree(top, ignore_errors=True)

    @staticmethod
    def _collect(
        payloads: typing.Sequence[PackagePayload], built: Path, output: Path
    ) -> typing.List[Path]:
        """Move the rpms ``rpmbuild`` wrote below ``built`` into ``output``, in
        payload order."""
        found = sorted(built.rglob("*.rpm"))
        result = []
        for payload in payloads:
            version = payload.main.version
            release = payload.main.release or "1"
            pattern = re.compile(
                re.escape(f"{payload.name}-{version}-{release}.")
                + r"[A-Za-z0-9_]+\.rpm"
            )
            matches = [path for path in found if pattern.fullmatch(path.name)]
            if len(matches) != 1:
                raise PkgForgeError(
                    f"package -f rpm: rpmbuild wrote {len(matches)} files for "
                    f"{payload.name}, expected one"
                )
            target = output / matches[0].name
            if target.exists():
                target.unlink()
            shutil.move(str(matches[0]), str(target))
            result.append(target)
        return result
