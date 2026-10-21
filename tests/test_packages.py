"""The ``package`` field: which package an entry ships in, the ``[[packages]]``
table, ``-P`` and the rule and step ``package`` keys."""

from __future__ import annotations

import json
import sqlite3
import typing
from pathlib import Path

import pytest

from pkgforge import DbError, Project, UsageError, open_db
from pkgforge.config import (
    DebianInfo,
    PackageInfo,
    ProjectConfig,
    RpmInfo,
    SubPackageInfo,
)
from pkgforge.entry import normalize_package

OLD_DB = "this DB was written by an older pkgforge: run initdb and record again"

CFG = """\
[package]
name = "hello"

[[packages]]
name = "${package.name}-doc"

[[rules]]
match = ["/usr/share/doc/**"]
package = "hello-doc"
"""


def run(cli, tmp_path: Path, *args: str, cfg: typing.Optional[str] = CFG):
    base = ["--db", str(tmp_path / "f.jsonl"), "--buildroot", str(tmp_path / "root")]
    if cfg is not None:
        path = tmp_path / "pkgforge.cfg"
        path.write_text(cfg, encoding="utf-8")
        base = ["--config", str(path), *base]
    return cli(*base, *args)


def records(tmp_path: Path) -> typing.Dict[str, typing.Dict[str, typing.Any]]:
    out = {}
    for line in (tmp_path / "f.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        out[rec.pop("path")] = rec
    return out


def source(tmp_path: Path, name: str = "x") -> Path:
    path = tmp_path / name
    path.write_text("x", encoding="utf-8")
    return path


# -- the helper ---------------------------------------------------------------


@pytest.mark.parametrize("value", ["-", ""])
def test_normalize_package_unset(value):
    assert normalize_package(value) == "-"


@pytest.mark.parametrize("value", ["hello", "hello-doc", "libfoo2.0", "g++", "a1"])
def test_normalize_package_accepts(value):
    assert normalize_package(value) == value


@pytest.mark.parametrize("value", ["Hello", "a", "-doc", "he llo", "he_llo", "--"])
def test_normalize_package_refuses(value):
    with pytest.raises(UsageError, match="invalid package"):
        normalize_package(value)


# -- recording ----------------------------------------------------------------


@pytest.mark.posix
def test_a_flag_records_the_package(cli, tmp_path):
    src = source(tmp_path)
    result = run(cli, tmp_path, "install", "-p", "-P", "hello-doc", str(src), "/opt/a")
    assert result.rc == 0, result.err
    assert records(tmp_path)["/opt/a/x"]["package"] == "hello-doc"


@pytest.mark.posix
def test_a_rule_fills_an_unset_package_and_a_flag_beats_it(cli, tmp_path):
    src = source(tmp_path)
    assert run(cli, tmp_path, "install", "-p", str(src), "/usr/share/doc/a").rc == 0
    flagged = run(
        cli, tmp_path, "install", "-p", "-P", "hello", str(src), "/usr/share/doc/b"
    )
    assert flagged.rc == 0, flagged.err
    recs = records(tmp_path)
    assert recs["/usr/share/doc/a/x"]["package"] == "hello-doc"
    assert recs["/usr/share/doc/b/x"]["package"] == "hello"


@pytest.mark.posix
def test_the_main_package_is_the_fallback(cli, tmp_path):
    src = source(tmp_path)
    assert run(cli, tmp_path, "install", "-p", str(src), "/opt/a").rc == 0
    assert records(tmp_path)["/opt/a/x"]["package"] == "hello"


@pytest.mark.posix
def test_without_a_package_table_the_package_is_unset(cli, tmp_path):
    src = source(tmp_path)
    assert run(cli, tmp_path, "install", "-p", str(src), "/opt/a", cfg=None).rc == 0
    assert records(tmp_path)["/opt/a/x"]["package"] == "-"


@pytest.mark.posix
def test_an_undeclared_package_exits_2_before_staging(cli, tmp_path):
    src = source(tmp_path)
    result = run(cli, tmp_path, "install", "-p", "-P", "other", str(src), "/opt/a")
    assert result.rc == 2
    assert "package 'other' is neither [package] name nor a [[packages]] name" in (
        result.err.decode()
    )
    assert not (tmp_path / "root" / "opt").exists()


@pytest.mark.posix
def test_a_package_flag_without_a_package_table_is_refused(cli, tmp_path):
    src = source(tmp_path)
    result = run(
        cli, tmp_path, "install", "-p", "-P", "hello", str(src), "/opt/a", cfg=None
    )
    assert result.rc == 2


def test_a_bad_package_flag_exits_2(cli, tmp_path):
    result = run(cli, tmp_path, "mkdir", "-P", "Bad_Name", "/opt/a")
    assert result.rc == 2
    assert "invalid package" in result.err.decode()


@pytest.mark.posix
def test_mkdir_and_symlink_take_a_package(cli, tmp_path):
    assert run(cli, tmp_path, "mkdir", "-p", "-P", "hello-doc", "/opt/d").rc == 0
    assert run(cli, tmp_path, "symlink", "-p", "-P", "hello-doc", "t", "/opt/l").rc == 0
    recs = records(tmp_path)
    assert recs["/opt/d"]["package"] == "hello-doc"
    assert recs["/opt/l"]["package"] == "hello-doc"


@pytest.mark.posix
def test_the_library_keyword(tmp_path):
    config = ProjectConfig(
        package=PackageInfo("hello"), packages=(SubPackageInfo("hello-doc"),)
    )
    project = Project(tmp_path / "root", tmp_path / "f.jsonl", config=config)
    [(_, doc)] = project.mkdir(["/opt/d"], package="hello-doc", parents=True)
    [(_, main)] = project.mkdir(["/opt/e"], parents=True)
    assert (doc["package"], main["package"]) == ("hello-doc", "hello")
    with pytest.raises(UsageError, match="package 'other' is neither"):
        project.mkdir(["/opt/f"], package="other", parents=True)


STAGE_CFG = CFG + """
[[stage]]
dir = "/opt/doc"
package = "hello-doc"

[[stage]]
symlink = "/opt/link"
target = "doc"
package = "hello-doc"

[[stage]]
file = "/opt/f"

[[stage]]
src = "x"
dest = "/opt/doc"
package = "hello-doc"
"""


@pytest.mark.posix
def test_a_stage_step_takes_its_package(tmp_path):
    (tmp_path / "x").write_text("x", encoding="utf-8")
    cfg = tmp_path / "pkgforge.cfg"
    cfg.write_text(STAGE_CFG, encoding="utf-8")
    from pkgforge import config as cfgmod

    project = Project(
        tmp_path / "root",
        tmp_path / "f.jsonl",
        config=cfgmod.load(cfg),
    )
    project.stage(init=True)
    recs = records(tmp_path)
    assert recs["/opt/doc"]["package"] == "hello-doc"
    assert recs["/opt/link"]["package"] == "hello-doc"
    assert recs["/opt/f"]["package"] == "hello"
    assert recs["/opt/doc/x"]["package"] == "hello-doc"


@pytest.mark.posix
def test_owned_parents_and_record_tree_children_follow_the_rules(cli, tmp_path):
    tree = tmp_path / "t"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "a.txt").write_text("x", encoding="utf-8")
    cfg = 'own = ["/usr/share/doc/hello-doc", "/opt/hello"]\n' + CFG
    result = run(
        cli,
        tmp_path,
        "install",
        "-p",
        "--record-tree",
        "-P",
        "hello",
        str(tree),
        "/usr/share/doc/hello-doc",
        cfg=cfg,
    )
    assert result.rc == 0, result.err
    recs = records(tmp_path)
    # The flag applies to the installed directory only.
    assert recs["/usr/share/doc/hello-doc/t"]["package"] == "hello"
    assert recs["/usr/share/doc/hello-doc"]["package"] == "hello-doc"
    assert recs["/usr/share/doc/hello-doc/t/sub"]["package"] == "hello-doc"
    assert recs["/usr/share/doc/hello-doc/t/sub/a.txt"]["package"] == "hello-doc"

    other = run(cli, tmp_path, "mkdir", "-p", "/opt/hello/bin", cfg=cfg)
    assert other.rc == 0, other.err
    assert records(tmp_path)["/opt/hello"]["package"] == "hello"


