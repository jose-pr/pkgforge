"""Tests for buildutils.

Cross-platform tests cover the DB record format, the exclude/match grammar, DB
read/write, and dbdump rendering. Tests that need POSIX facilities (chmod via
``FileEntry.apply``, real owner/group lookup) are guarded with ``os.name``.
"""

from __future__ import annotations

import os

import pytest
import yaml

import buildutils
from buildutils.common import AUTO, DEFAULT, BuildUtils, FileEntry, FileType, mode_to_octal
from buildutils.exclude import PathMatch, PathMatchStmt

POSIX = pytest.mark.skipif(os.name != "posix", reason="requires POSIX facilities")


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


def test_all_commands_registered():
    names = {c._parsername_ for c in BuildUtils._subcommands_}
    assert names == {"install", "scan", "dbdump", "initdb"}


def test_root_parser_builds_and_help_renders():
    parser = BuildUtils._parser_()
    text = parser.format_help()
    for name in ("install", "scan", "dbdump", "initdb"):
        assert name in text


@pytest.mark.parametrize("name", ["install", "scan", "dbdump", "initdb"])
def test_subcommand_parser_builds(name):
    # Building the whole tree exercises each subcommand's _parser_ (incl.
    # install's -D/-d override).
    parser = BuildUtils._parser_()
    assert parser is not None


# --------------------------------------------------------------------------
# FileEntry record format
# --------------------------------------------------------------------------


def test_mode_to_octal():
    assert mode_to_octal(0o100644) == "644"
    assert mode_to_octal(0o40755) == "755"
    assert mode_to_octal(0o777) == "777"


def test_from_path_mode_is_octal_string(tmp_path):
    f = tmp_path / "f"
    f.write_text("hi")
    entry = FileEntry.from_path(f)
    assert isinstance(entry["mode"], str)
    # An octal string, parseable back with base 8.
    int(entry["mode"], 8)
    assert entry["type"] == FileType.File


def test_from_path_dir_and_symlink_type(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    assert FileEntry.from_path(d)["type"] == FileType.Directory


@POSIX
def test_from_path_symlink_type(tmp_path):
    target = tmp_path / "t"
    target.write_text("x")
    link = tmp_path / "l"
    link.symlink_to(target)
    assert FileEntry.from_path(link)["type"] == FileType.Symlink


def test_resolve_for_fills_auto_from_disk(tmp_path):
    f = tmp_path / "f"
    f.write_text("hi")
    base: FileEntry = {
        "mode": AUTO,
        "owner": "root",
        "group": DEFAULT,
        "type": AUTO,
        "meta": {},
    }
    resolved = FileEntry.resolve_for(base, f)
    # AUTO fields resolved from disk; explicit values kept.
    int(resolved["mode"], 8)
    assert resolved["owner"] == "root"
    assert resolved["type"] == FileType.File


@POSIX
def test_apply_sets_mode(tmp_path):
    f = tmp_path / "f"
    f.write_text("hi")
    entry: FileEntry = {
        "mode": "600",
        "owner": DEFAULT,
        "group": DEFAULT,
        "type": FileType.File,
        "meta": {},
    }
    FileEntry.apply(entry, f)
    assert (f.stat().st_mode & 0o777) == 0o600


# --------------------------------------------------------------------------
# install (end-to-end, POSIX)
# --------------------------------------------------------------------------


def test_scan_default_buildroot_does_not_crash(tmp_path, monkeypatch):
    # Regression: buildroot default must be Path("."), not the str ".", or
    # scan.py's `self.buildroot / path` is str/str -> TypeError.
    from buildutils.scan import ScanCmd

    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f").write_text("x")
    db = tmp_path / "files.yaml"

    # Drive through the parser with NO --buildroot (default applies).
    parser = ScanCmd._parser_()
    inst = parser.parse_args(["--db", str(db), "sub"])
    inst()  # must not raise (the regression: str "." / str -> TypeError)
    recorded = yaml.safe_load(db.read_text()) or {}
    # Key form uses os separators; assert the file was recorded by basename.
    assert any(os.path.basename(k.replace("\\", "/")) == "f" for k in recorded)


@POSIX
def test_install_remove_source_directory(tmp_path):
    # Regression: --remove-source on a directory must rmtree, not unlink.
    from buildutils.install import Install

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.yaml"
    srcdir = tmp_path / "tree"
    (srcdir / "sub").mkdir(parents=True)
    (srcdir / "sub" / "a").write_text("x")

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "--remove-source",
            str(srcdir),
            "/opt/tree",
        ]
    )
    inst()
    assert not srcdir.exists()  # directory source removed
    assert (root / "opt" / "tree" / "tree" / "sub" / "a").exists()


@POSIX
def test_install_file_hardlinks_and_records(tmp_path):
    from buildutils.install import Install

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.yaml"
    src = tmp_path / "app.conf"
    src.write_text("hello")

    # Drive through the real parser (argv), as the CLI does.
    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-m",
            "640",
            str(src),
            "/etc",
        ]
    )
    inst()

    staged = root / "etc" / "app.conf"
    assert staged.exists()
    # Hardlink: same inode as the source (the regression guard for os.link).
    assert staged.stat().st_ino == src.stat().st_ino
    assert (staged.stat().st_mode & 0o777) == 0o640
    recorded = yaml.safe_load(db.read_text())
    assert "/etc/app.conf" in recorded
    assert recorded["/etc/app.conf"]["mode"] == "640"


