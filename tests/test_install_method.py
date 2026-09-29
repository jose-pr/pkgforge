"""Tests for ``install --method {copy,link,move}`` (``PKGFORGE_INSTALL_METHOD``).

The plain default (``copy``) path is already covered throughout
``tests/test_install.py``; every test here exercises ``link`` or ``move``, or
the option's own parsing/env plumbing. Tests needing POSIX facilities
(hardlinks, ``/dev/shm``) are marked ``@pytest.mark.posix``.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

import pkgforge
from pkgforge.install import Install

# --------------------------------------------------------------------------
# link
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_method_link_shares_inode(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "a.txt"
    src.write_text("hello")

    Install._parser_().parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "--method",
            "link",
            str(src),
            "/opt",
        ]
    )()

    staged = root / "opt" / "a.txt"
    assert staged.read_text() == "hello"
    assert staged.stat().st_ino == src.stat().st_ino


@pytest.mark.posix
def test_method_link_falls_back_across_devices(tmp_path, caplog):
    shm = Path("/dev/shm")
    if not shm.is_dir():
        pytest.skip("/dev/shm not available")
    if os.stat(shm).st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("/dev/shm is on the same filesystem as tmp_path")

    root = Path(tempfile.mkdtemp(dir=shm))
    try:
        src = tmp_path / "a.txt"
        src.write_text("hello")

        with caplog.at_level("WARNING", logger="pkgforge.install"):
            Install._parser_().parse_args(
                [
                    "--db",
                    str(tmp_path / "files.jsonl"),
                    "--buildroot",
                    str(root),
                    "-p",
                    "--method",
                    "link",
                    str(src),
                    "/opt",
                ]
            )()

        staged = root / "opt" / "a.txt"
        # A hardlink across /dev/shm <-> tmp_path fails EXDEV; the fallback
        # copy still stages the content, as a distinct inode.
        assert staged.read_text() == "hello"
        assert staged.stat().st_ino != src.stat().st_ino
        assert any(
            "link" in r.message and "copying" in r.message for r in caplog.records
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# move
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_method_move_consumes_source(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "a.txt"
    src.write_text("hello")

    Install._parser_().parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "--method",
            "move",
            str(src),
            "/opt",
        ]
    )()

    staged = root / "opt" / "a.txt"
    assert staged.read_text() == "hello"
    assert not src.exists()


@pytest.mark.posix
def test_method_move_rolls_back_on_failure(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "a.txt"
    src.write_text("hello")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("forced apply_entry failure")

    monkeypatch.setattr("pkgforge.install.apply_entry", _boom)

    inst = Install._parser_().parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "--method",
            "move",
            str(src),
            "/opt",
        ]
    )
    with pytest.raises(RuntimeError, match="forced apply_entry failure"):
        inst()

    # The source is restored and nothing usable was left at the destination.
    assert src.read_text() == "hello"
    assert not (root / "opt" / "a.txt").exists()


# --------------------------------------------------------------------------
# option plumbing: env var, CLI precedence, bad value, scope
# --------------------------------------------------------------------------


def test_method_env_var(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "a.txt"
    src.write_text("hello")
    monkeypatch.setenv("PKGFORGE_INSTALL_METHOD", "move")

    rc = pkgforge.main(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "install",
            "-p",
            str(src),
            "/opt",
        ]
    )

    assert rc == 0
    assert not src.exists()
    assert (root / "opt" / "a.txt").read_text() == "hello"


def test_method_cli_beats_env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "a.txt"
    src.write_text("hello")
    monkeypatch.setenv("PKGFORGE_INSTALL_METHOD", "move")

    rc = pkgforge.main(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "--method",
            "copy",
            str(src),
            "/opt",
        ]
    )

    assert rc == 0
    assert src.exists()  # copy never touches the source
    assert (root / "opt" / "a.txt").read_text() == "hello"


def test_method_bad_value_exits_2(tmp_path, cli):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "a.txt"
    src.write_text("hello")

    result = cli(
        "--db",
        str(tmp_path / "files.jsonl"),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "--method",
        "bogus",
        str(src),
        "/opt",
    )
    assert result.rc == 2
    # Not just any exit-2 argparse error (e.g. an unrecognized --method
    # flag): argparse's own choices check for the declared field.
    assert b"invalid choice" in result.err
    assert not (root / "opt").exists()


def test_method_ignored_for_stdin_and_archive(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()

    # A '-' (stdin) source has no source PATH to consume in the first place.
    fh = io.BytesIO(b"stdin content")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(fh))
    Install._parser_().parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "--method",
            "move",
            "-T",
            "-",
            "out",
        ]
    )()
    assert (root / "out").read_bytes() == b"stdin content"

    # An archive source is extracted fresh; --method move never touches it.
    archive = tmp_path / "data.tar"
    with tarfile.open(archive, "w") as tf:
        data = b"archived"
        info = tarfile.TarInfo("f")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    Install._parser_().parse_args(
        [
            "--db",
            str(tmp_path / "files2.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "--method",
            "move",
            str(archive),
            "/opt",
        ]
    )()
    assert archive.exists()
    assert (root / "opt" / "data" / "f").read_bytes() == b"archived"


# --------------------------------------------------------------------------
# directory sources
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_method_link_tree_shares_inodes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "tree"
    src.mkdir()
    (src / "f").write_text("f-content")
    (src / "sub").mkdir()
    (src / "sub" / "g").write_text("g-content")

    Install._parser_().parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            "--method",
            "link",
            str(src),
            "/opt/app",
        ]
    )()

    staged = root / "opt" / "app"
    assert (staged / "f").stat().st_ino == (src / "f").stat().st_ino
    assert (staged / "sub" / "g").stat().st_ino == (src / "sub" / "g").stat().st_ino


@pytest.mark.posix
def test_method_move_tree_rename_fast_path(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "tree"
    src.mkdir()
    (src / "f").write_text("f-content")
    f_ino = (src / "f").stat().st_ino

    Install._parser_().parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            "--method",
            "move",
            str(src),
            "/opt/app",
        ]
    )()

    # No pre-existing destination and no --exclude: the whole tree is
    # consumed by a single rename, so the file inside keeps its own inode.
    assert not src.exists()
    staged = root / "opt" / "app"
    assert (staged / "f").stat().st_ino == f_ino


@pytest.mark.posix
def test_method_move_tree_merge_keeps_excluded(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "tree"
    src.mkdir()
    (src / "keep.txt").write_text("keep")
    (src / "skip.pyc").write_text("skip")
    (src / "sub").mkdir()
    (src / "sub" / "moved.txt").write_text("moved")
    (src / "sub2").mkdir()
    (src / "sub2" / "skip2.pyc").write_text("skip2")

    dest_dir = root / "opt" / "app"
    dest_dir.mkdir(parents=True)  # pre-existing dst forces the merge path

    Install._parser_().parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            "--method",
            "move",
            "-X",
            "**/*.pyc",
            str(src),
            "/opt/app",
        ]
    )()

    # Non-excluded files (and directories left empty by moving them) reach
    # the destination and are gone from the source...
    assert (dest_dir / "keep.txt").read_text() == "keep"
    assert (dest_dir / "sub" / "moved.txt").read_text() == "moved"
    assert not (src / "keep.txt").exists()
    assert not (src / "sub").exists()

    # ...but an excluded file, and the directory holding it, stay behind in
    # the source; copytree still creates the (now empty) destination
    # counterpart of that directory, same as a plain --exclude copy would.
    assert not (dest_dir / "skip.pyc").exists()
    assert (src / "skip.pyc").read_text() == "skip"
    assert (dest_dir / "sub2").is_dir()
    assert list((dest_dir / "sub2").iterdir()) == []
    assert (src / "sub2" / "skip2.pyc").read_text() == "skip2"


@pytest.mark.posix
def test_method_move_tree_rolls_back_on_failure(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "tree"
    src.mkdir()
    (src / "f").write_text("f-content")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("forced apply_entry failure")

    monkeypatch.setattr("pkgforge.install.apply_entry", _boom)

    inst = Install._parser_().parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            "--method",
            "move",
            str(src),
            "/opt/app",
        ]
    )
    with pytest.raises(RuntimeError, match="forced apply_entry failure"):
        inst()

    assert (src / "f").read_text() == "f-content"
    assert not (root / "opt" / "app").exists()
