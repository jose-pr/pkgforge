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
import tempfile
from pathlib import Path

import pytest

from pkgforge.install import Install


@pytest.mark.posix
def test_install_file_copies_and_records(tmp_path):
    # F06: install copies file sources; it no longer hardlinks them.
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
    # F06: rewriting the source in place after install must not change the
    # already-staged copy (the old os.link behavior shared one inode).
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
    # F06: os.link failed with EXDEV when the build root is on another
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
    # F06: os.link raised EPERM under fs.protected_hardlinks for a source the
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
