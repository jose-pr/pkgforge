"""Tests for ``pkgforge.cfg``: the schema, its loading and its layering under
the command line."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from pkgforge import Project
from pkgforge.config import (
    EntryDefaults,
    EntryRule,
    PackageInfo,
    ProjectConfig,
    StageStep,
    load,
)
from pkgforge.exceptions import UsageError

SQLITE_HEADER = b"SQLite format 3\x00"

FULL = """\
buildroot = "root"
db = "files.jsonl"
db_format = "jsonl"
own = ["/opt/${package.name}", "/usr/share//doc/"]

[package]
name = "hello"
version = "1.2"
release = "3"
arch = "noarch"
summary = "Say hello"
license = "MIT"
maintainer = "Someone <s@example.org>"
description = "The ${package.name} program."
depends = ["glibc", "bash"]

[defaults]
owner = "root"
group = "root"
file_mode = "0644"
exec_mode = "755"
dir_mode = "755"

[[rules]]
match = ["/etc/**", "!/etc/skip"]
mode = "600"
owner = "${package.name}"

[[rules]]
match = ["(?type:directory)/var/**"]
group = "adm"

[[stage]]
src = "payload/hello"
dest = "/usr/bin/hello"
mode = "755"
meta = { rpmprefix = "%config" }
type = "file"
into = false
decompress = "GZ"
method = "link"
exclude = ["*.pyc"]
record_tree = true

[[stage]]
dir = "/var/lib/hello"
owner = "hello"

[[stage]]
file = "/etc/hello.conf"
mode = "600"

[[stage]]
symlink = "/usr/bin/hi"
target = "hello"
"""


def write(directory: Path, text: str, name: str = "pkgforge.cfg") -> Path:
    path = directory / name
    path.write_bytes(text.encode("utf-8"))
    return path


def test_full_document_loads_into_the_dataclasses(tmp_path):
    cfg = load(write(tmp_path, FULL))
    assert cfg == ProjectConfig(
        buildroot=str(tmp_path / "root"),
        db=str(tmp_path / "files.jsonl"),
        db_format="jsonl",
        own=("/opt/hello", "/usr/share/doc"),
        package=PackageInfo(
            name="hello",
            version="1.2",
            release="3",
            arch="noarch",
            summary="Say hello",
            license="MIT",
            maintainer="Someone <s@example.org>",
            description="The hello program.",
            depends=("glibc", "bash"),
        ),
        defaults=EntryDefaults(
            owner="root",
            group="root",
            file_mode="644",
            exec_mode="755",
            dir_mode="755",
        ),
        rules=(
            EntryRule(match=("/etc/**", "!/etc/skip"), mode="600", owner="hello"),
            EntryRule(match=("(?type:directory)/var/**",), group="adm"),
        ),
        stage=(
            StageStep(
                src=str(tmp_path / "payload/hello"),
                dest="/usr/bin/hello",
                mode="755",
                meta={"rpmprefix": "%config"},
                type="file",
                into=False,
                decompress="gz",
                method="link",
                exclude=("*.pyc",),
                record_tree=True,
            ),
            StageStep(dir="/var/lib/hello", owner="hello"),
            StageStep(file="/etc/hello.conf", mode="600"),
            StageStep(symlink="/usr/bin/hi", target="hello"),
        ),
    )


def _texts(value):
    """Every text value inside a loaded configuration."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _texts(key)
            yield from _texts(item)
    elif isinstance(value, tuple):
        for item in value:
            yield from _texts(item)
    elif dataclasses.is_dataclass(value):
        for item in dataclasses.fields(value):
            yield from _texts(getattr(value, item.name))


def test_text_values_are_plain_str(tmp_path):
    # Rules, defaults and steps put these values in the file DB, and YAML's
    # safe dumper refuses a str subclass.
    texts = list(_texts(load(write(tmp_path, FULL))))
    assert "hello" in texts and "%config" in texts
    assert {type(text) for text in texts} == {str}


