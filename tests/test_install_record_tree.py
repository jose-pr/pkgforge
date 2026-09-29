"""Tests for ``install --record-tree``/``PKGFORGE_INSTALL_RECORD_TREE``: a
directory or archive install also records every path below DESTINATION that
the DB does not already hold, honouring ``-X`` and filling gaps only.

Every test here is ``@pytest.mark.posix`` (real modes, symlinks, and
sometimes hardlinks are load-bearing). A ``[route]`` id of ``tarfile`` needs
PEP 706's extraction filter; a ``bsdtar`` id skips without the ``bsdtar``
binary and is forced onto that route by naming the archive ``payload.bin``
(a suffix :func:`pkgforge.install.archive._is_tar_source` never recognizes
as tar-family, whatever the actual bytes inside) -- matching
``tests/test_archive_exclude.py``.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from collections import Counter
from pathlib import Path

import pytest

import pkgforge
from pkgforge.command import PkgForgeCmd

requires_tar_filter = pytest.mark.skipif(
    not hasattr(tarfile, "data_filter"),
    reason="this Python's tarfile has no extraction filter (PEP 706)",
)
needs_bsdtar = pytest.mark.skipif(
    shutil.which("bsdtar") is None, reason="bsdtar not available"
)

ROUTES = [
    pytest.param("tarfile", marks=requires_tar_filter),
    pytest.param("bsdtar", marks=needs_bsdtar),
]


def _archive_path(tmp_path: Path, route: str) -> Path:
    return tmp_path / ("pkg.tar" if route == "tarfile" else "payload.bin")


def _add_file(tf: tarfile.TarFile, name: str, data: bytes = b"x") -> None:
    ti = tarfile.TarInfo(name=name)
    ti.size = len(data)
    ti.mode = 0o644
    tf.addfile(ti, io.BytesIO(data))


def _write_archive(path: Path, build) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        build(tf)
    path.write_bytes(buf.getvalue())


# --------------------------------------------------------------------------
# what gets recorded, and with what metadata
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_record_tree_records_children(tmp_path, cli):
    root = tmp_path / "root"
    src = tmp_path / "src"
    (src / "bin").mkdir(parents=True)
    (src / "sub").mkdir()
    (src / "a.txt").write_text("x")
    (src / "a.txt").chmod(0o644)
    (src / "bin" / "tool").write_text("#!/bin/sh\n")
    (src / "bin" / "tool").chmod(0o755)
    (src / "bin").chmod(0o755)
    (src / "sub").chmod(0o755)
    (src / "link").symlink_to("a.txt")

    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-D",
        "-d",
        "-m",
        "755",
        "-o",
        "root",
        "-g",
        "root",
        "-O",
        "k=v",
        "--record-tree",
        str(src),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert set(loaded) == {
        "/opt/app",
        "/opt/app/a.txt",
        "/opt/app/bin",
        "/opt/app/bin/tool",
        "/opt/app/sub",
        "/opt/app/link",
    }

    top = loaded["/opt/app"]
    assert top["mode"] == "755"
    assert top["owner"] == "root"
    assert top["group"] == "root"

    a = loaded["/opt/app/a.txt"]
    assert a["mode"] == "644"
    assert a["type"] == "file"
    assert a["owner"] == "root"
    assert a["group"] == "root"
    assert a["meta"] == {"k": "v"}

    tool = loaded["/opt/app/bin/tool"]
    assert tool["mode"] == "755"
    assert tool["type"] == "file"

    bindir = loaded["/opt/app/bin"]
    assert bindir["type"] == "directory"
    assert bindir["mode"] == "755"

    subdir = loaded["/opt/app/sub"]
    assert subdir["type"] == "directory"
    assert subdir["meta"] == {"k": "v"}

    link = loaded["/opt/app/link"]
    assert link["type"] == "symlink"
    assert link["mode"] == "-"
    assert link["owner"] == "root"
    assert link["group"] == "root"


@pytest.mark.posix
def test_record_tree_fills_gaps_only(tmp_path, cli):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"

    # Pre-install a setuid binary at the exact child path the tree install
    # will also stage, recording it with a mode the tree walk must not
    # overwrite.
    preexisting_src = tmp_path / "preexisting_tool"
    preexisting_src.write_text("x")
    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-T",
            "-m",
            "4755",
            "-o",
            "root",
            "-g",
            "root",
            str(preexisting_src),
            "/opt/app/bin/tool",
        ).rc
        == 0
    )

    src = tmp_path / "src"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "tool").write_text("new content")
    (src / "bin" / "tool").chmod(0o755)

    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-D",
        "-d",
        "--record-tree",
        str(src),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert loaded["/opt/app/bin/tool"]["mode"] == "4755"


@pytest.mark.posix
def test_record_tree_honors_exclude(tmp_path, cli):
    root = tmp_path / "root"
    dest = root / "opt" / "app"
    dest.mkdir(parents=True)
    (dest / "tmp").mkdir()
    (dest / "tmp" / "leftover.txt").write_text("stale")

    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.txt").write_text("keep")
    (src / "skip.pyc").write_text("skip")

    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-D",
        "-d",
        "-X",
        "**/*.pyc",
        "-X",
        "(?type:directory)**/tmp",
        "--record-tree",
        str(src),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert "/opt/app/keep.txt" in loaded
    assert "/opt/app/skip.pyc" not in loaded
    assert "/opt/app/tmp" not in loaded
    assert "/opt/app/tmp/leftover.txt" not in loaded


@pytest.mark.posix
def test_record_tree_absolute_exclude(tmp_path, cli):
    root = tmp_path / "root"
    src = tmp_path / "src"
    (src / "cache").mkdir(parents=True)
    (src / "cache" / "c.bin").write_text("cache-data")
    (src / "keep.txt").write_text("keep")

    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-D",
        "-d",
        "-X",
        "/opt/app/cache",
        "--record-tree",
        str(src),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert "/opt/app/keep.txt" in loaded
    assert "/opt/app/cache" not in loaded
    assert "/opt/app/cache/c.bin" not in loaded


@pytest.mark.posix
@pytest.mark.parametrize("route", ROUTES)
def test_record_tree_archive(tmp_path, cli, route):
    archive = _archive_path(tmp_path, route)

    def _build(tf):
        _add_file(tf, "keep.txt", b"keep")
        _add_file(tf, "drop.la", b"la")

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
        "--record-tree",
        str(archive),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert "/opt/app/keep.txt" in loaded
    assert "/opt/app/drop.la" not in loaded


@pytest.mark.posix
@pytest.mark.parametrize("method", ["link", "move"])
def test_record_tree_method(tmp_path, cli, method):
    root = tmp_path / "root"
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("data")
    (src / "f").chmod(0o644)

    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-D",
        "-d",
        "--method",
        method,
        "--record-tree",
        str(src),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert loaded["/opt/app/f"]["mode"] == "644"
    assert loaded["/opt/app/f"]["type"] == "file"


@pytest.mark.posix
def test_record_tree_noentry_records_nothing(tmp_path, cli):
    root = tmp_path / "root"
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x")

    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-D",
        "-d",
        "--noentry",
        "--record-tree",
        str(src),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()
    assert not db.exists()


@pytest.mark.posix
def test_record_tree_ignored_for_file_source(tmp_path, cli, caplog):
    import logging

    root = tmp_path / "root"
    src = tmp_path / "a.txt"
    src.write_text("hello")

    db = tmp_path / "files.jsonl"
    with caplog.at_level(logging.WARNING):
        result = cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "--record-tree",
            str(src),
            "/opt/bin",
        )
    assert result.rc == 0, result.err.decode()

    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert set(loaded) == {"/opt/bin/a.txt"}
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


@pytest.mark.posix
def test_record_tree_multi_source_merge(tmp_path, cli):
    root = tmp_path / "root"
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "from_a.txt").write_text("a")
    (b / "from_b.txt").write_text("b")

    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-D",
        "-d",
        "--record-tree",
        str(a),
        str(b),
        "/opt/x",
    )
    assert result.rc == 0, result.err.decode()

    keys = [
        json.loads(line)["path"] for line in db.read_text().splitlines() if line.strip()
    ]
    counts = Counter(keys)
    # /opt/x itself is written once per clone (each of the two merging
    # sources records its own top entry) -- only each CHILD key, walked by
    # both clones' own --record-tree pass over the shared destination, must
    # appear exactly once (the second clone's own loaddb() already sees the
    # first clone's child writes as known).
    assert counts["/opt/x/from_a.txt"] == 1
    assert counts["/opt/x/from_b.txt"] == 1


@pytest.mark.posix
def test_record_tree_skips_file_db(tmp_path, cli, caplog):
    import logging

    root = tmp_path / "root"
    dest = root / "opt" / "app"
    dest.mkdir(parents=True)
    db = dest / "files.jsonl"
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x")

    with caplog.at_level(logging.WARNING):
        result = cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-D",
            "-d",
            "--record-tree",
            str(src),
            "/opt/app",
        )
    assert result.rc == 0, result.err.decode()

    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert "/opt/app/files.jsonl" not in loaded
    assert "/opt/app/f" in loaded
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.posix
def test_record_tree_stdout_db(tmp_path, cli):
    root = tmp_path / "root"
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x")

    result = cli(
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-D",
        "-d",
        "--record-tree",
        str(src),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()

    recs = [json.loads(l) for l in result.out.decode().splitlines() if l.strip()]
    paths = {rec["path"] for rec in recs}
    assert paths == {"/opt/app", "/opt/app/f"}


# --------------------------------------------------------------------------
# option plumbing: env var, CLI precedence, bad value, default off
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_record_tree_env_var(tmp_path, monkeypatch):
    root = tmp_path / "root"
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x")
    monkeypatch.setenv("PKGFORGE_INSTALL_RECORD_TREE", "1")

    db = tmp_path / "files.jsonl"
    rc = pkgforge.main(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-D",
            "-d",
            str(src),
            "/opt/app",
        ]
    )
    assert rc == 0
    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert "/opt/app/f" in loaded


@pytest.mark.posix
def test_record_tree_no_flag_beats_env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x")
    monkeypatch.setenv("PKGFORGE_INSTALL_RECORD_TREE", "1")

    db = tmp_path / "files.jsonl"
    rc = pkgforge.main(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-D",
            "-d",
            "--no-record-tree",
            str(src),
            "/opt/app",
        ]
    )
    assert rc == 0
    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert set(loaded) == {"/opt/app"}


@pytest.mark.posix
def test_record_tree_bad_env_exits_2(tmp_path, monkeypatch, cli):
    root = tmp_path / "root"
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x")
    monkeypatch.setenv("PKGFORGE_INSTALL_RECORD_TREE", "bogus")

    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-D",
        "-d",
        str(src),
        "/opt/app",
    )
    assert result.rc == 2


@pytest.mark.posix
def test_record_tree_default_off_records_one_entry(tmp_path, cli):
    root = tmp_path / "root"
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x")

    db = tmp_path / "files.jsonl"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-D",
        "-d",
        str(src),
        "/opt/app",
    )
    assert result.rc == 0, result.err.decode()
    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert set(loaded) == {"/opt/app"}
