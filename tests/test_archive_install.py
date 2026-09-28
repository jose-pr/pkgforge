"""Tests for archive and directory install: extraction safety and re-runs.

Every regression test for the archive/directory-install security-review pass
lands here (``tests/test_install.py`` keeps the tests that predate it).
Tests that need POSIX facilities (symlinks, bsdtar, chmod/chown, device
nodes) are marked ``@pytest.mark.posix``; root-only tests are additionally
gated on ``os.geteuid() == 0`` and must be run as root (e.g. in the CI
``glibc-floor`` container, or a local root shell) to actually execute --
they are silently skipped on a non-root runner.
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from pkgforge.common import PkgForgeError
from pkgforge.install import BSDTAR_EXTRACT_FLAGS, Install, _extract_bsdtar

pytestmark = pytest.mark.posix


# --------------------------------------------------------------------------
# Re-running a directory install whose source holds symlinks
# --------------------------------------------------------------------------


def _make_symlink_tree(base: Path) -> Path:
    tree = base / "tree"
    tree.mkdir()
    (tree / "f").write_text("f-content")
    (tree / "sub").mkdir()
    (tree / "sub" / "g").write_text("g-content")
    (tree / "link").symlink_to("f")
    (tree / "dlink").symlink_to("sub")
    (tree / "dangling").symlink_to("no-such-target")
    return tree


def test_install_dir_rerun_with_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src = _make_symlink_tree(tmp_path)

    common = ["--db", str(db), "--buildroot", str(root), "-p", "-d", "-D"]
    Install._parser_().parse_args(common + [str(src), "/opt/app"])()

    staged = root / "opt" / "app"
    assert os.readlink(staged / "link") == "f"
    assert os.readlink(staged / "dlink") == "sub"
    assert os.readlink(staged / "dangling") == "no-such-target"

    inst = Install._parser_().parse_args(common + ["-m", "750", str(src), "/opt/app"])
    inst()

    assert os.readlink(staged / "link") == "f"
    assert os.readlink(staged / "dlink") == "sub"
    assert (staged.stat().st_mode & 0o777) == 0o750

    lines = db.read_text().splitlines()
    assert len(lines) == 2


def test_install_dir_rerun_retargets_link(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src = tmp_path / "tree"
    src.mkdir()
    (src / "f").write_text("f")
    (src / "sub").mkdir()
    (src / "sub" / "g").write_text("g")
    (src / "link").symlink_to("f")

    common = ["--db", str(db), "--buildroot", str(root), "-p", "-d", "-D"]
    Install._parser_().parse_args(common + [str(src), "/opt/app"])()
    staged = root / "opt" / "app"
    assert os.readlink(staged / "link") == "f"

    (src / "link").unlink()
    (src / "link").symlink_to("sub/g")
    Install._parser_().parse_args(common + [str(src), "/opt/app"])()
    assert os.readlink(staged / "link") == "sub/g"


def test_install_dir_does_not_write_through_stale_dst_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src = tmp_path / "tree"
    src.mkdir()
    (src / "f").write_text("new-content")

    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    staged_dir = root / "opt" / "app"
    staged_dir.mkdir(parents=True)
    (staged_dir / "f").symlink_to(outside)

    Install._parser_().parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            str(src),
            "/opt/app",
        ]
    )()

    assert outside.read_text() == "original"
    assert not (staged_dir / "f").is_symlink()
    assert (staged_dir / "f").read_text() == "new-content"


def test_install_dir_rerun_leaves_excluded_dst_entry(tmp_path):
    # Guard: an excluded name's destination entry (here, a leftover
    # symlink) must never be touched by _copy_ignore's stale-link cleanup.
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src = tmp_path / "tree"
    src.mkdir()
    (src / "keep.txt").write_text("keep")
    (src / "link.txt").symlink_to("keep.txt")

    staged_dir = root / "opt" / "app"
    staged_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    (staged_dir / "link.txt").symlink_to(outside)

    Install._parser_().parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            "-X",
            "link.txt",
            str(src),
            "/opt/app",
        ]
    )()

    assert (staged_dir / "link.txt").is_symlink()
    assert os.readlink(staged_dir / "link.txt") == str(outside)
    assert outside.read_text() == "untouched"


# --------------------------------------------------------------------------
# The bsdtar fallback never keeps bsdtar's own root defaults
# --------------------------------------------------------------------------


def _tar_bytes(build) -> bytes:
    """Build tar-format bytes via build(tf), a callback that adds
    members to the open :class:`tarfile.TarFile`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        build(tf)
    return buf.getvalue()