@pytest.mark.posix
def test_configured_values_reach_a_yaml_db(tmp_path, yaml_module):
    (tmp_path / "root").mkdir()
    (tmp_path / "a").write_bytes(b"a")
    cfg = write(
        tmp_path,
        'own = ["/opt/p"]\n[package]\nname = "p"\n'
        '[defaults]\nowner = "root"\ngroup = "${package.name}"\n'
        '[[stage]]\nsrc = "a"\ndest = "/opt/p"\nmeta = { rpmprefix = "%doc" }\n',
    )
    project = Project.from_config(
        cfg, buildroot=tmp_path / "root", db=tmp_path / "f.yaml"
    )
    project.stage()
    live = project.load()
    assert live["/opt/p"]["group"] == "p"
    assert live["/opt/p/a"]["meta"] == {"rpmprefix": "%doc"}


def test_empty_file_is_all_defaults(tmp_path):
    assert load(write(tmp_path, "")) == ProjectConfig()


_STEP = {"src": "a", "dest": "/a"}

BAD_VALUES = [
    ({"defaults": {"file_mode": "8"}}, "file_mode: invalid mode '8'"),
    ({"defaults": {"dir_mode": "--"}}, "dir_mode: invalid mode"),
    ({"defaults": {"exec_mode": "auto"}}, "exec_mode: invalid mode"),
    ({"defaults": {"owner": "-"}}, "owner: invalid owner"),
    ({"defaults": {"group": "bad%name"}}, "group: invalid group"),
    ({"rules": [{"match": []}]}, "match: needs at least one statement"),
    ({"rules": [{"match": ["(?bogus"]}]}, "match: unterminated"),
    ({"rules": [{"match": ["/a"], "mode": "9"}]}, "mode: invalid mode '9'"),
    ({"rules": [{"match": ["/a"], "owner": "a b"}]}, "owner: invalid owner"),
    ({"own": ["usr"]}, "own: 'usr' must be a '/'-rooted path"),
    ({"own": ["/usr/../x"]}, "without '..'"),
    ({"own": ["/"]}, "'/' is not a package directory"),
    ({"package": {"version": "1"}}, "name"),
    ({"stage": [{}]}, "step needs exactly one of src, dir, file, symlink"),
    (
        {"stage": [{"src": "a", "dest": "/a", "dir": "/d"}]},
        "step needs exactly one of src, dir, file, symlink",
    ),
    ({"stage": [{"src": "a"}]}, "dest: required with src"),
    ({"stage": [{"dir": "/d", "dest": "/a"}]}, "dest: only valid with src"),
    ({"stage": [{"symlink": "/l"}]}, "target: required with symlink"),
    ({"stage": [{"dir": "/d", "target": "t"}]}, "target: only valid with symlink"),
    (
        {"stage": [{"symlink": "/l", "target": "t", "mode": "755"}]},
        "mode: not valid with symlink",
    ),
    (
        {"stage": [{"symlink": "/l", "target": "t", "meta": {"target": "u"}}]},
        "meta: must not hold 'target' with symlink",
    ),
    ({"stage": [{"dir": "/d", "type": "file"}]}, "type: only valid with src"),
    ({"stage": [{"file": "/f", "record_tree": True}]}, "record_tree: only valid"),
    ({"stage": [{**_STEP, "type": "bogus"}]}, "type: invalid type 'bogus'"),
    ({"stage": [{**_STEP, "type": "--"}]}, "type: invalid type '--'"),
    ({"stage": [{**_STEP, "decompress": "rar"}]}, "decompress: unknown compression"),
    ({"stage": [{**_STEP, "method": "zip"}]}, "method: unknown install method"),
    ({"stage": [{**_STEP, "exclude": ["(?x"]}]}, "exclude: unterminated"),
    ({"stage": [{"dir": ""}]}, "dir: must not be empty"),
]


