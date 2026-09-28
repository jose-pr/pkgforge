"""Tests driving pkgforge through ``pkgforge.main(argv)``, as a user would.

Cross-platform tests use the ``cli`` fixture directly. POSIX-only tests (real
staging: chmod, chown, hardlinks, "/"-rooted destinations) are marked
``@pytest.mark.posix``.
"""

from __future__ import annotations

import argparse
import errno
import importlib.metadata
import json
import logging
import os
import re
import runpy
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import pkgforge
from pkgforge.db import open_db

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def _write(path: Path, content: str = "x") -> Path:
    path.write_text(content)
    return path


# --------------------------------------------------------------------------
# env isolation
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


@pytest.mark.parametrize("fmt", ["rpmspecfiles", "debian"])
def test_dbdump_stdout_honours_redirect(tmp_path, fmt):
    # A raw os.fdopen(sys.stdout.fileno(), ...) raises io.UnsupportedOperation
    # the moment sys.stdout isn't backed by a real fd (redirect_stdout here;
    # equally pytest capture or an embedding host).
    import contextlib
    import io

    db = tmp_path / "files.jsonl"
    _seed_tool_entry(db)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pkgforge.main(["--db", str(db), "dbdump", "-f", fmt, "-"])
    assert rc in (0, None)
    assert "/usr/bin/tool" in buf.getvalue()


def test_dbdump_stdout_keeps_print_order(tmp_path):
    # A subprocess so stdout is a real (block-buffered, non-tty) pipe: the
    # old fdopen-on-fd-1 approach wrote the manifest straight to the fd,
    # bypassing Python's own stdout buffer, so text already print()-ed but
    # not yet flushed came out AFTER it.
    db = tmp_path / "files.jsonl"
    _seed_tool_entry(db)
    script = (
        f"print('%files'); import pkgforge; "
        f"pkgforge.main(['--db', {str(db)!r}, 'dbdump', '-f', 'rpmspecfiles', '-'])"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    assert lines[0] == "%files"
    assert any("/usr/bin/tool" in line for line in lines[1:])


@pytest.mark.posix
def test_dbdump_closed_pipe_is_quiet(tmp_path):
    # Piping a large manifest into `head -1` closes the read end early: a
    # raw fd write there raised BrokenPipeError from inside dbdump AND again
    # from the fdopen's own finally-flush, producing a traceback plus an
    # "Exception ignored" message at interpreter shutdown.
    db = tmp_path / "files.jsonl"
    provider = open_db(db, for_read=False)
    for i in range(20000):
        provider.add(
            f"/a/{i:05d}",
            {"mode": "644", "owner": "-", "group": "-", "type": "file", "meta": {}},
        )
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
    cmd = f"{sys.executable} -m pkgforge --db {db} dbdump -f rpmspecfiles - | head -1"
    result = subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, text=True)
    assert "Traceback" not in result.stderr
    assert "Exception ignored" not in result.stderr


def test_dbdump_unknown_format_fails(tmp_path, cli):
    db = tmp_path / "files.jsonl"
    result = cli("--db", str(db), "dbdump", "-f", "toml", "-")
    assert result.rc != 0
    assert b"rpmspecfiles" in result.err


def test_dbdump_unknown_format_checked_before_db_load(tmp_path, cli):
    # A corrupt DB must never be the reported reason for a plain format typo:
    # the format is checked before anything is loaded.
    db = tmp_path / "files.jsonl"
    db.write_text("{bad\n", encoding="utf-8")
    result = cli("--db", str(db), "dbdump", "-f", "nope", "-")
    assert result.rc == 2
    err = result.err.decode()
    assert "unknown format 'nope'" in err
    assert "rpmspecfiles" in err
    assert "Traceback" not in err


def test_dbdump_registered_format_accepted(tmp_path, cli, monkeypatch):
    # Guard: the format check must stay a runtime registry lookup, not a
    # duho.Choice frozen at import, so a format registered later still works.
    from pkgforge.dbdump import PER_ENTRY_FORMATS, rpmspecfile

    db = tmp_path / "files.jsonl"
    _seed_tool_entry(db)
    monkeypatch.setitem(PER_ENTRY_FORMATS, "custom", rpmspecfile)
    result = cli("--db", str(db), "dbdump", "-f", "custom", "-")
    assert result.rc == 0
    assert b"/usr/bin/tool" in result.out