def test_bsdtar_argv_carries_policy_flags(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bsdtar")
    monkeypatch.setattr(subprocess, "run", fake_run)

    dst = tmp_path / "dst"
    dst.mkdir()
    _extract_bsdtar(Path("archive.bin"), dst)

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "bsdtar"
    assert "-x" in argv
    for flag in BSDTAR_EXTRACT_FLAGS:
        assert flag in argv
    assert argv[argv.index("-C") + 1] == str(dst)
    assert argv[argv.index("-f") + 1] == "archive.bin"


@pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not available")
def test_bsdtar_rejects_fifo_member(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / "payload.bin"

    def _build(tf):
        ti = tarfile.TarInfo(name="p")
        ti.type = tarfile.FIFOTYPE
        ti.mode = 0o644
        tf.addfile(ti)

    archive.write_bytes(_tar_bytes(_build))

    inst = Install._parser_().parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            str(archive),
            "/opt/app",
        ]
    )
    with pytest.raises(PkgForgeError, match="fifo"):
        inst()

    assert not (root / "opt" / "app").exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="root-only")
@pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not available")
def test_bsdtar_as_root_drops_owner_and_special_bits(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / "payload.bin"

    def _build(tf):
        suid = tarfile.TarInfo(name="suid-bin")
        suid.mode = 0o4755
        suid.uid = 4321
        suid.gid = 4321
        tf.addfile(suid, io.BytesIO(b""))

        world = tarfile.TarInfo(name="world-writable")
        world.mode = 0o666
        world.uid = 4321
        world.gid = 4321
        tf.addfile(world, io.BytesIO(b""))

    archive.write_bytes(_tar_bytes(_build))

    Install._parser_().parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            str(archive),
            "/opt/app",
        ]
    )()

    staged = root / "opt" / "app"
    suid_st = os.stat(staged / "suid-bin")
    assert suid_st.st_uid == 0
    assert not (suid_st.st_mode & stat.S_ISUID)
    world_st = os.stat(staged / "world-writable")
    assert not (world_st.st_mode & stat.S_IWOTH)


@pytest.mark.skipif(os.geteuid() != 0, reason="root-only")
@pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not available")
def test_bsdtar_as_root_rejects_char_device(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / "payload.bin"

    def _build(tf):
        dev = tarfile.TarInfo(name="null")
        dev.type = tarfile.CHRTYPE
        dev.devmajor = 1
        dev.devminor = 3
        dev.mode = 0o666
        tf.addfile(dev)

    archive.write_bytes(_tar_bytes(_build))

    inst = Install._parser_().parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            str(archive),
            "/opt/app",
        ]
    )
    with pytest.raises(PkgForgeError, match="null"):
        inst()

    assert not (root / "opt" / "app").exists()
    assert inst.loaddb() == {}


# --------------------------------------------------------------------------
# Tar extraction filter: absolute/climbing symlinks kept, escapes refused,
# re-extraction stays idempotent
# --------------------------------------------------------------------------


def _write_tar(path: Path, build) -> None:
    with tarfile.open(path, mode="w") as tf:
        build(tf)


def _add_file(tf, name, data=b"x", mode=0o644):
    ti = tarfile.TarInfo(name=name)
    ti.size = len(data)
    ti.mode = mode
    tf.addfile(ti, io.BytesIO(data))


def _add_symlink(tf, name, target):
    ti = tarfile.TarInfo(name=name)
    ti.type = tarfile.SYMTYPE
    ti.linkname = target
    tf.addfile(ti)


def _add_hardlink(tf, name, target):
    ti = tarfile.TarInfo(name=name)
    ti.type = tarfile.LNKTYPE
    ti.linkname = target
    tf.addfile(ti)


