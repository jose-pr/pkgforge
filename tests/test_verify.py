"""Tests for ``pkgforge verify`` and ``Project.verify``: the build root
against the file DB.

Tests that need a symlink or chmod are ``@pytest.mark.posix``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

import pytest

from pkgforge import Project, VerifyIssue
from pkgforge.exceptions import ExcludeSyntaxError, UsageError


@pytest.fixture
def staged(tmp_path: Path):
    """A build root holding ``/usr/bin/hello`` and ``/usr/share/doc/hello/README``
    recorded in a DB outside it; returns ``(root, db, project)``."""
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello").write_text("hello\n", encoding="utf-8")
    (src / "README").write_text("readme\n", encoding="utf-8")
    project = Project(root, db)
    project.init()
    project.install([src / "hello"], "/usr/bin/hello", into=False, parents=True)
    project.install(
        [src / "README"], "/usr/share/doc/hello/README", into=False, parents=True
    )
    return root, db, project


def _verify(cli, root: Path, db: Path, *args: str):
    return cli("--db", str(db), "--buildroot", str(root), "verify", *args)


def test_staged_tree_is_clean(cli, staged):
    root, db, _ = staged

    result = _verify(cli, root, db)

    assert (result.rc, result.out) == (0, b"")


def test_unrecorded_file_is_reported(cli, staged):
    root, db, _ = staged
    (root / "usr" / "bin" / "stray").write_text("x", encoding="utf-8")

    result = _verify(cli, root, db)

    assert result.rc == 1
    assert result.out == b"unrecorded /usr/bin/stray\n"
    assert b"error" not in result.err


def test_missing_file_is_reported(cli, staged):
    root, db, _ = staged
    (root / "usr" / "bin" / "hello").unlink()

    result = _verify(cli, root, db)

    assert (result.rc, result.out) == (1, b"missing /usr/bin/hello\n")


def test_missing_ghost_is_not_reported(cli, tmp_path, staged):
    root, db, project = staged
    src = tmp_path / "src" / "hello"
    project.install([src], "/var/log/hello.log", into=False, parents=True, kind="ghost")
    (root / "var" / "log" / "hello.log").unlink()

    result = _verify(cli, root, db)

    assert (result.rc, result.out) == (0, b"")


def test_file_replaced_by_directory_is_a_type_change(cli, staged):
    root, db, _ = staged
    (root / "usr" / "bin" / "hello").unlink()
    (root / "usr" / "bin" / "hello").mkdir()

    result = _verify(cli, root, db)

    assert result.rc == 1
    assert result.out == b"type /usr/bin/hello: recorded file, found directory\n"


def test_unrecorded_directory_is_not_reported(cli, staged):
    root, db, _ = staged
    (root / "usr" / "lib" / "empty").mkdir(parents=True)

    result = _verify(cli, root, db)

    assert (result.rc, result.out) == (0, b"")


@pytest.mark.posix
def test_retargeted_symlink_is_reported(cli, staged):
    root, db, project = staged
    project.symlink("hello", "/usr/bin/hi")
    link = root / "usr" / "bin" / "hi"
    link.unlink()
    os.symlink("other", link)

    result = _verify(cli, root, db)

    assert result.rc == 1
    assert result.out == b"target /usr/bin/hi: recorded hello, found other\n"


@pytest.mark.posix
def test_unchanged_symlink_is_clean(cli, staged):
    root, db, project = staged
    project.symlink("hello", "/usr/bin/hi")

    assert _verify(cli, root, db).rc == 0


@pytest.mark.posix
def test_symlink_replaced_by_file_is_a_type_change(cli, staged):
    root, db, project = staged
    project.symlink("hello", "/usr/bin/hi")
    link = root / "usr" / "bin" / "hi"
    link.unlink()
    link.write_text("x", encoding="utf-8")

    result = _verify(cli, root, db)

    assert result.out == b"type /usr/bin/hi: recorded symlink, found file\n"


@pytest.mark.posix
def test_mode_drift_is_not_reported(cli, staged):
    root, db, _ = staged
    (root / "usr" / "bin" / "hello").chmod(0o600)

    assert _verify(cli, root, db).rc == 0


def test_exclude_hides_both_sides(cli, tmp_path, staged):
    root, db, project = staged
    src = tmp_path / "src" / "hello"
    project.install([src], "/extra/gone", into=False, parents=True)
    (root / "extra" / "gone").unlink()
    (root / "extra" / "stray").write_text("x", encoding="utf-8")

    assert _verify(cli, root, db).rc == 1
    assert _verify(cli, root, db, "-X", "/extra/**").rc == 0


def test_excluded_directory_is_not_descended(cli, tmp_path, staged):
    root, db, project = staged
    src = tmp_path / "src" / "hello"
    project.install([src], "/extra/deep/gone", into=False, parents=True)
    (root / "extra" / "deep" / "gone").unlink()
    (root / "extra" / "deep" / "stray").write_text("x", encoding="utf-8")

    result = _verify(cli, root, db, "-X", "/extra")

    assert (result.rc, result.out) == (0, b"")


def test_file_db_inside_the_build_root_is_skipped(cli, tmp_path, caplog):
    root = tmp_path / "root"
    db = root / "files.jsonl"
    src = tmp_path / "hello"
    src.write_text("x", encoding="utf-8")
    project = Project(root, db)
    project.init()
    project.install([src], "/usr/bin/hello", into=False, parents=True)

    result = _verify(cli, root, db)

    assert (result.rc, result.out) == (0, b"")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.name for r in warnings] == ["pkgforge.verify"]
    assert "Skipping the file DB" in warnings[0].getMessage()


def test_json_output_has_four_keys_per_issue(cli, staged):
    root, db, _ = staged
    (root / "usr" / "bin" / "hello").unlink()
    (root / "usr" / "bin" / "hello").mkdir()
    (root / "usr" / "bin" / "stray").write_text("x", encoding="utf-8")

    result = _verify(cli, root, db, "-f", "json")

    assert result.rc == 1
    assert result.out.endswith(b"\n")
    document = json.loads(result.out)
    assert list(document) == ["issues"]
    assert document["issues"] == [
        {
            "path": "/usr/bin/hello",
            "problem": "type",
            "recorded": "file",
            "found": "directory",
        },
        {
            "path": "/usr/bin/stray",
            "problem": "unrecorded",
            "recorded": None,
            "found": "file",
        },
    ]


def test_json_output_of_a_clean_tree_is_an_empty_list(cli, staged):
    root, db, _ = staged

    result = _verify(cli, root, db, "--format", "json")

    assert (result.rc, json.loads(result.out)) == (0, {"issues": []})


def test_issues_sort_by_path_then_problem(staged):
    root, _, project = staged
    (root / "usr" / "bin" / "hello").unlink()
    (root / "usr" / "bin" / "aaa").write_text("x", encoding="utf-8")
    (root / "usr" / "bin" / "zzz").write_text("x", encoding="utf-8")

    issues = project.verify()

    assert issues == [
        VerifyIssue("/usr/bin/aaa", "unrecorded", None, "file"),
        VerifyIssue("/usr/bin/hello", "missing", "file", None),
        VerifyIssue("/usr/bin/zzz", "unrecorded", None, "file"),
    ]


def test_verify_logs_one_info_line(staged, caplog):
    _, _, project = staged

    with caplog.at_level(logging.INFO, logger="pkgforge.verify"):
        project.verify()

    messages = [r.getMessage() for r in caplog.records if r.name == "pkgforge.verify"]
    assert messages == ["verified 2 entries: 0 issue(s)"]


def test_no_db_is_a_usage_error(cli, tmp_path):
    result = cli("--buildroot", str(tmp_path), "verify")

    assert result.rc == 2
    assert b"pkgforge: error: no file DB configured" in result.err


def test_db_dash_is_a_usage_error(cli, tmp_path):
    result = cli("--db", "-", "--buildroot", str(tmp_path), "verify")

    assert result.rc == 2
    assert b"pkgforge: error:" in result.err


def test_missing_database_file_is_a_usage_error(cli, tmp_path):
    result = cli(
        "--db", str(tmp_path / "none.jsonl"), "--buildroot", str(tmp_path), "verify"
    )

    assert result.rc == 2
    assert b"no file DB at" in result.err


def test_missing_build_root_is_a_usage_error(cli, staged):
    root, db, _ = staged
    shutil.rmtree(root)

    result = _verify(cli, root, db)

    assert result.rc == 2
    assert b"does not exist" in result.err


def test_library_raises_usage_error_for_a_missing_build_root(staged):
    root, _, project = staged
    shutil.rmtree(root)

    with pytest.raises(UsageError, match="does not exist"):
        project.verify()


def test_library_refuses_a_bad_exclude_statement(staged):
    _, _, project = staged

    with pytest.raises(ExcludeSyntaxError):
        project.verify(exclude=["(?bogus:x)*"])
