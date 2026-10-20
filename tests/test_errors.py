"""Tests for pkgforge's error model: ``PkgForgeError``/``UsageError`` and the
``pkgforge.main()`` error boundary.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import pkgforge
from pkgforge.exceptions import PkgForgeError, UsageError


def test_no_subcommand_exits_2():
    with pytest.raises(SystemExit) as excinfo:
        pkgforge.main([])
    assert excinfo.value.code == 2


def test_usage_error_is_value_error():
    assert issubclass(UsageError, ValueError)
    assert issubclass(UsageError, PkgForgeError)


def _usage_error_argv(case: str, tmp_path):
    if case == "stdin_without_T":
        return ["install", "-", str(tmp_path / "dest_f")], "-T"
    if case == "missing_source":
        missing = tmp_path / "missing.txt"
        return ["install", str(missing), str(tmp_path / "dest_f")], str(missing)
    if case == "x_path_guard":
        a = tmp_path / "a.gz"
        b = tmp_path / "b.gz"
        a.write_bytes(b"")
        b.write_bytes(b"")
        return (
            ["install", "-x", str(a), str(b), str(tmp_path / "dest_f")],
            "looks like a path",
        )
    assert case == "symlink_without_target"
    return (
        ["install", "-T", "-t", "symlink", "-", str(tmp_path / "dest_f")],
        "target",
    )


@pytest.mark.parametrize(
    "case",
    ["stdin_without_T", "missing_source", "x_path_guard", "symlink_without_target"],
)
def test_main_usage_errors_exit_2(case, tmp_path, cli):
    # None of these pass -p: each one raises before the destination is
    # computed, so all four run the same way on Windows.
    argv, needle = _usage_error_argv(case, tmp_path)
    result = cli(*argv)
    err = result.err.decode()
    assert result.rc == 2
    assert err.count("\n") == 1
    assert "Traceback" not in err
    assert err.startswith("pkgforge: error:")
    assert needle in err


@pytest.mark.posix
def test_main_unknown_owner_exits_2(tmp_path, cli):
    src = tmp_path / "f"
    src.write_text("hi")
    result = cli(
        "--db",
        str(tmp_path / "db.jsonl"),
        "--buildroot",
        str(tmp_path / "root"),
        "install",
        "-p",
        "--chown",
        "-o",
        "nosuchuser-xyz",
        str(src),
        "/etc",
    )
    err = result.err.decode()
    assert result.rc == 2
    assert err.count("\n") == 1
    assert "Traceback" not in err
    assert "nosuchuser-xyz" in err


def test_main_oserror_exits_1(tmp_path, cli):
    # A regular file where the DB's parent directory should be: initdb's own
    # mkdir(parents=True) raises FileExistsError, on Windows too.
    afile = tmp_path / "afile"
    afile.write_text("x")
    result = cli("--db", str(afile / "x.jsonl"), "initdb")
    err = result.err.decode()
    assert result.rc == 1
    assert err.count("\n") == 1
    assert "Traceback" not in err
    assert err.startswith("pkgforge: error:")


def test_main_broken_pipe_is_silent(monkeypatch, capsys):
    # capsys (not the fd-capturing `cli` fixture): sys.stdout here has no
    # real file descriptor, so _silence_broken_pipe()'s dup2 is a no-op (its
    # own guard) instead of fighting pytest's fd-level capture machinery.
    def _raise(*_a, **_kw):
        raise BrokenPipeError()

    monkeypatch.setattr(pkgforge.dbdump.DbDump, "__call__", _raise)
    rc = pkgforge.main(["dbdump", "-f", "rpmspecfiles"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.posix
def test_broken_pipe_subprocess(tmp_path):
    # A real subprocess: `--db - scan /` over 3000 files, with the reader
    # closing its end after one line, must never surface a traceback.
    #
    # stderr goes to a real file, not a second pipe: scan logs one INFO line
    # per file, and reading only stdout here (as the reader would) can
    # otherwise deadlock the child on a full, undrained stderr pipe before it
    # ever writes enough to stdout for our single readline() to return.
    root = tmp_path / "root"
    root.mkdir()
    for i in range(3000):
        (root / f"f{i}").write_text("x")

    err_path = tmp_path / "stderr.log"
    with err_path.open("wb") as errfile:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pkgforge",
                "--db",
                "-",
                "--buildroot",
                str(root),
                "scan",
                "/",
            ],
            stdout=subprocess.PIPE,
            stderr=errfile,
        )
        try:
            proc.stdout.readline()
            proc.stdout.close()
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=15)
    err_text = err_path.read_text(errors="replace")
    assert proc.returncode == 1
    assert "Traceback" not in err_text
    assert "BrokenPipe" not in err_text
    assert "Exception ignored" not in err_text


def test_traceback_opt_in(tmp_path, cli, monkeypatch):
    monkeypatch.setenv("PKGFORGE_TRACEBACK", "1")
    result = cli("install", str(tmp_path / "missing.txt"), str(tmp_path / "dest_f"))
    err = result.err.decode()
    assert result.rc == 2
    lines = [line for line in err.splitlines() if line]
    assert lines[-1].startswith("pkgforge: error:")
    traceback_lines = [i for i, line in enumerate(lines) if "Traceback" in line]
    assert traceback_lines, err
    assert traceback_lines[0] < len(lines) - 1


def test_unexpected_exception_propagates(monkeypatch):
    # Guard: an exception outside the boundary's own classes must still
    # escape main() as a real traceback, not get swallowed.
    def _raise(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(pkgforge.dbdump.DbDump, "__call__", _raise)
    with pytest.raises(RuntimeError, match="boom"):
        pkgforge.main(["dbdump", "-f", "rpmspecfiles"])


@pytest.mark.parametrize("value", ["0", "no", "off", "", "bogus"])
def test_traceback_off_values_print_one_line(value, tmp_path, cli, monkeypatch):
    monkeypatch.setenv("PKGFORGE_TRACEBACK", value)
    result = cli("install", str(tmp_path / "missing.txt"), str(tmp_path / "dest_f"))
    err = result.err.decode()
    assert result.rc == 2
    assert err.count("\n") == 1
    assert "Traceback" not in err


def test_duho_traceback_variable_is_not_pkgforges_switch(tmp_path, cli, monkeypatch):
    monkeypatch.setenv("DUHO_TRACEBACK", "1")
    result = cli("install", str(tmp_path / "missing.txt"), str(tmp_path / "dest_f"))
    err = result.err.decode()
    assert result.rc == 2
    assert err.count("\n") == 1
    assert "Traceback" not in err


@pytest.mark.posix
def test_ctrl_c_exits_130_silently(monkeypatch, capsys):
    import os
    import signal

    def _interrupt(*_a, **_kw):
        os.kill(os.getpid(), signal.SIGINT)
        raise AssertionError("SIGINT was not delivered")

    monkeypatch.setattr(pkgforge.dbdump.DbDump, "__call__", _interrupt)
    rc = pkgforge.main(["dbdump", "-f", "rpmspecfiles"])
    assert rc == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def _truncated_archive(tmp_path, suffix):
    import io
    import tarfile

    payload = bytes(range(256)) * 2000
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name in ("f1", "f2"):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    data = buf.getvalue()
    if suffix == ".tar.gz":
        import gzip

        data = gzip.compress(data)
    path = tmp_path / ("trunc" + suffix)
    path.write_bytes(data[: len(data) // 2])
    return path


@pytest.mark.parametrize("suffix", [".tar", ".tar.gz"])
def test_truncated_tar_is_one_line(suffix, tmp_path, cli):
    archive = _truncated_archive(tmp_path, suffix)
    result = cli(
        "--db",
        str(tmp_path / "db.jsonl"),
        "--buildroot",
        str(tmp_path / "root"),
        "install",
        "-p",
        "-d",
        str(archive),
        "/t",
    )
    err = result.err.decode().strip()
    assert result.rc == 1
    assert err.count("\n") == 0
    assert err.startswith("pkgforge: error: ")
    assert str(archive) in err
    assert "Traceback" not in err


def test_corrupt_tgz_is_one_line(tmp_path, cli):
    archive = tmp_path / "garbage.tgz"
    archive.write_bytes(b"garbage")
    result = cli(
        "--db",
        str(tmp_path / "db.jsonl"),
        "--buildroot",
        str(tmp_path / "root"),
        "install",
        "-p",
        "-d",
        str(archive),
        "/t",
    )
    err = result.err.decode().strip()
    assert result.rc == 1
    assert err.count("\n") == 0
    assert err.startswith("pkgforge: error: ")


def test_sqlite_db_in_unwritable_directory_is_one_line(tmp_path, cli):
    import os

    locked = tmp_path / "ro"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("directory permissions are not enforced (root or Windows)")
        src = tmp_path / "src"
        src.write_text("x")
        result = cli(
            "--db",
            str(locked / "x.db"),
            "--buildroot",
            str(tmp_path / "root"),
            "install",
            "-p",
            str(src),
            "/y",
        )
    finally:
        locked.chmod(0o755)
    err = result.err.decode().strip().splitlines()
    assert result.rc == 1
    assert err[-1].startswith("pkgforge: error: ")
    assert "Traceback" not in "".join(err)


def test_sqlite_garbage_file_is_one_line(tmp_path, cli):
    db = tmp_path / "text.db"
    db.write_text("this is not a database\n")
    result = cli("--db-format", "sqlite", "--db", str(db), "dbdump", "-f", "rpm")
    err = result.err.decode().strip()
    assert result.rc == 1
    assert err.count("\n") == 0
    assert err.startswith("pkgforge: error: ")
    assert str(db) in err


def test_sqlite_bad_meta_json_is_one_line(tmp_path, cli):
    import sqlite3

    db = tmp_path / "meta.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE entries (path TEXT PRIMARY KEY, mode TEXT, owner TEXT, "
        '"group" TEXT, type TEXT, meta_json TEXT, removed INTEGER DEFAULT 0)'
    )
    conn.execute("INSERT INTO entries VALUES ('/a','644','-','-','file','{bad',0)")
    conn.commit()
    conn.close()
    result = cli("--db", str(db), "dbdump", "-f", "rpm")
    err = result.err.decode().strip()
    assert result.rc == 1
    assert err.count("\n") == 0
    assert "invalid meta_json" in err


def test_deeply_nested_jsonl_is_one_line(tmp_path, cli):
    db = tmp_path / "deep.jsonl"
    depth = 100_000
    db.write_text(
        '{"path": "/a", "mode": "644", "owner": "-", "group": "-", '
        '"type": "file", "meta": {"k": ' + "[" * depth + "]" * depth + "}}\n"
    )
    result = cli("--db", str(db), "dbdump", "-f", "rpm")
    err = result.err.decode().strip()
    assert result.rc == 1
    assert err.count("\n") == 0
    # A parser with a deep enough stack reads it; the schema then refuses
    # the non-string meta value instead.
    assert "nested too deeply" in err or "meta" in err


_YAML_HEAD = "/a:\n  mode: '644'\n  owner: '-'\n  group: '-'\n  type: file\n  meta:\n"


@pytest.mark.parametrize(
    "body",
    [
        "    k: " + "[" * 100_000 + "]" * 100_000 + "\n",
        "    k: " + "{a: " * 100 + "b" + "}" * 100 + "\n",
        "    k:\n" + "      " + "- " * 100 + "x\n",
        "    k:\n" + "".join(" " * (6 + 2 * i) + "a:\n" for i in range(200)),
    ],
    ids=["flow_list", "flow_map", "compact_sequence", "block_mapping"],
)
def test_deeply_nested_yaml_is_one_line(body, tmp_path, cli):
    # Checked before libyaml parses: its C composer would crash the
    # interpreter on deep nesting instead of raising.
    db = tmp_path / "deep.yaml"
    db.write_text(_YAML_HEAD + body)
    result = cli("--db", str(db), "dbdump", "-f", "rpm")
    err = result.err.decode().strip()
    assert result.rc == 1
    assert err.count("\n") == 0
    assert "nested too deeply" in err
