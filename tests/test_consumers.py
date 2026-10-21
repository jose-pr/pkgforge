"""Feeds dbdump's output to the real packaging consumers it targets:
``rpmbuild`` for ``rpmspecfiles``, ``dh_install``/``dh_installdirs`` for
``debian``. Skipped outright when the tool isn't on ``PATH``; not collected
by the plain unit-test run, but not excluded from it either -- these tests
are cheap when skipped.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import textwrap
import typing
from pathlib import Path

import pytest

from conftest import skip_if_fs_rejects_non_utf8
from pkgforge import Project

try:  # Unix-only; this whole module targets Linux consumer tools.
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-Unix
    grp = pwd = None

RPMBUILD = shutil.which("rpmbuild")
RPM = shutil.which("rpm")
DH_INSTALL = shutil.which("dh_install")
DH_INSTALLDIRS = shutil.which("dh_installdirs")
DH_FIXPERMS = shutil.which("dh_fixperms")
DH_GENCONTROL = shutil.which("dh_gencontrol")
DH_BUILDDEB = shutil.which("dh_builddeb")
DPKG_DEB = shutil.which("dpkg-deb")
FAKEROOT = shutil.which("fakeroot")


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


def _has_daemon_user_and_adm_group() -> bool:
    if pwd is None or grp is None:
        return False
    try:
        pwd.getpwnam("daemon")
        grp.getgrnam("adm")
    except KeyError:
        return False
    return True


requires_deb_build = pytest.mark.skipif(
    not (
        DH_INSTALL
        and DH_INSTALLDIRS
        and DH_FIXPERMS
        and DH_GENCONTROL
        and DH_BUILDDEB
        and DPKG_DEB
        and FAKEROOT
        and _has_daemon_user_and_adm_group()
    ),
    reason="debhelper/fakeroot deb-build toolchain (or the daemon user / "
    "adm group) not available",
)

#: The rpm dump format this host's own rpm can actually use: below 4.19 a
#: quoted `%files -f` name is macro-expanded twice and an unquoted name's
#: glob characters are matched by rpm's own globbing, so `rpmspecfiles`'
#: quoting (targets 4.19+) is not safe there -- `rpmspecfiles-pre419` is.
_HOST_FORMAT = "rpmspecfiles-pre419" if _RPM_BELOW_419 else "rpmspecfiles"
#: The spec format this host's own rpm can build, for the same reason.
_HOST_SPEC = "rpmspec-pre419" if _RPM_BELOW_419 else "rpmspec"
#: The format `_HOST_FORMAT` is not -- used by the cross-format guard below.
_OTHER_FORMAT = {
    "rpmspecfiles": "rpmspecfiles-pre419",
    "rpmspecfiles-pre419": "rpmspecfiles",
}
#: Character classes each format is measured `exact` for, on every leg it
#: targets (from `tests/rpm_probe.py --analyze`, measured 2026-09-29 on rpm
#: 4.14.3/4.16.1/4.18.2/6.0.2): a class not listed here is refused by
#: `dbdump` for that format rather than risking the wrong file.
_SUPPORTED = {
    "rpmspecfiles": frozenset(
        {
            "plain",
            "space",
            "utf8",
            "dquote",
            "backslash",
            "star",
            "qmark",
            "bracket",
            "brace",
        }
    ),
    "rpmspecfiles-pre419": frozenset({"plain", "utf8", "dquote", "backslash"}),
}


# --------------------------------------------------------------------------
# rpmbuild
# --------------------------------------------------------------------------


def _build_rpm(
    tmp_path: Path,
    root: Path,
    manifest: Path,
    name: str,
    log: typing.Optional[list] = None,
) -> Path:
    """Build a minimal noarch RPM whose payload is copied straight from
    ``root`` and whose file list comes from ``manifest`` (a rpmspecfiles
    dump). Returns the built .rpm's path.

    ``%install`` copies ``root`` into ``%{buildroot}`` itself (rather than
    staging directly into ``%{buildroot}``) because rpm's own
    ``%__spec_install_pre`` wipes ``%{buildroot}`` before ``%install`` runs.
    ``_unpackaged_files_terminate_build 0`` tolerates a staged file that
    ``manifest`` doesn't list (a ``--noentry`` sibling); ``AutoReqProv: no``
    and ``__os_install_post %{nil}`` skip dependency/brp scripts that would
    otherwise choke on an odd file name. When ``log`` is a list, rpmbuild's
    combined output is appended to it.
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
    if log is not None:
        log.append(result.stdout + result.stderr)
    rpms = list((topdir / "RPMS" / "noarch").glob("*.rpm"))
    assert len(rpms) == 1, rpms
    return rpms[0]


def _try_build_rpm(tmp_path: Path, root: Path, manifest: Path, name: str, *, cwd=None):
    """Like :func:`_build_rpm`, but tolerates a failed build (returns
    ``None`` instead of asserting ``rc == 0``): used where a build failure
    is itself an accepted outcome, not a test failure -- a format used with
    the wrong rpm may fail the build, but must never package the wrong
    file. ``cwd``, when given, is where the build actually runs (so a shell
    macro's side effect, if any escaped, lands somewhere the test
    controls)."""
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
        cwd=str(cwd) if cwd is not None else None,
    )
    if result.returncode != 0:
        return None
    rpms = list((topdir / "RPMS" / "noarch").glob("*.rpm"))
    if len(rpms) != 1:
        return None
    return rpms[0]


def _rpm_paths(rpm_path: Path) -> set:
    result = subprocess.run(
        ["rpm", "-qlp", str(rpm_path)], capture_output=True, text=True, check=True
    )
    return set(result.stdout.splitlines())


#: name -> character class, for the mixed-class cases the tests below stage.
_PLAIN_CLASS_NAMES = {
    "plain": "plain",
    "café": "utf8",
    'quo"te': "dquote",
    "with space": "space",
    "back\\slash": "backslash",
}
#: name -> (class, siblings that must never be swept in by an unescaped glob).
_GLOB_CLASS_NAMES = {
    "star*": ("star", ("starfish",)),
    "q?": ("qmark", ("qx",)),
    "br[x]": ("bracket", ("brx",)),
    "brace{a,b}": ("brace", ("bracea", "braceb")),
}


