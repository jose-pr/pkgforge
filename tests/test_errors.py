"""Tests for pkgforge's error model: ``PkgForgeError``/``UsageError`` and the
``pkgforge.main()`` error boundary.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import pkgforge
from pkgforge.command import PkgForge
from pkgforge.errors import PkgForgeError, UsageError


def test_root_command_not_runnable():
    # Guard: PkgForgeCmd/PkgForge no longer override __call__ -- duho's
    # own Cmd base already raises NotImplementedError naming the class, so a
    # bare root command still fails loud if ever reached directly.
    with pytest.raises(NotImplementedError):
        PkgForge()()

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

    monkeypatch.setattr(pkgforge.duho, "main", _raise)
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
    monkeypatch.setenv("DUHO_TRACEBACK", "1")
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

    monkeypatch.setattr(pkgforge.duho, "main", _raise)
    with pytest.raises(RuntimeError, match="boom"):
        pkgforge.main([])
