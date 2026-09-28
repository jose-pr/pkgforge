"""Feeds dbdump's output to the real packaging consumers it targets:
``rpmbuild`` for ``rpmspecfiles``, ``dh_install``/``dh_installdirs`` for
``debian``. Skipped outright when the tool isn't on ``PATH`` (see
``.agents/AGENTS.md`` Dev env for where to reach a host that has them); not
collected by the plain unit-test run, but not excluded from it either --
these tests are cheap when skipped.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

RPMBUILD = shutil.which("rpmbuild")
RPM = shutil.which("rpm")
DH_INSTALL = shutil.which("dh_install")
DH_INSTALLDIRS = shutil.which("dh_installdirs")


def _rpm_major_minor():
    out = subprocess.run(
        ["rpmbuild", "--version"], capture_output=True, text=True, check=True
    ).stdout
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


_RPM_VERSION = _rpm_major_minor() if RPMBUILD else None
_RPM_BELOW_419 = _RPM_VERSION is not None and _RPM_VERSION < (4, 19)

requires_rpmbuild = pytest.mark.skipif(
    RPMBUILD is None or RPM is None, reason="rpmbuild/rpm not installed"
)
requires_dh_install = pytest.mark.skipif(
    DH_INSTALL is None or DH_INSTALLDIRS is None,
    reason="dh_install/dh_installdirs not installed",
)
# rpm <4.19's %files -f parser expands and splits names differently (double
# macro expansion via specExpand + rpmExpand; see rpm4_quoted_globs.md) --
# strict xfail so a fixed floor turns this back into a real failure.
xfail_rpm_below_419 = pytest.mark.xfail(
    _RPM_BELOW_419,
    strict=True,
    reason="rpm <4.19 expands and splits %files names differently",
)


def _skip_if_fs_rejects_non_utf8(dirpath: Path) -> None:
    """Skip the test if this filesystem can't hold a non-UTF-8 name (e.g.
    macOS/APFS raises OSError(EILSEQ) on the raw byte sequence)."""
    probe = dirpath / os.fsdecode(b"probe-\xe9")
    try:
        probe.write_bytes(b"x")
    except OSError as exc:
        if exc.errno == errno.EILSEQ:
            pytest.skip("filesystem rejects non-UTF-8 names")
        raise
    else:
        probe.unlink()


# --------------------------------------------------------------------------
# rpmbuild
# --------------------------------------------------------------------------


def _build_rpm(tmp_path: Path, root: Path, manifest: Path, name: str) -> Path:
    """Build a minimal noarch RPM whose payload is copied straight from
    ``root`` and whose file list comes from ``manifest`` (a rpmspecfiles
    dump). Returns the built .rpm's path.

    ``%install`` copies ``root`` into ``%{buildroot}`` itself (rather than
    staging directly into ``%{buildroot}``) because rpm's own
    ``%__spec_install_pre`` wipes ``%{buildroot}`` before ``%install`` runs.
    ``_unpackaged_files_terminate_build 0`` tolerates a staged file that
    ``manifest`` doesn't list (a ``--noentry`` sibling); ``AutoReqProv: no``
    and ``__os_install_post %{nil}`` skip dependency/brp scripts that would
    otherwise choke on an odd file name.
    """
    topdir = tmp_path / "top"
    for sub in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS", "BUILDROOT"):
        (topdir / sub).mkdir(parents=True, exist_ok=True)
    spec = tmp_path / f"{name}.spec"
    spec.write_text(
        textwrap.dedent(f"""\
            Name: {name}
            Version: 1
            Release: 1
            Summary: pkgforge consumer test
            License: MIT
            BuildArch: noarch
            AutoReqProv: no

            %description
            pkgforge consumer test package.

            %install
            rm -rf %{{buildroot}}
            mkdir -p %{{buildroot}}
            cp -a {root}/. %{{buildroot}}/

            %files -f {manifest}
            """),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "rpmbuild",
            "-bb",
            "--nodeps",
            "--define",
            f"_topdir {topdir}",
            "--define",
            "_unpackaged_files_terminate_build 0",
            "--define",
            "__os_install_post %{nil}",
            str(spec),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rpms = list((topdir / "RPMS" / "noarch").glob("*.rpm"))
    assert len(rpms) == 1, rpms
    return rpms[0]


def _rpm_paths(rpm_path: Path) -> set:
    result = subprocess.run(
        ["rpm", "-qlp", str(rpm_path)], capture_output=True, text=True, check=True
    )
    return set(result.stdout.splitlines())


@requires_rpmbuild
@xfail_rpm_below_419
def test_rpmbuild_accepts_manifest(tmp_path, cli):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = tmp_path / "src"
    src.mkdir()

    names = ["café", 'quo"te', "with space", "back\\slash"]
    for name in names:
        (src / name).write_text("x", encoding="utf-8")
        result = cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-m",
            "644",
            "-o",
            "root",
            "-g",
            "root",
            str(src / name),
            "/opt/t",
        )
        assert result.rc == 0, result.err

    manifest = tmp_path / "files.txt"
    assert cli("--db", str(db), "dbdump", "-f", "rpmspecfiles", str(manifest)).rc == 0

    rpm_path = _build_rpm(tmp_path, root, manifest, "pf-consumer-rpm")
    assert _rpm_paths(rpm_path) == {f"/opt/t/{name}" for name in names}


@requires_rpmbuild
@xfail_rpm_below_419
def test_rpmbuild_quoted_glob_is_literal(tmp_path, cli):
    # Guard: rpm's own quoted-string globbing matches a glob character
    # literally, so _rpm_quote never needs to escape one -- this passed
    # even before rpmspecfiles was rpm-quoted at all (json.dumps also left
    # glob characters alone).
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = tmp_path / "src"
    src.mkdir()

    recorded = ["star*", "q?", "br[x]"]
    siblings = ["starfish", "qx", "brx"]
    for name in recorded:
        (src / name).write_text("x", encoding="utf-8")
        result = cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-m",
            "644",
            str(src / name),
            "/opt/g",
        )
        assert result.rc == 0, result.err
    for name in siblings:
        (src / name).write_text("x", encoding="utf-8")
        result = cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-m",
            "644",
            "--noentry",
            str(src / name),
            "/opt/g",
        )
        assert result.rc == 0, result.err

    manifest = tmp_path / "files.txt"
    assert cli("--db", str(db), "dbdump", "-f", "rpmspecfiles", str(manifest)).rc == 0

    rpm_path = _build_rpm(tmp_path, root, manifest, "pf-consumer-glob")
    assert _rpm_paths(rpm_path) == {f"/opt/g/{name}" for name in recorded}


# --------------------------------------------------------------------------
# dh_install / dh_installdirs
# --------------------------------------------------------------------------


def _write_debian_control_and_changelog(debian_dir: Path, pkg: str) -> None:
    (debian_dir / "control").write_text(
        textwrap.dedent(f"""\
            Source: {pkg}
            Section: utils
            Priority: optional
            Maintainer: pkgforge tests <noreply@example.invalid>
            Build-Depends: debhelper-compat (= 13)

            Package: {pkg}
            Architecture: all
            Description: pkgforge consumer test package
             Built only to exercise dh_install/dh_installdirs.
            """),
        encoding="utf-8",
    )
    (debian_dir / "changelog").write_text(
        textwrap.dedent(f"""\
            {pkg} (1.0) unstable; urgency=medium

              * Test package.

             -- pkgforge tests <noreply@example.invalid>  Mon, 01 Jan 2026 00:00:00 +0000
            """),
        encoding="utf-8",
    )


@requires_dh_install
def test_dh_install_ships_exact_files(tmp_path, cli):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = tmp_path / "src"
    src.mkdir()
    _skip_if_fs_rejects_non_utf8(src)

    pkg = "pf-consumer-deb"
    pkgroot = tmp_path / "pkg"
    debian_dir = pkgroot / "debian"
    debian_dir.mkdir(parents=True)
    _write_debian_control_and_changelog(debian_dir, pkg)

    destdir = "usr/share/t"
    # Step 5's escaping cases (a representative subset: every character class
    # dh_install treats specially), plus siblings staged but not recorded.
    recorded = [
        "with space",
        "star*",
        "q?",
        "br[x]",
        "brace{a,b}",
        "back\\slash",
    ]
    siblings = ["starfish", "qx", "brx", "bracea"]

    for name in recorded:
        (src / name).write_text("x", encoding="utf-8")
        result = cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-m",
            "644",
            str(src / name),
            f"/{destdir}",
        )
        assert result.rc == 0, result.err
    for name in siblings:
        (src / name).write_text("x", encoding="utf-8")
        result = cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-m",
            "644",
            "--noentry",
            str(src / name),
            f"/{destdir}",
        )
        assert result.rc == 0, result.err

    # A non-UTF-8 name (os.fsdecode of a raw Latin-1 byte, surrogate-escaped).
    nonutf8_name = os.fsdecode(b"caf\xe9")
    (src / nonutf8_name).write_bytes(b"x")
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-m",
        "644",
        str(src / nonutf8_name),
        f"/{destdir}",
    )
    assert result.rc == 0, result.err

    # An empty directory recorded with install -d, which only the debian
    # format's dirs artifact (not install) covers.
    emptysrc = tmp_path / "emptysrc"
    emptysrc.mkdir()
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-d",
        "-m",
        "750",
        str(emptysrc),
        "/var/lib/tool",
    )
    assert result.rc == 0, result.err

    assert cli("--db", str(db), "dbdump", "-f", "debian", str(debian_dir)).rc == 0

    dh_install_result = subprocess.run(
        ["dh_install", f"--sourcedir={root}"],
        cwd=pkgroot,
        capture_output=True,
        text=True,
    )
    assert dh_install_result.returncode == 0, (
        dh_install_result.stdout + dh_install_result.stderr
    )
    dh_installdirs_result = subprocess.run(
        ["dh_installdirs"], cwd=pkgroot, capture_output=True, text=True
    )
    assert dh_installdirs_result.returncode == 0, (
        dh_installdirs_result.stdout + dh_installdirs_result.stderr
    )

    shipped_root = pkgroot / "debian" / pkg
    shipped_files = set()
    for dirpath, _dirs, filenames in os.walk(shipped_root):
        for filename in filenames:
            rel = os.path.relpath(os.path.join(dirpath, filename), shipped_root)
            shipped_files.add(Path(rel).as_posix())

    expected = {f"{destdir}/{name}" for name in recorded}
    expected.add(f"{destdir}/{nonutf8_name}")
    assert shipped_files == expected
    assert (shipped_root / "var" / "lib" / "tool").is_dir()
    for name in siblings:
        assert not (shipped_root / destdir / name).exists()
