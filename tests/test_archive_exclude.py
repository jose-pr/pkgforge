"""Tests for ``install -X`` filtering an archive source's members: the
post-extraction prune (:func:`pkgforge.install.archive._prune_excluded`)
applied to both extraction routes (stdlib ``tarfile`` and ``bsdtar``)
before anything is merged or renamed onto DESTINATION.

Every test here is ``@pytest.mark.posix``. A ``[route]`` id of ``tarfile``
needs PEP 706's extraction filter (:data:`requires_tar_filter`, redeclared
here rather than imported, matching ``tests/test_archive_install.py``); a
``bsdtar`` id skips without the ``bsdtar`` binary and is forced onto that
route by naming the archive ``payload.bin`` (a suffix
:func:`pkgforge.install.archive._is_tar_source` never recognizes as
tar-family, whatever the actual bytes inside).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from pkgforge.command import PkgForgeCmd
from pkgforge.exclude import PathMatch, PathMatchStmt
from pkgforge.install.archive import _prune_excluded

requires_tar_filter = pytest.mark.skipif(
    not hasattr(tarfile, "data_filter"),
    reason="this Python's tarfile has no extraction filter (PEP 706)",
)

needs_bsdtar = pytest.mark.skipif(
    shutil.which("bsdtar") is None, reason="bsdtar not available"
)

#: One `pytest.param` per extraction route, each carrying the skip/version
#: guard that route needs.
ROUTES = [
    pytest.param("tarfile", marks=requires_tar_filter),
    pytest.param("bsdtar", marks=needs_bsdtar),
]


def _archive_path(tmp_path: Path, route: str) -> Path:
    # A ".tar" name routes through stdlib tarfile; "payload.bin" is never
    # tar-family by suffix, so it always falls back to bsdtar, whatever
    # bytes are actually written there.
    return tmp_path / ("pkg.tar" if route == "tarfile" else "payload.bin")


def _tar_bytes(build) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        build(tf)
    return buf.getvalue()


def _write_archive(path: Path, build) -> None:
    path.write_bytes(_tar_bytes(build))


def _add_file(tf: tarfile.TarFile, name: str, data: bytes = b"x") -> None:
    ti = tarfile.TarInfo(name=name)
    ti.size = len(data)
    ti.mode = 0o644
    tf.addfile(ti, io.BytesIO(data))


def _add_dir(tf: tarfile.TarFile, name: str) -> None:
    ti = tarfile.TarInfo(name=name)
    ti.type = tarfile.DIRTYPE
    ti.mode = 0o755
    tf.addfile(ti)


def _add_hardlink(tf: tarfile.TarFile, name: str, target: str) -> None:
    ti = tarfile.TarInfo(name=name)
    ti.type = tarfile.LNKTYPE
    ti.linkname = target
    tf.addfile(ti)


# --------------------------------------------------------------------------
# the prune itself, through a real install
# --------------------------------------------------------------------------


@pytest.mark.posix
@pytest.mark.parametrize("route", ROUTES)
def test_archive_exclude_prunes_members(tmp_path, cli, route):
    archive = _archive_path(tmp_path, route)

    def _build(tf):
        _add_file(tf, "keep.txt", b"keep")
        _add_file(tf, "drop.la", b"la")
        _add_dir(tf, "tmp")
        _add_file(tf, "tmp/f", b"f")
        _add_dir(tf, "tmp/sub")
        _add_file(tf, "tmp/sub/g", b"g")

    _write_archive(archive, _build)

    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-d",
        "-D",
        "-X",
        "*.la",
        "-X",
        "(?type:directory)**/tmp",
        str(archive),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    staged = root / "opt" / "app"
    assert (staged / "keep.txt").read_text() == "keep"
    assert not (staged / "drop.la").exists()
    assert not (staged / "tmp").exists()

    recorded = set(PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb())
    assert "/opt/app" in recorded


@pytest.mark.posix
@pytest.mark.parametrize("route", ROUTES)
def test_archive_exclude_implicit_directory(tmp_path, cli, route):
    # A tar has no member at all for a directory unless one was added
    # explicitly -- here only "a/tmp/f" exists, so "a/tmp" is an implicit
    # directory the extraction itself creates. The prune walks the
    # extracted tree on disk, not the archive's own member list, so it
    # still finds and removes "a/tmp".
    archive = _archive_path(tmp_path, route)
    _write_archive(archive, lambda tf: _add_file(tf, "a/tmp/f", b"data"))

    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-d",
        "-D",
        "-X",
        "*.la",
        "-X",
        "(?type:directory)**/tmp",
        str(archive),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    staged = root / "opt" / "app"
    assert (staged / "a").is_dir()
    assert not (staged / "a" / "tmp").exists()


@pytest.mark.posix
@pytest.mark.parametrize("route", ROUTES)
def test_archive_exclude_keeps_hardlink_to_excluded_member(tmp_path, cli, route):
    archive = _archive_path(tmp_path, route)

    def _build(tf):
        _add_file(tf, "a/tmp/x", b"payload")
        _add_hardlink(tf, "h", "a/tmp/x")

    _write_archive(archive, _build)

    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-d",
        "-D",
        "-X",
        "**/tmp",
        str(archive),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    staged = root / "opt" / "app"
    assert not (staged / "a" / "tmp").exists()
    # h was hardlinked to the same inode as the now-pruned a/tmp/x: pruning
    # one directory entry never deletes the underlying data while another
    # link to it still exists.
    assert (staged / "h").read_text() == "payload"


@pytest.mark.posix
@pytest.mark.parametrize("route", ROUTES)
def test_archive_exclude_matches_install_path(tmp_path, cli, caplog, route):
    import logging

    archive = _archive_path(tmp_path, route)
    _write_archive(archive, lambda tf: _add_file(tf, "docs/readme.txt", b"hi"))

    db1 = tmp_path / "files1.jsonl"
    root1 = tmp_path / "root1"
    result1 = cli(
        "--db",
        str(db1),
        "--buildroot",
        str(root1),
        "install",
        "-p",
        "-d",
        "-D",
        "-X",
        "/opt/app/docs",
        str(archive),
        "/opt/app",
    )
    assert result1.rc == 0, result1.err.decode()
    assert not (root1 / "opt" / "app" / "docs").exists()

    db2 = tmp_path / "files2.jsonl"
    root2 = tmp_path / "root2"
    with caplog.at_level(logging.WARNING):
        result2 = cli(
            "--db",
            str(db2),
            "--buildroot",
            str(root2),
            "install",
            "-p",
            "-d",
            "-D",
            "-X",
            "/docs",
            str(archive),
            "/opt/app",
        )
    assert result2.rc == 0, result2.err.decode()
    assert (root2 / "opt" / "app" / "docs" / "readme.txt").read_text() == "hi"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "/docs" in warnings[0].message


@pytest.mark.posix
def test_archive_exclude_merge_keeps_existing_dest_entries(tmp_path, cli):
    archive = tmp_path / "pkg.tar"

    def _build(tf):
        _add_file(tf, "keep.txt", b"new")
        _add_file(tf, "drop.la", b"new-la")

    _write_archive(archive, _build)

    root = tmp_path / "root"
    dst = root / "opt" / "app"
    dst.mkdir(parents=True)
    (dst / "drop.la").write_text("old-la")
    (dst / "old.txt").write_text("old")

    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-d",
        "-D",
        "-X",
        "*.la",
        str(archive),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    assert (dst / "keep.txt").read_text() == "new"
    # The archive's own drop.la was pruned before the merge; the merge
    # never touches an existing destination entry outside what it copies.
    assert (dst / "drop.la").read_text() == "old-la"
    assert (dst / "old.txt").read_text() == "old"


@pytest.mark.posix
def test_archive_exclude_meta_sees_O(tmp_path, cli):
    archive = tmp_path / "pkg.tar"

    def _build(tf):
        _add_file(tf, "a.conf", b"x")
        _add_file(tf, "b.txt", b"y")

    _write_archive(archive, _build)

    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-d",
        "-D",
        "-O",
        "keep=1",
        "-X",
        "(?meta:keep=1)*.conf",
        str(archive),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    staged = root / "opt" / "app"
    assert not (staged / "a.conf").exists()
    assert (staged / "b.txt").exists()


@pytest.mark.posix
@needs_bsdtar
def test_archive_exclude_from_stdin(tmp_path):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    payload = _tar_bytes(
        lambda tf: (
            _add_file(tf, "keep.txt", b"keep"),
            _add_file(tf, "drop.la", b"drop"),
        )
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
            "-D",
            "-d",
            "-X",
            "*.la",
            "-",
            "/opt/x",
        ],
        input=payload,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode()

    staged = root / "opt" / "x"
    assert (staged / "keep.txt").read_text() == "keep"
    assert not (staged / "drop.la").exists()


@pytest.mark.posix
@pytest.mark.parametrize("route", ROUTES)
def test_archive_exclude_special_member_still_refused(tmp_path, cli, route):
    archive = _archive_path(tmp_path, route)

    def _build(tf):
        ti = tarfile.TarInfo(name="p.fifo")
        ti.type = tarfile.FIFOTYPE
        ti.mode = 0o644
        tf.addfile(ti)

    _write_archive(archive, _build)

    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-d",
        "-D",
        "-X",
        "*.fifo",
        str(archive),
        "/opt/app",
    )
    assert result.rc == 1

    err_lines = [line for line in result.err.decode().splitlines() if line.strip()]
    assert len(err_lines) == 1
    assert "pkgforge: error:" in err_lines[0]
    assert not (root / "opt" / "app").exists()
    assert not any(tmp_path.rglob("*.pkgforge-tmp"))


# --------------------------------------------------------------------------
# _prune_excluded itself: never follows a symlink, in either direction
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_prune_excluded_never_follows_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "keep.txt").write_text("keep")
    (root / "drop.la").write_text("drop")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.la").write_text("outside-la")

    # A symlink into "outside" whose OWN name also matches the pattern:
    # it must be removed as itself (os.unlink), never rmtree'd (which
    # would refuse a symlink anyway) and never followed into "outside".
    (root / "escape.la").symlink_to(outside)

    matcher = PathMatch([PathMatchStmt.parse("**/*.la")], root, installroot="/opt/app")
    removed = _prune_excluded(root, matcher, {})

    assert removed == 2  # drop.la and the escape.la symlink itself
    assert not (root / "drop.la").exists()
    assert (root / "keep.txt").exists()
    assert not (root / "escape.la").exists()
    assert not (root / "escape.la").is_symlink()
    # Never followed: the outside file the symlink pointed at is untouched.
    assert (outside / "x.la").read_text() == "outside-la"