def test_dbdump_debian_onto_file_exits_2(tmp_path, cli):
    db = tmp_path / "files.jsonl"
    _seed_tool_entry(db)
    target = tmp_path / "out"
    target.write_text("existing", encoding="utf-8")

    result = cli("--db", str(db), "dbdump", "-f", "debian", str(target))
    assert result.rc == 2
    assert "directory" in result.err.decode()
    assert target.read_text(encoding="utf-8") == "existing"


def test_dbdump_rpm_onto_directory_exits_2(tmp_path, cli):
    db = tmp_path / "files.jsonl"
    _seed_tool_entry(db)
    target = tmp_path / "outdir"
    target.mkdir()

    result = cli("--db", str(db), "dbdump", "-f", "rpmspecfiles", str(target))
    assert result.rc == 2
    assert result.err
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("case", ["unset", "dash", "missing"])
def test_dbdump_warns_without_db(tmp_path, cli, caplog, case):
    argv = []
    if case == "dash":
        argv = ["--db", "-"]
    elif case == "missing":
        argv = ["--db", str(tmp_path / "nope.jsonl")]

    with caplog.at_level("WARNING"):
        result = cli(*argv, "dbdump", "-f", "rpmspecfiles", "-")
    assert result.rc == 0
    assert result.out == b""
    assert any(
        "empty manifest" in r.message
        for r in caplog.records
        if r.name == "pkgforge.dbdump"
    )
    if case == "missing":
        assert not (tmp_path / "nope.jsonl").exists()


def test_dbdump_warns_when_exclude_drops_all(tmp_path, cli, caplog):
    db = tmp_path / "files.jsonl"
    _seed_tool_entry(db)

    with caplog.at_level("WARNING"):
        result = cli("--db", str(db), "dbdump", "-X", "**", "-f", "rpmspecfiles", "-")
    assert result.rc == 0
    assert result.out == b""
    assert any(
        "survived" in r.message for r in caplog.records if r.name == "pkgforge.dbdump"
    )


def test_initdb_warns_without_db(tmp_path, cli, caplog, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with caplog.at_level("WARNING"):
        result = cli("initdb")
    assert result.rc == 0
    assert list(tmp_path.iterdir()) == []
    assert any(
        "nothing to initialize" in r.message
        for r in caplog.records
        if r.name == "pkgforge.initdb"
    )


@pytest.mark.parametrize("ext", ["jsonl", "yaml", "db"])
def test_compact_missing_db_creates_nothing(tmp_path, cli, caplog, ext):
    db = tmp_path / f"nope.{ext}"
    with caplog.at_level("WARNING"):
        result = cli("--db", str(db), "compact")
    assert result.rc == 0
    assert not db.exists()
    assert any(
        "nothing to compact" in r.message
        for r in caplog.records
        if r.name == "pkgforge.compact"
    )


def test_malformed_db_is_one_line_error(tmp_path, cli):
    db = tmp_path / "files.jsonl"
    db.write_text('{"path": "/a", not valid json\n', encoding="utf-8")

    result = cli("--db", str(db), "dbdump", "-f", "rpmspecfiles", "-")
    assert result.rc == 1
    err = result.err.decode()
    assert "Traceback" not in err
    lines = [line for line in err.splitlines() if line]
    assert len(lines) == 1


def test_dbdump_hand_edited_yaml_mode(tmp_path, cli):
    # A hand-edited YAML DB with an UNQUOTED mode must not be
    # misinterpreted as an int (0755 -> 493) and silently mispackaged.
    db = tmp_path / "files.yaml"
    db.write_text(
        "/usr/bin/tool:\n"
        "  mode: 0755\n"
        "  owner: root\n"
        "  group: root\n"
        "  type: file\n"
        "  meta: {}\n",
        encoding="utf-8",
    )
    result = cli("--db", str(db), "dbdump", "-f", "rpmspecfiles", "-")
    assert result.rc == 0
    assert b'%attr(0755,root,root) "/usr/bin/tool"' in result.out


# --------------------------------------------------------------------------
# --db-format validation (checked before the command runs)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["db", "stdout"])
def test_unknown_db_format_is_usage_error(tmp_path, cli, case):
    db = tmp_path / "x.db"
    argv = ["--db-format", "toml"]
    if case == "db":
        argv = ["--db", str(db), *argv]
    result = cli(*argv, "initdb")
    assert result.rc == 2
    err = result.err.decode()
    lines = [line for line in err.splitlines() if line]
    assert len(lines) == 1
    assert "choose from" in lines[0]
    assert "jsonl" in lines[0]
    assert "Traceback" not in err
    if case == "db":
        assert not db.exists()


def test_unknown_db_format_from_env(tmp_path, cli, monkeypatch):
    monkeypatch.setenv("PKGFORGE_DB_FORMAT", "toml")
    db = tmp_path / "x.db"
    result = cli("--db", str(db), "initdb")
    assert result.rc == 2
    err = result.err.decode()
    assert "choose from" in err
    assert "jsonl" in err
    assert "Traceback" not in err
    assert not db.exists()

    assert cli("--help").rc == 0


@pytest.mark.posix
def test_unknown_db_format_stages_nothing(tmp_path, cli):
    root = tmp_path / "root"
    src = _write(tmp_path / "a")
    result = cli(
        "--db-format",
        "toml",
        "--buildroot",
        str(root),
        "install",
        "-p",
        str(src),
        "/opt",
    )
    assert result.rc == 2
    assert not root.exists()


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
def test_scan_drop_stale_sqlite_sees_walk(tmp_path, cli):
    # Pin: the walk's batch() must commit before --drop-stale's own reload,
    # or a second sqlite connection wouldn't see the rows this walk just
    # wrote and would wrongly tombstone them too.
    root = tmp_path / "root"
    db = tmp_path / "files.db"
    tree = root / "usr" / "share" / "tool"
    tree.mkdir(parents=True)
    (tree / "keep").write_text("x")
    stale = tree / "gone"
    stale.write_text("y")

    assert (
        cli("--db", str(db), "--buildroot", str(root), "scan", "/usr/share/tool").rc
        == 0
    )

    stale.unlink()
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "scan",
        "--drop-stale",
        "/usr/share/tool",
    )
    assert result.rc == 0

    loaded = open_db(db, for_read=True).load()
    assert loaded["/usr/share/tool/keep"] is not None
    assert loaded["/usr/share/tool/gone"] is None


