"""Tests for the ``install`` subcommand.

Every new ``install`` regression test from the staging-semantics rework lands
here (``tests/test_pkgforge.py`` keeps the tests that predate it). Tests that
need POSIX facilities (chmod, chown, symlinks, subprocess decompressors) are
marked ``@pytest.mark.posix``; a handful that only exercise argument parsing
or in-memory state run on any platform and say so in a comment.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from pkgforge.install import Install


@pytest.mark.posix
def test_install_file_copies_and_records(tmp_path):
    # install copies file sources; it no longer hardlinks them.
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src = tmp_path / "app.conf"
    src.write_text("hello")
    os.chmod(src, 0o644)

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-m",
            "640",
            str(src),
            "/etc",
        ]
    )
    inst()

    staged = root / "etc" / "app.conf"
    assert staged.exists()
    # Not a hardlink: a distinct inode from the source.
    assert staged.stat().st_ino != src.stat().st_ino
    # -m never touches the source.
    assert (src.stat().st_mode & 0o777) == 0o644
    assert (staged.stat().st_mode & 0o777) == 0o640
    recorded = inst.loaddb()
    assert recorded["/etc/app.conf"]["mode"] == "640"


@pytest.mark.posix
def test_install_file_source_rewrite_keeps_staged(tmp_path):
    # Rewriting the source in place after install must not change the
    # already-staged copy (a hardlink would share the source's inode).
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src = tmp_path / "tool.conf"
    src.write_text("v1")

    parser = Install._parser_()
    parser.parse_args(
        ["--db", str(db), "--buildroot", str(root), "-p", str(src), "/etc"]
    )()

    src.write_text("v2-rebuilt")
    staged = root / "etc" / "tool.conf"
    assert staged.read_text() == "v1"


@pytest.mark.posix
def test_install_file_cross_filesystem_root(tmp_path):
    # A hardlink fails with EXDEV when the build root is on another
    # filesystem (e.g. the documented PKGFORGE_ROOT=/tmp/stage on tmpfs);
    # shutil.copy2 works across filesystems.
    shm = Path("/dev/shm")
    if not shm.is_dir():
        pytest.skip("/dev/shm not available")
    if os.stat(shm).st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("/dev/shm is on the same filesystem as tmp_path")

    root = Path(tempfile.mkdtemp(dir=shm))
    try:
        db = tmp_path / "files.jsonl"
        src = tmp_path / "app.bin"
        src.write_text("payload")

        parser = Install._parser_()
        parser.parse_args(
            ["--db", str(db), "--buildroot", str(root), "-p", str(src), "/opt"]
        )()

        staged = root / "opt" / "app.bin"
        assert staged.read_text() == "payload"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.posix
def test_install_non_owned_source(tmp_path):
    # A hardlink raised EPERM under fs.protected_hardlinks for a source the
    # user does not own; a copy never touches the source's ownership at all.
    candidate = None
    for name in ("true", "false", "env", "sh"):
        path = shutil.which(name)
        if path is None:
            continue
        st = os.stat(path)
        if st.st_uid != os.getuid():
            candidate = Path(path)
            break
    if candidate is None:
        pytest.skip("no non-owned, readable source found on PATH")

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"

    parser = Install._parser_()
    parser.parse_args(
        ["--db", str(db), "--buildroot", str(root), "-p", str(candidate), "/opt"]
    )()

    staged = root / "opt" / candidate.name
    assert staged.exists()
    assert staged.read_bytes() == candidate.read_bytes()


# --------------------------------------------------------------------------
# Direct Python-API construction must not silently enable decompress
# --------------------------------------------------------------------------


def test_install_direct_construction_does_not_decompress_path():
    # Cross-platform: constructing Install() directly (not through the
    # parser) with no `decompress=` must keep the documented default
    # (False), not silently infer decompression the way a bare CLI -x does.
    inst = Install(source=Path("a.txt"), destination=Path("opt"))
    assert inst.decompress is False


def test_install_direct_construction_does_not_decompress_list():
    inst = Install(source=[Path("a.txt")], destination=Path("opt"))
    assert inst.decompress is False


@pytest.mark.posix
def test_install_direct_construction_never_runs_source(tmp_path):
    # Before the fix, leaving `decompress` out made __call__ infer a
    # decompressor from the source's suffix and run it as a subprocess --
    # for a `.sh` source that means executing it. Guard both the bare-Path
    # and the list form.
    root = tmp_path / "root"
    root.mkdir()
    marker = tmp_path / "marker"
    src = tmp_path / "hello.sh"
    src.write_text(f"#!/bin/sh\ntouch {marker}\necho ran\n")
    src.chmod(src.stat().st_mode | stat.S_IEXEC)

    for source in (src, [src]):
        if marker.exists():
            marker.unlink()
        Install(
            source=source,
            destination=Path("/usr/bin"),
            buildroot=root,
            parents=True,
            db=None,
        )()
        assert not marker.exists()
        staged = root / "usr" / "bin" / "hello.sh"
        assert staged.read_bytes() == src.read_bytes()
