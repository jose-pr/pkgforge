"""``[[users]]`` and ``[[groups]]``, the base-passwd sets, and the check that an
owner or group is declared when it is recorded."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pkgforge import Project, UsageError
from pkgforge.config import GroupAccount, ProjectConfig, UserAccount
from pkgforge.entry import BASE_GROUPS, BASE_USERS, _group_name, _user_name

PACKAGE = '[package]\nname = "hi"\n'


def cfg_file(tmp_path: Path, text: str) -> str:
    path = tmp_path / "pkgforge.cfg"
    path.write_bytes(text.encode("utf-8"))
    return str(path)


def read_db(db: Path) -> dict:
    import json

    out = {}
    for line in db.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        out[rec.pop("path")] = rec
    return out


def source(tmp_path: Path) -> Path:
    src = tmp_path / "c"
    src.write_bytes(b"x")
    return src


# -- the sets ----------------------------------------------------------------


def test_the_base_sets_are_the_base_passwd_names():
    assert "www-data" in BASE_USERS and "nobody" in BASE_USERS
    assert "adm" in BASE_GROUPS and "nogroup" in BASE_GROUPS
    assert "adm" not in BASE_USERS
    assert "root" in BASE_USERS and "root" in BASE_GROUPS
    assert isinstance(BASE_USERS, frozenset) and isinstance(BASE_GROUPS, frozenset)


# -- the schema --------------------------------------------------------------

BAD_TABLES = [
    ("empty name", PACKAGE + '[[users]]\nname = ""\n', "name"),
    ("sentinel name", PACKAGE + '[[users]]\nname = "--"\n', "name"),
    ("percent in name", PACKAGE + '[[users]]\nname = "a%b"\n', "name"),
    ("a number for a name", PACKAGE + "[[users]]\nname = 5\n", "quoted string"),
    ("a capital in a created name", PACKAGE + '[[users]]\nname = "Hello"\n', "name"),
    (
        "a created name too long",
        PACKAGE + f'[[users]]\nname = "{"a" * 32}"\n',
        "cannot be created",
    ),
    ("a base user", PACKAGE + '[[users]]\nname = "daemon"\n', "base-passwd"),
    ("a base group", PACKAGE + '[[groups]]\nname = "adm"\n', "base-passwd"),
    (
        "a user named like a base group",
        PACKAGE + '[[users]]\nname = "adm"\n',
        "base-passwd group",
    ),
    (
        "a quote in the description",
        PACKAGE + '[[users]]\nname = "hi"\ndescription = "a \\"b\\""\n',
        "description",
    ),
    (
        "a control character in the description",
        PACKAGE + '[[users]]\nname = "hi"\ndescription = "a\\u0007b"\n',
        "description",
    ),
    (
        "a backslash in the description",
        PACKAGE + '[[users]]\nname = "hi"\ndescription = "a\\\\b"\n',
        "description",
    ),
    (
        "a backslash in the home",
        PACKAGE + '[[users]]\nname = "hi"\nhome = "/var/a\\\\b"\n',
        "home",
    ),
    (
        "a description for an existing user",
        PACKAGE + '[[users]]\nname = "hi"\ncreate = false\ndescription = "x"\n',
        "description: only valid with create = true",
    ),
    (
        "groups for an existing user",
        PACKAGE + '[[users]]\nname = "hi"\ncreate = false\ngroups = ["adm"]\n',
        "groups: only valid with create = true",
    ),
    ("a relative home", PACKAGE + '[[users]]\nname = "hi"\nhome = "var/hi"\n', "home"),
    (
        "a space in the shell",
        PACKAGE + '[[users]]\nname = "hi"\nshell = "/bin/a b"\n',
        "shell",
    ),
    (
        "a user declared twice",
        PACKAGE + '[[users]]\nname = "hi"\n[[users]]\nname = "hi"\n',
        "declared twice",
    ),
    (
        "a group declared twice",
        PACKAGE + '[[groups]]\nname = "hi"\n[[groups]]\nname = "hi"\n',
        "declared twice",
    ),
    (
        "a group named like a created user",
        PACKAGE + '[[users]]\nname = "hi"\n[[groups]]\nname = "hi"\n',
        "group of the created user",
    ),
    (
        "an unknown supplementary group",
        PACKAGE + '[[users]]\nname = "hi"\ngroups = ["nosuch"]\n',
        "neither a base-passwd group",
    ),
    (
        "a created user without a package name",
        '[[users]]\nname = "hi"\n',
        "need [package] name",
    ),
    (
        "a created group without a package name",
        '[[groups]]\nname = "hi"\n',
        "need [package] name",
    ),
    (
        "an unknown key",
        PACKAGE + '[[users]]\nname = "hi"\nuid = 5\n',
        "uid",
    ),
    (
        "a defaults owner nothing declares",
        PACKAGE + '[defaults]\nowner = "nosuch"\n',
        "defaults: owner 'nosuch'",
    ),
    (
        "a defaults group nothing declares",
        PACKAGE + '[defaults]\ngroup = "nosuch"\n',
        "defaults: group 'nosuch'",
    ),
    (
        "a rule owner nothing declares",
        PACKAGE + '[[rules]]\nmatch = ["/a"]\nowner = "nosuch"\n',
        "rules[0]: owner 'nosuch'",
    ),
    (
        "a step group nothing declares",
        PACKAGE + '[[stage]]\ndir = "/a"\ngroup = "nosuch"\n',
        "stage[0]: group 'nosuch'",
    ),
]


@pytest.mark.parametrize(
    "text,message", [c[1:] for c in BAD_TABLES], ids=[c[0] for c in BAD_TABLES]
)
def test_a_bad_account_table_exits_two(cli, tmp_path, text, message):
    result = cli("--config", cfg_file(tmp_path, text), "stage")
    assert result.rc == 2, result.err
    assert message in result.err.decode()


def test_declared_accounts_are_accepted(tmp_path):
    config = ProjectConfig.from_dict(
        {
            "package": {"name": "hi"},
            "users": [
                {"name": "hi", "groups": ["adm", "ops"], "shell": "/bin/sh"},
                {"name": "svc.x$", "create": False},
            ],
            "groups": [{"name": "ops"}, {"name": "Existing-Group", "create": False}],
            "defaults": {"owner": "svc.x$", "group": "hi"},
            "rules": [{"match": ["/a"], "owner": "hi", "group": "Existing-Group"}],
        }
    )
    assert [u.name for u in config.users] == ["hi", "svc.x$"]
    assert config.groups[1] == GroupAccount(name="Existing-Group", create=False)


def test_a_group_can_name_an_existing_user_that_is_not_created():
    config = ProjectConfig(
        users=(UserAccount(name="Svc", create=False),),
        groups=(GroupAccount(name="Svc", create=False),),
    )
    assert config.groups[0].name == "Svc"


# -- the record path ---------------------------------------------------------


@pytest.mark.posix
def test_a_declared_owner_is_recorded_and_an_unknown_one_is_refused(cli, tmp_path):
    root, db = tmp_path / "r", tmp_path / "f.jsonl"
    src = source(tmp_path)
    cfg = cfg_file(tmp_path, PACKAGE + '[[users]]\nname = "hi"\n')
    base = ("--config", cfg, "--db", str(db), "--buildroot", str(root))

    # -o hi and the implied group -g hi.
    assert (
        cli(
            *base, "-q", "install", "-p", "-o", "hi", "-g", "hi", str(src), "/opt/hi"
        ).rc
        == 0
    )
    got = read_db(db)["/opt/hi/c"]
    assert (got["owner"], got["group"]) == ("hi", "hi")

    result = cli(*base, "-q", "install", "-p", "-o", "nosuch", str(src), "/opt/no")
    assert result.rc == 2
    assert (
        "owner 'nosuch' is neither a base-passwd account nor declared in [[users]]"
        in result.err.decode()
    )
    assert sorted(p.name for p in (root / "opt").iterdir()) == ["hi"]
    assert set(read_db(db)) == {"/opt/hi/c"}

    result = cli(*base, "-q", "install", "-p", "-g", "nosuch", str(src), "/opt/no")
    assert result.rc == 2
    assert "group 'nosuch' is neither a base-passwd group" in result.err.decode()
    assert not (root / "opt" / "no").exists()


@pytest.mark.posix
def test_base_names_need_no_declaration(cli, tmp_path):
    root, db = tmp_path / "r", tmp_path / "f.jsonl"
    result = cli(
        "--db", str(db), "--buildroot", str(root), "-q", "install", "-p",
        "-o", "www-data", "-g", "adm", str(source(tmp_path)), "/opt/b",
    )  # fmt: skip
    assert result.rc == 0, result.err
    got = read_db(db)["/opt/b/c"]
    assert (got["owner"], got["group"]) == ("www-data", "adm")


@pytest.mark.posix
def test_noentry_records_nothing_so_checks_nothing(cli, tmp_path):
    result = cli(
        "--buildroot", str(tmp_path / "r"), "-q", "install", "-p", "--noentry",
        "-o", "svc", str(source(tmp_path)), "/opt/b",
    )  # fmt: skip
    assert result.rc == 0, result.err


@pytest.mark.posix
def test_a_mkdir_and_a_symlink_owner_are_checked(cli, tmp_path):
    base = ("--db", str(tmp_path / "f.jsonl"), "--buildroot", str(tmp_path / "r"))
    assert cli(*base, "mkdir", "-p", "-o", "svc", "/opt/d").rc == 2
    assert cli(*base, "symlink", "-g", "svc", "x", "/opt/l").rc == 2
    assert not (tmp_path / "r" / "opt" / "d").exists()


def test_the_library_refuses_an_undeclared_owner_without_a_config(tmp_path):
    src = source(tmp_path)
    project = Project(tmp_path / "r", tmp_path / "f.jsonl")
    with pytest.raises(UsageError, match="owner 'svc' is neither a base-passwd"):
        project.install([src], "/opt/x", owner="svc", parents=True)
    with pytest.raises(UsageError, match="group 'svc' is neither a base-passwd"):
        project.scan("/", group="svc")

    declared = Project(
        tmp_path / "r",
        tmp_path / "f.jsonl",
        config=ProjectConfig(users=(UserAccount(name="svc", create=False),)),
    )
    records = declared.install([src], "/opt/x", owner="svc", parents=True)
    assert records[0][1]["owner"] == "svc"
    with pytest.raises(UsageError, match="group 'svc' is neither"):
        declared.install([src], "/opt/y", group="svc", parents=True)


@pytest.mark.posix
def test_scan_refuses_an_explicit_name_before_walking(cli, tmp_path):
    root, db = tmp_path / "r", tmp_path / "f.jsonl"
    (root / "opt").mkdir(parents=True)
    (root / "opt" / "f").write_bytes(b"x")
    result = cli(
        "--db", str(db), "--buildroot", str(root), "scan", "--owner", "svc", "/opt"
    )
    assert result.rc == 2
    assert "owner 'svc' is neither a base-passwd account" in result.err.decode()
    assert not db.exists() or not read_db(db)


def _runner_names():
    if not hasattr(os, "geteuid"):
        pytest.skip("needs POSIX accounts")
    user, group = _user_name(os.getuid()), _group_name(os.getgid())
    if user in BASE_USERS or user == "-":
        pytest.skip(f"the runner {user!r} is a base-passwd account")
    return user, group


@pytest.mark.posix
def test_scan_auto_owner_needs_the_runner_declared(cli, tmp_path, host_accounts_cfg):
    user, _ = _runner_names()
    root, db = tmp_path / "r", tmp_path / "f.jsonl"
    (root / "opt").mkdir(parents=True)
    (root / "opt" / "f").write_bytes(b"x")
    base = ("--db", str(db), "--buildroot", str(root), "scan", "--owner=--")

    result = cli(*base, "/opt")
    assert result.rc == 2
    assert f"owner {user!r} is neither a base-passwd account" in result.err.decode()
    assert not db.exists() or not read_db(db)

    host_accounts_cfg()
    assert cli(*base, "/opt").rc == 0
    assert read_db(db)["/opt/f"]["owner"] == user


@pytest.mark.posix
def test_scan_auto_group_needs_the_group_declared(cli, tmp_path, host_accounts_cfg):
    _, group = _runner_names()
    if group in BASE_GROUPS:
        pytest.skip(f"the runner's group {group!r} is a base-passwd group")
    root, db = tmp_path / "r", tmp_path / "f.jsonl"
    (root / "opt").mkdir(parents=True)
    (root / "opt" / "f").write_bytes(b"x")
    result = cli(
        "--db", str(db), "--buildroot", str(root), "scan", "--group=--", "/opt"
    )
    assert result.rc == 2
    assert f"group {group!r} is neither a base-passwd group" in result.err.decode()
    host_accounts_cfg()
    result = cli(
        "--db", str(db), "--buildroot", str(root), "scan", "--group=--", "/opt"
    )
    assert result.rc == 0, result.err


def test_host_accounts_fixture_declares_only_non_base_names(host_accounts):
    for user in host_accounts["users"]:
        assert user.name not in BASE_USERS and user.create is False
    for group in host_accounts["groups"]:
        assert group.name not in BASE_GROUPS and group.create is False
