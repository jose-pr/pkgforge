"""``[defaults]`` and ``[[rules]]``, applied when an entry is recorded."""

from __future__ import annotations

import json
import os
import typing
from pathlib import Path

import pytest

from pkgforge import Project
from pkgforge.config import EntryDefaults, EntryRule, ProjectConfig

UNSET = {"mode": "-", "owner": "-", "group": "-", "meta": {}}


def entry(kind: str = "directory", **over) -> typing.Dict[str, typing.Any]:
    return {**UNSET, "type": kind, **over}


def project(tmp_path: Path, db: typing.Optional[str] = None, **tables) -> Project:
    config = ProjectConfig(**tables)
    return Project(
        tmp_path / "root", None if db is None else tmp_path / db, config=config
    )


def rule(*match: str, **fields: str) -> EntryRule:
    return EntryRule(match=match, **fields)


def recorded(db: Path) -> typing.Dict[str, typing.Dict[str, typing.Any]]:
    out = {}
    for line in db.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        out[rec.pop("path")] = rec
    return out


def write(path: Path, text: str = "x", *, mode: typing.Optional[int] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    if mode is not None:
        path.chmod(mode)
    return path


# -- resolution order -------------------------------------------------------


def test_defaults_by_type(tmp_path):
    p = project(
        tmp_path,
        defaults=EntryDefaults(
            owner="root", group="adm", file_mode="644", exec_mode="755", dir_mode="750"
        ),
    )
    directory = p._configured("/d", entry("directory"))
    assert (directory["mode"], directory["owner"], directory["group"]) == (
        "750",
        "root",
        "adm",
    )
    link = p._configured("/l", entry("symlink"))
    assert (link["mode"], link["owner"], link["group"]) == ("-", "root", "adm")
    # A file that is not there is not executable.
    plain = p._configured("/missing", entry("file"))
    assert plain["mode"] == "644"


@pytest.mark.posix
def test_defaults_tell_an_executable_from_a_file(tmp_path):
    p = project(tmp_path, defaults=EntryDefaults(file_mode="644", exec_mode="755"))
    write(tmp_path / "root/bin/tool", mode=0o700)
    write(tmp_path / "root/etc/conf", mode=0o600)
    assert p._configured("/bin/tool", entry("file"))["mode"] == "755"
    assert p._configured("/etc/conf", entry("file"))["mode"] == "644"
    other = write(tmp_path / "other", mode=0o700)
    assert p._configured("/etc/conf", entry("file"), local=other)["mode"] == "755"


def test_a_flag_beats_a_rule_beats_defaults(tmp_path):
    p = project(
        tmp_path,
        defaults=EntryDefaults(owner="d", group="d", dir_mode="755"),
        rules=(rule("/etc/**", mode="700", owner="r", group="r"),),
    )
    got = p._configured("/etc/x", entry("directory", mode="600", owner="flag"))
    assert (got["mode"], got["owner"], got["group"]) == ("600", "flag", "r")
    got = p._configured("/usr/x", entry("directory"))
    assert (got["mode"], got["owner"], got["group"]) == ("755", "d", "d")


def test_the_first_matching_rule_only_and_per_field(tmp_path):
    p = project(
        tmp_path,
        defaults=EntryDefaults(dir_mode="755"),
        rules=(
            rule("/a/**", group="first"),
            rule("/a/**", mode="700", group="second"),
        ),
    )
    got = p._configured("/a/x", entry("directory"))
    # The first rule sets only the group: the mode is not taken from the
    # second rule but from [defaults].
    assert (got["mode"], got["group"]) == ("755", "first")


def test_rules_see_the_type_and_honour_negation(tmp_path):
    p = project(
        tmp_path,
        rules=(
            rule("(?type:directory)/opt/**", mode="750"),
            rule("!/srv/keep", "/srv/**", mode="600"),
        ),
    )
    assert p._configured("/opt/a", entry("directory"))["mode"] == "750"
    assert p._configured("/opt/a", entry("file"))["mode"] == "-"
    assert p._configured("/srv/a", entry("file"))["mode"] == "600"
    assert p._configured("/srv/keep", entry("file"))["mode"] == "-"


def test_configured_leaves_its_argument_alone(tmp_path):
    p = project(tmp_path, rules=(rule("/a", mode="600"),))
    original = entry("file")
    got = p._configured("/a", original)
    assert got["mode"] == "600" and original["mode"] == "-"


def test_a_rule_owner_comes_from_the_package_name(tmp_path):
    cfg = tmp_path / "pkgforge.cfg"
    cfg.write_bytes(
        b'[package]\nname = "hello"\n[[rules]]\nmatch = ["/var/**"]\n'
        b'owner = "${package.name}"\n'
    )
    p = Project.from_config(cfg, buildroot=tmp_path / "root")
    assert p._configured("/var/lib", entry("directory"))["owner"] == "hello"


# -- the record path --------------------------------------------------------


@pytest.mark.posix
def test_install_without_a_mode_stages_the_rule_mode(tmp_path):
    p = project(
        tmp_path,
        "db.jsonl",
        rules=(rule("/etc/**", mode="640", group="adm"),),
        defaults=EntryDefaults(file_mode="600"),
    )
    p.init()
    src = write(tmp_path / "a.conf", mode=0o666)
    other = write(tmp_path / "b.txt", mode=0o666)
    p.install([src], "/etc", parents=True)
    p.install([other], "/srv", parents=True)
    assert os.stat(tmp_path / "root/etc/a.conf").st_mode & 0o777 == 0o640
    assert os.stat(tmp_path / "root/srv/b.txt").st_mode & 0o777 == 0o600
    db = recorded(tmp_path / "db.jsonl")
    assert (db["/etc/a.conf"]["mode"], db["/etc/a.conf"]["group"]) == ("640", "adm")
    assert db["/srv/b.txt"]["mode"] == "600"
    p.install([src], "/etc", mode="600", parents=True)
    assert os.stat(tmp_path / "root/etc/a.conf").st_mode & 0o777 == 0o600


@pytest.mark.posix
def test_record_tree_children_take_the_rule_before_the_disk(tmp_path):
    p = project(tmp_path, "db.jsonl", rules=(rule("/opt/tree/ruled/**", mode="640"),))
    p.init()
    src = tmp_path / "tree"
    write(src / "ruled/file", mode=0o666)
    write(src / "plain", mode=0o664)
    (src / "link").symlink_to("plain")
    p.install([src], "/opt/tree", into=False, parents=True, record_tree=True)
    db = recorded(tmp_path / "db.jsonl")
    assert db["/opt/tree/ruled/file"]["mode"] == "640"
    assert db["/opt/tree/plain"]["mode"] == "664"
    assert db["/opt/tree/link"]["mode"] == "-"


@pytest.mark.posix
def test_scan_applies_rules_unless_the_mode_is_read_from_disk(tmp_path):
    p = project(tmp_path, rules=(rule("/opt/**", mode="640"),))
    write(tmp_path / "root/opt/f", mode=0o600)
    (tmp_path / "root/opt/l").symlink_to("f")
    default = dict(p.scan("/opt"))
    assert default["/opt/f"]["mode"] == "640"
    assert default["/opt/l"]["mode"] == "-"
    from_disk = dict(p.scan("/opt", mode="--"))
    assert from_disk["/opt/f"]["mode"] == "600"


# -- the command line -------------------------------------------------


def config_file(tmp_path: Path, text: str) -> str:
    path = tmp_path / "pkgforge.cfg"
    path.write_bytes(text.encode("utf-8"))
    return str(path)


@pytest.mark.posix
def test_rules_apply_through_the_command_line(cli, tmp_path):
    cfg = config_file(
        tmp_path,
        'db = "f.jsonl"\nbuildroot = "root"\n[[rules]]\n'
        'match = ["/etc/hello/**"]\nmode = "640"\ngroup = "adm"\n',
    )
    (tmp_path / "root").mkdir()
    src = write(tmp_path / "hello.conf")
    db = tmp_path / "f.jsonl"
    assert cli("--config", cfg, "-q", "initdb").rc == 0
    result = cli("--config", cfg, "-q", "install", "-p", str(src), "/etc/hello")
    assert result.rc == 0, result.err
    got = recorded(db)["/etc/hello/hello.conf"]
    assert (got["mode"], got["group"]) == ("640", "adm")
    result = cli(
        "--config", cfg, "-q", "install", "-p", "-m", "600", str(src), "/etc/hello"
    )
    assert result.rc == 0, result.err
    assert recorded(db)["/etc/hello/hello.conf"]["mode"] == "600"
