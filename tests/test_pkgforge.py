"""Tests for pkgforge.

Cross-platform tests cover the DB record format, the exclude/match grammar, DB
read/write, and dbdump rendering. Tests that need POSIX facilities (chmod via
``FileEntry.apply``, real owner/group lookup) are guarded with ``os.name``.
"""

from __future__ import annotations

import os

import pytest
import yaml

import pkgforge
from pkgforge.common import AUTO, DEFAULT, PkgForge, FileEntry, FileType, mode_to_octal
from pkgforge.exclude import PathMatch, PathMatchStmt

POSIX = pytest.mark.skipif(os.name != "posix", reason="requires POSIX facilities")


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


def test_all_commands_registered():
    names = {c._parsername_ for c in PkgForge._subcommands_}
    assert names == {"install", "scan", "dbdump", "initdb", "compact"}


def test_root_parser_builds_and_help_renders():
    parser = PkgForge._parser_()
    text = parser.format_help()
    for name in ("install", "scan", "dbdump", "initdb"):
        assert name in text


def test_subcommand_parser_builds():
    # Building the whole tree exercises each subcommand's _parser_ (incl.
    # install's -D/-d override).
    parser = PkgForge._parser_()
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
    from pkgforge.scan import ScanCmd

    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f").write_text("x")
    db = tmp_path / "files.jsonl"

    # Drive through the parser with NO --buildroot (default applies).
    parser = ScanCmd._parser_()
    inst = parser.parse_args(["--db", str(db), "sub"])
    inst()  # must not raise (the regression: str "." / str -> TypeError)
    # Read back via loaddb (format-agnostic) rather than assuming a raw format.
    recorded = inst.loaddb()
    # Key form uses os separators; assert the file was recorded by basename.
    assert any(os.path.basename(k.replace("\\", "/")) == "f" for k in recorded)


@POSIX
def test_install_remove_source_directory(tmp_path):
    # Regression: --remove-source on a directory must rmtree, not unlink.
    from pkgforge.install import Install

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
    from pkgforge.install import Install

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
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
    recorded = inst.loaddb()  # format-agnostic read
    assert "/etc/app.conf" in recorded
    assert recorded["/etc/app.conf"]["mode"] == "640"


def test_install_multi_source_absolute_exclude(tmp_path):
    # Regression: PathMatch used to rewrite the shared parsed --exclude
    # statements in place, so the SECOND source got a double-prefixed pattern
    # (<src2>/<src1>/... ) that matched nothing and silently excluded nothing.
    from pkgforge.install import Install

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    for name in ("src1", "src2"):
        d = tmp_path / name / "sub"
        (d / "skip").mkdir(parents=True)
        (d / "skip" / "x").write_text("x")
        (d / "keep").mkdir()
        (d / "keep" / "y").write_text("y")

    # Absolute pattern: anchored to each source root, rebased per source.
    pattern = os.path.join(tmp_path.anchor, "sub", "skip")
    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            str(tmp_path / "src1"),
            str(tmp_path / "src2"),
            # Relative destination keeps this test cross-platform: a
            # "/"-rooted one is not absolute on Windows (no drive).
            "opt/out",
        ]
    )
    # Set the parsed statements directly rather than via argv: the exact
    # shape --exclude produces has varied across duho releases, and what
    # this guards is the reuse of ONE statement list across the per-source
    # clones, which is what `install` does either way.
    inst.exclude = [PathMatchStmt.parse(pattern)]
    inst()

    for name in ("src1", "src2"):
        staged = root / "opt" / "out" / name / "sub"
        assert (staged / "keep" / "y").exists(), name
        # The bug: only src1 was excluded; src2 kept the whole tree.
        assert not (staged / "skip").exists(), name


def test_install_decompress_from_stdin_raises_clear_error():
    # Regression: bare -x with a stdin source used to die with
    # AttributeError: 'str' object has no attribute 'suffix'.
    from pkgforge.install import Install

    parser = Install._parser_()
    inst = parser.parse_args(["-x", "-T", "-", "/dest/f"])
    with pytest.raises(ValueError, match="cannot infer compression from stdin"):
        inst()


@POSIX
def test_install_symlink_source_records_target(tmp_path):
    # A symlink source is copied as a symlink and its target recorded in meta.
    from pkgforge.install import Install

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    link = tmp_path / "app.link"
    link.symlink_to("/usr/bin/app")

    parser = Install._parser_()
    inst = parser.parse_args(
        ["--db", str(db), "--buildroot", str(root), "-p", str(link), "/usr/bin"]
    )
    inst()

    staged = root / "usr" / "bin" / "app.link"
    assert staged.is_symlink()
    assert os.readlink(staged) == "/usr/bin/app"
    recorded = inst.loaddb()["/usr/bin/app.link"]
    assert recorded["type"] == "symlink"
    assert recorded["meta"]["target"] == "/usr/bin/app"


@POSIX
def test_install_symlink_type_with_meta_target(tmp_path):
    # --type symlink with no real source: the target comes from -O target=.
    from pkgforge.install import Install

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-T",
            "--type",
            "symlink",
            "-O",
            "target=/usr/bin/app",
            "-",
            "/usr/bin/app.link",
        ]
    )
    inst()

    staged = root / "usr" / "bin" / "app.link"
    assert staged.is_symlink()
    assert os.readlink(staged) == "/usr/bin/app"


