"""Tests driving pkgforge through ``pkgforge.main(argv)``, as a user would.

Cross-platform tests use the ``cli`` fixture directly. POSIX-only tests (real
staging: chmod, chown, hardlinks, "/"-rooted destinations) are marked
``@pytest.mark.posix``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from pkgforge.db import open_db

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def _write(path: Path, content: str = "x") -> Path:
    path.write_text(content)
    return path


# --------------------------------------------------------------------------
# env isolation (F51)
# --------------------------------------------------------------------------


def test_env_is_isolated():
    from pkgforge.initdb import InitDb

    inst = InitDb._parser_().parse_args([])
    assert inst.db is None
    assert inst.db_format is None
    assert inst.buildroot == Path(".")


@pytest.mark.posix
def test_env_vars_configure_root_db_and_format(tmp_path):
    root = tmp_path / "root"
    (root / "opt").mkdir(parents=True)
    src = _write(tmp_path / "src.txt")
    db = tmp_path / "f.jsonl"  # suffix says jsonl; PKGFORGE_DB_FORMAT overrides it

    env = {
        **os.environ,
        "PKGFORGE_ROOT": str(root),
        "PKGFORGE_DB": str(db),
        "PKGFORGE_DB_FORMAT": "sqlite",
        "PYTHONPATH": str(SRC_DIR),
    }
    subprocess.run(
        [sys.executable, "-m", "pkgforge", "install", "-m", "700", str(src), "/opt/"],
        cwd=tmp_path,
        env=env,
        check=True,
    )

    staged = root / "opt" / "src.txt"
    assert stat.S_IMODE(staged.stat().st_mode) == 0o700
    assert open_db(db, for_read=True).format == "sqlite"
    loaded = open_db(db, "sqlite", for_read=True).load()
    assert loaded["/opt/src.txt"]["mode"] == "700"


# --------------------------------------------------------------------------
# dbdump (cross-platform)
# --------------------------------------------------------------------------


def _seed_tool_entry(db):
    # Seed the DB directly (not through `install`): a "/"-rooted destination
    # through `install` only works on POSIX (buildpath() has no drive to
    # relative_to() against on Windows), but dbdump itself is cross-platform.
    open_db(db, for_read=False).add(
        "/usr/bin/tool",
        {"mode": "755", "owner": "root", "group": "root", "type": "file", "meta": {}},
    )


def test_dbdump_rpm_to_stdout(tmp_path, cli):
    db = tmp_path / "files.jsonl"
    _seed_tool_entry(db)

    result = cli("--db", str(db), "dbdump", "-f", "rpmspecfiles", "-")
    assert result.rc == 0
    assert b'%attr(755,root,root) "/usr/bin/tool"' in result.out


def test_dbdump_debian_to_stdout(tmp_path, cli):
    db = tmp_path / "files.jsonl"
    _seed_tool_entry(db)

    result = cli("--db", str(db), "dbdump", "-f", "debian", "-")
    assert result.rc == 0
    assert b"# === install ===\n" in result.out
    assert b"usr/bin/tool usr/bin" in result.out


def test_dbdump_unknown_format_fails(tmp_path, cli):
    db = tmp_path / "files.jsonl"
    result = cli("--db", str(db), "dbdump", "-f", "toml", "-")
    assert result.rc != 0
    assert b"rpmspecfiles" in result.err


# --------------------------------------------------------------------------
# initdb (cross-platform)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ["jsonl", "yaml", "db"])
def test_initdb_creates_parent_and_truncates(tmp_path, cli, ext):
    db = tmp_path / "nested" / f"files.{ext}"

    result = cli("--db", str(db), "--buildroot", str(tmp_path), "initdb")
    assert result.rc == 0
    assert db.parent.is_dir()

    provider = open_db(db, for_read=False)
    provider.add(
        "/seed", {"mode": "644", "owner": "-", "group": "-", "type": "file", "meta": {}}
    )
    assert open_db(db, for_read=True).load() != {}

    result = cli("--db", str(db), "--buildroot", str(tmp_path), "initdb")
    assert result.rc == 0
    assert open_db(db, for_read=True).load() == {}


def test_initdb_without_db_is_a_noop(tmp_path, cli, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = cli("initdb")
    assert result.rc == 0
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# install / scan (POSIX: real staging)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("placement", ["before", "after"])
@pytest.mark.posix
def test_global_options_placement(tmp_path, cli, placement):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = _write(tmp_path / "a")
    globalopts = ["--db", str(db), "--buildroot", str(root)]
    if placement == "before":
        argv = [*globalopts, "install", "-p", str(src), "/opt"]
    else:
        argv = ["install", "-p", *globalopts, str(src), "/opt"]

    assert cli(*argv).rc == 0
    row = open_db(db, for_read=True).load()["/opt/a"]
    assert row is not None


@pytest.mark.posix
def test_install_without_db_prints_jsonl(tmp_path, cli):
    root = tmp_path / "root"
    src = _write(tmp_path / "a")
    result = cli(
        "--buildroot", str(root), "install", "-p", "-m", "644", str(src), "/opt"
    )
    assert result.rc == 0
    rec = json.loads(result.out.decode().strip())
    assert rec["path"] == "/opt/a"
    assert rec["mode"] == "644"


@pytest.mark.parametrize("ext", ["jsonl", "yaml", "db"])
@pytest.mark.posix
def test_install_scan_dump_roundtrip(tmp_path, cli, ext):
    root = tmp_path / "root"
    db = tmp_path / f"files.{ext}"
    src = _write(tmp_path / "tool")
    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-m",
            "755",
            str(src),
            "/usr/bin",
        ).rc
        == 0
    )
    share = tmp_path / "share"
    share.mkdir()
    _write(share / "data.txt")
    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-d",
            str(share),
            "/usr/share/tool",
        ).rc
        == 0
    )
    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "--missing",
            "/usr/share/tool",
        ).rc
        == 0
    )

    result = cli("--db", str(db), "dbdump", "-f", "rpmspecfiles", "-")
    assert result.rc == 0
    lines = {line for line in result.out.decode().splitlines() if line}
    assert '%attr(755,-,-) "/usr/bin/tool"' in lines
    # install -d with no -T nests under the source's own basename ("share").
    assert any('"/usr/share/tool/share/data.txt"' in line for line in lines)


@pytest.mark.posix
def test_install_D_creates_parents(tmp_path, cli):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = _write(tmp_path / "a")
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-D",
        str(src),
        "/deep/nested/dir",
    )
    assert result.rc == 0
    staged = root / "deep" / "nested" / "dir"
    assert staged.read_text() == "x"


@pytest.mark.posix
def test_install_noentry_records_nothing(tmp_path, cli):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = _write(tmp_path / "a")
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "--noentry",
        str(src),
        "/opt",
    )
    assert result.rc == 0
    assert (root / "opt" / "a").exists()
    assert not db.exists()


@pytest.mark.posix
def test_install_remove_source_file(tmp_path, cli):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = _write(tmp_path / "a")
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "--remove-source",
        str(src),
        "/opt",
    )
    assert result.rc == 0
    assert not src.exists()
    assert (root / "opt" / "a").exists()


@pytest.mark.posix
def test_install_chown_current_user(tmp_path, cli):
    import grp
    import pwd

    try:
        group_name = grp.getgrgid(os.getgid()).gr_name
    except KeyError:
        pytest.skip("current gid has no group name")
    user_name = pwd.getpwuid(os.getuid()).pw_name

    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = _write(tmp_path / "a")
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "--chown",
        "-o",
        user_name,
        "-g",
        group_name,
        str(src),
        "/opt",
    )
    assert result.rc == 0
    st = (root / "opt" / "a").stat()
    assert st.st_uid == os.getuid()
    assert st.st_gid == os.getgid()


@pytest.mark.posix
def test_scan_missing_keeps_existing_entries(tmp_path, cli):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (sub / "a").write_text("a")
    (sub / "b").write_text("b")

    provider = open_db(db, for_read=False)
    provider.add(
        "/sub/a",
        {"mode": "600", "owner": "-", "group": "-", "type": "file", "meta": {}},
    )

    result = cli("--db", str(db), "--buildroot", str(root), "scan", "--missing", "/sub")
    assert result.rc == 0
    loaded = open_db(db, for_read=True).load()
    assert loaded["/sub/a"]["mode"] == "600"  # untouched
    assert "/sub/b" in loaded


@pytest.mark.posix
def test_install_source_is_destination_keeps_file(tmp_path, cli):
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    etc = root / "etc"
    etc.mkdir(parents=True)
    target = etc / "b.conf"
    target.write_text("keep me")

    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-T",
        "-m",
        "600",
        str(target),
        "/etc/b.conf",
    )
    assert result.rc == 0
    assert target.read_text() == "keep me"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert open_db(db, for_read=True).load()["/etc/b.conf"]["mode"] == "600"
