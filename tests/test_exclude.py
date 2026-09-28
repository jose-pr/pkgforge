"""New regression/guard tests added while fixing the ``--exclude`` grammar.

The exclude tests that predate this pass stay in ``test_pkgforge.py``; every
test added by this pass lands here instead (see the plan's own decision on
this split).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pkgforge.exclude import PathMatch, PathMatchStmt


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
# glob engine: ** recursion, anchoring, root handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        # A relative pattern (with or without an explicit "**") matches at
        # any depth, and an anchored "**/*.pyc" recurses too -- identically
        # on every supported Python version, unlike PurePath.match.
        ("**/*.pyc", "/x.pyc", True),
        ("**/*.pyc", "/a/b/x.pyc", True),
        ("**/*.pyc", "/a/b/c/x.pyc", True),
        ("/**/*.pyc", "/x.pyc", True),
        ("/**/*.pyc", "/a/b/x.pyc", True),
        ("/**/*.pyc", "/a/b/c/x.pyc", True),
        ("*.pyc", "/x.pyc", True),
        ("*.pyc", "/a/b/x.pyc", True),
        ("*.pyc", "/a/b/c/x.pyc", True),
        # A trailing "**" means "the contents of this directory" -- one or
        # more segments -- not the directory itself.
        ("**/tmp/**", "/tmp/f", True),
        ("**/tmp/**", "/a/tmp/s/f", True),
        ("**/tmp/**", "/tmp", None),
        ("**/tmp/**", "/a/tmp", None),
        ("/opt/app/**", "/opt/app/a/b/c", True),
        ("/opt/app/**", "/opt/app", None),
        # A "**" in the middle matches zero or more full segments.
        ("/a/**/*.pyc", "/a/x.pyc", True),
        ("/a/**/*.pyc", "/a/b/x.pyc", True),
        ("/a/**/*.pyc", "/a/b/c/x.pyc", True),
        ("/a/**/*.pyc", "/x.pyc", None),
        ("**/etc/*", "/etc/app.conf", True),
        # "[...]"/"[!...]" is a character class.
        ("/opt/[!x]pp/*", "/opt/app/a", True),
    ],
)
def test_glob_matrix(pattern, path, expected, make_entry):
    m = PathMatch([PathMatchStmt.parse(pattern)])
    assert m.match(Path(path), make_entry()) is expected


def test_relative_pattern_stays_inside_root(tmp_path, make_entry):
    # A relative pattern is matched against the path taken relative to the
    # ROOT, not right-anchored against the full local path -- it can no
    # longer reach a component above the root just because the root shares
    # a name with the pattern's own leading segment.
    root = tmp_path / "srv" / "app"
    m = PathMatch([PathMatchStmt.parse("app/*.conf")], root)
    assert m.match(root / "main.conf", make_entry()) is None
    assert m.match(root / "app" / "x.conf", make_entry()) is True

    # A path outside the root entirely (only reachable via the Python API)
    # keeps matching by its own text, with no ValueError from relative_to().
    outside = PathMatch([PathMatchStmt.parse("*.conf")], root)
    elsewhere = tmp_path.parent / "elsewhere" / "x.conf"
    assert outside.match(elsewhere, make_entry()) is True


def test_relative_root_keeps_anchor(tmp_path, monkeypatch, make_entry):
    # Guards: a relative --buildroot (the default ".") must not make an
    # absolute pattern's rebased copy float and match at any depth.
    monkeypatch.chdir(tmp_path)
    m = PathMatch([PathMatchStmt.parse("/tmp")], Path("."))
    assert m.match(Path("tmp"), make_entry()) is True
    assert m.match(Path("a/tmp"), make_entry()) is None


def test_root_glob_characters_are_literal(tmp_path, monkeypatch, make_entry):
    # Guards: glob metacharacters in the root's own text (e.g. a source
    # directory named "pkg[1]") must not be read as glob syntax.
    monkeypatch.chdir(tmp_path)
    root = Path("pkg[1]")
    m = PathMatch([PathMatchStmt.parse("/skip")], root)
    assert m.match(root / "skip", make_entry()) is True
    assert m.match(root / "keep", make_entry()) is None


def test_single_file_root_matches_name(tmp_path, make_entry):
    # A single-file scan uses the file itself as the root; a relative
    # pattern must still match it by its own name.
    root = tmp_path / "z.pyc"
    m = PathMatch([PathMatchStmt.parse("*.pyc")], root)
    assert m.match(root, make_entry()) is True


@pytest.mark.filterwarnings("error::FutureWarning")
def test_escaped_root_compiles_without_warning(tmp_path, make_entry):
    # Guards: the escaped root text must not compile to a regex with an
    # unescaped "[" inside a class (Python's re module warns
    # "Possible nested set" for that, which -W error::FutureWarning traps).
    root = tmp_path / "pkg[1]"
    m = PathMatch([PathMatchStmt.parse("/skip")], root)
    assert m.match(root / "skip", make_entry()) is True


# --------------------------------------------------------------------------
# end-to-end: per-command anchoring and recursion, through the real CLI
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_install_doublestar_any_depth(tmp_path, cli):
    # A recursive "**" excludes a .pyc at every depth, not only depth 2.
    src = tmp_path / "src"
    for rel in ("x.pyc", "a/x.pyc", "a/b/x.pyc", "a/b/c/x.pyc", "a/b/c/keep.txt"):
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"

    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-d",
        "-X",
        "/**/*.pyc",
        str(src),
        "/opt/app",
    )
    assert result.rc == 0

    staged = root / "opt" / "app" / "src"
    assert not any(staged.rglob("*.pyc"))
    assert (staged / "a" / "b" / "c" / "keep.txt").exists()


@pytest.mark.posix
def test_dbdump_trailing_doublestar(tmp_path, cli):
    # A trailing "**" drops everything below the directory, but keeps the
    # directory's own %dir entry.
    root = tmp_path / "root"
    db = tmp_path / "files.jsonl"
    src = tmp_path / "src"
    (src / "a" / "b").mkdir(parents=True)
    (src / "a" / "b" / "c").write_text("x")

    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-d",
            str(src),
            "/opt/app",
        ).rc
        == 0
    )
    # Scan the PARENT of /opt/app, so /opt/app itself is recorded as a
    # %dir entry too (scan records a scanned PATH's children, not itself).
    assert cli("--db", str(db), "--buildroot", str(root), "scan", "/opt").rc == 0

    out = tmp_path / "out.txt"
    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "dbdump",
        "-f",
        "rpmspecfiles",
        "-X",
        "/opt/app/**",
        str(out),
    )
    assert result.rc == 0
    text = out.read_text()
    assert text.strip() == '%dir %attr(-,-,-) "/opt/app"'
    assert "/opt/app/src" not in text


@pytest.mark.posix
def test_scan_default_buildroot_anchor(tmp_path, monkeypatch, cli, cmd):
    # Guards: the default (relative ".") buildroot must not make an absolute
    # -X pattern float and match at every depth (F35).
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tmp").mkdir()
    (tmp_path / "a" / "tmp").mkdir(parents=True)

    result = cli("--db", "files.jsonl", "scan", "-X", "/tmp", "/")
    assert result.rc == 0

    recorded = {k.replace("\\", "/") for k in cmd(name="files.jsonl").loaddb()}
    assert "/a/tmp" in recorded
    assert "/tmp" not in recorded