def test_tar_keeps_absolute_and_climbing_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / "pkg.tar"

    def _build(tf):
        _add_symlink(tf, "lib/abs", "/usr/lib/libfoo.so.1")
        _add_symlink(tf, "lib/climb", "../../../../usr/lib/libfoo.so.1")

    _write_tar(archive, _build)

    common = ["--db", str(db), "--buildroot", str(root), "-p", "-d", "-D"]
    for _ in range(2):
        Install._parser_().parse_args(common + [str(archive), "/opt/app"])()
        staged = root / "opt" / "app"
        assert os.readlink(staged / "lib" / "abs") == "/usr/lib/libfoo.so.1"
        assert (
            os.readlink(staged / "lib" / "climb") == "../../../../usr/lib/libfoo.so.1"
        )


@pytest.mark.parametrize(
    "kind",
    [
        "dotdot",
        "outside_dirlink",
        "inside_dirlink",
        "outside_hardlink",
        "absolute_hardlink",
    ],
)
def test_tar_rejects_member_escape(tmp_path, kind):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / "pkg.tar"
    outside = tmp_path / "outside.txt"
    outside.write_text("original")

    if kind == "dotdot":

        def _build(tf):
            _add_file(tf, "../../../escaped.txt", b"pwned")

    elif kind == "outside_dirlink":

        def _build(tf):
            _add_symlink(tf, "e", str(tmp_path))
            _add_file(tf, "e/outside.txt", b"pwned")

    elif kind == "inside_dirlink":

        def _build(tf):
            ti = tarfile.TarInfo(name="sub")
            ti.type = tarfile.DIRTYPE
            ti.mode = 0o755
            tf.addfile(ti)
            _add_symlink(tf, "dl", "sub")
            _add_file(tf, "dl/x", b"data")

    elif kind == "outside_hardlink":

        def _build(tf):
            _add_hardlink(tf, "h", "../../../outside.txt")

    else:  # absolute_hardlink

        def _build(tf):
            _add_hardlink(tf, "h", "/outside.txt")

    _write_tar(archive, _build)

    inst = Install._parser_().parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            str(archive),
            "/opt/app",
        ]
    )
    with pytest.raises(PkgForgeError):
        inst()

    assert outside.read_text() == "original"


def test_tar_absolute_hardlink_to_member(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / "pkg.tar"
    # An absolute-looking hardlink target names ANOTHER ARCHIVE MEMBER by
    # its own (relative) name, not a real host path -- "/a" refers to the
    # member "a" once the leading "/" is stripped, and must be accepted.

    def _build(tf):
        _add_file(tf, "a", b"content")
        _add_hardlink(tf, "h", "/a")

    _write_tar(archive, _build)

    Install._parser_().parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            str(archive),
            "/opt/app",
        ]
    )()

    staged = root / "opt" / "app"
    assert (staged / "h").read_text() == "content"
    assert os.stat(staged / "h").st_ino == os.stat(staged / "a").st_ino


def test_tar_special_file_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / "pkg.tar"

    def _build(tf):
        ti = tarfile.TarInfo(name="p")
        ti.type = tarfile.FIFOTYPE
        tf.addfile(ti)

    _write_tar(archive, _build)

    inst = Install._parser_().parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            str(archive),
            "/opt/app",
        ]
    )
    with pytest.raises(PkgForgeError, match="p"):
        inst()
    assert not (root / "opt" / "app").exists()


def test_extract_tar_refuses_without_filter(tmp_path, monkeypatch):
    import pkgforge.install as install_mod

    monkeypatch.setattr(install_mod, "_TARFILE_HAS_FILTER", False)
    archive = tmp_path / "pkg.tar"
    _write_tar(archive, lambda tf: _add_file(tf, "a", b"x"))
    dst = tmp_path / "dst"
    dst.mkdir()

    with pytest.raises(PkgForgeError, match="extraction filter"):
        install_mod._extract_tar(archive, dst)
    assert not (dst / "a").exists()


