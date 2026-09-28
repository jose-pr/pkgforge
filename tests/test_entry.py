"""Tests for the typed ``FileEntry`` module functions (``common.py``).

``FileEntry`` is a ``TypedDict``: its values are plain dicts, so
``FileEntry.from_args``/``from_path``/``resolve_for``/``apply`` only ever
worked when called *unbound* through the class. These tests pin that the
typed module functions (``entry_from_args``, ``entry_from_path``,
``resolve_entry``, ``apply_entry``) exist, are exported, and agree with the
compat aliases.
"""

from __future__ import annotations

import os

import pytest

import pkgforge
from pkgforge.common import (
    AUTO,
    DEFAULT,
    FileEntry,
    FileEntryArgs,
    FileType,
    apply_entry,
    entry_from_args,
    entry_from_path,
    mode_to_octal,
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
        assert getattr(pkgforge, name) is getattr(pkgforge.common, name)
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

    monkeypatch.setattr(pkgforge.common.os, "chmod", fake_chmod)

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