@pytest.mark.parametrize(("data", "message"), BAD_VALUES)
def test_bad_values_name_the_field(data, message):
    with pytest.raises(UsageError) as excinfo:
        ProjectConfig.from_dict(data)
    assert message in str(excinfo.value)


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ('[[rules]]\nmatch = ["/a"]\nmode = 0o644\n', "rules[0].mode"),
        ("[defaults]\nowner = true\n", "defaults.owner"),
        ('[package]\nname = "x"\nversion = 1.10\n', "package.version"),
        ("own = [1]\n", "own[0]"),
    ],
)
def test_unquoted_values_are_refused(tmp_path, text, where):
    with pytest.raises(UsageError) as excinfo:
        load(write(tmp_path, text))
    message = str(excinfo.value)
    assert "expected a quoted string" in message
    assert where in message


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('dbb = "x"\n', "dbb"),
        ('[[rules]]\nmatch = ["/a"]\nmdoe = "644"\n', "mdoe"),
        ('[install]\nmode = "644"\n', "install"),
        ('[[users]]\nname = "u"\n', "users"),
        ('[[stage]]\nsrc = "a"\ndest = "/a"\nkind = "doc"\n', "kind"),
    ],
)
def test_unknown_keys_are_refused(tmp_path, text, key):
    with pytest.raises(UsageError) as excinfo:
        load(write(tmp_path, text))
    assert key in str(excinfo.value)
    assert str(tmp_path / "pkgforge.cfg") in str(excinfo.value)


def test_references_are_replaced_everywhere(tmp_path):
    cfg = load(
        write(
            tmp_path,
            """own = ["/opt/${package.name}"]
[package]
name = "demo"
description = "$${x} stays"
[[rules]]
match = ["/a"]
owner = "${package.name}"
[[stage]]
src = "a"
dest = "/opt/${package.name}/a"
""",
        )
    )
    assert cfg.own == ("/opt/demo",)
    assert cfg.rules[0].owner == "demo"
    assert cfg.stage[0].dest == "/opt/demo/a"
    assert cfg.package.description == "${x} stays"


def test_an_undefined_reference_is_an_error(tmp_path):
    text = '[package]\nname = "demo"\n[[rules]]\nmatch = ["/a"]\nowner = "${package.nmae}"\n'
    with pytest.raises(UsageError, match="undefined reference"):
        load(write(tmp_path, text))


def test_paths_are_relative_to_the_file(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    absolute = str(tmp_path / "abs")
    cfg = load(
        write(
            sub,
            'buildroot = "root"\ndb = "files.jsonl"\n'
            '[[stage]]\nsrc = "payload"\ndest = "/a"\n'
            f"[[stage]]\nsrc = '{absolute}'\ndest = \"/b\"\n",
        )
    )
    assert cfg.buildroot == str(sub / "root")
    assert cfg.db == str(sub / "files.jsonl")
    assert [step.src for step in cfg.stage] == [str(sub / "payload"), absolute]


def test_absolute_paths_and_no_db_stay(tmp_path):
    absolute = str(tmp_path / "abs")
    cfg = load(write(tmp_path, f"buildroot = '{absolute}'\ndb = \"-\"\n"))
    assert cfg.buildroot == absolute
    assert cfg.db == "-"


def test_malformed_file_names_path_and_line(tmp_path):
    path = write(tmp_path, "[package\n")
    with pytest.raises(UsageError) as excinfo:
        load(path)
    message = str(excinfo.value)
    assert message.startswith(str(path))
    assert re.search(r":\d+:", message)


def test_missing_file_is_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "missing.cfg")


# -- the command line -------------------------------------------------------


def files_below(directory: Path) -> set:
    return {
        p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()
    }


DB_CASES = [
    # (file db, PKGFORGE_DB, flags before the command, flags after, expected)
    pytest.param(None, None, (), (), None, id="default"),
    pytest.param("from_file.jsonl", None, (), (), "from_file.jsonl", id="file"),
    pytest.param(
        "from_file.jsonl", "from_env.jsonl", (), (), "from_env.jsonl", id="env"
    ),
    pytest.param(
        "from_file.jsonl",
        "from_env.jsonl",
        ("--db", "from_flag.jsonl"),
        (),
        "from_flag.jsonl",
        id="flag-before",
    ),
    pytest.param(
        "from_file.jsonl",
        "from_env.jsonl",
        (),
        ("--db", "from_flag.jsonl"),
        "from_flag.jsonl",
        id="flag-after",
    ),
]