# --------------------------------------------------------------------------
# DB read/write round-trip
# --------------------------------------------------------------------------


def _cmd(tmp_path, **over):
    inst = BuildUtils.__new__(BuildUtils)
    inst.db = tmp_path / "files.yaml"
    inst.buildroot = tmp_path
    for k, v in over.items():
        setattr(inst, k, v)
    return inst


def test_add_and_load_db_roundtrip(tmp_path):
    cmd = _cmd(tmp_path)
    entry: FileEntry = {
        "mode": "644",
        "owner": "root",
        "group": "root",
        "type": FileType.File,
        "meta": {"k": "v"},
    }
    cmd.add_entry("/usr/bin/x", entry)
    db = cmd.loaddb()
    assert "/usr/bin/x" in db
    assert db["/usr/bin/x"]["mode"] == "644"
    assert db["/usr/bin/x"]["type"] == "file"


def test_remove_entry_marks_none(tmp_path):
    cmd = _cmd(tmp_path)
    cmd.add_entry(
        "/a", {"mode": "644", "owner": "-", "group": "-", "type": FileType.File, "meta": {}}
    )
    cmd.remove_entry("/a")
    db = cmd.loaddb()
    # Last write for /a is the removal marker.
    assert db["/a"] is None


def test_loaddb_missing_returns_empty(tmp_path):
    cmd = _cmd(tmp_path)
    assert cmd.loaddb() == {}


# --------------------------------------------------------------------------
# exclude / match grammar
# --------------------------------------------------------------------------


def _entry(type=FileType.File, meta=None):
    return {
        "mode": "644",
        "owner": "-",
        "group": "-",
        "type": type,
        "meta": meta or {},
    }


def test_glob_match():
    m = PathMatch([PathMatchStmt.parse("**/*.pyc")])
    from pathlib import Path

    assert m.match(Path("/a/b/x.pyc"), _entry()) is True
    assert m.match(Path("/a/b/x.py"), _entry()) is None


def test_type_test():
    from pathlib import Path

    m = PathMatch([PathMatchStmt.parse("(?type:directory)**")])
    assert m.match(Path("/a"), _entry(type=FileType.Directory)) is True
    assert m.match(Path("/a"), _entry(type=FileType.File)) in (None, False)


def test_meta_test():
    from pathlib import Path

    m = PathMatch([PathMatchStmt.parse("(?meta:keep=1)**")])
    assert m.match(Path("/a"), _entry(meta={"keep": "1"})) is True
    assert m.match(Path("/a"), _entry(meta={"keep": "0"})) in (None, False)


def test_inverted_type_test():
    # Regression: (?!type:...) must actually invert (helper used to drop its return).
    from pathlib import Path

    m = PathMatch([PathMatchStmt.parse("(?!type:file)**")])
    # A directory is NOT a file -> inverted test passes -> match True.
    assert m.match(Path("/a"), _entry(type=FileType.Directory)) is True
    # A file IS a file -> inverted test fails.
    assert m.match(Path("/a"), _entry(type=FileType.File)) in (None, False)


def test_negated_statement():
    from pathlib import Path

    m = PathMatch([PathMatchStmt.parse("!**/*.pyc")])
    assert m.match(Path("/a/x.pyc"), _entry()) is False


def test_empty_matcher_matches_all():
    from pathlib import Path

    assert PathMatch([]).match(Path("/anything"), _entry()) is True


# --------------------------------------------------------------------------
# dbdump rendering
# --------------------------------------------------------------------------


def test_rpmspecfile_render():
    from buildutils.dbdump import rpmspecfile

    line = rpmspecfile(
        "/usr/bin/x",
        {"mode": "755", "owner": "root", "group": "root", "type": "file", "meta": {}},
    )
    assert line == b'%attr(755,root,root) "/usr/bin/x"\n'


def test_rpmspecfile_dir_prefix():
    from buildutils.dbdump import rpmspecfile

    line = rpmspecfile(
        "/etc/app",
        {"mode": "755", "owner": "root", "group": "root", "type": "directory", "meta": {}},
    )
    assert line.startswith(b"%dir ")


def test_rpmspecfile_rpmprefix_meta():
    from buildutils.dbdump import rpmspecfile

    line = rpmspecfile(
        "/etc/app.conf",
        {
            "mode": "644",
            "owner": "root",
            "group": "root",
            "type": "file",
            "meta": {"rpmprefix": "%config(noreplace)"},
        },
    )
    assert line.startswith(b"%config(noreplace) %attr(")


def test_dbdump_writes_manifest(tmp_path, capsysbinary):
    from buildutils.dbdump import DbDump

    db = tmp_path / "files.yaml"
    db.write_text(
        yaml.safe_dump(
            {
                "/usr/bin/x": {
                    "mode": "755",
                    "owner": "root",
                    "group": "root",
                    "type": "file",
                    "meta": {},
                },
                "/removed": None,
            }
        )
    )
    out = tmp_path / "out.txt"
    cmd = DbDump.__new__(DbDump)
    cmd.db = db
    cmd.buildroot = tmp_path
    cmd.exclude = []
    cmd.format = "rpmspecfiles"
    cmd.output = out
    cmd()
    text = out.read_bytes()
    assert b'%attr(755,root,root) "/usr/bin/x"' in text
    # None (removed) entries are skipped.
    assert b"/removed" not in text
