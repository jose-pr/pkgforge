"""Tests for ``dbdump --stdin``: reading the file DB as JSON Lines from
standard input instead of ``--db``.
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

    def __init__(self, isatty: bool = False):
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty

    def read(self, *args, **kwargs):
        raise AssertionError("stdin.read() must not be called")

    @property
    def buffer(self):
        raise AssertionError("stdin.buffer must not be touched")


def test_stdin_renders_records(cli, make_entry, monkeypatch):
    entry = make_entry(mode="755")
    monkeypatch.setattr(
        sys, "stdin", _stdin_bytes(_jsonl_line("/usr/bin/tool", entry).encode())
    )

    result = cli("dbdump", "--stdin", "-f", "rpmspecfiles", "-")
    assert result.rc == 0, result.err
    assert result.out == b'%attr(755,root,root) "/usr/bin/tool"\n'


@pytest.mark.parametrize("fmt", ["rpmspecfiles", "debian"])
def test_stdin_matches_file_db(tmp_path, cli, make_entry, monkeypatch, fmt):
    records = [
        ("/usr/bin/tool", make_entry(mode="755")),
        ("/usr/share/doc/x", make_entry(mode="644")),
        ("/usr/share", make_entry(mode="755", type="directory")),
    ]
    text = "".join(_jsonl_line(path, entry) for path, entry in records)

    db_path = tmp_path / "files.jsonl"
    provider = open_db(db_path, for_read=False)
    for path, entry in records:
        provider.add(path, entry)

    file_result = cli("--db", str(db_path), "dbdump", "-f", fmt, "-")
    assert file_result.rc == 0, file_result.err

    monkeypatch.setattr(sys, "stdin", _stdin_bytes(text.encode()))
    stdin_result = cli("dbdump", "--stdin", "-f", fmt, "-")
    assert stdin_result.rc == 0, stdin_result.err

    assert stdin_result.out == file_result.out
    assert stdin_result.out  # both invocations actually rendered something


def test_stdin_last_record_wins_and_tombstones(cli, make_entry, monkeypatch):
    entry1 = make_entry(mode="644")
    entry2 = make_entry(mode="755")
    text = (
        _jsonl_line("/a", entry1)
        + _jsonl_line("/a", entry2)
        + _jsonl_line("/b", entry1)
        + _jsonl_line("/b", None)
    )
    monkeypatch.setattr(sys, "stdin", _stdin_bytes(text.encode()))

    result = cli("dbdump", "--stdin", "-f", "rpmspecfiles", "-")
    assert result.rc == 0, result.err
    out = result.out.decode()
    assert '%attr(755,root,root) "/a"' in out
    assert "/b" not in out


def test_stdin_tty_refused(cli, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _NeverRead(isatty=True))

    result = cli("dbdump", "--stdin", "-f", "rpmspecfiles", "-")
    assert result.rc == 2
    assert "stdin is a terminal" in result.err.decode()


def test_stdin_closed_refused(cli, monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)

    result = cli("dbdump", "--stdin", "-f", "rpmspecfiles", "-")
    assert result.rc == 2
    assert "stdin is closed" in result.err.decode()


def test_stdin_empty_warns(cli, monkeypatch, caplog):
    monkeypatch.setattr(sys, "stdin", _stdin_bytes(b""))

    with caplog.at_level("WARNING"):
        result = cli("dbdump", "--stdin", "-f", "rpmspecfiles", "-")
    assert result.rc == 0, result.err
    assert result.out == b""
    assert any(
        "stdin held no DB records" in r.message
        for r in caplog.records
        if r.name == "pkgforge.dbdump"
    )


def test_stdin_without_buffer(cli, make_entry, monkeypatch):
    entry = make_entry(mode="750")
    text = _jsonl_line("/opt/tool", entry)
    # No .buffer at all (e.g. duho's MCP-mode stand-in stdin): the plain
    # `.read()` text path must still work.
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    result = cli("dbdump", "--stdin", "-f", "rpmspecfiles", "-")
    assert result.rc == 0, result.err
    assert result.out == b'%attr(750,root,root) "/opt/tool"\n'


def test_stdin_bad_record_names_line(cli, make_entry, monkeypatch):
    entry = make_entry()
    text = _jsonl_line("/a", entry) + "not-json\n"
    monkeypatch.setattr(sys, "stdin", _stdin_bytes(text.encode()))

    result = cli("dbdump", "--stdin", "-f", "rpmspecfiles", "-")
    assert result.rc == 1
    assert "<stdin>:2: invalid JSON Lines record:" in result.err.decode()


def test_stdin_non_utf8(cli, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _stdin_bytes(b"\xff\xfe\xfa"))

    result = cli("dbdump", "--stdin", "-f", "rpmspecfiles", "-")
    assert result.rc == 1
    assert "<stdin>:" in result.err.decode()


def test_stdin_ignores_db_and_format(tmp_path, cli, make_entry, monkeypatch, caplog):
    other_db = tmp_path / "other.jsonl"
    open_db(other_db, for_read=False).add("/usr/bin/other", make_entry())
    monkeypatch.setenv("PKGFORGE_DB", str(other_db))
    monkeypatch.setenv("PKGFORGE_DB_FORMAT", "yaml")

    entry = make_entry(mode="700")
    monkeypatch.setattr(
        sys, "stdin", _stdin_bytes(_jsonl_line("/usr/bin/tool", entry).encode())
    )

    with caplog.at_level("DEBUG", logger="pkgforge.dbdump"):
        result = cli(
            "--loglevel",
            "pkgforge.dbdump:DEBUG",
            "dbdump",
            "--stdin",
            "-f",
            "rpmspecfiles",
            "-",
        )
    assert result.rc == 0, result.err
    out = result.out.decode()
    assert '"/usr/bin/tool"' in out
    assert "/usr/bin/other" not in out
    assert any(
        "ignoring --db" in r.message
        for r in caplog.records
        if r.name == "pkgforge.dbdump"
    )


def test_stdin_checks_format_first(cli, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _NeverRead(isatty=False))

    result = cli("dbdump", "-f", "nope", "--stdin")
    assert result.rc == 2
    err = result.err.decode()
    assert "nope" in err
    assert "unrecognized" not in err


def test_db_dash_warning_points_to_stdin(cli, caplog):
    with caplog.at_level("WARNING"):
        result = cli("--db", "-", "dbdump", "-f", "rpmspecfiles", "-")
    assert result.rc == 0, result.err
    assert any(
        "pass --stdin to read records from standard input" in r.message
        for r in caplog.records
        if r.name == "pkgforge.dbdump"
    )


def test_stdin_flag_has_no_env():
    from pkgforge.dbdump import DbDump

    parser = DbDump._parser_()
    action = next(a for a in parser._actions if "--stdin" in a.option_strings)
    assert "PKGFORGE" not in action.help
    assert "env" not in action.help.lower()


@pytest.mark.parametrize(
    "command",
    [
        ["dbdump", "-f", "rpmspecfiles", "-"],
        ["compact"],
        ["initdb"],
    ],
    ids=["dbdump", "compact", "initdb"],
)
@pytest.mark.parametrize("db_case", ["flag", "env", "unset"])
def test_db_dash_never_reads_stdin(tmp_path, cli, monkeypatch, db_case, command):
    monkeypatch.setattr(sys, "stdin", _NeverRead(isatty=False))
    monkeypatch.chdir(tmp_path)
    global_argv = []
    if db_case == "flag":
        global_argv = ["--db", "-"]
    elif db_case == "env":
        monkeypatch.setenv("PKGFORGE_DB", "-")
    # "unset": neither --db nor PKGFORGE_DB, cwd is tmp_path (empty).

    result = cli(*global_argv, *command)
    assert result.rc == 0, result.err


@pytest.mark.posix
def test_install_pipe_into_dbdump_stdin(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "f"
    src.write_text("hi\n", encoding="utf-8")

    scrub = (
        "PKGFORGE_ROOT",
        "PKGFORGE_DB",
        "PKGFORGE_DB_FORMAT",
        "PKGFORGE_MCP",
        "PKG_FORGE_MCP",
        "AGENT_HELP",
        "AGENTS_HELP",
    )
    env = {k: v for k, v in os.environ.items() if k not in scrub}

    installer = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pkgforge",
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
            [sys.executable, "-m", "pkgforge", "dbdump", "--stdin", "-f", "rpm", "-"],
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