def test_tar_routes_to_bsdtar_without_filter(tmp_path, monkeypatch):
    import pkgforge.install as install_mod

    monkeypatch.setattr(install_mod, "_TARFILE_HAS_FILTER", False)
    calls = []
    monkeypatch.setattr(
        install_mod, "_extract_bsdtar", lambda src, dst: calls.append((src, dst))
    )

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / "pkg.tar"
    _write_tar(archive, lambda tf: _add_file(tf, "a", b"x"))

    Install._parser_().parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            str(archive),
            "/opt/app",
        ]
    )()

    assert len(calls) == 1
    assert calls[0][0] == archive


def test_tar_refused_without_filter_or_bsdtar(tmp_path, monkeypatch):
    import pkgforge.install as install_mod

    monkeypatch.setattr(install_mod, "_TARFILE_HAS_FILTER", False)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / "pkg.tar"
    _write_tar(archive, lambda tf: _add_file(tf, "a", b"x"))

    inst = Install._parser_().parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            str(archive),
            "/opt/app",
        ]
    )
    with pytest.raises(PkgForgeError, match="bsdtar"):
        inst()
    assert not (root / "opt" / "app").exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="root-only")
def test_tar_as_root_drops_owner(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / "pkg.tar"

    def _build(tf):
        ti = tarfile.TarInfo(name="owned")
        ti.size = 1
        ti.uid = 4321
        ti.gid = 4321
        tf.addfile(ti, io.BytesIO(b"x"))

    _write_tar(archive, _build)

    Install._parser_().parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            str(archive),
            "/opt/app",
        ]
    )()

    st = os.stat(root / "opt" / "app" / "owned")
    assert st.st_uid == 0


# --------------------------------------------------------------------------
# Archive-to-directory naming: an extracted archive drops its suffix,
# a directory source keeps its own name
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix",
    [
        ".tar",
        ".tar.gz",
        ".tgz",
        ".tar.bz2",
        ".tbz2",
        ".tbz",
        ".tar.xz",
        ".txz",
        ".TAR.GZ",
        ".iso",
        ".zip",
        ".tar.zst",
    ],
)
def test_archive_dir_name(suffix):
    from pkgforge.install import _archive_dir_name

    assert _archive_dir_name(f"foo-1.0{suffix}") == "foo-1.0"


def test_archive_dir_name_bare_suffix_unchanged():
    # A source literally named ".tgz" (nothing before the suffix) keeps its
    # name -- never returns an empty string.
    from pkgforge.install import _archive_dir_name

    assert _archive_dir_name(".tgz") == ".tgz"


@pytest.mark.parametrize("suffix", [".tgz", ".tbz2", ".tbz", ".txz", ".TAR.GZ", ".Tgz"])
def test_install_archive_strips_suffix(tmp_path, suffix):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / f"foo-1.0{suffix}"

    def _build(tf):
        _add_file(tf, "x", b"data")

    _write_tar(archive, _build)

    inst = Install._parser_().parse_args(
        ["--db", str(db), "--buildroot", str(root), "-p", "-d", str(archive), "/opt"]
    )
    inst()

    assert (root / "opt" / "foo-1.0" / "x").read_bytes() == b"data"
    assert "/opt/foo-1.0" in inst.loaddb()


@pytest.mark.parametrize("suffix", [".tar", ".tar.gz", ".tar.bz2", ".tar.xz"])
def test_install_archive_strips_suffix_guard(tmp_path, suffix):
    # Guard: the old loop already stripped a literal .tar/.tar.gz/.tar.bz2/
    # .tar.xz suffix before this fix.
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / f"foo-1.0{suffix}"

    def _build(tf):
        _add_file(tf, "x", b"data")

    _write_tar(archive, _build)

    Install._parser_().parse_args(
        ["--db", str(db), "--buildroot", str(root), "-p", "-d", str(archive), "/opt"]
    )()

    assert (root / "opt" / "foo-1.0" / "x").read_bytes() == b"data"