# -- the cfg ------------------------------------------------------------------


def test_the_cfg_hydrates_nested_tables():
    config = ProjectConfig.from_dict(
        {
            "package": {
                "name": "hello",
                "version": "1.0",
                "release": "2",
                "arch": "any",
                "maintainer": "A Person <a@example.org>",
                "homepage": "https://example.org",
                "rpm": {"requires": ["bash"]},
                "debian": {
                    "depends": ["bash"],
                    "section": "utils",
                    "priority": "optional",
                },
            },
            "packages": [
                {
                    "name": "hello-doc",
                    "arch": "all",
                    "rpm": {"requires": ["hello"]},
                    "debian": {"depends": ["hello"]},
                }
            ],
            "rules": [{"match": ["/a"], "package": "hello-doc"}],
            "stage": [{"dir": "/a", "package": "hello"}],
        }
    )
    assert config.package.rpm == RpmInfo(requires=("bash",))
    assert config.package.debian == DebianInfo(("bash",), "utils", "optional")
    assert config.packages[0].debian.depends == ("hello",)
    assert config.rules[0].package == "hello-doc"
    assert config.stage[0].package == "hello"


def _load(**tables):
    return ProjectConfig.from_dict(tables)


@pytest.mark.parametrize(
    ("tables", "message"),
    [
        (
            {
                "package": {"name": "hello"},
                "packages": [{"name": "hello-doc"}, {"name": "hello-doc"}],
            },
            r"packages\[1\]: 'hello-doc' is declared twice",
        ),
        (
            {"package": {"name": "hello"}, "packages": [{"name": "hello"}]},
            r"packages\[0\]: 'hello' is the \[package\] name",
        ),
        ({"packages": [{"name": "hello-doc"}]}, r"\[\[packages\]\] need \[package\]"),
        (
            {
                "package": {"name": "hello"},
                "rules": [{"match": ["/a"], "package": "other"}],
            },
            r"rules\[0\]: package 'other' is neither \[package\] name nor a",
        ),
        (
            {
                "package": {"name": "hello"},
                "stage": [{"dir": "/a", "package": "other"}],
            },
            r"stage\[0\]: package 'other' is neither",
        ),
        (
            {"rules": [{"match": ["/a"], "package": "other"}]},
            r"rules\[0\]: package 'other' is neither",
        ),
        (
            {"rules": [{"match": ["/a"], "package": "-"}]},
            "package: invalid package '-'",
        ),
        ({"package": {"name": "hello", "depends": ["bash"]}}, "depends"),
        ({"package": {"name": "Hello"}}, "name: 'Hello' is not a package name"),
        ({"package": {"name": "h"}}, "name: 'h' is not a package name"),
        ({"package": {"name": "hello", "version": "1.0-1"}}, "version: '1.0-1'"),
        ({"package": {"name": "hello", "version": "v1"}}, "version: 'v1'"),
        ({"package": {"name": "hello", "release": "1-1"}}, "release: '1-1'"),
        ({"package": {"name": "hello", "arch": "x86_64"}}, "arch: 'x86_64'"),
        ({"package": {"name": "hello", "maintainer": "a@b.c"}}, "maintainer:"),
        (
            {"package": {"name": "hello", "maintainer": "A <a@b.c>, B <b@c.d>"}},
            "maintainer:",
        ),
        (
            {"package": {"name": "hello", "summary": "a\nb"}},
            "summary: must be one line",
        ),
        ({"package": {"name": "hello", "homepage": "a\tb"}}, "homepage: must be one"),
        (
            {"package": {"name": "hello", "description": "a\x07b"}},
            "description: must hold no control character",
        ),
        (
            {"package": {"name": "hello", "rpm": {"requires": [""]}}},
            "requires: an item must not be empty",
        ),
        (
            {"package": {"name": "hello", "rpm": {"requires": ["a\nb"]}}},
            "requires: must be one line",
        ),
        (
            {"package": {"name": "hello", "debian": {"depends": ["a, b"]}}},
            "depends: 'a, b' must not contain ','",
        ),
        (
            {"package": {"name": "hello", "debian": {"section": "A b"}}},
            "section: 'A b'",
        ),
        (
            {"package": {"name": "hello", "debian": {"priority": "high"}}},
            "priority: 'high'",
        ),
        (
            {
                "package": {"name": "hello"},
                "packages": [{"name": "hello-doc", "arch": "none"}],
            },
            "arch: 'none'",
        ),
        (
            {"package": {"name": "hello"}, "packages": [{"name": "hello-doc", "x": 1}]},
            "x",
        ),
    ],
)
def test_cfg_refusals(tables, message):
    pytest.importorskip("anycfg")
    with pytest.raises(UsageError, match=message):
        _load(**tables)


