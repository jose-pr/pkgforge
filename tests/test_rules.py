"""``[defaults]``, ``[[rules]]`` and owned directories, applied when an entry is
recorded."""

from __future__ import annotations

import io
import json
import os
import sys
import typing
from pathlib import Path

import pytest

from pkgforge import FileType, Project, UsageError
from pkgforge.config import EntryDefaults, EntryRule, ProjectConfig

UNSET = {
    "mode": "-",
    "owner": "-",
    "group": "-",
    "kind": "-",
    "caps": "-",
    "package": "-",
    "meta": {},
}


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
        defaults=EntryDefaults(owner="daemon", group="daemon", dir_mode="755"),
        rules=(rule("/etc/**", mode="700", owner="bin", group="bin"),),
    )
    got = p._configured("/etc/x", entry("directory", mode="600", owner="sys"))
    assert (got["mode"], got["owner"], got["group"]) == ("600", "sys", "bin")
    got = p._configured("/usr/x", entry("directory"))
    assert (got["mode"], got["owner"], got["group"]) == ("755", "daemon", "daemon")


def test_the_first_matching_rule_only_and_per_field(tmp_path):
    p = project(
        tmp_path,
        defaults=EntryDefaults(dir_mode="755"),
        rules=(
            rule("/a/**", group="tty"),
            rule("/a/**", mode="700", group="disk"),
        ),
    )
    got = p._configured("/a/x", entry("directory"))
    # The first rule sets only the group: the mode is not taken from the
    # second rule but from [defaults].
    assert (got["mode"], got["group"]) == ("755", "tty")


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
        b'[[users]]\nname = "${package.name}"\n'
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
    # A child is chmod'ed to its rule's mode; with no rule the disk is kept.
    root = tmp_path / "root/opt/tree"
    assert os.stat(root / "ruled/file").st_mode & 0o777 == 0o640
    assert os.stat(root / "plain").st_mode & 0o777 == 0o664


@pytest.mark.posix
def test_record_tree_children_are_chmoded_to_the_defaults(tmp_path):
    p = project(
        tmp_path,
        "db.jsonl",
        defaults=EntryDefaults(dir_mode="700", exec_mode="711"),
        rules=(rule("/opt/p/t/x", mode="750"),),
    )
    p.init()
    src = tmp_path / "src"
    write(src / "t/x/f", mode=0o755)
    (src / "t/x").chmod(0o755)
    p.install([src / "t"], "/opt/p/t", into=False, parents=True, record_tree=True)
    base = tmp_path / "root/opt/p/t"
    assert os.stat(base / "x").st_mode & 0o777 == 0o750
    assert os.stat(base / "x/f").st_mode & 0o777 == 0o711
    db = recorded(tmp_path / "db.jsonl")
    assert db["/opt/p/t/x"]["mode"] == "750"
    assert db["/opt/p/t/x/f"]["mode"] == "711"


@pytest.mark.posix
def test_a_scan_records_the_rule_mode_and_leaves_the_disk(tmp_path):
    p = project(tmp_path, rules=(rule("/opt/**", mode="640"),))
    write(tmp_path / "root/opt/f", mode=0o600)
    assert dict(p.scan("/opt"))["/opt/f"]["mode"] == "640"
    assert os.stat(tmp_path / "root/opt/f").st_mode & 0o777 == 0o600


@pytest.mark.posix
def test_an_owned_parent_is_recorded_not_chmoded(tmp_path):
    p = project(
        tmp_path,
        "db.jsonl",
        own=("/opt/p",),
        rules=(rule("/opt/p", mode="750"),),
    )
    p.init()
    write(tmp_path / "root/opt/p/f", mode=0o644)
    (tmp_path / "root/opt/p").chmod(0o755)
    list(p.scan("/opt/p"))
    assert recorded(tmp_path / "db.jsonl")["/opt/p"]["mode"] == "750"
    assert os.stat(tmp_path / "root/opt/p").st_mode & 0o777 == 0o755


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


# -- owned directories ------------------------------------------------------


