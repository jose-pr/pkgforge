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

import os
import tarfile
from pathlib import Path

import pytest

from pkgforge.install import Install

pytestmark = pytest.mark.posix


# --------------------------------------------------------------------------
# C105: re-running a directory install whose source holds symlinks
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