@pytest.mark.posix
def test_scan_non_utf8_name_sqlite_one_line_error(tmp_path, cli):
    root = tmp_path / "root"
    tree = root / "usr" / "share" / "tool"
    tree.mkdir(parents=True)
    (tree / "ok").write_text("x")
    bad_name = os.fsencode(str(tree)) + b"/raw\xe9"
    try:
        with open(bad_name, "wb") as fh:
            fh.write(b"y")
    except OSError as exc:
        if exc.errno == errno.EILSEQ:  # e.g. APFS refuses non-UTF-8 names
            pytest.skip("filesystem rejects non-UTF-8 names")
        raise

    db = tmp_path / "files.db"
    result = cli("--db", str(db), "--buildroot", str(root), "scan", "/usr/share/tool")
    assert result.rc == 1
    err = result.err.decode(errors="replace")
    assert "UTF-8" in err
    assert "Traceback" not in err


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


# --------------------------------------------------------------------------
# in-process CLI surface (cross-platform)
# --------------------------------------------------------------------------


def test_help_lists_every_subcommand(cli):
    from pkgforge.common import PkgForge

    result = cli("--help")
    assert result.rc == 0
    text = result.out.decode()
    for c in PkgForge._subcommands_:
        assert c._parsername_ in text


def test_unknown_subcommand_exits_2(cli):
    result = cli("bogus")
    assert result.rc == 2
    assert result.err


def test_decompress_path_kind_without_destination_exits_2(cli):
    # -x takes the next token as its (optional) kind, so "SRC" is swallowed
    # and "DST" is the only source left: the required destination is missing.
    result = cli("install", "-x", "SRC", "DST")
    assert result.rc == 2
    assert result.err


def test_print_completion_bash(cli):
    result = cli("--print-completion", "bash")
    assert result.rc == 0
    assert b" -F _duho_complete_" in result.out


def test_prog_name_is_pkgforge():
    from pkgforge.common import PkgForge

    parser = PkgForge._parser_()
    assert parser.prog == "pkgforge"
    assert parser.format_usage().startswith("usage: pkgforge")


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_binds_pkgforge(cli, shell):
    result = cli("--print-completion", shell)
    assert result.rc == 0
    text = result.out.decode()
    assert "PkgForge" not in text
    if shell == "bash":
        last_line = text.rstrip().splitlines()[-1]
        assert re.search(r"-F _duho_complete_pkgforge_[0-9a-f]+ pkgforge$", last_line)
    elif shell == "zsh":
        assert text.startswith("#compdef pkgforge")
    else:
        assert "complete -c 'pkgforge'" in text


