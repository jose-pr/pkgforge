"""Tests for env-driven configuration (``PKGFORGE_ROOT``/``DB``/``DB_FORMAT``)
being read when ``main()``/``duho.parse`` runs, not once at import time, and
for the build-root <-> local-path helpers (``localpath``/``buildpath``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import pkgforge
from pkgforge.common import PkgForgeCmd

# conftest.py's autouse `_isolated_env` fixture scrubs every PKGFORGE_* var
# around each test, so a test that needs one sets it explicitly via
# monkeypatch -- never relies on a developer shell's own exports.


def test_env_db_read_at_main_time(tmp_path, monkeypatch):
    db = tmp_path / "late.jsonl"
    monkeypatch.setenv("PKGFORGE_DB", str(db))

    rc = pkgforge.main(["initdb"])

    assert rc == 0
    assert db.exists()


@pytest.mark.parametrize("order", ["before", "after"])
def test_cli_db_beats_env(order, tmp_path, monkeypatch):
    # Guard: a global --db, whichever side of the subcommand it's on, must
    # keep winning over env now that env is read at parse time too.
    cli_db = tmp_path / "cli.jsonl"
    env_db = tmp_path / "env.jsonl"
    monkeypatch.setenv("PKGFORGE_DB", str(env_db))

    argv = (
        ["--db", str(cli_db), "initdb"]
        if order == "before"
        else ["initdb", "--db", str(cli_db)]
    )
    rc = pkgforge.main(argv)

    assert rc == 0
    assert cli_db.exists()
    assert not env_db.exists()


@pytest.mark.posix
def test_env_root_read_at_main_time(tmp_path, monkeypatch):
    root = tmp_path / "lateroot"
    root.mkdir()
    src = tmp_path / "src"
    src.write_text("hi")
    db = tmp_path / "db.jsonl"
    monkeypatch.setenv("PKGFORGE_ROOT", str(root))
    monkeypatch.setenv("PKGFORGE_DB", str(db))

    rc = pkgforge.main(["install", "-p", str(src), "/etc"])

    assert rc == 0
    assert (root / "etc" / "src").exists()


def test_direct_construction_keeps_import_env(tmp_path):
    # Guard (subprocess, since the env must be set BEFORE `import pkgforge`
    # for the import-time snapshot to see it): a command built directly in
    # Python, not through main()/duho.parse, must keep resolving PKGFORGE_DB
    # the way it always has -- from the environment at import time.
    db = tmp_path / "start.jsonl"
    script = (
        "import pkgforge\n"
        "from pkgforge.initdb import InitDb\n"
        "print(InitDb().db)\n"
    )
    env = dict(os.environ)
    for name in (
        "PKGFORGE_ROOT",
        "PKGFORGE_DB_FORMAT",
        "PKGFORGE_MCP",
        "PKG_FORGE_MCP",
        "AGENT_HELP",
        "AGENTS_HELP",
    ):
        env.pop(name, None)
    env["PKGFORGE_DB"] = str(db)

    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == str(db)


@pytest.mark.parametrize("mode", ["subprocess", "main"])
def test_empty_db_format_env_is_unset(mode, tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "usr").mkdir(parents=True)
    (root / "usr" / "a").write_text("x")
    db = tmp_path / "x.jsonl"

    if mode == "subprocess":
        env = dict(os.environ)
        env["PKGFORGE_DB_FORMAT"] = ""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pkgforge",
                "-q",
                "--db",
                str(db),
                "--buildroot",
                str(root),
                "scan",
                "/usr",
            ],
            env=env,
        )
        assert result.returncode == 0
    else:
        # Guard: this must already pass -- main()'s own env layer already
        # goes through the `type=` converter, which treats "" as unset.
        monkeypatch.setenv("PKGFORGE_DB_FORMAT", "")
        rc = pkgforge.main(
            ["-q", "--db", str(db), "--buildroot", str(root), "scan", "/usr"]
        )
        assert rc == 0

    assert db.exists()
    assert db.read_text().strip().startswith("{")


# --------------------------------------------------------------------------
# localpath / buildpath (build-root <-> "/"-rooted path translation)
# --------------------------------------------------------------------------


def test_localpath_accepts_str_and_relative():
    cmd = PkgForgeCmd(buildroot=Path("."), db=None, db_format=None)
    assert cmd.localpath("/usr/bin/x") == Path("usr/bin/x")
    assert cmd.localpath("a/b") == Path("a/b")


@pytest.mark.posix
def test_buildpath_roundtrip(tmp_path):
    # Guard: already correct, must not regress. POSIX-only: buildpath()'s
    # "/" root has no drive to anchor to on Windows (see test_cli.py).
    cmd = PkgForgeCmd(buildroot=tmp_path, db=None, db_format=None)
    local = tmp_path / "usr" / "bin" / "x"
    assert cmd.localpath(cmd.buildpath(local)) == local


def test_scan_str_buildroot(tmp_path):
    # buildroot is annotated Path, but a str buildroot from the Python
    # API must not TypeError (self.buildroot / ... on a str used to).
    from pkgforge.scan import ScanCmd

    root = tmp_path / "root"
    (root / "usr").mkdir(parents=True)
    (root / "usr" / "a").write_text("x")
    db = tmp_path / "db.jsonl"

    cmd = ScanCmd(buildroot=str(root), db=db, db_format=None, path="/usr")
    cmd()  # must not raise

    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    # Compare basenames only: os.walk yields "\\"-joined paths on Windows.
    assert {Path(k).name for k in loaded} == {"a"}
