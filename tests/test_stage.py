"""``mkdir``, ``symlink`` and ``stage``: entries with no source, and the steps of
``pkgforge.cfg``."""

from __future__ import annotations

import logging
import os
import typing
from pathlib import Path

import pytest

from pkgforge import Project, UsageError
from pkgforge.db import open_db


def _live(db: Path) -> typing.Dict[str, typing.Dict[str, typing.Any]]:
    return {
        path: entry for path, entry in open_db(db).load().items() if entry is not None
    }


@pytest.fixture
def run(cli, tmp_path):
    """Run a command with the build root and file DB under ``tmp_path``."""
    (tmp_path / "root").mkdir()

    def _run(*args: str):
        return cli(
            "--buildroot",
            str(tmp_path / "root"),
            "--db",
            str(tmp_path / "f.jsonl"),
            *args,
        )

    return _run


# -- mkdir ---------------------------------------------------------------


@pytest.mark.posix
def test_mkdir_twice_leaves_one_live_directory(run, tmp_path):
    for _ in range(2):
        result = run(
            "mkdir", "-p", "-m", "750", "-o", "root", "-g", "adm", "/var/lib/x"
        )
        assert result.rc == 0, result.err
    assert (tmp_path / "root/var/lib/x").is_dir()
    assert _live(tmp_path / "f.jsonl") == {
        "/var/lib/x": {
            "mode": "750",
            "owner": "root",
            "group": "adm",
            "type": "directory",
            "meta": {},
        }
    }


@pytest.mark.posix
def test_mkdir_records_every_path_in_order(run, tmp_path):
    result = run("mkdir", "-p", "/opt/a", "/opt/b")
    assert result.rc == 0, result.err
    assert list(_live(tmp_path / "f.jsonl")) == ["/opt/a", "/opt/b"]


@pytest.mark.posix
def test_mkdir_without_parents_under_a_missing_parent_exits_2(run, tmp_path):
    result = run("mkdir", "/opt/missing/x")
    assert result.rc == 2
    assert b"does not exist" in result.err
    assert not (tmp_path / "root/opt").exists()


@pytest.mark.posix
def test_mkdir_without_a_file_db_prints_the_record(cli, tmp_path):
    (tmp_path / "root").mkdir()
    result = cli("--buildroot", str(tmp_path / "root"), "mkdir", "-p", "/opt/a")
    assert result.rc == 0
    assert b'"path": "/opt/a"' in result.out
    assert b'"type": "directory"' in result.out


def test_mkdir_needs_a_path(cli):
    assert cli("mkdir").rc == 2


# -- symlink -------------------------------------------------------------


@pytest.mark.posix
def test_symlink_creates_the_link_and_records_its_target(run, tmp_path):
    result = run("symlink", "-p", "-o", "root", "hello", "/usr/bin/hi")
    assert result.rc == 0, result.err
    assert os.readlink(tmp_path / "root/usr/bin/hi") == "hello"
    entry = _live(tmp_path / "f.jsonl")["/usr/bin/hi"]
    assert entry["type"] == "symlink"
    assert entry["meta"] == {"target": "hello"}
    assert entry["owner"] == "root"


@pytest.mark.posix
def test_symlink_twice_is_harmless(run, tmp_path):
    for _ in range(2):
        assert run("symlink", "-p", "hello", "/usr/bin/hi").rc == 0
    assert os.readlink(tmp_path / "root/usr/bin/hi") == "hello"
    assert list(_live(tmp_path / "f.jsonl")) == ["/usr/bin/hi"]


def test_symlink_has_no_mode_option(cli):
    result = cli("symlink", "--mode", "755", "hello", "/usr/bin/hi")
    assert result.rc == 2
    assert b"unrecognized arguments" in result.err or b"--mode" in result.err


# -- the library ---------------------------------------------------------


def test_mkdir_refuses_a_single_path_text(tmp_path):
    with pytest.raises(TypeError):
        Project(tmp_path).mkdir("/opt/a")  # type: ignore[arg-type]


def test_symlink_refuses_an_empty_target_and_a_meta_target(tmp_path):
    project = Project(tmp_path)
    with pytest.raises(UsageError, match="non-empty target"):
        project.symlink("", "/usr/bin/hi")
    with pytest.raises(UsageError, match="target"):
        project.symlink("hello", "/usr/bin/hi", meta={"target": "other"})
    with pytest.raises(TypeError):
        project.symlink(b"hello", "/usr/bin/hi")  # type: ignore[arg-type]


@pytest.mark.posix
def test_installs_run_by_an_operation_log_under_its_logger(tmp_path, caplog):
    source = tmp_path / "f"
    source.write_bytes(b"x")
    project = Project(tmp_path / "root")
    (tmp_path / "root").mkdir()
    logger = logging.getLogger("pkgforge.mkdir")
    with caplog.at_level(logging.WARNING):
        project._run_install([source], "/f", into=False, exclude=["*.o"], logger=logger)
    assert [r.name for r in caplog.records] == ["pkgforge.mkdir"]
