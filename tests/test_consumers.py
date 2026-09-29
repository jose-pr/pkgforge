"""Feeds dbdump's output to the real packaging consumers it targets:
``rpmbuild`` for ``rpmspecfiles``, ``dh_install``/``dh_installdirs`` for
``debian``. Skipped outright when the tool isn't on ``PATH``; not collected
by the plain unit-test run, but not excluded from it either -- these tests
are cheap when skipped.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import subprocess
import tarfile
import textwrap
from pathlib import Path

import pytest

from conftest import skip_if_fs_rejects_non_utf8

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
def test_rpmbuild_other_format_never_silently_wrong(tmp_path, cli):
    # A format used with the wrong rpm may fail the build, but must never
    # silently package a sibling (overmatch) or the wrong file (wrong).
    # For each class the format this host does NOT match supports,
    # build with that other format anyway and check the outcome.
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

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    build_script = (
        "dh_fixperms && "
        f"sh debian/fixperms debian/{pkg} && "
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

    vartool = members["var/lib/tool"]
    assert vartool.isdir()
    assert (vartool.uname, vartool.gname) == ("daemon", "adm")
    assert _mode("var/lib/tool") == 0o750
