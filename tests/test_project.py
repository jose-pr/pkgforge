"""``pkgforge.Project``, the Python API: driven without any command class or
command line."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pkgforge import Project
from pkgforge.db import DbProvider, JSONLinesDb, open_db
from pkgforge.dbdump import dump
from pkgforge.entry import FileType
from pkgforge.exceptions import UsageError
from pkgforge.exclude import PathMatchStmt


@pytest.fixture
def staged(tmp_path):
    """A build root, a source file and a source tree to install."""
    root = tmp_path / "root"
    root.mkdir()
    app = tmp_path / "app"
    app.write_text("#!/bin/sh\n")
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "data").write_text("data")
    return root, app, tree


@pytest.mark.posix
def test_project_install_scan_and_dump_round_trip(tmp_path, staged):
    root, app, tree = staged
    db = tmp_path / "files.jsonl"
    project = Project(root, db)
    project.init()

    written = project.install([app], "/usr/bin", mode="755", parents=True)
    assert [(path, entry["mode"]) for path, entry in written] == [
        ("/usr/bin/app", "755")
    ]

    written = project.install([tree], "/opt", parents=True, record_tree=True)
    assert [path for path, _entry in written] == [
        "/opt/tree",
        "/opt/tree/sub",
        "/opt/tree/sub/data",
    ]

    (root / "etc").mkdir()
    (root / "etc" / "app.conf").write_text("conf")
    scanned = project.scan("/etc", mode="600")
    assert [(path, entry["mode"]) for path, entry in scanned] == [
        ("/etc/app.conf", "600")
    ]

    entries = project.entries()
    paths = [path for path, _entry in entries]
    assert paths == sorted(paths)
    assert "/usr/bin/app" in paths and "/etc/app.conf" in paths

    rpm = project.dump("rpm")
    assert isinstance(rpm, bytes)
    assert b"%attr(755" in rpm and b"/usr/bin/app" in rpm

    out = tmp_path / "debian"
    rendered = project.dump("debian", out)
    assert isinstance(rendered, dict) and rendered
    assert {name: (out / name).read_bytes() for name in rendered} == rendered


def test_project_without_a_db_returns_records_and_writes_nothing(tmp_path, staged):
    root, app, _tree = staged
    project = Project(root)

    written = project.install([app], "/usr/bin", parents=True)

    assert [path for path, _entry in written] == ["/usr/bin/app"]
    assert (root / "usr" / "bin" / "app").read_text() == "#!/bin/sh\n"
    assert project.load() == {}
    assert project.entries() == []
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".jsonl"] == []


def test_project_init_without_a_db_is_a_usage_error(tmp_path):
    with pytest.raises(UsageError, match="no file DB"):
        Project(tmp_path).init()


@pytest.mark.parametrize("sources", ["a", Path("a")])
def test_install_refuses_a_bare_path_for_sources(tmp_path, sources):
    with pytest.raises(TypeError, match="sequence"):
        Project(tmp_path).install(sources, "/x")


def test_install_refuses_a_path_destination(tmp_path):
    with pytest.raises(TypeError, match="dest"):
        Project(tmp_path).install(["a"], Path("/x"))  # type: ignore[arg-type]


def test_constructor_validates_its_arguments(tmp_path):
    with pytest.raises(ValueError, match="'-'"):
        Project(tmp_path, "-")
    with pytest.raises(UsageError, match="toml"):
        Project(tmp_path, tmp_path / "f.db", db_format="toml")
    with pytest.raises(ValueError, match="db_format"):
        Project(tmp_path, open_db(tmp_path / "f.jsonl"), db_format="jsonl")
    with pytest.raises(TypeError, match="buildroot"):
        Project(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="db"):
        Project(tmp_path, 5)  # type: ignore[arg-type]


def test_constructor_opens_nothing_and_takes_a_provider(tmp_path):
    project = Project(tmp_path, tmp_path / "files.jsonl")
    assert isinstance(project.db, JSONLinesDb)
    assert not (tmp_path / "files.jsonl").exists()

    provider = open_db(tmp_path / "other.jsonl")
    assert Project(tmp_path, provider).db is provider
    assert isinstance(provider, DbProvider)


def test_dump_without_a_db_file_is_a_usage_error(tmp_path):
    with pytest.raises(UsageError, match="no file DB"):
        Project(tmp_path).dump("rpm")
    with pytest.raises(UsageError, match="no file DB at"):
        Project(tmp_path, tmp_path / "missing.jsonl").dump("rpm")


def test_dump_with_an_unknown_format_names_it_before_the_db_is_read(tmp_path):
    with pytest.raises(UsageError, match="nosuchformat"):
        Project(tmp_path, tmp_path / "missing.jsonl").dump("nosuchformat")


def test_dump_checks_the_output_shape_before_the_db_is_read(tmp_path):
    with pytest.raises(UsageError, match="directory"):
        Project(tmp_path, tmp_path / "missing.jsonl").dump("rpm", tmp_path)


def test_dbdump_dump_renders_an_in_memory_db(tmp_path, make_entry):
    db = {
        "/b": make_entry(mode="644"),
        "/a": make_entry(mode="755"),
        "/gone": None,
    }

    rendered = dump(db, "rpm")

    assert isinstance(rendered, bytes)
    assert rendered.index(b'"/a"') < rendered.index(b'"/b"')
    assert b"/gone" not in rendered

    out = tmp_path / "files.txt"
    assert dump(db, "rpm", out) == rendered
    assert out.read_bytes() == rendered
    assert dump(db, "rpm", exclude=["/a"]).count(b"%attr") == 1


def test_install_takes_a_file_type_only(tmp_path):
    project = Project(tmp_path)
    with pytest.raises(TypeError, match="FileType"):
        project.install([""], "/x", type="directory", into=False)
    project.install([""], "/x", type=FileType.Directory, into=False)
    assert (tmp_path / "x").is_dir()


def test_exclude_takes_text_only(tmp_path):
    project = Project(tmp_path)
    with pytest.raises(TypeError, match="text, not PathMatchStmt"):
        project.scan("/", exclude=[PathMatchStmt.parse("x")])
    with pytest.raises(TypeError, match="text, not PathMatchStmt"):
        project.install([""], "/y", exclude=[PathMatchStmt.parse("x")])
    assert os.listdir(tmp_path) == []