@POSIX
def test_install_decompress_gz(tmp_path):
    # Bare -x infers "gz" from the suffix and runs the real gunzip.
    import gzip
    import shutil as _shutil

    from pkgforge.install import Install

    if _shutil.which("gunzip") is None:
        pytest.skip("gunzip not available")

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src = tmp_path / "app.conf.gz"
    with gzip.open(src, "wb") as fh:
        fh.write(b"hello gz\n")

    parser = Install._parser_()
    inst = parser.parse_args(
        ["--db", str(db), "--buildroot", str(root), "-p", "-x", str(src), "/etc"]
    )
    inst()

    staged = root / "etc" / "app.conf"  # .gz stripped from the destination
    assert staged.read_bytes() == b"hello gz\n"
    assert "/etc/app.conf" in inst.loaddb()


# --------------------------------------------------------------------------
# DB read/write round-trip
# --------------------------------------------------------------------------


def _cmd(tmp_path, **over):
    inst = PkgForge.__new__(PkgForge)
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


def test_compact_command(tmp_path):
    from pkgforge.compact import Compact

    db = tmp_path / "files.jsonl"
    parser = Compact._parser_()
    # Populate an append log with a superseded entry and a removal.
    seed = PkgForge.__new__(PkgForge)
    seed.db = db
    seed.db_format = None
    seed.buildroot = tmp_path
    seed.add_entry("/x", {"mode": "644", "owner": "-", "group": "-", "type": "file", "meta": {}})
    seed.add_entry("/x", {"mode": "600", "owner": "-", "group": "-", "type": "file", "meta": {}})
    seed.add_entry("/y", {"mode": "644", "owner": "-", "group": "-", "type": "file", "meta": {}})
    seed.remove_entry("/y")
    assert len([l for l in db.read_text().splitlines() if l.strip()]) == 4

    inst = parser.parse_args(["--db", str(db), "--buildroot", str(tmp_path)])
    inst()
    lines = [l for l in db.read_text().splitlines() if l.strip()]
    assert len(lines) == 1  # only live /x remains
    assert inst.loaddb()["/x"]["mode"] == "600"


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


def test_nonmatching_recursive_dir_statement_falls_through():
    # Regression: a directory failing a recursive (**) pattern used to return
    # False, which short-circuited PathMatch.match and vetoed every later
    # statement. It must fall through (None) so statement 2 gets to decide.
    from pathlib import Path

    stmts = [PathMatchStmt.parse("**/*.pyc"), PathMatchStmt.parse("(?type:directory)**/tmp")]
    m = PathMatch(stmts)
    assert m.match(Path("/a/tmp"), _entry(type=FileType.Directory)) is True
    # Order must not matter for these non-overlapping statements.
    assert PathMatch(list(reversed(stmts))).match(
        Path("/a/tmp"), _entry(type=FileType.Directory)
    ) is True


def test_single_nonmatching_recursive_dir_still_keeps():
    # The fix must not change single-statement behavior: no decision -> _default.
    from pathlib import Path

    m = PathMatch([PathMatchStmt.parse("**/*.pyc")])
    assert m.match(Path("/a/tmp"), _entry(type=FileType.Directory)) is None


def test_root_rebase_does_not_mutate_shared_statements(tmp_path):
    # Regression: PathMatch.__init__ rewrote stmt.pattern IN PLACE, so a
    # second construction over the same parsed statements re-prefixed the
    # already-rebased pattern (/a/** -> /src1/a/** -> /src2/src1/a/**).
    # This is exactly what a multi-source install does: one PathMatch per
    # source over one shared parsed statement list.
    src1 = tmp_path / "src1"
    src2 = tmp_path / "src2"
    # Absolute on every platform (POSIX "/", Windows "C:\") so the rebase
    # branch is actually taken here, not just on the Linux runtime.
    pattern = os.path.join(tmp_path.anchor, "a", "**")

    stmts = [PathMatchStmt.parse(pattern)]
    first = PathMatch(stmts, src1)
    second = PathMatch(stmts, src2)

    assert stmts[0].pattern == pattern  # caller's statement untouched
    assert first[0].pattern == os.fspath(src1 / "a" / "**")
    # The bug: this used to be <src2>/<src1>/a/**.
    assert second[0].pattern == os.fspath(src2 / "a" / "**")
    # Each matcher still excludes under its own root.
    assert first.match(src1 / "a" / "x", _entry()) is True
    assert second.match(src2 / "a" / "x", _entry()) is True


def test_relative_pattern_statement_is_shared_not_copied(tmp_path):
    # A relative pattern needs no rewriting: rebased() returns self.
    stmt = PathMatchStmt.parse("**/*.pyc")
    assert PathMatch([stmt], tmp_path)[0] is stmt


# --------------------------------------------------------------------------
# dbdump rendering
# --------------------------------------------------------------------------


def test_rpmspecfile_render():
    from pkgforge.dbdump import rpmspecfile

    line = rpmspecfile(
        "/usr/bin/x",
        {"mode": "755", "owner": "root", "group": "root", "type": "file", "meta": {}},
    )
    assert line == b'%attr(755,root,root) "/usr/bin/x"\n'


def test_rpmspecfile_dir_prefix():
    from pkgforge.dbdump import rpmspecfile

    line = rpmspecfile(
        "/etc/app",
        {"mode": "755", "owner": "root", "group": "root", "type": "directory", "meta": {}},
    )
    assert line.startswith(b"%dir ")


def test_rpmspecfile_rpmprefix_meta():
    from pkgforge.dbdump import rpmspecfile

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
    from pkgforge.dbdump import DbDump

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
