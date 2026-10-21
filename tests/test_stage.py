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
            "kind": "file",
            "caps": "-",
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


# -- stage ---------------------------------------------------------------

CFG = """\
[[stage]]
dir = "/var/lib/app"
mode = "750"

[[stage]]
src = "src/tool"
dest = "/usr/bin"
mode = "755"

[[stage]]
symlink = "/usr/bin/t"
target = "tool"

[[stage]]
file = "/var/log/app.log"
"""


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """A directory holding ``pkgforge.cfg`` and its source, as the working
    directory, with the build root and file DB beside it."""
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src/tool").write_bytes(b"#!/bin/sh\n")
    (work / "pkgforge.cfg").write_bytes(CFG.encode("utf-8"))
    monkeypatch.chdir(work)
    monkeypatch.delenv("PKGFORGE_CONFIG", raising=False)
    monkeypatch.setenv("PKGFORGE_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("PKGFORGE_DB", str(tmp_path / "f.jsonl"))
    (tmp_path / "root").mkdir()
    return work


@pytest.mark.posix
def test_stage_runs_the_steps_in_order(cli, project_dir, tmp_path):
    result = cli("stage")
    assert result.rc == 0, result.err
    assert list(_live(tmp_path / "f.jsonl")) == [
        "/var/lib/app",
        "/usr/bin/tool",
        "/usr/bin/t",
        "/var/log/app.log",
    ]
    assert (tmp_path / "root/usr/bin/tool").read_bytes() == b"#!/bin/sh\n"
    assert os.readlink(tmp_path / "root/usr/bin/t") == "tool"
    assert (tmp_path / "root/var/log/app.log").read_bytes() == b""


@pytest.mark.posix
def test_stage_returns_every_record(tmp_path):
    from pkgforge.config import ProjectConfig, StageStep

    (tmp_path / "root").mkdir()
    (tmp_path / "a").write_bytes(b"a")
    config = ProjectConfig(
        stage=(
            StageStep(src=str(tmp_path / "a"), dest="/opt"),
            StageStep(dir="/opt/d"),
        ),
        own=("/opt",),
    )
    records = Project(tmp_path / "root", config=config).stage()
    assert [path for path, _ in records] == ["/opt", "/opt/a", "/opt/d"]


@pytest.mark.posix
def test_stage_with_a_missing_source_stages_nothing(cli, project_dir, tmp_path):
    (project_dir / "src/tool").unlink()
    result = cli("stage")
    assert result.rc == 2
    assert b"[[stage]] #2: source" in result.err
    assert not (tmp_path / "root/var").exists()
    assert not (tmp_path / "f.jsonl").exists()


def test_stage_without_steps_exits_2(cli, project_dir):
    (project_dir / "pkgforge.cfg").write_bytes(b'[package]\nname = "x"\n')
    result = cli("stage")
    assert result.rc == 2
    assert b"no [[stage]] steps" in result.err


@pytest.mark.posix
def test_stage_init_resets_an_existing_db(cli, project_dir, tmp_path):
    db = tmp_path / "f.jsonl"
    open_db(db).add(
        "/stale",
        {
            "mode": "644",
            "owner": "-",
            "group": "-",
            "type": "file",
            "kind": "file",
            "caps": "-",
            "meta": {},
        },
    )
    assert cli("stage", "--init").rc == 0
    assert "/stale" not in _live(db)
    assert "/usr/bin/tool" in _live(db)


@pytest.mark.posix
def test_stage_without_init_keeps_the_db(cli, project_dir, tmp_path):
    db = tmp_path / "f.jsonl"
    open_db(db).add(
        "/stale",
        {
            "mode": "644",
            "owner": "-",
            "group": "-",
            "type": "file",
            "kind": "file",
            "caps": "-",
            "meta": {},
        },
    )
    assert cli("stage").rc == 0
    assert "/stale" in _live(db)


@pytest.mark.posix
def test_stage_init_without_a_file_db_warns_once_and_stages(
    cli, project_dir, tmp_path, monkeypatch, caplog
):
    monkeypatch.delenv("PKGFORGE_DB")
    with caplog.at_level(logging.WARNING, logger="pkgforge.stage"):
        result = cli("stage", "--init")
    assert result.rc == 0, result.err
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert (tmp_path / "root/usr/bin/tool").exists()


@pytest.mark.posix
def test_a_step_exclude_is_relative_to_its_source(cli, project_dir, tmp_path):
    tree = project_dir / "src/tree"
    (tree / "skip").mkdir(parents=True)
    (tree / "skip/a").write_bytes(b"a")
    (tree / "keep").write_bytes(b"k")
    (project_dir / "src/skip").write_bytes(b"s")
    (project_dir / "pkgforge.cfg").write_bytes(
        b'[[stage]]\nsrc = "src/tree"\ndest = "/opt"\nexclude = ["/skip"]\n'
        b"record_tree = true\n"
    )
    assert cli("stage").rc == 0
    assert (tmp_path / "root/opt/tree/keep").exists()
    assert not (tmp_path / "root/opt/tree/skip").exists()


@pytest.mark.posix
def test_stage_without_a_file_db_prints_the_records_only(
    cli, project_dir, tmp_path, monkeypatch
):
    monkeypatch.delenv("PKGFORGE_DB")
    result = cli("-q", "stage")
    assert result.rc == 0
    assert result.err == b""
    paths = [line for line in result.out.splitlines() if line.startswith(b"{")]
    assert len(paths) == 4
    assert b'"/usr/bin/tool"' in paths[1]


@pytest.mark.posix
def test_quiet_stage_writes_nothing_to_stderr(cli, project_dir):
    result = cli("-q", "stage")
    assert result.rc == 0
    assert result.err == b""


@pytest.mark.posix
def test_stage_logs_one_line_per_step_and_a_summary(cli, project_dir, caplog):
    with caplog.at_level(logging.INFO, logger="pkgforge.stage"):
        result = cli("stage")
    assert result.rc == 0, result.err
    records = [r for r in caplog.records if r.name == "pkgforge.stage"]
    assert {r.levelno for r in records} == {logging.INFO}
    # The installs a step runs log their own "Installing" lines under the
    # same logger; the step and summary lines are the stage's.
    lines = [
        r.getMessage()
        for r in records
        if r.getMessage().startswith(("[[stage]]", "staged"))
    ]
    assert len(lines) == 5
    assert lines[0] == "[[stage]] #1: dir -> /var/lib/app"
    assert lines[1] == "[[stage]] #2: src/tool -> /usr/bin"
    assert lines[-1] == "staged 4 step(s), 4 record(s)"