@pytest.mark.parametrize(("file", "env", "before", "after", "expected"), DB_CASES)
def test_db_layers(cli, tmp_path, monkeypatch, file, env, before, after, expected):
    monkeypatch.chdir(tmp_path)
    config = ()
    if file is not None:
        config = ("--config", str(write(tmp_path, f'db = "{file}"\n', "p.cfg")))
    if env is not None:
        monkeypatch.setenv("PKGFORGE_DB", env)
    result = cli(*config, *before, "initdb", *after)
    assert result.rc == 0, result.err
    assert files_below(tmp_path) - {"p.cfg"} == (
        set() if expected is None else {expected}
    )


FORMAT_CASES = [
    # (file db_format, PKGFORGE_DB_FORMAT, flags before, flags after, is sqlite)
    pytest.param(None, None, (), (), False, id="default"),
    pytest.param("sqlite", None, (), (), True, id="file"),
    pytest.param("sqlite", "jsonl", (), (), False, id="env"),
    pytest.param(
        "jsonl", "jsonl", ("--db-format", "sqlite"), (), True, id="flag-before"
    ),
    pytest.param(
        "jsonl", "jsonl", (), ("--db-format", "sqlite"), True, id="flag-after"
    ),
]


@pytest.mark.parametrize(("file", "env", "before", "after", "sqlite"), FORMAT_CASES)
def test_db_format_layers(cli, tmp_path, monkeypatch, file, env, before, after, sqlite):
    monkeypatch.chdir(tmp_path)
    text = 'db = "files.dat"\n' + (f'db_format = "{file}"\n' if file else "")
    if env is not None:
        monkeypatch.setenv("PKGFORGE_DB_FORMAT", env)
    result = cli(
        "--config", str(write(tmp_path, text, "p.cfg")), *before, "initdb", *after
    )
    assert result.rc == 0, result.err
    head = (tmp_path / "files.dat").read_bytes()[: len(SQLITE_HEADER)]
    assert (head == SQLITE_HEADER) is sqlite


ROOT_CASES = [
    # (file buildroot, PKGFORGE_ROOT, flags before, flags after, where /x lands)
    pytest.param(None, None, (), (), "x", id="default"),
    pytest.param("from_file", None, (), (), "conf/from_file/x", id="file"),
    pytest.param("from_file", "from_env", (), (), "from_env/x", id="env"),
    pytest.param(
        "from_file",
        "from_env",
        ("--buildroot", "from_flag"),
        (),
        "from_flag/x",
        id="flag-before",
    ),
    pytest.param(
        "from_file",
        "from_env",
        (),
        ("--buildroot", "from_flag"),
        "from_flag/x",
        id="flag-after",
    ),
]


@pytest.mark.parametrize(("file", "env", "before", "after", "expected"), ROOT_CASES)
def test_buildroot_layers(
    cli, tmp_path, monkeypatch, file, env, before, after, expected
):
    monkeypatch.chdir(tmp_path)
    config = ()
    if file is not None:
        (tmp_path / "conf").mkdir()
        config = (
            "--config",
            str(write(tmp_path / "conf", f'buildroot = "{file}"\n', "p.cfg")),
        )
    if env is not None:
        monkeypatch.setenv("PKGFORGE_ROOT", env)
    result = cli(*config, *before, "install", *after, "-D", "", "/x")
    assert result.rc == 0, result.err
    assert files_below(tmp_path) - {"conf/p.cfg"} == {expected}


