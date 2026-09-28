"""Tests for pkgforge.

Cross-platform tests cover the DB record format, the exclude/match grammar, DB
read/write, and dbdump rendering. Tests that need POSIX facilities (chmod via
``FileEntry.apply``, real owner/group lookup) are marked ``@pytest.mark.posix``
(registered and skipped off POSIX in ``conftest.py``).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest
import yaml

from pkgforge.common import AUTO, DEFAULT, PkgForge, FileEntry, FileType, mode_to_octal
from pkgforge.exclude import PathMatch, PathMatchStmt

# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


def test_all_commands_registered():
    names = {c._parsername_ for c in PkgForge._subcommands_}
    assert names == {"install", "scan", "dbdump", "initdb", "compact"}


def test_root_parser_builds_and_help_renders():
    parser = PkgForge._parser_()
    text = parser.format_help()
    for name in {c._parsername_ for c in PkgForge._subcommands_}:
        assert name in text
    assert ":class:" not in text
    assert "``" not in text
    for var in ("PKGFORGE_DB", "PKGFORGE_DB_FORMAT", "PKGFORGE_ROOT"):
        assert var in text
    assert "jsonl" in text


def test_command_loggers_under_pkgforge(tmp_path, caplog):
    from pkgforge.initdb import InitDb

    for cls in PkgForge._subcommands_:
        assert cls._logger_name_ == "pkgforge." + cls._parsername_

    caplog.set_level(logging.INFO, logger="pkgforge")
    InitDb(db=tmp_path / "x.jsonl")()
    assert any(
        r.name == "pkgforge.initdb" and "Initialized empty DB" in r.getMessage()
        for r in caplog.records
    )

    caplog.clear()
    caplog.set_level(logging.ERROR, logger="pkgforge")
    InitDb(db=tmp_path / "y.jsonl")()
    assert caplog.records == []


def test_install_shortcuts_translate():
    # -D -> -Tp (no-target-directory + parents); -d -> --type directory.
    from pkgforge.install import Install

    parser = Install._parser_()
    inst = parser.parse_args(["-D", "-d", "src", "/dst"])
    assert inst.no_target_directory is True
    assert inst.parents is True
    assert inst.type == "directory"


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


def test_from_path_dir_type(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    assert FileEntry.from_path(d)["type"] == FileType.Directory


@pytest.mark.posix
def test_from_path_symlink_type(tmp_path):
    target = tmp_path / "t"
    target.write_text("x")
    link = tmp_path / "l"
    link.symlink_to(target)
    assert FileEntry.from_path(link)["type"] == FileType.Symlink


@pytest.mark.posix
def test_from_path_unknown_ids_fall_back_to_default(tmp_path, monkeypatch):
    import grp
    import pwd

    def _raise(*_a, **_kw):
        raise KeyError

    monkeypatch.setattr(pwd, "getpwuid", _raise)
    monkeypatch.setattr(grp, "getgrgid", _raise)

    f = tmp_path / "f"
    f.write_text("hi")
    entry = FileEntry.from_path(f)
    assert entry["owner"] == DEFAULT
    assert entry["group"] == DEFAULT


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


@pytest.mark.posix
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


def test_scan_logs_one_info_summary(tmp_path, caplog):
    from pkgforge.scan import ScanCmd

    root = tmp_path / "root"
    usr = root / "usr"
    usr.mkdir(parents=True)
    for name in ("a", "b", "c"):
        (usr / name).write_text(name)
    db = tmp_path / "f.jsonl"

    caplog.set_level(logging.DEBUG, logger="pkgforge")
    parser = ScanCmd._parser_()
    inst = parser.parse_args(["--db", str(db), "--buildroot", str(root), "/usr"])
    inst()

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert [r.getMessage() for r in info_records if "Scanning" in r.getMessage()]
    summaries = [
        r.getMessage() for r in info_records if r.getMessage().startswith("Scanned")
    ]
    assert len(summaries) == 1
    assert re.search(r"Scanned .*: 3 path\(s\) recorded", summaries[0])

    debug_records = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "Updating file entry for" in r.getMessage()
    ]
    assert len(debug_records) == 3


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
    recorded = inst.loaddb()
    assert {k.replace("\\", "/") for k in recorded} == {"/sub/f"}


def test_scan_dotdot_escape_refused(tmp_path, cli):
    root = tmp_path / "root"
    root.mkdir()
    (root / "sib").mkdir()
    db = tmp_path / "files.jsonl"

    result = cli("--db", str(db), "--buildroot", str(root), "scan", "/../sib")
    assert result.rc == 2
    assert not db.exists()


@pytest.mark.posix
def test_scan_staged_absolute_link_ok(tmp_path):
    # PATH is the link itself: an absolute (likely out-of-root) target must
    # not be followed -- only the leaf's parent is checked for a link that
    # doesn't name an existing directory.
    from pkgforge.scan import ScanCmd

    root = tmp_path / "root"
    (root / "usr" / "bin").mkdir(parents=True)
    link = root / "usr" / "bin" / "app.link"
    link.symlink_to("/usr/bin/app")
    db = tmp_path / "files.jsonl"

    parser = ScanCmd._parser_()
    inst = parser.parse_args(
        ["--db", str(db), "--buildroot", str(root), "/usr/bin/app.link"]
    )
    inst()

    recorded = inst.loaddb()
    assert recorded["/usr/bin/app.link"]["type"] == "symlink"


@pytest.mark.posix
def test_scan_relative_root_at_slash_refused(tmp_path, monkeypatch, cli):
    # Same footgun as install: PKGFORGE_ROOT unset with a cwd of '/' must
    # not silently scan (and record) the live filesystem.
    monkeypatch.chdir("/")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "f").write_text("x")
    db = tmp_path / "db.jsonl"

    result = cli("--db", str(db), "scan", str(tree))
    assert result.rc == 2
    assert not db.exists()


def test_scan_root_path_ok(tmp_path):
    # PATH "/" means the build root itself.
    from pkgforge.scan import ScanCmd

    root = tmp_path / "root"
    (root / "usr").mkdir(parents=True)
    (root / "usr" / "a").write_text("x")
    db = tmp_path / "files.jsonl"

    parser = ScanCmd._parser_()
    inst = parser.parse_args(["--db", str(db), "--buildroot", str(root), "/"])
    inst()

    # buildpath() is POSIX-only (see PkgForgeCmd's own header note): on
    # Windows the recorded keys stringify with backslashes.
    recorded = {k.replace("\\", "/") for k in inst.loaddb()}
    assert "/usr/a" in recorded


@pytest.mark.posix
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


def test_install_multi_source_absolute_exclude(tmp_path):
    # Guards: PathMatch must not rewrite the shared parsed --exclude
    # statements in place -- a second source must not get a double-prefixed
    # pattern (<src2>/<src1>/...) that matches nothing.
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
            # Absolute pattern through argv. `--exclude` is a repeatable
            # single-value option, so it parses to a FLAT list of statements
            # and does not swallow the positional run behind it.
            "-X",
            pattern,
            str(tmp_path / "src1"),
            str(tmp_path / "src2"),
            # Relative destination keeps this test cross-platform: a
            # "/"-rooted one is not absolute on Windows (no drive).
            "opt/out",
        ]
    )
    assert [s.pattern for s in inst.exclude] == [pattern]
    inst()

    for name in ("src1", "src2"):
        staged = root / "opt" / "out" / name / "sub"
        assert (staged / "keep" / "y").exists(), name
        # The bug: only src1 was excluded; src2 kept the whole tree.
        assert not (staged / "skip").exists(), name


def test_install_decompress_from_stdin_raises_clear_error():
    # Guards: bare -x with a stdin source must raise a clear ValueError, not
    # AttributeError: 'str' object has no attribute 'suffix'.
    from pkgforge.install import Install

    parser = Install._parser_()
    inst = parser.parse_args(["-x", "-T", "-", "/dest/f"])
    with pytest.raises(ValueError, match="cannot infer compression from stdin"):
        inst()


@pytest.mark.posix
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


@pytest.mark.posix
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


@pytest.mark.posix
@pytest.mark.parametrize("form", ["explicit", "inferred"])
def test_install_decompress_gz(tmp_path, form):
    # -x runs the real gunzip, both with an explicit kind and inferring it.
    # -x takes an OPTIONAL argument, so it swallows the next token: the kind
    # goes right after it, and a bare -x has to trail the positionals.
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

    common = ["--db", str(db), "--buildroot", str(root), "-p"]
    if form == "explicit":
        argv = common + ["-x", "gz", str(src), "/etc"]
    else:
        argv = common + [str(src), "/etc", "-x"]

    parser = Install._parser_()
    inst = parser.parse_args(argv)
    inst()

    staged = root / "etc" / "app.conf"  # .gz stripped from the destination
    assert staged.read_bytes() == b"hello gz\n"
    assert "/etc/app.conf" in inst.loaddb()


def test_install_decompress_kind_that_is_a_path_raises_clear_error():
    # Guards: `install -x SRC DST EXTRA` makes SRC the *kind* (argparse gives
    # -x the next token); it must raise, not run SRC as a decompressor command.
    from pkgforge.install import Install

    parser = Install._parser_()
    inst = parser.parse_args(["-x", "app.conf.gz", "src", "/etc", "/dest/f"])
    assert inst.decompress == "app.conf.gz"
    with pytest.raises(ValueError, match="looks like a path"):
        inst()


# --------------------------------------------------------------------------
# DB read/write round-trip
# --------------------------------------------------------------------------


def test_add_and_load_db_roundtrip(cmd, make_entry):
    inst = cmd()
    inst.add_entry("/usr/bin/x", make_entry(mode="644", meta={"k": "v"}))
    db = inst.loaddb()
    assert "/usr/bin/x" in db
    assert db["/usr/bin/x"]["mode"] == "644"
    assert db["/usr/bin/x"]["type"] == "file"


def test_remove_entry_marks_none(cmd, make_entry):
    inst = cmd()
    inst.add_entry("/a", make_entry(owner=DEFAULT, group=DEFAULT))
    inst.remove_entry("/a")
    db = inst.loaddb()
    # Last write for /a is the removal marker.
    assert db["/a"] is None


def test_loaddb_missing_returns_empty(cmd):
    assert cmd().loaddb() == {}


def test_compact_command(tmp_path, cmd, make_entry):
    from pkgforge.compact import Compact

    db = tmp_path / "files.jsonl"
    parser = Compact._parser_()
    # Populate an append log with a superseded entry and a removal.
    seed = cmd(name="files.jsonl")
    seed.add_entry("/x", make_entry(mode="644", owner=DEFAULT, group=DEFAULT))
    seed.add_entry("/x", make_entry(mode="600", owner=DEFAULT, group=DEFAULT))
    seed.add_entry("/y", make_entry(mode="644", owner=DEFAULT, group=DEFAULT))
    seed.remove_entry("/y")
    assert len([line for line in db.read_text().splitlines() if line.strip()]) == 4

    inst = parser.parse_args(["--db", str(db), "--buildroot", str(tmp_path)])
    inst()
    lines = [line for line in db.read_text().splitlines() if line.strip()]
    assert len(lines) == 1  # only live /x remains
    assert inst.loaddb()["/x"]["mode"] == "600"


# --------------------------------------------------------------------------
# exclude / match grammar
# --------------------------------------------------------------------------


def test_glob_match(make_entry):
    m = PathMatch([PathMatchStmt.parse("**/*.pyc")])
    assert m.match(Path("/a/b/x.pyc"), make_entry()) is True
    assert m.match(Path("/a/b/x.py"), make_entry()) is None


def test_type_test(make_entry):
    m = PathMatch([PathMatchStmt.parse("(?type:directory)**")])
    assert m.match(Path("/a"), make_entry(type=FileType.Directory)) is True
    # A statement whose inline test fails does not apply: None, never False --
    # False would veto every later statement.
    assert m.match(Path("/a"), make_entry(type=FileType.File)) is None


def test_meta_test(make_entry):
    m = PathMatch([PathMatchStmt.parse("(?meta:keep=1)**")])
    assert m.match(Path("/a"), make_entry(meta={"keep": "1"})) is True
    assert m.match(Path("/a"), make_entry(meta={"keep": "0"})) is None


def test_inverted_type_test(make_entry):
    # Guards: (?!type:...) must actually invert; the helper must not drop its
    # return.
    m = PathMatch([PathMatchStmt.parse("(?!type:file)**")])
    # A directory is NOT a file -> inverted test passes -> match True.
    assert m.match(Path("/a"), make_entry(type=FileType.Directory)) is True
    # A file IS a file -> inverted test fails: None, never False.
    assert m.match(Path("/a"), make_entry(type=FileType.File)) is None


def test_negated_statement(make_entry):
    m = PathMatch([PathMatchStmt.parse("!**/*.pyc")])
    assert m.match(Path("/a/x.pyc"), make_entry()) is False


def test_empty_matcher_matches_all(make_entry):
    assert PathMatch([]).match(Path("/anything"), make_entry()) is True


def test_nonmatching_recursive_dir_statement_falls_through(make_entry):
    # Guards: a directory failing a recursive (**) pattern must fall through
    # (None), not return False -- False would short-circuit PathMatch.match
    # and veto every later statement.
    stmts = [
        PathMatchStmt.parse("**/*.pyc"),
        PathMatchStmt.parse("(?type:directory)**/tmp"),
    ]
    m = PathMatch(stmts)
    assert m.match(Path("/a/tmp"), make_entry(type=FileType.Directory)) is True
    # Order must not matter for these non-overlapping statements.
    assert (
        PathMatch(list(reversed(stmts))).match(
            Path("/a/tmp"), make_entry(type=FileType.Directory)
        )
        is True
    )


def test_single_nonmatching_recursive_dir_still_keeps(make_entry):
    # The fix must not change single-statement behavior: no decision -> _default.
    m = PathMatch([PathMatchStmt.parse("**/*.pyc")])
    assert m.match(Path("/a/tmp"), make_entry(type=FileType.Directory)) is None


def test_root_rebase_does_not_mutate_shared_statements(tmp_path, make_entry):
    # Guards: PathMatch.__init__ must not rewrite stmt.pattern in place -- a
    # second construction over the same parsed statements must not re-prefix
    # an already-rebased pattern (/a/** -> /src1/a/** -> /src2/src1/a/**).
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
    assert first.match(src1 / "a" / "x", make_entry()) is True
    assert second.match(src2 / "a" / "x", make_entry()) is True


def test_relative_pattern_statement_is_shared_not_copied(tmp_path):
    # A relative pattern needs no rewriting: rebased() returns self.
    stmt = PathMatchStmt.parse("**/*.pyc")
    assert PathMatch([stmt], tmp_path)[0] is stmt


def test_parse_structure():
    # Pins parse()'s result shape across its construct-then-assign cleanup:
    # a leading "!" negates the whole statement, each inline test is
    # collected, and the trailing glob is whatever is left over.
    stmt = PathMatchStmt.parse("!(?!type:file)(?meta:k=v)a/*")
    assert stmt.negate is True
    assert len(stmt.tests) == 2
    assert stmt.pattern == "a/*"


@pytest.mark.parametrize(
    "clsname,rest",
    [
        ("Install", ["src", "/dst"]),
        ("ScanCmd", ["/path"]),
        ("DbDump", ["-f", "rpmspecfiles", "-"]),
    ],
)
def test_exclude_option_shape(clsname, rest):
    # Pins the shared ExcludeArgs field: --exclude/-X is an append action,
    # metavar STMT, no nargs override, and an empty-list default -- the same
    # shape on every command that takes it.
    from pkgforge.dbdump import DbDump
    from pkgforge.exclude import ExcludeArgs
    from pkgforge.install import Install
    from pkgforge.scan import ScanCmd

    classes = {"Install": Install, "ScanCmd": ScanCmd, "DbDump": DbDump}
    cls = classes[clsname]
    assert issubclass(cls, ExcludeArgs)

    actions = {a.dest: a for a in cls._parser_()._actions}
    action = actions["exclude"]
    assert action.option_strings == ["--exclude", "-X"]
    assert type(action).__name__ == "_AppendAction"
    assert action.metavar == "STMT"
    assert action.nargs is None
    assert action.default == []

    parsed = cls._parser_().parse_args(["-X", "a", "-X", "b", *rest])
    assert len(parsed.exclude) == 2


def test_pathtest_factory_alias():
    # PathTest.GENERATORS/.factory stay as back-compat aliases after moving
    # the registry off the Protocol body.
    from pkgforge.exclude import PathTest

    test = PathTest.factory("type", "file", False)
    assert test(Path("/a"), {"type": "file", "meta": {}}) is True
    assert test(Path("/a"), {"type": "directory", "meta": {}}) is False


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
        {
            "mode": "755",
            "owner": "root",
            "group": "root",
            "type": "directory",
            "meta": {},
        },
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


def test_dbdump_writes_manifest(tmp_path):
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
    parser = DbDump._parser_()
    cmd = parser.parse_args(
        ["--db", str(db), "--buildroot", str(tmp_path), "-f", "rpmspecfiles", str(out)]
    )
    cmd()
    text = out.read_bytes()
    assert b'%attr(755,root,root) "/usr/bin/x"' in text
    # None (removed) entries are skipped.
    assert b"/removed" not in text