def test_install_directory_source_keeps_dotted_name(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src = tmp_path / "conf.tar.d"
    src.mkdir()
    (src / "a").write_text("x")

    Install._parser_().parse_args(
        ["--db", str(db), "--buildroot", str(root), "-p", "-d", str(src), "/etc"]
    )()

    assert (root / "etc" / "conf.tar.d" / "a").read_text() == "x"


def test_install_tar_routes_through_tarfile_guard(tmp_path, monkeypatch):
    # Guard: a plain .tar.gz source still goes through the stdlib tarfile
    # path, not bsdtar, after the naming rework.
    import pkgforge.install as install_mod

    calls = []
    monkeypatch.setattr(install_mod, "_extract_tar", lambda src, dst: calls.append(src))
    monkeypatch.setattr(
        install_mod,
        "_extract_bsdtar",
        lambda src, dst: pytest.fail("must not route through bsdtar"),
    )

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    archive = tmp_path / "foo-1.0.tar.gz"
    _write_tar(archive, lambda tf: _add_file(tf, "x", b"data"))

    Install._parser_().parse_args(
        ["--db", str(db), "--buildroot", str(root), "-p", "-d", str(archive), "/opt"]
    )()

    assert calls == [archive]


@pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not available")
def test_install_zip_via_bsdtar(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src_dir = tmp_path / "bundle"
    src_dir.mkdir()
    (src_dir / "a").write_text("data")
    archive = tmp_path / "bundle.zip"
    subprocess.run(
        ["bsdtar", "-c", "-f", str(archive), "-C", str(src_dir), "a"],
        check=True,
    )

    inst = Install._parser_().parse_args(
        ["--db", str(db), "--buildroot", str(root), "-p", "-d", str(archive), "/opt"]
    )
    inst()

    assert (root / "opt" / "bundle" / "a").read_text() == "data"
    assert "/opt/bundle" in inst.loaddb()


@pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not available")
def test_install_archive_from_stdin_via_bsdtar_guard(tmp_path):
    # Guard: -x/-T stdin archive install already worked through bsdtar
    # before this fix; naming a stdin destination never goes through
    # _archive_dir_name at all (it always needs an explicit -T name).
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src_dir = tmp_path / "bundle"
    src_dir.mkdir()
    (src_dir / "a").write_text("data")
    archive = tmp_path / "bundle.tar"
    subprocess.run(
        ["bsdtar", "-c", "-f", str(archive), "-C", str(tmp_path), "bundle"],
        check=True,
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pkgforge",
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-d",
            "-T",
            "-",
            "/opt/x",
        ],
        input=archive.read_bytes(),
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert (root / "opt" / "x" / "bundle" / "a").read_text() == "data"


# --------------------------------------------------------------------------
# A directory copy skips the build root, destination and DB inside it
# --------------------------------------------------------------------------


def test_install_dir_containing_buildroot_skips_it(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.txt").write_text("A")
    (proj / "build" / "root" / "usr" / "bin").mkdir(parents=True)
    (proj / "build" / "root" / "usr" / "bin" / "tool").write_text("tool")
    monkeypatch.chdir(proj)

    from pkgforge.initdb import InitDb

    InitDb._parser_().parse_args(["--db", "files.jsonl"])()
    assert Path("files.jsonl").exists()

    inst = Install._parser_().parse_args(
        [
            "--buildroot",
            "build/root",
            "--db",
            "files.jsonl",
            "-p",
            "-T",
            "-d",
            ".",
            "/opt/app",
        ]
    )
    inst()

    staged = Path("build/root/opt/app")
    assert (staged / "src" / "a.txt").read_text() == "A"
    assert (staged / "build").is_dir()
    assert list((staged / "build").iterdir()) == []
    assert not (staged / "files.jsonl").exists()
    assert "/opt/app" in inst.loaddb()


def test_install_dir_containing_buildroot_with_exclude(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.txt").write_text("A")
    (proj / "src" / "b.pyc").write_text("compiled")
    (proj / "build" / "root").mkdir(parents=True)
    monkeypatch.chdir(proj)

    Install._parser_().parse_args(
        [
            "--buildroot",
            "build/root",
            "--db",
            "files.jsonl",
            "-p",
            "-T",
            "-d",
            "-X",
            "**/*.pyc",
            ".",
            "/opt/app",
        ]
    )()

    staged = Path("build/root/opt/app")
    assert (staged / "src" / "a.txt").read_text() == "A"
    assert not (staged / "src" / "b.pyc").exists()
    assert (staged / "build").is_dir()
    assert list((staged / "build").iterdir()) == []