@requires_rpmbuild
def test_rpmbuild_accepts_manifest(tmp_path, cli):
    # For each name, dbdump either renders it into a manifest that rpmbuild
    # packages exactly (a class _HOST_FORMAT supports), or refuses it
    # outright (a class it doesn't) -- never a skip either way.
    root = tmp_path / "root"
    src = tmp_path / "src"
    src.mkdir()
    supported = _SUPPORTED[_HOST_FORMAT]

    for name, cls in _PLAIN_CLASS_NAMES.items():
        db = tmp_path / f"files-{cls}.jsonl"
        broot = root / cls
        (src / name).write_text("x", encoding="utf-8")
        result = cli(
            "--db",
            str(db),
            "--buildroot",
            str(broot),
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

        manifest = tmp_path / f"files-{cls}.txt"
        dump = cli("--db", str(db), "dbdump", "-f", _HOST_FORMAT, str(manifest))
        if cls in supported:
            assert dump.rc == 0, dump.err
            rpm_path = _build_rpm(
                tmp_path / f"build-{cls}", broot, manifest, f"pf-consumer-rpm-{cls}"
            )
            assert _rpm_paths(rpm_path) == {f"/opt/t/{name}"}
        else:
            assert dump.rc == 1, dump.err
            assert not manifest.exists()


@requires_rpmbuild
def test_rpmbuild_quoted_glob_is_literal(tmp_path, cli):
    # Guard: rpm's own quoted-string globbing matches a glob character
    # literally, so a supporting format never needs to escape one; a class
    # the format doesn't support must be refused outright instead of
    # overmatching a sibling -- this passed even before rpmspecfiles was
    # rpm-quoted at all (json.dumps also left glob characters alone).
    root = tmp_path / "root"
    src = tmp_path / "src"
    src.mkdir()
    supported = _SUPPORTED[_HOST_FORMAT]

    for name, (cls, siblings) in _GLOB_CLASS_NAMES.items():
        db = tmp_path / f"files-{cls}.jsonl"
        broot = root / cls
        (src / name).write_text("x", encoding="utf-8")
        result = cli(
            "--db",
            str(db),
            "--buildroot",
            str(broot),
            "install",
            "-p",
            "-m",
            "644",
            str(src / name),
            "/opt/g",
        )
        assert result.rc == 0, result.err
        for sibling in siblings:
            (src / sibling).write_text("x", encoding="utf-8")
            result = cli(
                "--db",
                str(db),
                "--buildroot",
                str(broot),
                "install",
                "-p",
                "-m",
                "644",
                "--noentry",
                str(src / sibling),
                "/opt/g",
            )
            assert result.rc == 0, result.err

        manifest = tmp_path / f"files-{cls}.txt"
        dump = cli("--db", str(db), "dbdump", "-f", _HOST_FORMAT, str(manifest))
        if cls in supported:
            assert dump.rc == 0, dump.err
            rpm_path = _build_rpm(
                tmp_path / f"build-{cls}", broot, manifest, f"pf-consumer-glob-{cls}"
            )
            assert _rpm_paths(rpm_path) == {f"/opt/g/{name}"}
        else:
            assert dump.rc == 1, dump.err
            assert not manifest.exists()


@requires_rpmbuild
def test_rpmbuild_packages_pinned_attrs_config_and_symlink(tmp_path, cli):
    # A pinned %attr, a %config(noreplace) prefix, a directory and a symlink
    # package as recorded, and rpm has nothing to warn about for the symlink.
    root = tmp_path / "root"
    src = tmp_path / "conf"
    src.write_text("x", encoding="utf-8")
    db = tmp_path / "files.jsonl"
    result = cli(
        "--db", str(db), "--buildroot", str(root), "install", "-p",
        "-m", "640", "-o", "root", "-g", "adm", "-k", "config-noreplace",
        str(src), "/etc/t",
    )  # fmt: skip
    assert result.rc == 0, result.err
    (root / "etc" / "t" / "link").symlink_to("conf")
    with db.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "path": "/etc/t/link",
                    "mode": "777",
                    "owner": "root",
                    "group": "root",
                    "type": "symlink",
                    "kind": "file",
                    "caps": "-",
                    "package": "-",
                    "meta": {},
                }
            )
            + "\n"
        )

    manifest = tmp_path / "files.txt"
    dump = cli("--db", str(db), "dbdump", "-f", _HOST_FORMAT, str(manifest))
    assert dump.rc == 0, dump.err
    assert "%attr(-,root,root) " in manifest.read_text(encoding="utf-8")
    log: list = []
    rpm_path = _build_rpm(tmp_path / "build", root, manifest, "pf-attrs", log)
    assert "not applicable" not in log[0]
    listing = subprocess.run(
        [
            "rpm", "-qp", "--qf",
            "[%{FILENAMES} %{FILEMODES:octal} %{FILEUSERNAME} %{FILEGROUPNAME} "
            "%{FILEFLAGS:fflags}\n]",
            str(rpm_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()  # fmt: skip
    by_path = {line.split()[0]: line.split()[1:] for line in listing}
    assert by_path["/etc/t/conf"][:3] == ["100640", "root", "adm"]
    assert "c" in by_path["/etc/t/conf"][3] and "n" in by_path["/etc/t/conf"][3]
    assert by_path["/etc/t/link"][0].startswith("120")


@requires_rpmbuild
def test_rpmbuild_other_format_never_silently_wrong(tmp_path, cli):
    # rpmspecfiles-pre419 used with rpm 4.19+ may fail the build, but must
    # never silently package a sibling (overmatch) or the wrong file. The
    # reverse does not hold: rpmspecfiles on rpm below 4.19 is measured to
    # overmatch glob names, which is why rpmspecfiles-pre419 exists.
    if _RPM_BELOW_419:
        pytest.skip("rpmspecfiles on rpm < 4.19 overmatches glob names by design")
    other_format = _OTHER_FORMAT[_HOST_FORMAT]
    other_supported = _SUPPORTED[other_format]

    root = tmp_path / "root"
    src = tmp_path / "src"
    src.mkdir()

    cases = {}
    cases.update((n, (c, ())) for n, c in _PLAIN_CLASS_NAMES.items())
    cases.update(_GLOB_CLASS_NAMES)

    checked = 0
    for name, (cls, siblings) in cases.items():
        if cls not in other_supported:
            continue
        checked += 1
        db = tmp_path / f"other-{cls}.jsonl"
        broot = root / cls
        (src / name).write_text("x", encoding="utf-8")
        result = cli(
            "--db",
            str(db),
            "--buildroot",
            str(broot),
            "install",
            "-p",
            "-m",
            "644",
            str(src / name),
            "/opt/o",
        )
        assert result.rc == 0, result.err
        for sibling in siblings:
            (src / sibling).write_text("x", encoding="utf-8")
            result = cli(
                "--db",
                str(db),
                "--buildroot",
                str(broot),
                "install",
                "-p",
                "-m",
                "644",
                "--noentry",
                str(src / sibling),
                "/opt/o",
            )
            assert result.rc == 0, result.err

        manifest = tmp_path / f"other-{cls}.txt"
        dump = cli("--db", str(db), "dbdump", "-f", other_format, str(manifest))
        # The other format must always be a known one, whichever rpm this
        # host has: an "unknown format" here (exit 2) would mean the
        # cross-format format itself doesn't exist.
        assert dump.rc != 2, dump.err
        if dump.rc != 0:
            continue  # refused, or rpmbuild will fail below: both accepted
        rpm_path = _try_build_rpm(
            tmp_path / f"other-build-{cls}", broot, manifest, f"pfother{cls}"
        )
        if rpm_path is None:
            continue  # a failed build is an accepted outcome here
        assert _rpm_paths(rpm_path) == {f"/opt/o/{name}"}
    assert checked  # the other format must support at least one class here


@requires_rpmbuild
def test_rpmbuild_percent_never_runs(tmp_path, cli):
    # A staged "%(...)" name must never actually run its shell command,
    # for either format this host can select between.
    root = tmp_path / "root"
    src = tmp_path / "src"
    src.mkdir()
    name = "%(touch pfmark)"
    (src / name).write_text("x", encoding="utf-8")

    formats = [_HOST_FORMAT, _OTHER_FORMAT[_HOST_FORMAT]]
    for fmt in formats:
        db = tmp_path / f"pct-{fmt}.jsonl"
        broot = root / fmt
        result = cli(
            "--db",
            str(db),
            "--buildroot",
            str(broot),
            "install",
            "-p",
            "-m",
            "644",
            str(src / name),
            "/opt/p",
        )
        assert result.rc == 0, result.err

        manifest = tmp_path / f"pct-{fmt}.txt"
        dump = cli("--db", str(db), "dbdump", "-f", fmt, str(manifest))
        assert dump.rc != 2, dump.err  # both formats are known
        if dump.rc != 0:
            continue  # refused: an accepted outcome

        build_cwd = tmp_path / f"pct-build-{fmt}"
        build_cwd.mkdir()
        _try_build_rpm(build_cwd, broot, manifest, f"pfpct{fmt}", cwd=build_cwd)
        assert not (build_cwd / "pfmark").exists()


# --------------------------------------------------------------------------
# dh_install / dh_installdirs
# --------------------------------------------------------------------------


def _write_debian_control_and_changelog(
    debian_dir: Path, pkg: str, rules_requires_root: bool = False
) -> None:
    rrr = "Rules-Requires-Root: binary-targets\n" if rules_requires_root else ""
    (debian_dir / "control").write_text(
        textwrap.dedent(f"""\
            Source: {pkg}
            Section: utils
            Priority: optional
            Maintainer: pkgforge tests <noreply@example.invalid>
            Build-Depends: debhelper-compat (= 13)
            {rrr}
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


def _package_cfg(directory: Path, pkg: str) -> str:
    """Write a ``pkgforge.cfg`` naming the package ``pkg`` in ``directory`` (the
    debian format names its files after the package); returns its path."""
    cfg = directory / "pkgforge.cfg"
    cfg.write_text(f'[package]\nname = "{pkg}"\n', encoding="utf-8")
    return str(cfg)


def _stage_dh_entry(
    cli,
    db: Path,
    root: Path,
    src: Path,
    dest: str,
    *,
    mode: str = "-",
    owner: str = "-",
    group: str = "-",
    is_dir: bool = False,
    noentry: bool = False,
):
    """Stage ``src`` at ``dest`` via ``install``, asserting success.

    Shared by every consumer test below that builds up a file DB entry by
    entry before dumping it to ``debian``.
    """
    args = ["--db", str(db), "--buildroot", str(root), "install", "-p"]
    if is_dir:
        args.append("-d")
    args += ["-m", mode, "-o", owner, "-g", group]
    if noentry:
        args.append("--noentry")
    args += [str(src), dest]
    result = cli(*args)
    assert result.rc == 0, result.err
    return result


@requires_dh_install
def test_dh_install_ships_exact_files(tmp_path, cli):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = tmp_path / "src"
    src.mkdir()
    skip_if_fs_rejects_non_utf8(src)

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
        _stage_dh_entry(cli, db, root, src / name, f"/{destdir}", mode="644")
    for name in siblings:
        (src / name).write_text("x", encoding="utf-8")
        _stage_dh_entry(
            cli, db, root, src / name, f"/{destdir}", mode="644", noentry=True
        )

    # A non-UTF-8 name (os.fsdecode of a raw Latin-1 byte, surrogate-escaped).
    nonutf8_name = os.fsdecode(b"caf\xe9")
    (src / nonutf8_name).write_bytes(b"x")
    _stage_dh_entry(cli, db, root, src / nonutf8_name, f"/{destdir}", mode="644")

    # An empty directory recorded with install -d, which only the debian
    # format's dirs artifact (not install) covers.
    emptysrc = tmp_path / "emptysrc"
    emptysrc.mkdir()
    _stage_dh_entry(cli, db, root, emptysrc, "/var/lib/tool", mode="750", is_dir=True)

    cfg = _package_cfg(tmp_path, pkg)
    dump = cli(
        "--config", cfg, "--db", str(db), "dbdump", "-f", "debian", str(debian_dir)
    )
    assert dump.rc == 0

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


@requires_deb_build
def test_dh_install_fixperms_builds_deb(tmp_path, cli):
    """Build a real ``.deb`` under ``fakeroot``, applying the generated
    ``fixperms`` script from ``override_dh_fixperms`` after ``dh_fixperms``,
    and read the pins back from the built package's own filesystem tarball.

    ``dh_fixperms`` first normalizes every mode/owner/group to its own
    defaults (root:root, and it strips setuid/setgid); running ``fixperms``
    afterward is what proves the pins -- ``pf-tool``'s ``04755`` in
    particular can only survive if ``fixperms`` ran *after* dh_fixperms already
    stripped it.
    """
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = tmp_path / "src"
    src.mkdir()
    skip_if_fs_rejects_non_utf8(src)

    pkg = "pf-consumer-fixperms"
    pkgroot = tmp_path / "pkg"
    debian_dir = pkgroot / "debian"
    debian_dir.mkdir(parents=True)
    _write_debian_control_and_changelog(debian_dir, pkg, rules_requires_root=True)

    destdir = "usr/share/t"

    (src / "pf-tool").write_text("x", encoding="utf-8")
    _stage_dh_entry(
        cli,
        db,
        root,
        src / "pf-tool",
        "/usr/bin",
        mode="4755",
        owner="root",
        group="root",
    )

    (src / "with space").write_text("x", encoding="utf-8")
    _stage_dh_entry(
        cli,
        db,
        root,
        src / "with space",
        f"/{destdir}",
        mode="600",
        owner="daemon",
        group="adm",
    )

    (src / "it's").write_text("x", encoding="utf-8")
    _stage_dh_entry(cli, db, root, src / "it's", f"/{destdir}", group="adm")

    (src / "d$x").write_text("x", encoding="utf-8")
    _stage_dh_entry(cli, db, root, src / "d$x", f"/{destdir}", owner="daemon")

    (src / "star*").write_text("x", encoding="utf-8")
    _stage_dh_entry(cli, db, root, src / "star*", f"/{destdir}", mode="640")

    (src / "back\\slash").write_text("x", encoding="utf-8")
    _stage_dh_entry(cli, db, root, src / "back\\slash", f"/{destdir}", mode="604")

    nonutf8_name = os.fsdecode(b"caf\xe9")
    (src / nonutf8_name).write_bytes(b"x")
    _stage_dh_entry(cli, db, root, src / nonutf8_name, f"/{destdir}", mode="640")

    emptysrc = tmp_path / "emptysrc"
    emptysrc.mkdir()
    _stage_dh_entry(
        cli,
        db,
        root,
        emptysrc,
        "/var/lib/tool",
        mode="750",
        owner="daemon",
        group="adm",
        is_dir=True,
    )

    cfg = _package_cfg(tmp_path, pkg)
    dump = cli(
        "--config", cfg, "--db", str(db), "dbdump", "-f", "debian", str(debian_dir)
    )
    assert dump.rc == 0

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

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    build_script = (
        "dh_fixperms && "
        "sh debian/fixperms debian && "
        "dh_gencontrol && "
        f"dh_builddeb --destdir={out_dir}"
    )
    build_result = subprocess.run(
        ["fakeroot", "sh", "-c", build_script],
        cwd=pkgroot,
        capture_output=True,
        text=True,
        # dh_builddeb is invoked directly here, not through dpkg-buildpackage,
        # so Rules-Requires-Root: binary-targets in debian/control is never
        # read into DEB_RULES_REQUIRES_ROOT -- debhelper then treats it as
        # "no" and dh_builddeb runs dpkg-deb --root-owner-group, which
        # normalizes every owner/group to root:root regardless of what
        # fixperms just chowned. Export it explicitly, mirroring what
        # dpkg-buildpackage itself would export from the control file.
        env={**os.environ, "DEB_RULES_REQUIRES_ROOT": "binary-targets"},
    )
    assert build_result.returncode == 0, build_result.stdout + build_result.stderr

    debs = list(out_dir.glob("*.deb"))
    assert len(debs) == 1, debs
    fsys_tarfile = subprocess.run(
        ["dpkg-deb", "--fsys-tarfile", str(debs[0])],
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(fsys_tarfile)) as tf:
        members = {
            (m.name[2:] if m.name.startswith("./") else m.name): m
            for m in tf.getmembers()
        }

    def _mode(name: str) -> int:
        return stat.S_IMODE(members[name].mode)

    tool = members["usr/bin/pf-tool"]
    assert _mode("usr/bin/pf-tool") == 0o4755, "fixperms must run after dh_fixperms"
    assert (tool.uname, tool.gname) == ("root", "root")

    space = members[f"{destdir}/with space"]
    assert _mode(f"{destdir}/with space") == 0o600
    assert (space.uname, space.gname) == ("daemon", "adm")

    # Unpinned fields keep dh_fixperms' own default (root:root, 644 under
    # usr/share).
    apostrophe = members[f"{destdir}/it's"]
    assert (apostrophe.uname, apostrophe.gname) == ("root", "adm")
    assert _mode(f"{destdir}/it's") == 0o644

    dollar = members[f"{destdir}/d$x"]
    assert (dollar.uname, dollar.gname) == ("daemon", "root")
    assert _mode(f"{destdir}/d$x") == 0o644

    star = members[f"{destdir}/star*"]
    assert (star.uname, star.gname) == ("root", "root")
    assert _mode(f"{destdir}/star*") == 0o640

    assert _mode(f"{destdir}/back\\slash") == 0o604
    assert _mode(f"{destdir}/{nonutf8_name}") == 0o640

    # A directory install without -T lands at <dest>/<source name>.
    emptydir = members["var/lib/tool/emptysrc"]
    assert emptydir.isdir()
    assert (emptydir.uname, emptydir.gname) == ("daemon", "adm")
    assert _mode("var/lib/tool/emptysrc") == 0o750


# --------------------------------------------------------------------------
# dh_install: the whole `dh` sequence, driven by the recipes in formats.md
# --------------------------------------------------------------------------

FORMATS_MD = Path(__file__).resolve().parents[1] / "docs" / "guide" / "formats.md"


def _missing_dh_sequence_tool():
    """The first tool the full-sequence deb builds need that is not on
    PATH, or ``None``."""
    for tool in ("dh", "dpkg-buildpackage", "dpkg-deb", "fakeroot"):
        if shutil.which(tool) is None:
            return tool
    return None


_MISSING_DH_TOOL = _missing_dh_sequence_tool()
requires_dh_sequence = pytest.mark.skipif(
    _MISSING_DH_TOOL is not None,
    reason=f"{_MISSING_DH_TOOL} not installed",
)


def _doc_rules_recipe(marker: str) -> str:
    """The ``debian/rules`` recipe in formats.md whose text holds ``marker``."""
    text = FORMATS_MD.read_text(encoding="utf-8")
    for body in re.findall(r"```make\n(.*?)```", text, re.DOTALL):
        if marker in body:
            return body
    raise AssertionError(f"no make recipe containing {marker!r} in {FORMATS_MD}")


#: What a user's stage.sh holds: one ``pkgforge install`` per artifact. A
#: man page (renamed to .gz by dh_compress when pinned), a doc over 4 KiB
#: (compressed too), a setuid helper, a root:adm config, a recorded empty
#: state directory, and a directory under /usr/local (removed by
#: dh_usrlocal).
_STAGE_SH = """\
set -eu
pf() { pkgforge -q "$@"; }
pf initdb
E="$(mktemp -d)"
pf install -D -d -m 750 -o root -g adm "$E" /var/lib/PKG
pf install -D -d -m 755 -o root -g root "$E" /usr/local/lib/PKG
rmdir "$E"
pf install -p -m 664 -o root -g adm src/pf.1 /usr/share/man/man1
pf install -p -m 640 -o root -g adm src/BIG.txt /usr/share/doc/PKG
pf install -p -m 4755 -o root -g root src/helper /usr/bin
pf install -p -m 640 -o root -g adm src/pf.conf /etc/PKG
"""


def _build_deb_with_recipe(tmp_path: Path, recipe: str, pkg: str):
    """Build the package with ``dpkg-buildpackage`` from ``recipe`` (a
    ``debian/rules`` copied out of formats.md). Returns the build result and
    the built ``.deb``'s tar members keyed by path without the leading
    ``./``."""
    work = tmp_path / "work"
    pkgroot = work / pkg
    debian_dir = pkgroot / "debian"
    (debian_dir / "source").mkdir(parents=True)
    (pkgroot / "src").mkdir()
    _write_debian_control_and_changelog(debian_dir, pkg, rules_requires_root=True)
    (debian_dir / "source" / "format").write_text("3.0 (native)\n", encoding="utf-8")

    (pkgroot / "src" / "pf.1").write_text(".TH PF 1\n", encoding="utf-8")
    (pkgroot / "src" / "BIG.txt").write_text("x" * 5000, encoding="utf-8")
    (pkgroot / "src" / "helper").write_text("#!/bin/sh\n", encoding="utf-8")
    (pkgroot / "src" / "pf.conf").write_text("k = v\n", encoding="utf-8")
    (pkgroot / "stage.sh").write_text(_STAGE_SH.replace("PKG", pkg), encoding="utf-8")
    _package_cfg(pkgroot, pkg)

    # dh_strip_nondeterminism is a separate package some hosts lack; the
    # recipe has nothing to strip.
    rules = recipe.replace("<pkg>", pkg) + "\noverride_dh_strip_nondeterminism:\n"
    (debian_dir / "rules").write_text(rules, encoding="utf-8")
    (debian_dir / "rules").chmod(0o755)

    return _run_dpkg_buildpackage(tmp_path, pkgroot)


def _pkgforge_on_path(tmp_path: Path) -> dict:
    """An environment whose PATH starts with a ``pkgforge`` wrapper around
    this interpreter: the recipes call ``pkgforge`` by name, as a user would."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = bindir / "pkgforge"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -m pkgforge "$@"\n', encoding="utf-8"
    )
    wrapper.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    # An inherited PKGFORGE_CONFIG would redirect the recipes' own --config.
    env.pop("PKGFORGE_CONFIG", None)
    return env


def _build_debs(tmp_path: Path, pkgroot: Path):
    """``dpkg-buildpackage -b`` in ``pkgroot``; returns the build result and the
    ``.deb`` files built next to it, by file name."""
    result = subprocess.run(
        ["dpkg-buildpackage", "-b", "-us", "-uc", "-d"],
        cwd=pkgroot,
        capture_output=True,
        text=True,
        env=_pkgforge_on_path(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result, {deb.name: deb for deb in pkgroot.parent.glob("*.deb")}


def _run_dpkg_buildpackage(tmp_path: Path, pkgroot: Path):
    """``dpkg-buildpackage -b`` in ``pkgroot``; returns the build result and
    the built ``.deb``'s tar members keyed by path without the leading ``./``."""
    result, built = _build_debs(tmp_path, pkgroot)
    debs = list(built.values())
    assert len(debs) == 1, debs
    fsys_tarfile = subprocess.run(
        ["dpkg-deb", "--fsys-tarfile", str(debs[0])], capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(fsys_tarfile)) as tf:
        members = {
            (m.name[2:] if m.name.startswith("./") else m.name).rstrip("/"): m
            for m in tf.getmembers()
        }
    return result, members


def _control_member(deb: Path, name: str) -> str:
    """The text of the maintainer script or control file ``name`` in ``deb``."""
    control = subprocess.run(
        ["dpkg-deb", "--ctrl-tarfile", str(deb)], capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(control)) as tf:
        member = tf.extractfile(f"./{name}")
        assert member is not None, name
        return member.read().decode("utf-8")


def _assert_full_sequence_pins(result, members, pkg: str):
    def pinned(name: str, mode: int, owner: str, group: str):
        member = members[name]
        assert stat.S_IMODE(member.mode) == mode, name
        assert (member.uname, member.gname) == (owner, group), name

    # dh_compress renamed these; the pins followed.
    pinned("usr/share/man/man1/pf.1.gz", 0o664, "root", "adm")
    pinned(f"usr/share/doc/{pkg}/BIG.txt.gz", 0o640, "root", "adm")
    pinned("usr/bin/helper", 0o4755, "root", "root")
    pinned(f"etc/{pkg}/pf.conf", 0o640, "root", "adm")
    assert members[f"var/lib/{pkg}"].isdir()
    pinned(f"var/lib/{pkg}", 0o750, "root", "adm")

    # dh_usrlocal removed the directory; fixperms reported it and went on.
    assert not [name for name in members if name.startswith("usr/local/lib/")]
    assert "skipping missing" in result.stdout + result.stderr
    assert f"/usr/local/lib/{pkg}" in result.stdout + result.stderr


@requires_dh_sequence
def test_dh_install_full_sequence_separate_root(tmp_path):
    """The separate-stage-root recipe: stage in override_dh_auto_build and
    install with ``dh_install --sourcedir``."""
    recipe = _doc_rules_recipe("--sourcedir")
    result, members = _build_deb_with_recipe(tmp_path, recipe, "pf-seq-root")
    _assert_full_sequence_pins(result, members, "pf-seq-root")


@requires_dh_sequence
def test_dh_install_full_sequence_debian_tmp(tmp_path):
    """The debian/tmp recipe: stage in override_dh_auto_install and call
    ``dh_installdirs`` again once ``dirs`` exists."""
    recipe = _doc_rules_recipe("\tdh_installdirs\n")
    result, members = _build_deb_with_recipe(tmp_path, recipe, "pf-seq-tmp")
    _assert_full_sequence_pins(result, members, "pf-seq-tmp")


# --------------------------------------------------------------------------
# entry kinds and file capabilities
# --------------------------------------------------------------------------


def _stage_kinds_db(cli, root: Path, db: Path, src: Path) -> None:
    """Record one entry of each kind, a ghost directory and a file with
    capabilities: ``/etc/kc`` and ``/opt/kc`` configs (only the second needs a
    ``conffiles`` line), a doc, a license, a ghost file and directory."""
    src.mkdir()
    (src / "f").write_text("x", encoding="utf-8")
    base = ["--db", str(db), "--buildroot", str(root)]
    rows = (
        ("config-noreplace", "/etc/kc", ()),
        ("config", "/opt/kc", ()),
        ("doc", "/usr/share/doc/kc", ()),
        ("license", "/usr/share/licenses/kc", ()),
        ("ghost", "/var/log", ()),
        ("file", "/usr/bin", ("--caps", "cap_net_raw=ep")),
    )
    for kind, dest, extra in rows:
        # Distinct file names, so every row is its own path.
        name = {"/var/log": "kc.log", "/usr/bin": "kc"}.get(dest, "kc.conf")
        target = src / name
        target.write_text("x", encoding="utf-8")
        result = cli(
            *base, "install", "-p", "-m", "644", "-k", kind, *extra, str(target), dest
        )
        assert result.rc == 0, result.err
    result = cli(*base, "mkdir", "-p", "-k", "ghost", "/run/kc")
    assert result.rc == 0, result.err


@requires_rpmbuild
def test_rpmbuild_kinds_and_caps(tmp_path, cli):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    _stage_kinds_db(cli, root, db, tmp_path / "src")
    manifest = tmp_path / "files.txt"
    assert cli("--db", str(db), "dbdump", "-f", _HOST_FORMAT, str(manifest)).rc == 0
    rpm_path = _build_rpm(tmp_path / "build", root, manifest, "pf-kinds")
    listing = subprocess.run(
        [
            "rpm", "-qp", "--qf", "[%{FILENAMES}|%{FILEFLAGS}|%{FILECAPS}\\n]",
            str(rpm_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()  # fmt: skip
    rows = {path: (int(flags), caps) for path, flags, caps in map(_split3, listing)}
    assert rows["/etc/kc/kc.conf"][0] == 17  # config(noreplace)
    assert rows["/opt/kc/kc.conf"][0] == 1  # config
    assert rows["/usr/share/doc/kc/kc.conf"][0] == 2  # doc
    assert rows["/usr/share/licenses/kc/kc.conf"][0] & 128  # license
    assert rows["/var/log/kc.log"][0] == 64  # ghost
    assert rows["/run/kc"][0] == 64  # ghost directory
    assert rows["/usr/bin/kc"] == (0, "cap_net_raw=ep")
    assert all(caps == "" for path, (_f, caps) in rows.items() if path != "/usr/bin/kc")


def _split3(row: str) -> typing.Tuple[str, str, str]:
    path, flags, caps = row.split("|")
    return path, flags, caps


@requires_dh_sequence
def test_dh_install_kinds_and_caps(tmp_path, cli):
    """Ghosts stay out of the payload (``dh_missing --fail-missing`` accepts
    the staged ghost file through ``not-installed``), a config outside ``/etc``
    is listed in ``conffiles`` once, and ``postinst`` sets the capability."""
    pkg = "pf-kinds"
    pkgroot = tmp_path / "work" / pkg
    debian_dir = pkgroot / "debian"
    (debian_dir / "source").mkdir(parents=True)
    _write_debian_control_and_changelog(debian_dir, pkg)
    (debian_dir / "source" / "format").write_text("3.0 (native)\n", encoding="utf-8")
    stage = tmp_path / "stage"
    # dh clean removes debian/tmp, so the staged tree is copied in at the
    # point a recipe would stage into it.
    (debian_dir / "rules").write_text(
        "#!/usr/bin/make -f\n"
        "%:\n"
        "\tdh $@\n"
        "\n"
        "override_dh_auto_install:\n"
        "\tmkdir -p debian/tmp\n"
        f"\tcp -a {stage}/. debian/tmp/\n"
        "\n"
        "override_dh_strip_nondeterminism:\n",
        encoding="utf-8",
    )
    (debian_dir / "rules").chmod(0o755)

    db = tmp_path / "files.jsonl"
    _stage_kinds_db(cli, stage, db, tmp_path / "src")
    cfg = _package_cfg(tmp_path, pkg)
    dump = cli(
        "--config", cfg, "--db", str(db), "dbdump", "-f", "debian", str(debian_dir)
    )
    assert dump.rc == 0

    _result, members = _run_dpkg_buildpackage(tmp_path, pkgroot)
    assert "etc/kc/kc.conf" in members and "opt/kc/kc.conf" in members
    assert "var/log/kc.log" not in members
    assert "run/kc" not in members

    deb = next((tmp_path / "work").glob("*.deb"))
    conffiles = _control_member(deb, "conffiles").split()
    assert sorted(conffiles) == ["/etc/kc/kc.conf", "/opt/kc/kc.conf"]
    postinst = _control_member(deb, "postinst")
    assert "#DEBHELPER#" not in postinst
    setcap = 'setcap cap_net_raw=ep "$DPKG_ROOT"/usr/bin/kc'
    assert setcap in postinst


# --------------------------------------------------------------------------
# examples/: the packages a user starts from must build
# --------------------------------------------------------------------------

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _example_source(dest: Path) -> Path:
    """``dest`` holding only what a user writes: examples/pkgforge.cfg and src/."""
    dest.mkdir(parents=True)
    shutil.copy(EXAMPLES / "pkgforge.cfg", dest / "pkgforge.cfg")
    shutil.copytree(EXAMPLES / "src", dest / "src")
    return dest


def _generate(
    tree: Path,
    fmt: str,
    output: Path,
    scratch: Path,
    *,
    skeleton: bool = False,
    force: bool = False,
) -> None:
    """What ``pkgforge stage`` then ``pkgforge dbdump -f FMT OUTPUT`` do for the
    pkgforge.cfg in ``tree``, staging into ``scratch``."""
    scratch.mkdir(exist_ok=True)
    project = Project.from_config(
        tree / "pkgforge.cfg", buildroot=scratch / "root", db=scratch / "files.jsonl"
    )
    project.stage(init=True)
    project.dump(fmt, output, skeleton=skeleton, force=force)


def _append_strip_override(rules: Path) -> None:
    """Give ``rules`` an empty ``override_dh_strip_nondeterminism`` target when
    the host has no such tool (Fedora's debhelper does not)."""
    if shutil.which("dh_strip_nondeterminism") is None:
        with rules.open("a", encoding="utf-8") as handle:
            handle.write("\noverride_dh_strip_nondeterminism:\n")


def _query(rpm_path: Path, *args: str) -> str:
    return subprocess.run(
        ["rpm", "-qp", *args, str(rpm_path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _rpmbuild_spec(
    base: Path, tree: Path, spec: Path, *, noclean: bool = False
) -> typing.Dict[str, Path]:
    """``rpmbuild -bb`` of ``spec`` with ``tree``'s pkgforge.cfg; returns the
    RPMs built, by package name."""
    topdir = base / "top"
    for sub in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS", "BUILDROOT"):
        (topdir / sub).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "rpmbuild",
            "-bb",
            "--nodeps",
            *(["--noclean"] if noclean else []),
            "--define",
            f"_topdir {topdir}",
            "--define",
            f"pkgforge_config {tree / 'pkgforge.cfg'}",
            "--define",
            f"proj {tree}",
            str(spec),
        ],
        capture_output=True,
        text=True,
        env=_pkgforge_on_path(base),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {
        _query(path, "--qf", "%{NAME}"): path
        for path in (topdir / "RPMS").rglob("*.rpm")
    }


_ROW_FIELDS = "%{FILEMODES:octal}|%{FILEUSERNAME}|%{FILEGROUPNAME}"


@requires_rpmbuild
def test_rpmbuild_generated_spec_builds_two_packages(tmp_path):
    """From pkgforge.cfg and its sources alone, ``pkgforge stage`` and
    ``dbdump -f rpmspec`` give a spec that ``rpmbuild`` builds into two
    packages: the README only in the second, and the first carrying the modes,
    owners and capability the file DB recorded."""
    tree = _example_source(tmp_path / "hello")
    spec = tree / "hello.spec"
    _generate(tree, _HOST_SPEC, spec, tmp_path / "generated")
    rpms = _rpmbuild_spec(tmp_path, tree, spec, noclean=True)
    assert sorted(rpms) == ["hello", "hello-doc"]
    assert sorted(path.name for path in rpms.values()) == [
        "hello-1.0-1.noarch.rpm",
        "hello-doc-1.0-1.noarch.rpm",
    ]

    listing = _query(rpms["hello-doc"], "-l").splitlines()
    assert listing == ["/usr/share/doc/hello-doc", "/usr/share/doc/hello-doc/README"]
    assert "/usr/share/doc/hello-doc/README" not in _query(rpms["hello"], "-l")

    packaged: typing.Dict[str, typing.Dict[str, tuple]] = {}
    for name, path in rpms.items():
        rows = _query(path, "--qf", f"[%{{FILENAMES}}|{_ROW_FIELDS}\\n]").splitlines()
        packaged[name] = {}
        for row in rows:
            file, mode, user, group = row.split("|")
            packaged[name][file] = (int(mode, 8) & 0o7777, user, group)
    caps = _query(rpms["hello"], "--qf", "[%{FILENAMES} %{FILECAPS}\\n]").splitlines()
    assert "/usr/libexec/hello/helper cap_net_bind_service=ep" in caps

    (db,) = (tmp_path / "top" / "BUILD").rglob("pkgforge.jsonl")
    recorded = [
        json.loads(line)
        for line in db.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert recorded
    for entry in recorded:
        path = entry["path"]
        package = "hello" if entry["package"] == "-" else entry["package"]
        assert path in packaged[package], path
        mode, user, group = packaged[package][path]
        if entry["mode"] != "-":
            assert mode == int(entry["mode"], 8), path
        if entry["owner"] != "-":
            assert user == entry["owner"], path
        if entry["group"] != "-":
            assert group == entry["group"], path


@requires_dh_sequence
def test_dh_install_generated_skeleton_builds_two_packages(tmp_path):
    """From pkgforge.cfg and its sources alone, ``pkgforge stage`` and
    ``dbdump -f debian --skeleton`` give a debian/ that ``dpkg-buildpackage``
    builds into two packages: the README only in the second, and
    ``libcap2-bin`` only where a capability is set."""
    pkgroot = _example_source(tmp_path / "work" / "hello")
    debian = pkgroot / "debian"
    _generate(pkgroot, "debian", debian, tmp_path / "generated", skeleton=True)
    _append_strip_override(debian / "rules")
    _result, debs = _build_debs(tmp_path, pkgroot)
    assert sorted(debs) == ["hello-doc_1.0-1_all.deb", "hello_1.0-1_all.deb"]

    readme = "usr/share/doc/hello-doc/README"
    assert readme in _payload(debs["hello-doc_1.0-1_all.deb"])
    assert readme not in _payload(debs["hello_1.0-1_all.deb"])
    assert "usr/bin/hello" in _payload(debs["hello_1.0-1_all.deb"])

    def depends(name: str) -> str:
        control = _control_member(debs[name], "control")
        # dpkg drops the field when every part of it is empty.
        return next(
            (line for line in control.splitlines() if line.startswith("Depends:")),
            "",
        )

    assert "libcap2-bin" in depends("hello_1.0-1_all.deb")
    assert "libcap2-bin" not in depends("hello-doc_1.0-1_all.deb")


@requires_dh_sequence
def test_dh_install_example_debian_builds(tmp_path):
    """examples/debian builds with the same flow as the recipe tests, from a
    copy of examples/ (dpkg-buildpackage writes next to the source tree)."""
    pkgroot = tmp_path / "work" / "hello"
    shutil.copytree(EXAMPLES, pkgroot)
    (pkgroot / "debian" / "rules").chmod(0o755)
    _append_strip_override(pkgroot / "debian" / "rules")

    _result, debs = _build_debs(tmp_path, pkgroot)
    assert sorted(debs) == ["hello-doc_1.0-1_all.deb", "hello_1.0-1_all.deb"]
    members = _payload(debs["hello_1.0-1_all.deb"])

    def pinned(name: str, mode: int, owner: str, group: str):
        member = members[name]
        assert stat.S_IMODE(member.mode) == mode, name
        assert (member.uname, member.gname) == (owner, group), name

    pinned("usr/bin/hello", 0o755, "root", "root")
    pinned("usr/libexec/hello/helper", 0o755, "root", "root")
    pinned("etc/hello/hello.conf", 0o640, "root", "adm")
    # Owned by the hello account, which does not exist while dpkg unpacks:
    # dh_fixperms' root-owned mode is what ships, and postinst applies the rest.
    pinned("var/lib/hello", 0o755, "root", "root")
    assert members["usr/bin/hi"].issym()
    assert not [name for name in members if name.startswith("usr/share/doc/hello-doc")]
    assert "usr/share/doc/hello-doc/README" in _payload(debs["hello-doc_1.0-1_all.deb"])
    # The ghost log is neither shipped nor reported as missing, and the
    # capability is set by the maintainer script.
    assert "var/log/hello.log" not in members
    postinst = _control_member(debs["hello_1.0-1_all.deb"], "postinst")
    assert 'setcap cap_net_bind_service=ep "$DPKG_ROOT"/usr/libexec/hello/helper' in (
        postinst
    )


#: The shell staging that examples/pkgforge.cfg describes: the stage-only and
#: shell-staged packages must come out the same.
_EXAMPLE_SHELL_STAGING = """#!/bin/sh
set -eu
D="$(dirname "$0")"
S="$D/src"
pf() { pkgforge -q --config "$D/stage.cfg" "$@"; }

pf initdb

E="$(mktemp -d)"
for d in /usr/libexec/hello /etc/hello /usr/share/licenses/hello; do
  pf install -D -d -m 755 -o root -g root "$E" "$d"
done
pf install -D -d -m 755 -o root -g root -P hello-doc "$E" /usr/share/doc/hello-doc
pf install -D -d -m 750 -o hello -g hello "$E" /var/lib/hello
pf install -D -d -m 755 -o hello -g hello -k ghost "$E" /run/hello
rmdir "$E"

pf install -p -m 755 -o root -g root "$S/hello" /usr/bin
pf install -D -t symlink -O target=hello - /usr/bin/hi
pf install -p -m 755 -o root -g root --caps cap_net_bind_service=ep "$S/helper" /usr/libexec/hello
pf install -p -m 640 -o root -g adm -k config-noreplace "$S/hello.conf" /etc/hello
pf install -p -m 644 -o root -g root "$S/hello.service" /usr/lib/systemd/system
pf install -p -m 644 -o root -g root -k license "$S/LICENSE" /usr/share/licenses/hello
pf install -p -m 644 -o root -g root -k doc -P hello-doc "$S/README" /usr/share/doc/hello-doc
pf install -D -m 640 -o root -g adm -k ghost - /var/log/hello.log < /dev/null

pf sysfiles
"""

#: The tables of examples/pkgforge.cfg that the shell staging still needs: the
#: packages for the generated files and the account they create.
_EXAMPLE_SHELL_CFG = """\
[package]
name = "hello"

[[packages]]
name = "hello-doc"

[[users]]
name = "hello"
description = "hello example daemon"
home = "/var/lib/hello"
"""

_STAGE_ONLY_SPEC = 'pkgforge -q --config "%{pkgforge_config}" stage --init\n'
_STAGE_ONLY_RULES = "\tpkgforge -q --config pkgforge.cfg stage --init\n"


def _example_tree(dest: Path, *, shell: bool, spec: bool = False) -> Path:
    """A copy of examples/ at ``dest``: as shipped, or with its recipes running
    the shell staging instead of ``pkgforge stage`` (and a pkgforge.cfg that
    only names the packages and the account, for the dumps to read). With
    ``spec``, hello.spec is first written again for this host's rpm."""
    shutil.copytree(EXAMPLES, dest)
    (dest / "debian" / "rules").chmod(0o755)
    if spec:
        _generate(
            dest, _HOST_SPEC, dest / "hello.spec", dest.parent / "generated", force=True
        )
    if shell:
        (dest / "stage.sh").write_text(_EXAMPLE_SHELL_STAGING, encoding="utf-8")
        (dest / "stage.cfg").write_text(_EXAMPLE_SHELL_CFG, encoding="utf-8")
        for name, old, new in (
            ("hello.spec", _STAGE_ONLY_SPEC, "sh %{proj}/stage.sh\n"),
            ("debian/rules", _STAGE_ONLY_RULES, "\tsh stage.sh\n"),
        ):
            text = (dest / name).read_text(encoding="utf-8")
            assert text.count(old) == 1, name
            (dest / name).write_text(text.replace(old, new), encoding="utf-8")
        (dest / "pkgforge.cfg").write_text(_EXAMPLE_SHELL_CFG, encoding="utf-8")
    return dest


_RPM_ROWS = (
    "[%{FILENAMES}|%{FILEMODES:octal}|%{FILEUSERNAME}|%{FILEGROUPNAME}"
    "|%{FILEFLAGS}|%{FILELINKTOS}|%{FILECAPS}\\n]"
)


def _build_example_rpms(base: Path, *, shell: bool = False) -> typing.Dict[str, Path]:
    """Build examples/hello.spec, as this host's rpm reads it, under ``base``;
    returns the RPMs by package name."""
    base.mkdir(exist_ok=True)
    tree = _example_tree(base / "examples", shell=shell, spec=True)
    rpms = _rpmbuild_spec(base, tree, tree / "hello.spec")
    assert sorted(rpms) == ["hello", "hello-doc"]
    return rpms


def _example_rpm_rows(base: Path, *, shell: bool) -> typing.Dict[str, typing.List[str]]:
    """Build examples/hello.spec under ``base`` and return the sorted per-file
    rows of each RPM."""
    return {
        name: sorted(_query(path, "--qf", _RPM_ROWS).splitlines())
        for name, path in _build_example_rpms(base, shell=shell).items()
    }


@requires_rpmbuild
def test_rpmbuild_example_cfg_matches_shell_staging(tmp_path):
    """The RPMs built by ``pkgforge stage`` from examples/pkgforge.cfg list the
    same paths, modes, owners, flags and link targets as the ones built by the
    shell staging it replaces."""
    staged = _example_rpm_rows(tmp_path / "stage", shell=False)
    shell = _example_rpm_rows(tmp_path / "shell", shell=True)
    assert len(staged["hello"]) == 14
    assert len(staged["hello-doc"]) == 2
    assert staged == shell


def _build_example_debs(base: Path, *, shell: bool = False) -> typing.Dict[str, Path]:
    """Build examples/debian under ``base``; returns the .debs by package."""
    base.mkdir(exist_ok=True)
    pkgroot = _example_tree(base / "work" / "hello", shell=shell)
    _append_strip_override(pkgroot / "debian" / "rules")
    _result, built = _build_debs(base, pkgroot)
    debs = {name.partition("_")[0]: path for name, path in built.items()}
    assert sorted(debs) == ["hello", "hello-doc"]
    return debs


def _example_deb_members(base: Path, *, shell: bool) -> typing.Dict[str, set]:
    """Build examples/debian under ``base`` and return each payload's
    ``(name, type, mode, uname, gname, linkname)`` set."""
    return {
        package: {
            (
                name,
                member.type,
                stat.S_IMODE(member.mode),
                member.uname,
                member.gname,
                member.linkname,
            )
            for name, member in _payload(deb).items()
        }
        for package, deb in _build_example_debs(base, shell=shell).items()
    }


@requires_dh_sequence
def test_dh_install_example_cfg_matches_shell_staging(tmp_path):
    """The .debs built by ``pkgforge stage`` from examples/pkgforge.cfg have the
    same payloads as the ones built by the shell staging it replaces."""
    staged = _example_deb_members(tmp_path / "stage", shell=False)
    shell = _example_deb_members(tmp_path / "shell", shell=True)
    assert any(name == "usr/bin/hi" for name, *_ in staged["hello"])
    assert staged == shell


# --------------------------------------------------------------------------
# examples/: the account the package creates, and its install-time setup
# --------------------------------------------------------------------------


@requires_rpmbuild
def test_rpmbuild_example_accounts_and_scripts(tmp_path):
    """The RPM carries the sysusers.d and tmpfiles.d files, the pre-install
    script that creates the account only where rpm does not read the shipped
    file (below 4.19), and the post-install tmpfiles call; its rows keep the
    account, the ghost and the capability."""
    rpm_path = _build_example_rpms(tmp_path)["hello"]

    def query(*args: str) -> str:
        return _query(rpm_path, *args)

    pre, _, post = query("--scripts").partition("postinstall scriptlet")
    if _RPM_BELOW_419:
        assert "systemd-sysusers -" in pre
        assert "u hello - " in pre
    else:
        # rpm 4.19+ reads the shipped sysusers.d file itself.
        assert "user(hello)" in query("--provides")
        assert "systemd-sysusers" not in pre
    assert "systemd-tmpfiles --create /usr/lib/tmpfiles.d/hello.conf" in post

    rows = {}
    for row in query(
        "--qf",
        "[%{FILENAMES}|%{FILEMODES:octal}|%{FILEUSERNAME}|%{FILEGROUPNAME}"
        "|%{FILEFLAGS}|%{FILECAPS}\\n]",
    ).splitlines():
        path, mode, user, group, flags, caps = row.split("|")
        rows[path] = (mode, user, group, int(flags), caps)
    for name in ("sysusers", "tmpfiles"):
        assert rows[f"/usr/lib/{name}.d/hello.conf"][:3] == ("100644", "root", "root")
    assert rows["/var/lib/hello"][:3] == ("40750", "hello", "hello")
    assert rows["/run/hello"][3] == 64
    assert rows["/usr/libexec/hello/helper"][4] == "cap_net_bind_service=ep"


@requires_dh_sequence
def test_dh_install_example_accounts_and_scripts(tmp_path):
    """The .deb ships the generated files; ``dh_installsysusers`` and
    ``dh_installtmpfiles`` add their snippets to ``postinst``, which then
    applies the ownership of ``/var/lib/hello`` before the capability is set."""
    deb = _build_example_debs(tmp_path)["hello"]
    members = _payload(deb)
    assert "usr/lib/sysusers.d/hello.conf" in members
    assert "usr/lib/tmpfiles.d/hello.conf" in members
    state = members["var/lib/hello"]
    assert (stat.S_IMODE(state.mode), state.uname, state.gname) == (
        0o755,
        "root",
        "root",
    )
    assert "run/hello" not in members

    postinst = _control_member(deb, "postinst")
    order = [
        postinst.index(text)
        for text in (
            "systemd-sysusers",
            "systemd-tmpfiles",
            "dpkg-statoverride --update --add hello hello 750 /var/lib/hello",
            "setcap cap_net_bind_service=ep",
        )
    ]
    assert order == sorted(order), postinst
    assert "dpkg-statoverride --remove /var/lib/hello" in _control_member(deb, "postrm")
    depends = next(
        line
        for line in _control_member(deb, "control").splitlines()
        if line.startswith("Depends:")
    )
    assert "systemd-sysusers" in depends and "libcap2-bin" in depends


def _install_allowed() -> bool:
    """Whether a test may install a package on this host: asked for with
    ``PKGFORGE_CONSUMER_INSTALL=1`` and able to act as root."""
    if os.environ.get("PKGFORGE_CONSUMER_INSTALL") != "1":
        return False
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    if shutil.which("sudo") is None:
        return False
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0


requires_install = pytest.mark.skipif(
    not _install_allowed(),
    reason="PKGFORGE_CONSUMER_INSTALL=1 and root (or sudo -n) required: "
    "installs a package on this host",
)


def _as_root(*command: str) -> typing.List[str]:
    """``command`` run as root: itself when already root, else through ``sudo -n``."""
    if os.geteuid() == 0:
        return list(command)
    return ["sudo", "-n", *command]


def _run_as_root(*command: str, check: bool = True):
    result = subprocess.run(_as_root(*command), capture_output=True, text=True)
    if check:
        assert result.returncode == 0, f"{command}: {result.stdout}{result.stderr}"
    return result


def _assert_installed_example_state():
    """What a host holds once the hello package is installed: the account,
    the state directory it owns, and the capability on the helper."""
    user = pwd.getpwnam("hello")
    group = grp.getgrnam("hello")
    state = os.stat("/var/lib/hello")
    assert stat.S_IMODE(state.st_mode) == 0o750
    assert (state.st_uid, state.st_gid) == (user.pw_uid, group.gr_gid)
    caps = subprocess.run(
        ["getcap", "/usr/libexec/hello/helper"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "cap_net_bind_service" in caps and "ep" in caps


@requires_rpmbuild
@requires_install
def test_rpmbuild_example_installs_accounts(tmp_path):
    """Installing the RPM on a host without the account creates it, owns
    ``/var/lib/hello`` by it and has ``%post`` create ``/run/hello``."""
    rpm_path = _build_example_rpms(tmp_path)["hello"]
    try:
        _run_as_root("rpm", "-i", str(rpm_path))
        _assert_installed_example_state()
        run = os.stat("/run/hello")
        assert run.st_uid == pwd.getpwnam("hello").pw_uid
    finally:
        _run_as_root("rpm", "-e", "hello", check=False)


@requires_dh_sequence
@requires_install
def test_dh_install_example_installs_accounts(tmp_path):
    """Installing the .deb creates the account, overrides the ownership of
    ``/var/lib/hello`` once, keeps an administrator's override on reinstall
    and drops it on purge."""
    deb = _build_example_debs(tmp_path)["hello"]

    def override() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["dpkg-statoverride", "--list", "/var/lib/hello"],
            capture_output=True,
            text=True,
        )

    try:
        _run_as_root("dpkg", "-i", str(deb))
        _assert_installed_example_state()
        assert override().stdout.strip() == "hello hello 750 /var/lib/hello"

        _run_as_root("dpkg-statoverride", "--remove", "/var/lib/hello")
        _run_as_root(
            "dpkg-statoverride", "--update", "--add", "root", "adm", "0700",
            "/var/lib/hello",
        )  # fmt: skip
        _run_as_root("dpkg", "-i", str(deb))
        assert override().stdout.strip() == "root adm 700 /var/lib/hello"

        _run_as_root("dpkg", "--purge", "hello")
        assert override().returncode == 1
    finally:
        _run_as_root("dpkg", "--purge", "hello", check=False)


# --------------------------------------------------------------------------
# two packages from one source
# --------------------------------------------------------------------------

_TWO_PACKAGE_CFG = """\
[package]
name = "hello"

[[packages]]
name = "hello-doc"

[[packages]]
name = "hello-empty"
"""


def _payload(deb: Path) -> typing.Dict[str, tarfile.TarInfo]:
    """The members of ``deb``'s payload, keyed by path without the leading ``./``."""
    fsys_tarfile = subprocess.run(
        ["dpkg-deb", "--fsys-tarfile", str(deb)], capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(fsys_tarfile)) as tf:
        return {
            (m.name[2:] if m.name.startswith("./") else m.name).rstrip("/"): m
            for m in tf.getmembers()
        }


@requires_dh_sequence
def test_dh_install_two_packages_split_payload(tmp_path):
    """One source, two binary packages: each payload holds only the entries
    recorded for it, ``libcap2-bin`` reaches only the package with a capability
    (through ``${pkgforge:Depends}``), and a ghost file of the second package
    is neither shipped nor reported by ``dh_missing``."""
    pkgroot = tmp_path / "work" / "hello"
    debian_dir = pkgroot / "debian"
    (debian_dir / "source").mkdir(parents=True)
    (pkgroot / "src").mkdir()
    (pkgroot / "src" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (pkgroot / "src" / "README").write_text("docs\n", encoding="utf-8")
    (pkgroot / "pkgforge.cfg").write_text(_TWO_PACKAGE_CFG, encoding="utf-8")
    (debian_dir / "source" / "format").write_text("3.0 (native)\n", encoding="utf-8")
    (debian_dir / "changelog").write_text(
        "hello (1.0) unstable; urgency=medium\n\n"
        "  * Test package.\n\n"
        " -- pkgforge tests <noreply@example.invalid>  Mon, 01 Jan 2026 00:00:00 +0000\n",
        encoding="utf-8",
    )
    stanza = (
        "Package: {name}\n"
        "Architecture: all\n"
        "Depends: ${{misc:Depends}}, ${{pkgforge:Depends}}\n"
        "Description: pkgforge consumer test package\n"
        " Built only to exercise the per-package files.\n"
    )
    (debian_dir / "control").write_text(
        "Source: hello\n"
        "Section: utils\n"
        "Priority: optional\n"
        "Maintainer: pkgforge tests <noreply@example.invalid>\n"
        "Build-Depends: debhelper-compat (= 13)\n"
        "Rules-Requires-Root: binary-targets\n\n"
        + stanza.format(name="hello")
        + "\n"
        + stanza.format(name="hello-doc"),
        encoding="utf-8",
    )
    (pkgroot / "stage.sh").write_text(
        "set -eu\n"
        'pf() { pkgforge -q "$@"; }\n'
        "pf initdb\n"
        "pf install -p -m 755 -o root -g root --caps cap_net_raw=ep src/tool /usr/bin\n"
        "pf install -p -m 644 -o root -g root -P hello-doc src/README"
        " /usr/share/doc/hello-doc\n"
        "pf install -D -m 640 -o root -g adm -k ghost -P hello-doc - "
        "/var/log/hello-doc.log < /dev/null\n",
        encoding="utf-8",
    )
    recipe = _doc_rules_recipe("\tdh_installdirs\n")
    rules = recipe + "\noverride_dh_strip_nondeterminism:\n"
    (debian_dir / "rules").write_text(rules, encoding="utf-8")
    (debian_dir / "rules").chmod(0o755)

    result = subprocess.run(
        ["dpkg-buildpackage", "-b", "-us", "-uc", "-d"],
        cwd=pkgroot,
        capture_output=True,
        text=True,
        env=_pkgforge_on_path(tmp_path),
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "hello-doc.log" not in output  # dh_missing found nothing to report

    work = tmp_path / "work"
    main_deb = work / "hello_1.0_all.deb"
    doc_deb = work / "hello-doc_1.0_all.deb"
    assert sorted(p.name for p in work.glob("*.deb")) == [
        doc_deb.name,
        main_deb.name,
    ]

    main_files = _payload(main_deb)
    doc_files = _payload(doc_deb)
    assert "usr/bin/tool" in main_files
    assert "usr/share/doc/hello-doc/README" not in main_files
    assert "usr/share/doc/hello-doc/README" in doc_files
    assert "usr/bin/tool" not in doc_files
    assert "var/log/hello-doc.log" not in main_files
    assert "var/log/hello-doc.log" not in doc_files

    control = _control_member(main_deb, "control")
    assert "libcap2-bin" in next(
        line for line in control.splitlines() if line.startswith("Depends:")
    )
    assert "libcap2-bin" not in _control_member(doc_deb, "control")
    assert 'setcap cap_net_raw=ep "$DPKG_ROOT"/usr/bin/tool' in _control_member(
        main_deb, "postinst"
    )


_TWO_PACKAGE_SPEC = """\
Name: hello
Version: 1
Release: 1
Summary: pkgforge consumer test
License: MIT
BuildArch: noarch
AutoReqProv: no

%description
pkgforge consumer test package.

%package -n hello-doc
Summary: pkgforge consumer test documentation

%description -n hello-doc
Documentation of the consumer test package.

%package -n hello-empty
Summary: pkgforge consumer test package with no file

%description -n hello-empty
Holds nothing.

%install
rm -rf %{{buildroot}}
mkdir -p %{{buildroot}}
cp -a {root}/. %{{buildroot}}/

%files -f {lists}/files-hello.txt

%files -n hello-doc -f {lists}/files-hello-doc.txt

%files -n hello-empty -f {lists}/files-hello-empty.txt
"""


@requires_rpmbuild
def test_rpmbuild_two_packages_file_lists(tmp_path, cli):
    """``dbdump`` into a directory writes one ``%files`` list per package; each
    built RPM holds only its own entries, and a package with no entry (its list
    one empty line) builds as an empty package."""
    cfg = tmp_path / "pkgforge.cfg"
    cfg.write_text(_TWO_PACKAGE_CFG, encoding="utf-8")
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = tmp_path / "src"
    src.mkdir()
    (src / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (src / "README").write_text("docs\n", encoding="utf-8")
    base = ["--config", str(cfg), "--db", str(db), "--buildroot", str(root)]
    install = ["install", "-p", "-m", "644", "-o", "root", "-g", "root"]
    assert cli(*base, *install, "-m", "755", str(src / "tool"), "/usr/bin").rc == 0
    result = cli(
        *base, *install, "-P", "hello-doc", str(src / "README"), "/usr/share/doc/hello"
    )
    assert result.rc == 0, result.err

    lists = tmp_path / "lists"
    lists.mkdir()
    dump = cli(*base, "dbdump", "-f", _HOST_FORMAT, str(lists))
    assert dump.rc == 0, dump.err
    assert (lists / "files-hello-empty.txt").read_bytes() == b"\n"

    topdir = tmp_path / "top"
    for sub in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS", "BUILDROOT"):
        (topdir / sub).mkdir(parents=True)
    spec = tmp_path / "hello.spec"
    spec.write_text(_TWO_PACKAGE_SPEC.format(root=root, lists=lists), encoding="utf-8")
    build = subprocess.run(
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
    assert build.returncode == 0, build.stdout + build.stderr

    def listing(name: str) -> typing.List[str]:
        (rpm_path,) = (topdir / "RPMS").rglob(f"{name}-1-1.*.rpm")
        out = subprocess.run(
            ["rpm", "-qpl", str(rpm_path)], capture_output=True, text=True, check=True
        ).stdout
        return out.splitlines()

    assert listing("hello") == ["/usr/bin/tool"]
    assert listing("hello-doc") == ["/usr/share/doc/hello/README"]
    assert listing("hello-empty") == ["(contains no files)"]


# --------------------------------------------------------------------------
# package -f deb: the .deb pkgforge writes itself
# --------------------------------------------------------------------------

DPKG = shutil.which("dpkg")
requires_dpkg_deb = pytest.mark.skipif(
    DPKG_DEB is None, reason="dpkg-deb not installed"
)
requires_dpkg_root = pytest.mark.skipif(
    DPKG_DEB is None or DPKG is None or FAKEROOT is None,
    reason="dpkg, dpkg-deb or fakeroot not installed",
)


def _build_direct_debs(
    base: Path, cfg: typing.Optional[str] = None
) -> typing.Dict[str, Path]:
    """Stage the example (or a cfg written from ``cfg``) under ``base`` and run
    ``package -f deb`` on it; returns the .debs by package."""
    base.mkdir(exist_ok=True)
    tree = _example_source(base / "work")
    if cfg is not None:
        (tree / "pkgforge.cfg").write_bytes(cfg.encode("utf-8"))
    project = Project.from_config(
        tree / "pkgforge.cfg", buildroot=base / "root", db=base / "files.jsonl"
    )
    project.stage(init=True)
    paths = project.package("deb", base / "out")
    return {path.name.partition("_")[0]: path for path in paths}


def _maybe_control(deb: Path, name: str) -> typing.Optional[str]:
    """``name`` of ``deb``'s control archive, or ``None`` when it has none."""
    try:
        return _control_member(deb, name)
    except KeyError:
        return None


def _dpkg_deb(*args: str) -> str:
    return subprocess.run(
        ["dpkg-deb", *args], capture_output=True, text=True, check=True
    ).stdout


@requires_dpkg_deb
def test_dpkg_deb_reads_example(tmp_path):
    """``dpkg-deb`` reads both packages and lists the owners and modes the DB
    records."""
    debs = _build_direct_debs(tmp_path)
    assert sorted(debs) == ["hello", "hello-doc"]
    for deb in debs.values():
        assert "new Debian package, version 2.0." in _dpkg_deb("--info", str(deb))
    assert _dpkg_deb("-f", str(debs["hello"]), "Package", "Version", "Depends") == (
        "Package: hello\nVersion: 1.0-1\n"
        "Depends: systemd | systemd-standalone-sysusers | systemd-sysusers, "
        "libcap2-bin\n"
    )
    assert _dpkg_deb("-f", str(debs["hello-doc"]), "Source") == "hello\n"
    rows = {
        line.split()[5]: line.split()[:2]
        for line in _dpkg_deb("--contents", str(debs["hello"])).splitlines()
    }
    assert rows["./etc/hello/hello.conf"] == ["-rw-r-----", "root/adm"]
    assert rows["./var/lib/hello/"] == ["drwxr-x---", "root/root"]
    assert rows["./usr/bin/hello"] == ["-rwxr-xr-x", "root/root"]
    assert rows["./usr/bin/hi"] == ["lrwxrwxrwx", "root/root"]
    assert "./run/hello/" not in rows
    doc = _dpkg_deb("--contents", str(debs["hello-doc"]))
    assert "./usr/share/doc/hello-doc/README" in doc


_PARENT_DIRS = {"usr", "usr/share", "usr/share/doc"}


@requires_dh_sequence
def test_dpkg_deb_example_matches_dh_build(tmp_path):
    """The .debs ``package -f deb`` writes match the ones debhelper builds from
    the same pkgforge.cfg: control fields, conffiles, payload paths, modes and
    owners (an entry owned by an account created at install time is root:root
    in both), apart from debhelper's own changelog."""
    direct = _build_direct_debs(tmp_path / "direct")
    built = _build_example_debs(tmp_path / "dh")
    assert sorted(direct) == sorted(built) == ["hello", "hello-doc"]
    install_time = {"var/lib/hello"}
    for package, deb in direct.items():
        other = built[package]
        for field in (
            "Package",
            "Version",
            "Architecture",
            "Depends",
            "Section",
            "Priority",
            "Description",
        ):
            assert _dpkg_deb("-f", str(deb), field) == _dpkg_deb(
                "-f", str(other), field
            ), field
        # debhelper keeps an empty conffiles file that the direct package omits.
        assert (_maybe_control(deb, "conffiles") or "") == (
            _maybe_control(other, "conffiles") or ""
        )

        ours, theirs = _payload(deb), _payload(other)
        changelog = f"usr/share/doc/{package}/changelog.Debian.gz"
        extra = set(theirs) - set(ours)
        assert changelog in extra
        assert extra <= {changelog, f"usr/share/doc/{package}"} | _PARENT_DIRS
        assert set(ours) <= set(theirs)
        for name, member in ours.items():
            twin = theirs[name]
            assert member.type == twin.type, name
            assert (member.uname, member.gname) == (twin.uname, twin.gname), name
            if name not in install_time:
                assert stat.S_IMODE(member.mode) == stat.S_IMODE(twin.mode), name
            assert member.linkname == twin.linkname, name
    assert {"var/lib/hello"} <= set(_payload(direct["hello"]))


_PLAIN_CFG = """\
[package]
name = "plain"
version = "2.5"
summary = "A plain package"
license = "MIT"
maintainer = "Example Maintainer <maintainer@example.org>"
arch = "all"

[[stage]]
src = "src/hello"
dest = "/usr/bin"
mode = "755"

[[stage]]
symlink = "/usr/bin/pl"
target = "hello"

[[stage]]
src = "src/hello.conf"
dest = "/etc/plain"
mode = "644"
kind = "config-noreplace"

[[stage]]
src = "src/README"
dest = "/usr/share/doc/plain"
mode = "644"
kind = "doc"
"""


@requires_dpkg_root
def test_dpkg_deb_installs_into_scratch_root(tmp_path):
    """dpkg installs a package with no account, unit or capability into a scratch
    root as an unprivileged user, and ``--verify`` finds nothing altered."""
    deb = _build_direct_debs(tmp_path, _PLAIN_CFG)["plain"]
    root = tmp_path / "target"
    (root / "var/lib/dpkg/info").mkdir(parents=True)
    (root / "var/lib/dpkg/updates").mkdir()
    (root / "var/lib/dpkg/status").write_bytes(b"")
    (root / "var/lib/dpkg/available").write_bytes(b"")
    dpkg = [
        "dpkg",
        f"--root={root}",
        f"--log={tmp_path / 'dpkg.log'}",
        "--force-script-chrootless",
        "--force-not-root",
    ]
    install = subprocess.run(
        ["fakeroot", *dpkg, "-i", str(deb)], capture_output=True, text=True
    )
    assert install.returncode == 0, install.stdout + install.stderr
    assert (root / "usr/bin/hello").is_file()
    assert os.readlink(root / "usr/bin/pl") == "hello"
    assert (root / "etc/plain/hello.conf").is_file()
    conffiles = (root / "var/lib/dpkg/info/plain.conffiles").read_text(encoding="utf-8")
    assert conffiles.split()[0] == "/etc/plain/hello.conf"
    verify = subprocess.run(
        ["dpkg", f"--root={root}", "--verify", "plain"], capture_output=True, text=True
    )
    assert (verify.returncode, verify.stdout, verify.stderr) == (0, "", "")


@requires_dpkg_root
@requires_install
def test_dpkg_deb_example_installs(tmp_path):
    """Installing the direct .deb creates the account, owns ``/var/lib/hello``
    by it through ``dpkg-statoverride``, sets the capability, and a purge
    removes the override."""
    deb = _build_direct_debs(tmp_path)["hello"]

    def override() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["dpkg-statoverride", "--list", "/var/lib/hello"],
            capture_output=True,
            text=True,
        )

    try:
        _run_as_root("dpkg", "-i", str(deb))
        _assert_installed_example_state()
        assert override().stdout.strip() == "hello hello 750 /var/lib/hello"
        _run_as_root("dpkg", "--purge", "hello")
        assert override().returncode == 1
    finally:
        _run_as_root("dpkg", "--purge", "hello", check=False)


# --------------------------------------------------------------------------
# package -f rpm: the .rpm pkgforge builds through rpmbuild
# --------------------------------------------------------------------------


def _build_direct_rpms(base: Path) -> typing.Dict[str, Path]:
    """Stage the example under ``base`` and run ``package -f rpm`` on it;
    returns the RPMs by package name."""
    base.mkdir(exist_ok=True)
    tree = _example_source(base / "work")
    project = Project.from_config(
        tree / "pkgforge.cfg", buildroot=base / "root", db=base / "files.jsonl"
    )
    project.stage(init=True)
    return {
        _query(path, "--qf", "%{NAME}"): path
        for path in project.package("rpm", base / "out")
    }


def _rpm_rows(rpm_path: Path) -> typing.List[str]:
    """The sorted per-file rows of ``rpm_path``: path, mode, owner, group and
    the file flags by name."""
    return sorted(
        _query(
            rpm_path,
            "--qf",
            f"[%{{FILENAMES}}|{_ROW_FIELDS}|%{{FILEFLAGS:fflags}}\n]",
        ).splitlines()
    )


@requires_rpmbuild
def test_rpmbuild_package_example_matches_spec_build(tmp_path):
    """``package -f rpm`` and ``rpmbuild`` of the generated spec give the same
    packages: names, per-file modes, owners, groups and flags, and scriptlets."""
    direct = _build_direct_rpms(tmp_path / "direct")
    built = _build_example_rpms(tmp_path / "spec")
    assert sorted(direct) == sorted(built) == ["hello", "hello-doc"]
    assert sorted(path.name for path in direct.values()) == [
        "hello-1.0-1.noarch.rpm",
        "hello-doc-1.0-1.noarch.rpm",
    ]
    for name, path in direct.items():
        assert _rpm_rows(path) == _rpm_rows(built[name]), name
        assert _query(path, "--scripts") == _query(built[name], "--scripts"), name
    rows = _rpm_rows(direct["hello"])
    assert len(rows) == 14
    assert any(row.startswith("/etc/hello/hello.conf|100640|root|adm|") for row in rows)
    assert any(row.startswith("/var/lib/hello|40750|hello|hello|") for row in rows)


@requires_rpmbuild
def test_rpmbuild_package_unit_scriptlets(tmp_path):
    """The RPM carries the systemd scriptlets for ``hello.service`` where the
    host's rpm has the macros, and never the unexpanded macro text."""
    hello = _build_direct_rpms(tmp_path)["hello"]
    scripts = _query(hello, "--scripts")
    macros = subprocess.run(
        ["rpm", "--eval", "%{?systemd_post:1}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if macros == "1":
        assert "systemd-update-helper" in scripts or "systemctl" in scripts
        assert "hello.service" in scripts
    assert "systemd_post" not in scripts
    assert "systemd_preun" not in scripts
    assert "systemd_postun" not in scripts


@requires_rpmbuild
@requires_install
def test_rpmbuild_package_example_installs(tmp_path):
    """Installing the direct RPM creates the account, owns ``/var/lib/hello`` by
    it, sets the capability and has ``%post`` create ``/run/hello``."""
    rpm_path = _build_direct_rpms(tmp_path)["hello"]
    try:
        _run_as_root("rpm", "-i", str(rpm_path))
        _assert_installed_example_state()
        assert os.stat("/run/hello").st_uid == pwd.getpwnam("hello").pw_uid
    finally:
        _run_as_root("rpm", "-e", "hello", check=False)


# --------------------------------------------------------------------------
# package -f arch: the pacman package pkgforge writes itself
# --------------------------------------------------------------------------

PACMAN = shutil.which("pacman")
requires_pacman = pytest.mark.skipif(PACMAN is None, reason="pacman not installed")


def _build_direct_arch(base: Path) -> typing.Dict[str, Path]:
    """Stage the example under ``base`` and run ``package -f arch`` on it;
    returns the packages by name."""
    base.mkdir(exist_ok=True)
    tree = _example_source(base / "work")
    project = Project.from_config(
        tree / "pkgforge.cfg", buildroot=base / "root", db=base / "files.jsonl"
    )
    project.stage(init=True)
    paths = project.package("arch", base / "out")
    return {path.name.rsplit("-", 3)[0]: path for path in paths}


def _pacman_info(package: Path) -> typing.Dict[str, str]:
    """``pacman -Qip`` of ``package`` as ``{field: value}``."""
    out = subprocess.run(
        ["pacman", "-Qip", str(package)], capture_output=True, text=True, check=True
    ).stdout
    fields = {}
    for line in out.splitlines():
        key, sep, value = line.partition(" : ")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


@requires_pacman
def test_pacman_reads_example(tmp_path):
    """pacman reads both packages: name, version, architecture and the install
    script of ``hello`` only."""
    packages = _build_direct_arch(tmp_path)
    assert sorted(packages) == ["hello", "hello-doc"]
    hello = _pacman_info(packages["hello"])
    assert (hello["Name"], hello["Version"], hello["Architecture"]) == (
        "hello",
        "1.0-1",
        "any",
    )
    assert hello["Install Script"] == "Yes"
    doc = _pacman_info(packages["hello-doc"])
    assert (doc["Name"], doc["Version"], doc["Architecture"]) == (
        "hello-doc",
        "1.0-1",
        "any",
    )
    assert doc["Install Script"] == "No"


@requires_pacman
@requires_install
def test_pacman_example_installs(tmp_path):
    """Installing the package creates the account, owns ``/var/lib/hello`` and
    ``/run/hello`` by it, gives the config its group and sets the capability;
    ``pacman -Qkk`` then warns only about the two owners pacman cannot store."""
    package = _build_direct_arch(tmp_path)["hello"]
    try:
        _run_as_root("pacman", "-U", "--noconfirm", str(package))
        _assert_installed_example_state()
        assert os.stat("/run/hello").st_uid == pwd.getpwnam("hello").pw_uid
        conf = os.stat("/etc/hello/hello.conf")
        assert grp.getgrgid(conf.st_gid).gr_name == "adm"
        check = _run_as_root("pacman", "-Qkk", "hello", check=False)
        warned = {
            match.group(1).lstrip("/"): match.group(2)
            for match in re.finditer(
                r"^warning: hello: (\S+) \((.*)\)$", check.stdout + check.stderr, re.M
            )
        }
        assert set(warned) == {"var/lib/hello", "etc/hello/hello.conf"}, (
            check.stdout + check.stderr
        )
        assert all("mismatch" in text for text in warned.values()), warned
    finally:
        _run_as_root("pacman", "-R", "--noconfirm", "hello", check=False)


# --------------------------------------------------------------------------
# package -f apk: the Alpine package pkgforge writes itself
# --------------------------------------------------------------------------

APK = shutil.which("apk")
requires_apk = pytest.mark.skipif(APK is None, reason="apk not installed")


@requires_apk
@requires_install
def test_apk_example_installs(tmp_path):
    """Installing the package creates the account before its files are
    extracted, owns ``/var/lib/hello`` and ``/run/hello`` by it, gives the
    config its group and sets the capability; ``apk audit`` then reports none
    of the package's paths."""
    tree = _example_source(tmp_path / "work")
    project = Project.from_config(
        tree / "pkgforge.cfg", buildroot=tmp_path / "root", db=tmp_path / "files.jsonl"
    )
    project.stage(init=True)
    package = project.package("apk", tmp_path / "out")[0]
    assert package.name == "hello-1.0-r1.apk"
    with tarfile.open(package) as archive:
        packaged = {
            member.name
            for member in archive
            if "hello" in member.name or member.name == "usr/bin/hi"
        }
    assert "usr/bin/hello" in packaged
    try:
        _run_as_root("apk", "add", "--allow-untrusted", str(package))
        _assert_installed_example_state()
        assert os.stat("/run/hello").st_uid == pwd.getpwnam("hello").pw_uid
        assert stat.S_IMODE(os.stat("/run/hello").st_mode) == 0o755
        conf = os.stat("/etc/hello/hello.conf")
        assert grp.getgrgid(conf.st_gid).gr_name == "adm"
        # setcap in the post-install script gives the helper an extended
        # attribute the apk database does not hold, which audit reports as `x`.
        audit = _run_as_root("apk", "audit", "--system", check=False)
        reported = {
            line.partition(" ")[2] for line in audit.stdout.splitlines() if line
        }
        reported -= {"usr/libexec/hello/helper"}
        assert not reported & packaged, audit.stdout + audit.stderr
    finally:
        _run_as_root("apk", "del", "hello", check=False)
