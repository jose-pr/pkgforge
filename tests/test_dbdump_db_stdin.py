"""Tests for ``dbdump --db -``: reading the file DB as JSON Lines from
standard input.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

import pytest

from pkgforge.db import open_db
from pkgforge.db.jsonl import _jsonl_line


def _stdin_bytes(data: bytes) -> io.TextIOWrapper:
    """A stand-in for ``sys.stdin`` backed by raw bytes, with a real
    ``.buffer`` (like the real ``sys.stdin``)."""
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")


class _NeverRead:
    """A ``sys.stdin`` stand-in that raises if ever actually read from --
    used to prove a code path never touches stdin."""

    def read(self, *args, **kwargs):
        raise AssertionError("stdin.read() must not be called")

    @property
    def buffer(self):
        raise AssertionError("stdin.buffer must not be touched")


DUMP = ("dbdump", "--db", "-", "-f", "rpmspecfiles", "-")


def test_db_dash_renders_records(cli, make_entry, monkeypatch):
    entry = make_entry(mode="755")
    monkeypatch.setattr(
        sys, "stdin", _stdin_bytes(_jsonl_line("/usr/bin/tool", entry).encode())
    )

    result = cli(*DUMP)
    assert result.rc == 0, result.err
    assert result.out == b'%attr(755,root,root) "/usr/bin/tool"\n'


def test_db_dash_after_the_subcommand(cli, make_entry, monkeypatch):
    monkeypatch.setattr(
        sys, "stdin", _stdin_bytes(_jsonl_line("/usr/bin/tool", make_entry()).encode())
    )
    result = cli("dbdump", "-f", "rpmspecfiles", "--db", "-", "-")
    assert result.rc == 0, result.err
    assert b'"/usr/bin/tool"' in result.out


@pytest.mark.parametrize("fmt", ["rpmspecfiles", "debian"])
def test_db_dash_matches_file_db(tmp_path, cli, make_entry, monkeypatch, fmt):
    records = [
        ("/usr/bin/tool", make_entry(mode="755")),
        ("/usr/share/doc/x", make_entry(mode="644")),
        ("/usr/share", make_entry(mode="755", type="directory")),
    ]
    text = "".join(_jsonl_line(path, entry) for path, entry in records)

    db_path = tmp_path / "files.jsonl"
    provider = open_db(db_path)
    for path, entry in records:
        provider.add(path, entry)

    file_result = cli("--db", str(db_path), "dbdump", "-f", fmt, "-")
    assert file_result.rc == 0, file_result.err

    monkeypatch.setattr(sys, "stdin", _stdin_bytes(text.encode()))
    stdin_result = cli("--db", "-", "dbdump", "-f", fmt, "-")
    assert stdin_result.rc == 0, stdin_result.err

    assert stdin_result.out == file_result.out
    assert stdin_result.out  # both invocations actually rendered something


def test_db_dash_last_record_wins_and_tombstones(cli, make_entry, monkeypatch):
    entry1 = make_entry(mode="644")
    entry2 = make_entry(mode="755")
    text = (
        _jsonl_line("/a", entry1)
        + _jsonl_line("/a", entry2)
        + _jsonl_line("/b", entry1)
        + _jsonl_line("/b", None)
    )
    monkeypatch.setattr(sys, "stdin", _stdin_bytes(text.encode()))

    result = cli(*DUMP)
    assert result.rc == 0, result.err
    out = result.out.decode()
    assert '%attr(755,root,root) "/a"' in out
    assert "/b" not in out


def test_db_dash_closed_refused(cli, monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)

    result = cli(*DUMP)
    assert result.rc == 2
    assert "stdin is closed" in result.err.decode()


@pytest.mark.posix
def test_db_dash_terminal_refused(cli, monkeypatch):
    import pty

    master, slave = pty.openpty()
    try:
        with os.fdopen(slave, "r") as tty:
            monkeypatch.setattr(sys, "stdin", tty)
            result = cli(*DUMP)
    finally:
        os.close(master)
    assert result.rc == 2
    assert "terminal or a device" in result.err.decode()


@pytest.mark.posix
def test_db_dash_devnull_refused(cli, monkeypatch):
    with open(os.devnull) as null:
        monkeypatch.setattr(sys, "stdin", null)
        result = cli(*DUMP)
    assert result.rc == 2
    assert "terminal or a device" in result.err.decode()
    assert result.out == b""


def test_db_dash_regular_file_accepted(tmp_path, cli, make_entry, monkeypatch):
    path = tmp_path / "records.jsonl"
    path.write_text(_jsonl_line("/usr/bin/tool", make_entry()), encoding="utf-8")
    with open(path, encoding="utf-8") as fh:
        monkeypatch.setattr(sys, "stdin", fh)
        result = cli(*DUMP)
    assert result.rc == 0, result.err
    assert b'"/usr/bin/tool"' in result.out


def test_db_dash_empty_input_warns(cli, monkeypatch, caplog):
    monkeypatch.setattr(sys, "stdin", _stdin_bytes(b""))

    with caplog.at_level("WARNING"):
        result = cli(*DUMP)
    assert result.rc == 0, result.err
    assert result.out == b""
    assert any(
        "stdin held no DB records" in r.message
        for r in caplog.records
        if r.name == "pkgforge.dbdump"
    )


def test_db_dash_without_buffer(cli, make_entry, monkeypatch):
    entry = make_entry(mode="750")
    text = _jsonl_line("/opt/tool", entry)
    # No .buffer at all: the plain `.read()` text path must still work.
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    result = cli(*DUMP)
    assert result.rc == 0, result.err
    assert result.out == b'%attr(750,root,root) "/opt/tool"\n'


@pytest.mark.parametrize("buffered", [True, False])
def test_db_dash_ignores_a_leading_bom(cli, make_entry, monkeypatch, buffered):
    text = _jsonl_line("/opt/tool", make_entry())
    if buffered:
        data = b"\xef\xbb\xbf" + text.encode()
        monkeypatch.setattr(sys, "stdin", _stdin_bytes(data))
    else:
        monkeypatch.setattr(sys, "stdin", io.StringIO("﻿" + text))
    result = cli(*DUMP)
    assert result.rc == 0, result.err
    assert b'"/opt/tool"' in result.out


def test_db_dash_bad_record_names_line(cli, make_entry, monkeypatch):
    text = _jsonl_line("/a", make_entry()) + "not-json\n"
    monkeypatch.setattr(sys, "stdin", _stdin_bytes(text.encode()))

    result = cli(*DUMP)
    assert result.rc == 1
    err = result.err.decode()
    assert "<stdin>:2: invalid JSON Lines record:" in err
    assert "--db-format" not in err


def test_db_dash_schema_error_names_stdin_line_and_path(cli, monkeypatch):
    text = (
        '{"path": "/a", "mode": 644, "owner": "-", "group": "-", '
        '"type": "file", "meta": {}}\n'
    )
    monkeypatch.setattr(sys, "stdin", _stdin_bytes(text.encode()))

    result = cli(*DUMP)
    assert result.rc == 1
    err = result.err.decode()
    assert err.count("\n") == 1
    assert "<stdin>:1" in err and "'/a'" in err and "mode" in err


def test_db_dash_non_utf8(cli, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _stdin_bytes(b"\xff\xfe\xfa"))

    result = cli(*DUMP)
    assert result.rc == 1
    assert "<stdin>:" in result.err.decode()


def test_db_dash_ignores_db_format(cli, make_entry, monkeypatch):
    monkeypatch.setenv("PKGFORGE_DB_FORMAT", "yaml")
    monkeypatch.setattr(
        sys, "stdin", _stdin_bytes(_jsonl_line("/usr/bin/tool", make_entry()).encode())
    )

    result = cli(*DUMP)
    assert result.rc == 0, result.err
    assert b'"/usr/bin/tool"' in result.out


def test_db_dash_checks_format_first(cli, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _NeverRead())

    result = cli("dbdump", "--db", "-", "-f", "nope")
    assert result.rc == 2
    err = result.err.decode()
    assert "nope" in err
    assert "unrecognized" not in err


def test_stdin_flag_is_gone(cli):
    result = cli("dbdump", "--stdin", "-f", "rpmspecfiles", "-")
    assert result.rc == 2
    assert "--stdin" in result.err.decode()


def test_env_db_dash_reads_stdin(cli, make_entry, monkeypatch):
    monkeypatch.setenv("PKGFORGE_DB", "-")
    monkeypatch.setattr(
        sys, "stdin", _stdin_bytes(_jsonl_line("/usr/bin/tool", make_entry()).encode())
    )
    result = cli("dbdump", "-f", "rpmspecfiles", "-")
    assert result.rc == 0, result.err
    assert b'"/usr/bin/tool"' in result.out


@pytest.mark.parametrize("db_case", ["flag", "env"])
def test_compact_refuses_db_dash_without_reading_stdin(
    tmp_path, cli, monkeypatch, db_case
):
    monkeypatch.setattr(sys, "stdin", _NeverRead())
    monkeypatch.chdir(tmp_path)
    argv = []
    if db_case == "flag":
        argv = ["--db", "-"]
    else:
        monkeypatch.setenv("PKGFORGE_DB", "-")

    result = cli(*argv, "compact")
    assert result.rc == 2
    assert "--db -" in result.err.decode()


@pytest.mark.parametrize("db_case", ["flag", "env", "unset"])
def test_initdb_with_db_dash_never_reads_stdin(tmp_path, cli, monkeypatch, db_case):
    monkeypatch.setattr(sys, "stdin", _NeverRead())
    monkeypatch.chdir(tmp_path)
    argv = []
    if db_case == "flag":
        argv = ["--db", "-"]
    elif db_case == "env":
        monkeypatch.setenv("PKGFORGE_DB", "-")

    result = cli(*argv, "initdb")
    assert result.rc == 0, result.err
    assert list(tmp_path.iterdir()) == []


def test_scan_drop_stale_refuses_db_dash(tmp_path, cli):
    (tmp_path / "root").mkdir()
    result = cli("--db", "-", "-r", str(tmp_path / "root"), "scan", "--drop-stale", "/")
    assert result.rc == 2


def _env():
    scrub = (
        "PKGFORGE_ROOT",
        "PKGFORGE_DB",
        "PKGFORGE_DB_FORMAT",
        "PKGFORGE_MCP",
        "PKG_FORGE_MCP",
        "AGENT_HELP",
        "AGENTS_HELP",
    )
    return {k: v for k, v in os.environ.items() if k not in scrub}


@pytest.mark.posix
def test_install_pipe_into_dbdump(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "f"
    src.write_text("hi\n", encoding="utf-8")
    env = _env()

    installer = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pkgforge",
            "--db",
            "-",
            "-r",
            str(root),
            "install",
            "-D",
            "-m",
            "644",
            str(src),
            "/usr/share/f",
        ],
        stdout=subprocess.PIPE,
        env=env,
    )
    try:
        dump = subprocess.run(
            [sys.executable, "-m", "pkgforge", "dbdump", "--db", "-", "-f", "rpm", "-"],
            stdin=installer.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=60,
        )
    finally:
        installer.stdout.close()
        installer.wait(timeout=60)

    assert installer.returncode == 0
    assert dump.returncode == 0, dump.stderr.decode()
    assert b'%attr(644,-,-) "/usr/share/f"' in dump.stdout


@pytest.mark.posix
def test_dbdump_db_dash_from_devnull_exits_2():
    dump = subprocess.run(
        [sys.executable, "-m", "pkgforge", "dbdump", "--db", "-", "-f", "rpm", "-"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
        timeout=60,
    )
    assert dump.returncode == 2
    assert dump.stdout == b""