def test_lookup_order(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PKGFORGE_CONFIG")
    write(tmp_path, 'db = "cwd.jsonl"\n')
    write(tmp_path, 'db = "env.jsonl"\n', "env.cfg")
    write(tmp_path, 'db = "flag.jsonl"\n', "flag.cfg")
    configs = {"pkgforge.cfg", "env.cfg", "flag.cfg"}

    def created(*argv):
        before = files_below(tmp_path)
        result = cli(*argv)
        assert result.rc == 0, result.err
        found = files_below(tmp_path) - before
        for name in found:
            (tmp_path / name).unlink()
        return found

    assert created("initdb") == {"cwd.jsonl"}
    monkeypatch.setenv("PKGFORGE_CONFIG", "env.cfg")
    assert created("initdb") == {"env.jsonl"}
    assert created("--config", "flag.cfg", "initdb") == {"flag.jsonl"}
    monkeypatch.setenv("PKGFORGE_CONFIG", "")
    assert created("initdb") == set()
    assert files_below(tmp_path) == configs


def test_environment_naming_no_file_is_an_error(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PKGFORGE_CONFIG", "missing.cfg")
    result = cli("initdb")
    assert result.rc == 2
    assert (
        result.err == b"pkgforge: error: PKGFORGE_CONFIG names no file: missing.cfg\n"
    )


def test_flag_naming_no_file_is_an_error(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = cli("--config", "missing.cfg", "initdb")
    assert result.rc == 2
    assert b"missing.cfg" in result.err


BAD_FILES = [
    pytest.param("[package\n", "pkgforge.cfg:1:", id="malformed"),
    pytest.param('dbb = "x"\n', "dbb", id="unknown-key"),
    pytest.param(
        '[package]\nname = "a"\n[defaults]\nowner = "${package.nmae}"\n',
        "undefined reference",
        id="undefined-reference",
    ),
]


@pytest.mark.parametrize(("text", "needle"), BAD_FILES)
def test_a_bad_file_is_one_error_line(cli, tmp_path, monkeypatch, text, needle):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, text)
    monkeypatch.delenv("PKGFORGE_CONFIG")
    result = cli("initdb")
    err = result.err.decode()
    assert result.rc == 2
    assert err.startswith("pkgforge: error: ")
    assert err.count("\n") == 1
    assert needle in err
    assert "Traceback" not in err


def test_traceback_precedes_the_error_line(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "[package\n")
    monkeypatch.delenv("PKGFORGE_CONFIG")
    monkeypatch.setenv("PKGFORGE_TRACEBACK", "1")
    result = cli("initdb")
    lines = result.err.decode().splitlines()
    assert result.rc == 2
    assert lines[0].startswith("Traceback")
    assert lines[-1].startswith("pkgforge: error: pkgforge.cfg:1:")


def test_a_full_file_leaves_a_command_working(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = write(tmp_path, FULL.replace("payload/hello", "p"), "p.cfg")
    result = cli("--config", str(path), "-q", "install", "-D", "", "/x")
    assert result.rc == 0, result.err
    assert (tmp_path / "root" / "x").is_file()


# -- the library -------------------------------------------------------------


def test_project_defaults_to_an_empty_config(tmp_path):
    assert Project(tmp_path).config == ProjectConfig()


def test_project_rejects_a_foreign_config(tmp_path):
    with pytest.raises(TypeError, match="ProjectConfig"):
        Project(tmp_path, config="x")


def test_from_config_takes_the_file_values(tmp_path):
    path = write(
        tmp_path, 'buildroot = "root"\ndb = "files.dat"\ndb_format = "sqlite"\n'
    )
    project = Project.from_config(path)
    assert project.buildroot == tmp_path / "root"
    assert Path(project.db.path) == tmp_path / "files.dat"
    assert project.db.NAME == "sqlite"
    assert project.config == load(path)


def test_from_config_arguments_beat_the_file(tmp_path):
    path = write(
        tmp_path, 'buildroot = "root"\ndb = "files.dat"\ndb_format = "sqlite"\n'
    )
    project = Project.from_config(
        path,
        buildroot=tmp_path / "other",
        db=tmp_path / "other.jsonl",
        db_format="jsonl",
    )
    assert project.buildroot == tmp_path / "other"
    assert Path(project.db.path) == tmp_path / "other.jsonl"
    assert project.db.NAME == "jsonl"


def test_from_config_defaults_and_no_db(tmp_path):
    project = Project.from_config(write(tmp_path, ""))
    assert project.buildroot == Path(".")
    assert project.db is None
    project = Project.from_config(write(tmp_path, 'db = "-"\n', "dash.cfg"))
    assert project.db is None