@pytest.mark.posix
def test_owned_parents_are_recorded_first_and_once(tmp_path):
    p = project(
        tmp_path,
        "db.jsonl",
        own=("/usr/share/doc/hello",),
        defaults=EntryDefaults(owner="root", group="root", dir_mode="755"),
    )
    p.init()
    src = write(tmp_path / "README")
    records = p.install([src], "/usr/share/doc/hello", parents=True)
    assert [path for path, _ in records] == [
        "/usr/share/doc/hello",
        "/usr/share/doc/hello/README",
    ]
    parent = records[0][1]
    assert parent["type"] == "directory"
    assert (parent["mode"], parent["owner"]) == ("755", "root")
    # Recorded, never chmod'ed.
    assert os.stat(tmp_path / "root/usr/share/doc/hello").st_mode & 0o777 != 0o000
    other = write(tmp_path / "NEWS")
    assert [p_ for p_, _ in p.install([other], "/usr/share/doc/hello")] == [
        "/usr/share/doc/hello/NEWS"
    ]
    assert set(recorded(tmp_path / "db.jsonl")) == {
        "/usr/share/doc/hello",
        "/usr/share/doc/hello/README",
        "/usr/share/doc/hello/NEWS",
    }


@pytest.mark.posix
def test_owned_parents_nest_below_the_prefix(tmp_path):
    p = project(tmp_path, "db.jsonl", own=("/opt/app",))
    p.init()
    src = write(tmp_path / "f")
    records = p.install([src], "/opt/app/lib/x", parents=True)
    assert [path for path, _ in records] == [
        "/opt/app",
        "/opt/app/lib",
        "/opt/app/lib/x",
        "/opt/app/lib/x/f",
    ]


@pytest.mark.posix
def test_an_explicit_entry_for_the_prefix_is_kept(tmp_path):
    p = project(tmp_path, "db.jsonl", own=("/opt/app",))
    p.init()
    p.install(
        [""], "/opt/app", mode="700", type=FileType.Directory, into=False, parents=True
    )
    records = p.install([write(tmp_path / "f")], "/opt/app")
    assert [path for path, _ in records] == ["/opt/app/f"]
    assert recorded(tmp_path / "db.jsonl")["/opt/app"]["mode"] == "700"


@pytest.mark.posix
def test_noentry_writes_no_parents(tmp_path):
    p = project(tmp_path, "db.jsonl", own=("/opt/app",))
    p.init()
    assert (
        p.install([write(tmp_path / "f")], "/opt/app", parents=True, record=False) == []
    )
    assert recorded(tmp_path / "db.jsonl") == {}


@pytest.mark.posix
def test_without_own_install_never_loads_the_db(tmp_path, monkeypatch):
    p = project(tmp_path, "db.jsonl", rules=(rule("/opt/**", mode="640"),))
    p.init()
    calls = []
    real = Project.load
    monkeypatch.setattr(Project, "load", lambda self: calls.append(1) or real(self))
    p.install([write(tmp_path / "f")], "/opt", parents=True)
    assert calls == []


@pytest.mark.posix
def test_the_db_is_read_once_per_operation(tmp_path, monkeypatch):
    p = project(tmp_path, "db.jsonl", own=("/opt/a", "/opt/b"))
    p.init()
    calls = []
    real = Project.load
    monkeypatch.setattr(Project, "load", lambda self: calls.append(1) or real(self))
    p.install(
        [write(tmp_path / "f"), write(tmp_path / "g")],
        "/opt/a",
        parents=True,
    )
    assert len(calls) == 1
    p.install([write(tmp_path / "h")], "/opt/b", parents=True)
    assert len(calls) == 2


def test_a_removal_writes_no_parents(tmp_path):
    p = project(tmp_path, own=("/opt/app",))
    assert p._write("/opt/app/x", None) is None


@pytest.mark.posix
def test_scan_records_owned_parents_once(tmp_path):
    p = project(tmp_path, own=("/opt/app",))
    write(tmp_path / "root/opt/app/lib/a")
    write(tmp_path / "root/opt/app/lib/b")
    paths = [path for path, _ in p.scan("/opt")]
    assert paths.count("/opt/app") == 1
    assert paths.index("/opt/app") < paths.index("/opt/app/lib/a")


# -- dbdump --dirs ----------------------------------------------------------