# -- the DB -------------------------------------------------------------------

RECORD = {
    "mode": "644",
    "owner": "root",
    "group": "root",
    "type": "file",
    "kind": "doc",
    "caps": "-",
    "package": "hello-doc",
    "meta": {},
}


@pytest.mark.parametrize("name", ["f.jsonl", "f.yaml", "f.sqlite"])
def test_an_eight_key_record_round_trips(tmp_path, name):
    if name.endswith("yaml"):
        pytest.importorskip("yaml")
    provider = open_db(tmp_path / name)
    provider.init()
    provider.add("/a", RECORD)
    assert open_db(tmp_path / name).load() == {"/a": RECORD}


def test_a_non_string_package_is_refused(tmp_path):
    db = tmp_path / "f.jsonl"
    db.write_text(
        json.dumps({"path": "/a", **{**RECORD, "package": 5}}) + "\n", encoding="utf-8"
    )
    with pytest.raises(DbError, match="package must be a string, got 5"):
        open_db(db).load()


def test_a_seven_key_record_is_refused(tmp_path):
    record = {k: v for k, v in RECORD.items() if k != "package"}
    db = tmp_path / "f.jsonl"
    db.write_text(json.dumps({"path": "/a", **record}) + "\n", encoding="utf-8")
    with pytest.raises(DbError, match=f"missing field package; {OLD_DB}"):
        open_db(db).load()


def test_a_sqlite_table_without_the_column_is_refused(tmp_path):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE entries (path TEXT PRIMARY KEY, mode TEXT, owner TEXT, "
        '"group" TEXT, type TEXT, kind TEXT, caps TEXT, meta_json TEXT, '
        "removed INTEGER DEFAULT 0)"
    )
    conn.commit()
    conn.close()
    provider = open_db(db)
    with pytest.raises(DbError, match=OLD_DB):
        provider.load()
    with pytest.raises(DbError, match=OLD_DB):
        provider.add("/b", RECORD)
