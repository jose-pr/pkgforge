"""Tests for the typed ``FileEntry`` module functions (``entry.py``).

``FileEntry`` is a ``TypedDict``: its values are plain dicts, so
``FileEntry.from_args``/``from_path``/``resolve_for``/``apply`` only ever
worked when called *unbound* through the class. These tests pin that the
typed module functions (``entry_from_args``, ``entry_from_path``,
``resolve_entry``, ``apply_entry``) exist, are exported, and agree with the
compat aliases.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import pkgforge
from pkgforge.entry import (
    AUTO,
    DEFAULT,
    FileEntry,
    FileEntryArgs,
    FileType,
    UsageError,
    apply_entry,
    entry_from_args,
    entry_from_path,
    mode_to_octal,
    normalize_mode,
    resolve_entry,
)

# pwd/grp are Unix-only; imported inside the @pytest.mark.posix tests that use
# them, never at module level (this file is collected on Windows too).


def test_entry_functions_match_class_aliases(tmp_path):
    f = tmp_path / "f"
    f.write_text("hi")

    args = FileEntryArgs(mode="640", owner=DEFAULT, group=DEFAULT, type=FileType.File)

    direct = entry_from_args(args)
    alias = FileEntry.from_args(args)
    assert direct == alias

    direct = entry_from_path(f)
    alias = FileEntry.from_path(f)
    assert direct == alias

    base: FileEntry = {
        "mode": "--",
        "owner": DEFAULT,
        "group": DEFAULT,
        "type": "--",
        "meta": {},
    }
    direct = resolve_entry(base, f)
    alias = FileEntry.resolve_for(base, f)
    assert direct == alias


def test_entry_functions_exported():
    for name in ("entry_from_args", "entry_from_path", "resolve_entry", "apply_entry"):
        assert name in pkgforge.__all__
        assert getattr(pkgforge, name) is getattr(pkgforge.entry, name)
    assert apply_entry is pkgforge.apply_entry


# --------------------------------------------------------------------------
# apply_entry: chown-then-chmod order, symlinks, and the glibc floor
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_apply_entry_skips_chmod_on_symlink(tmp_path):
    target = tmp_path / "t"
    target.write_text("x")
    target.chmod(0o600)
    link = tmp_path / "l"
    link.symlink_to(target)

    entry: FileEntry = {
        "mode": "777",
        "owner": DEFAULT,
        "group": DEFAULT,
        "type": "symlink",
        "meta": {},
    }
    apply_entry(entry, link)
    assert (target.stat().st_mode & 0o777) == 0o600


@pytest.mark.posix
def test_apply_entry_chmod_without_nofollow_support(tmp_path, monkeypatch):
    # Simulates glibc < 2.32: os.chmod raises NotImplementedError whenever
    # asked to not follow symlinks. apply_entry must never ask for that on a
    # regular file, so the plain chmod still succeeds.
    real_chmod = os.chmod

    def fake_chmod(path, mode, *, follow_symlinks=True):
        if follow_symlinks is False:
            raise NotImplementedError(
                "chmod: follow_symlinks unavailable on this platform"
            )
        return real_chmod(path, mode)

    monkeypatch.setattr(pkgforge.entry.os, "chmod", fake_chmod)

    f = tmp_path / "f"
    f.write_text("hi")
    entry: FileEntry = {
        "mode": "644",
        "owner": DEFAULT,
        "group": DEFAULT,
        "type": "file",
        "meta": {},
    }
    apply_entry(entry, f)
    assert (f.stat().st_mode & 0o777) == 0o644


@pytest.mark.posix
def test_install_symlink_with_mode_records_entry(tmp_path):
    from pkgforge.install import Install

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    target = tmp_path / "target"
    target.write_text("x")
    target.chmod(0o600)
    link = tmp_path / "app.link"
    link.symlink_to(target)

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-m",
            "777",
            str(link),
            "/usr/bin",
        ]
    )
    inst()  # must not raise (the regression: NotImplementedError on Linux)

    staged = root / "usr" / "bin" / "app.link"
    assert staged.is_symlink()
    recorded = inst.loaddb()["/usr/bin/app.link"]
    assert recorded["mode"] == "777"
    assert (target.stat().st_mode & 0o777) == 0o600


@pytest.mark.posix
def test_apply_entry_auto_mode_on_symlink(tmp_path):
    # API only, not via the CLI's `-m--`: py3.9's argparse parses that as []
    # (a separate defect, 06b's), which would pass at baseline there too.
    target = tmp_path / "t"
    target.write_text("x")
    target.chmod(0o600)
    link = tmp_path / "l"
    link.symlink_to(target)

    entry: FileEntry = {
        "mode": AUTO,
        "owner": DEFAULT,
        "group": DEFAULT,
        "type": AUTO,
        "meta": {},
    }
    resolved = resolve_entry(entry, link)
    apply_entry(resolved, link)  # must not raise

    assert resolved["mode"] == mode_to_octal(os.lstat(link).st_mode)
    assert (target.stat().st_mode & 0o777) == 0o600


@pytest.mark.posix
@pytest.mark.parametrize(
    "requested_mode,expected", [("4755", 0o4755), ("2755", 0o2755)]
)
def test_apply_entry_chown_keeps_special_bits(tmp_path, requested_mode, expected):
    import grp
    import pwd

    f = tmp_path / "f"
    f.write_text("x")
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name

    entry: FileEntry = {
        "mode": requested_mode,
        "owner": user,
        "group": group,
        "type": "file",
        "meta": {},
    }
    apply_entry(entry, f, chown=True)
    assert (f.stat().st_mode & 0o7777) == expected


@pytest.mark.posix
def test_apply_entry_chown_default_mode_keeps_setuid(tmp_path):
    import pwd

    f = tmp_path / "f"
    f.write_text("x")
    f.chmod(0o4755)
    user = pwd.getpwuid(os.getuid()).pw_name

    entry: FileEntry = {
        "mode": DEFAULT,
        "owner": user,
        "group": DEFAULT,
        "type": "file",
        "meta": {},
    }
    apply_entry(entry, f, chown=True)
    assert (f.stat().st_mode & 0o7000) == 0o4000


@pytest.mark.posix
def test_apply_entry_empty_owner_is_default(tmp_path):
    # An empty owner/group must be treated like DEFAULT ("-"), never passed
    # to pwd.getpwnam("")/grp.getgrnam("").
    f = tmp_path / "f"
    f.write_text("x")

    entry: FileEntry = {
        "mode": DEFAULT,
        "owner": "",
        "group": "",
        "type": "file",
        "meta": {},
    }
    apply_entry(entry, f, chown=True)  # must not raise (no getpwnam("") lookup)


def test_apply_entry_rejects_unresolved_auto_mode(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")

    entry: FileEntry = {
        "mode": AUTO,
        "owner": DEFAULT,
        "group": DEFAULT,
        "type": "file",
        "meta": {},
    }
    with pytest.raises(UsageError, match="unresolved"):
        apply_entry(entry, f)


# --------------------------------------------------------------------------
# normalize_mode / --mode parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("644", "644"),
        ("0644", "644"),
        ("7", "7"),
        ("0000", "0"),
        ("-", DEFAULT),
        ("--", AUTO),
        ("auto", AUTO),
    ],
)
def test_parse_mode_normalizes(value, expected):
    assert normalize_mode(value) == expected


@pytest.mark.parametrize("command", ["install", "scan"])
@pytest.mark.parametrize(
    "value",
    ["0o644", "6_44", "17777", "u=rwx", "999", "", " 644", "00644"],
)
def test_mode_rejected_at_parse(command, value, tmp_path, cli):
    db = tmp_path / "db.jsonl"
    root = tmp_path / "root"
    if command == "install":
        src = tmp_path / "src"
        src.write_text("hi")
        argv = [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-m",
            value,
            str(src),
            "dest",
        ]
    else:
        argv = [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "-m",
            value,
            "some/path",
        ]
    result = cli(*argv)
    assert result.rc == 2
    assert not db.exists()


@pytest.mark.parametrize(
    "field,argv",
    [
        ("mode", ["--mode=--"]),
        ("mode", ["-m--"]),
        ("mode", ["-m", "auto"]),
        ("owner", ["--owner=--"]),
    ],
)
def test_mode_attached_auto_sentinel(field, argv):
    # On Python 3.9, argparse strips an attached "--" to [] BEFORE any
    # converter runs, so this goes through entry_from_args -- the one place
    # both the 3.9 [] artifact and the 3.14 literal "--" normalize the same
    # way -- rather than asserting on the raw parsed attribute. Uses the
    # FileEntryArgs mixin directly (type defaults to None, unlike Install's
    # DEFAULT sentinel), since resolving `type` itself is unrelated here.
    parser = FileEntryArgs._parser_()
    ns = parser.parse_args(argv)
    entry = entry_from_args(ns)
    assert entry[field] == AUTO


def test_detached_mode_sentinel_exits_2():
    # Guard: a detached "-m --" is argparse's own end-of-options marker and
    # already exited 2 before this change; it must keep doing so.
    from pkgforge.install import Install

    parser = Install._parser_()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["-m", "--", "src", "/dst"])
    assert excinfo.value.code == 2


def test_api_bad_mode_stages_nothing(tmp_path):
    from pkgforge.install import Install

    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "src"
    src.write_text("hi")
    db = tmp_path / "db.jsonl"

    inst = Install(
        mode="0o644",
        source=src,
        destination=Path("/etc"),
        buildroot=root,
        db=db,
        parents=True,
    )
    with pytest.raises(UsageError):
        inst()
    assert list(root.iterdir()) == []
    assert not db.exists()


@pytest.mark.posix
def test_install_mode_normalized_and_resolved(tmp_path):
    from pkgforge.install import Install

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "db.jsonl"

    src_a = tmp_path / "src_a"
    src_a.write_text("hi")
    parser = Install._parser_()
    parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-m",
            "0644",
            str(src_a),
            "/a",
        ]
    )()

    src_b = tmp_path / "src_b"
    src_b.write_text("hi")
    src_b.chmod(0o750)
    parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "--mode=--",
            str(src_b),
            "/b",
        ]
    )()

    loaded = pkgforge.PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert loaded["/a/src_a"]["mode"] == "644"
    assert loaded["/b/src_b"]["mode"] == "750"
    assert loaded["/b/src_b"]["mode"] != []

    from pkgforge.dbdump import RpmSpecFiles

    assert (
        RpmSpecFiles().render_entry("/b/src_b", loaded["/b/src_b"])
        == b'%attr(750,-,-) "/b/src_b"\n'
    )


# --------------------------------------------------------------------------
# --type parsing
# --------------------------------------------------------------------------


def test_help_hides_private_type_sentinel():
    import re

    from pkgforge.install import Install
    from pkgforge.scan import ScanCmd

    assert "_AUTO" not in Install._parser_().format_help()
    scan_help = ScanCmd._parser_().format_help()
    assert "_AUTO" not in scan_help
    # The class docstring itself mentions "--type/-t" in prose; only the
    # OPTIONS listing (after that heading -- "options:" on 3.10+, "optional
    # arguments:" on 3.9) must omit the flag.
    heading = re.search(r"\n(?:optional arguments|options):\n", scan_help)
    assert heading, scan_help
    assert "--type" not in scan_help[heading.end() :]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("file", FileType.File),
        ("FILE", FileType.File),
        ("Directory", FileType.Directory),
    ],
)
def test_install_type_parses_any_case(value, expected):
    from pkgforge.install import Install

    parser = Install._parser_()
    inst = parser.parse_args(["-t", value, "src", "/dst"])
    assert inst.type == expected


@pytest.mark.parametrize("value", ["bogus", "auto", "_AUTO"])
def test_type_rejects_unknown(value, tmp_path, cli):
    src = tmp_path / "src"
    src.write_text("hi")
    result = cli("install", "-t", value, str(src), "/dst")
    err = result.err.decode()
    assert result.rc == 2
    assert "file" in err and "directory" in err and "symlink" in err


@pytest.mark.posix
def test_install_type_auto_sentinel_detects(tmp_path):
    from pkgforge.install import Install

    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "src"
    src.write_text("hi")
    db = tmp_path / "db.jsonl"

    parser = Install._parser_()
    parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "--type=--",
            str(src),
            "/a",
        ]
    )()

    inst = Install(
        type=FileType._AUTO,
        source=src,
        destination=Path("/b"),
        buildroot=root,
        db=db,
        parents=True,
    )
    inst()  # must not raise (the regression: NotImplementedError on the API)

    loaded = pkgforge.PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert loaded["/a/src"]["type"] == "file"
    assert loaded["/b/src"]["type"] == "file"


@pytest.mark.parametrize("value", ["file", "-"])
def test_scan_type_ignored_with_warning(value, tmp_path, cli, caplog):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f").write_text("x")
    db = tmp_path / "db.jsonl"

    with caplog.at_level("WARNING"):
        result = cli(
            "--db", str(db), "--buildroot", str(root), "scan", "-t", value, "sub"
        )
    assert result.rc == 0
    assert any("ignored" in r.message for r in caplog.records)

    loaded = pkgforge.PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    # Normalize DB keys: os.walk yields "\\"-joined paths on Windows.
    loaded = {k.replace("\\", "/"): v for k, v in loaded.items()}
    assert loaded["/sub/f"]["type"] == "file"