def test_verbose_flag_reaches_command_logger(tmp_path, cli):
    logger = logging.getLogger("pkgforge.initdb")

    result = cli("-v", "--db", str(tmp_path / "x.jsonl"), "initdb")
    assert result.rc == 0
    assert logger.getEffectiveLevel() == logging.DEBUG

    result = cli(
        "--loglevel",
        "pkgforge.initdb:WARNING",
        "--db",
        str(tmp_path / "y.jsonl"),
        "initdb",
    )
    assert result.rc == 0
    assert logger.getEffectiveLevel() == logging.WARNING


def test_every_option_has_help():
    from pkgforge.common import PkgForge

    def _check(parser):
        for action in parser._actions:
            if isinstance(action, argparse._HelpAction):
                continue
            if isinstance(action, argparse._SubParsersAction):
                continue
            assert action.help not in (None, ""), (parser.prog, action.dest)

    root = parser = PkgForge._parser_()
    _check(root)
    subparsers_action = next(
        a for a in root._actions if isinstance(a, argparse._SubParsersAction)
    )
    for subparser in subparsers_action.choices.values():
        _check(subparser)


def test_install_help_shows_decompress_order():
    from pkgforge.install import Install
    from pkgforge.dbdump import DbDump

    install_text = " ".join(Install._parser_().format_help().split())
    assert "-x KIND SRC DST" in install_text

    dbdump_text = " ".join(DbDump._parser_().format_help().split())
    assert "rpmspecfiles" in dbdump_text
    assert "debian" in dbdump_text


def test_loglevel_help_grammar(cli):
    result = cli("--help")
    assert result.rc == 0
    text = result.out.decode()
    assert "[NAME:]LEVEL" in text
    assert "--verbose" in text
    assert "--quiet" in text
    assert "KEY=VALUE" not in text


@pytest.mark.parametrize("value", ["bogus", "pkgforge.initdb=DEBUG"])
def test_malformed_loglevel_exits_2(tmp_path, cli, value):
    result = cli("--loglevel", value, "--db", str(tmp_path / "x.jsonl"), "initdb")
    assert result.rc == 2
    err = result.err.decode()
    assert "invalid log level" in err
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("--verbose", logging.DEBUG),
        ("--quiet", logging.WARNING),
        ("--loglevel=WARNING", logging.WARNING),
    ],
)
def test_long_and_bare_level_flags(tmp_path, cli, flag, expected):
    logger = logging.getLogger("pkgforge.initdb")
    result = cli(flag, "--db", str(tmp_path / "x.jsonl"), "initdb")
    assert result.rc == 0
    assert logger.getEffectiveLevel() == expected


@pytest.mark.posix
def test_no_color_stderr_is_plain(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "usr").mkdir(parents=True)
    (root / "usr" / "a").write_text("x")

    env = {**os.environ, "PYTHONPATH": str(SRC_DIR), "NO_COLOR": "1"}
    env.pop("FORCE_COLOR", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pkgforge",
            "-r",
            str(root),
            "--db",
            str(tmp_path / "f.jsonl"),
            "scan",
            "/usr",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
    )
    err = result.stderr.decode()
    assert "Scanning" in err
    assert "\x1b[" not in err


def test_version_names_pkgforge(cli):
    try:
        importlib.metadata.version("pkgforge")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("pkgforge is not installed as a distribution")
    result = cli("--version")
    assert result.rc == 0
    assert result.out.decode().strip() == f"pkgforge {pkgforge.__version__}"


def test_python_m_runs_main(monkeypatch, capfdbinary):
    monkeypatch.setattr(sys, "argv", ["pkgforge", "--help"])
    capfdbinary.readouterr()
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("pkgforge", run_name="__main__")
    assert excinfo.value.code in (0, None)
    out = capfdbinary.readouterr().out
    assert b"usage" in out.lower()


def test_version_matches_distribution(cli):
    try:
        version = importlib.metadata.version("pkgforge")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("pkgforge is not installed as a distribution")
    result = cli("--version")
    assert result.rc == 0
    assert version in result.out.decode()