def db_with_dirs(tmp_path: Path) -> Path:
    path = tmp_path / "files.jsonl"
    rows = [
        ("/usr/share/doc", "directory"),
        ("/usr/share/doc/hello", "directory"),
        ("/usr/share/doc/hello/README", "file"),
        ("/usr/share/doc/hello-other", "directory"),
        ("/usr/bin/hello", "file"),
    ]
    lines = [
        json.dumps(
            {
                "path": name,
                "mode": "-",
                "owner": "-",
                "group": "-",
                "type": kind,
                "kind": "file",
                "caps": "-",
                "package": "-",
                "meta": {},
            }
        )
        for name, kind in rows
    ]
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def manifest(text: bytes) -> typing.List[str]:
    return [line.split('"')[1] for line in text.decode("utf-8").splitlines()]


def test_dirs_owned_drops_other_directories(tmp_path):
    db = db_with_dirs(tmp_path)
    p = Project(tmp_path, db, config=ProjectConfig(own=("/usr/share/doc/hello",)))
    assert manifest(p.dump("rpmspecfiles")) == [
        "/usr/bin/hello",
        "/usr/share/doc",
        "/usr/share/doc/hello",
        "/usr/share/doc/hello-other",
        "/usr/share/doc/hello/README",
    ]
    assert manifest(p.dump("rpmspecfiles", dirs="owned")) == [
        "/usr/bin/hello",
        "/usr/share/doc/hello",
        "/usr/share/doc/hello/README",
    ]


def test_dirs_owned_needs_prefixes(tmp_path):
    p = Project(tmp_path, db_with_dirs(tmp_path))
    with pytest.raises(UsageError, match="needs owned prefixes"):
        p.dump("rpmspecfiles", dirs="owned")
    with pytest.raises(UsageError, match="unknown dirs"):
        p.dump("rpmspecfiles", dirs="some")


def test_dbdump_dirs_through_the_command_line(cli, tmp_path, monkeypatch):
    db = db_with_dirs(tmp_path)
    base = ("--db", str(db), "dbdump", "-f", "rpmspecfiles")
    result = cli(*base, "--dirs", "owned", "--own", "/usr/share/doc/hello", "-")
    assert result.rc == 0, result.err
    assert manifest(result.out) == [
        "/usr/bin/hello",
        "/usr/share/doc/hello",
        "/usr/share/doc/hello/README",
    ]
    result = cli(*base, "--dirs", "owned", "-")
    assert result.rc == 2
    assert b"--dirs owned needs owned prefixes" in result.err

    monkeypatch.setattr(sys, "stdin", io.StringIO(db.read_text(encoding="utf-8")))
    result = cli(
        "--db", "-", "dbdump", "-f", "rpmspecfiles", "--dirs", "owned",
        "--own", "/usr/share/doc/hello", "-",
    )  # fmt: skip
    assert result.rc == 0, result.err
    assert manifest(result.out) == [
        "/usr/bin/hello",
        "/usr/share/doc/hello",
        "/usr/share/doc/hello/README",
    ]
    result = cli("--db", "-", "dbdump", "-f", "rpmspecfiles", "--dirs", "owned", "-")
    assert result.rc == 2
    assert b"--dirs owned needs owned prefixes" in result.err


# -- the command line -------------------------------------------------------


def config_file(tmp_path: Path, text: str) -> str:
    path = tmp_path / "pkgforge.cfg"
    path.write_bytes(text.encode("utf-8"))
    return str(path)


@pytest.mark.parametrize("prefix", ["rel", "/", "/a/../b"])
def test_a_bad_own_prefix_exits_two(cli, tmp_path, prefix):
    result = cli("--db", str(tmp_path / "d.jsonl"), "initdb", "--own", prefix)
    assert result.rc == 2
    assert b"own" in result.err


@pytest.mark.posix
def test_own_on_the_command_line_replaces_the_files(cli, tmp_path):
    cfg = config_file(tmp_path, 'own = ["/etc/hello"]\n')
    (tmp_path / "root").mkdir()
    src = write(tmp_path / "f")
    db = tmp_path / "d.jsonl"
    common = ("--config", cfg, "--db", str(db), "--buildroot", str(tmp_path / "root"))
    assert cli(*common, "initdb").rc == 0
    assert cli(*common, "-q", "install", "-p", str(src), "/etc/hello").rc == 0
    assert "/etc/hello" in recorded(db)
    assert cli(*common, "initdb").rc == 0
    result = cli(
        *common, "-q", "install", "--own", "/opt/x", "-p", str(src), "/etc/hello"
    )
    assert result.rc == 0, result.err
    assert set(recorded(db)) == {"/etc/hello/f"}


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
