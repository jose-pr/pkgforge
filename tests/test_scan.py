"""Tests for the ``scan`` subcommand.

``tests/test_pkgforge.py`` keeps the ``scan`` tests that predate this file
(parser wiring, build-root containment, the default-buildroot regression).
Every new ``scan`` regression test lands here. Tests that need POSIX
facilities (chmod, real owner/group names, symlinks) are marked
``@pytest.mark.posix``.
"""

from __future__ import annotations

import pytest

from pkgforge.common import PkgForgeCmd
from pkgforge.dbdump import rpmspecfile

# --------------------------------------------------------------------------
# What scan records (F01): below PATH, not PATH itself; '-' unless AUTO;
# replaces existing entries unless --missing.
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_scan_records_below_path_not_path_itself(tmp_path, cli):
    root = tmp_path / "root"
    (root / "usr" / "share" / "tool" / "a").mkdir(parents=True)
    (root / "usr" / "share" / "tool" / "a" / "s.txt").write_text("x")
    db = tmp_path / "files.jsonl"

    assert (
        cli("--db", str(db), "--buildroot", str(root), "scan", "/usr/share/tool").rc
        == 0
    )

    keys = set(PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb())
    assert keys == {"/usr/share/tool/a", "/usr/share/tool/a/s.txt"}
    assert "/usr/share/tool" not in keys
    assert "/usr/share" not in keys


@pytest.mark.posix
def test_scan_fields_default_to_dash_and_auto_reads_disk(tmp_path, cli):
    root = tmp_path / "root"
    (root / "usr" / "share" / "tool").mkdir(parents=True)
    f = root / "usr" / "share" / "tool" / "s.txt"
    f.write_text("x")
    f.chmod(0o644)

    db = tmp_path / "files.jsonl"
    assert (
        cli("--db", str(db), "--buildroot", str(root), "scan", "/usr/share/tool").rc
        == 0
    )
    plain = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    entry = plain["/usr/share/tool/s.txt"]
    assert entry["mode"] == "-"
    assert entry["owner"] == "-"
    assert entry["group"] == "-"

    db2 = tmp_path / "files2.jsonl"
    assert (
        cli(
            "--db",
            str(db2),
            "--buildroot",
            str(root),
            "scan",
            "--mode=--",
            "--owner=--",
            "--group=--",
            "/usr/share/tool",
        ).rc
        == 0
    )
    resolved = PkgForgeCmd(db=db2, db_format=None, buildroot=root).loaddb()
    entry = resolved["/usr/share/tool/s.txt"]
    assert entry["mode"] == "644"
    assert entry["owner"] not in ("-", "--")
    assert entry["group"] not in ("-", "--")


@pytest.mark.posix
def test_scan_without_missing_replaces_install_entry(tmp_path, cli):
    root = tmp_path / "root"
    src = tmp_path / "tool"
    src.write_text("x")

    db = tmp_path / "files.jsonl"
    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-m",
            "755",
            "-o",
            "root",
            "-g",
            "root",
            str(src),
            "/usr/bin",
        ).rc
        == 0
    )
    before = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert before["/usr/bin/tool"]["mode"] == "755"

    assert cli("--db", str(db), "--buildroot", str(root), "scan", "/usr/bin").rc == 0
    after = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert after["/usr/bin/tool"]["mode"] == "-"

    # --missing leaves an existing (install-recorded) entry untouched.
    db2 = tmp_path / "files2.jsonl"
    assert (
        cli(
            "--db",
            str(db2),
            "--buildroot",
            str(root),
            "install",
            "-p",
            "-m",
            "755",
            "-o",
            "root",
            "-g",
            "root",
            str(src),
            "/usr/bin",
        ).rc
        == 0
    )
    assert (
        cli(
            "--db", str(db2), "--buildroot", str(root), "scan", "--missing", "/usr/bin"
        ).rc
        == 0
    )
    kept = PkgForgeCmd(db=db2, db_format=None, buildroot=root).loaddb()
    assert kept["/usr/bin/tool"]["mode"] == "755"


@pytest.mark.posix
def test_scan_documented_recipe_owns_no_shared_dirs(tmp_path, cli):
    """The README/unattended.md recipe: stage a package-owned directory
    exactly, then scan only that subtree. No entry below it renders as a
    `%dir` for a shared prefix (/usr, /usr/bin, /usr/share, /etc)."""
    root = tmp_path / "root"
    share = tmp_path / "share"
    (share / "a").mkdir(parents=True)
    (share / "a" / "s.txt").write_text("x")

    db = tmp_path / "files.jsonl"
    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "install",
            "-D",
            "-d",
            "-m",
            "755",
            "-o",
            "root",
            "-g",
            "root",
            str(share),
            "/usr/share/tool",
        ).rc
        == 0
    )
    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "--missing",
            "--mode=--",
            "-o",
            "root",
            "-g",
            "root",
            "/usr/share/tool",
        ).rc
        == 0
    )

    db_entries = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    lines = {
        path: rpmspecfile(path, entry).decode()
        for path, entry in db_entries.items()
        if entry is not None
    }
    for shared in ("/usr", "/usr/bin", "/usr/share", "/etc"):
        assert shared not in lines, lines
    assert (
        lines["/usr/share/tool/a"] == '%dir %attr(755,root,root) "/usr/share/tool/a"\n'
    )
