"""Tests for the ``scan`` subcommand.

``tests/test_pkgforge.py`` keeps the ``scan`` tests that predate this file
(parser wiring, build-root containment, the default-buildroot regression).
Every new ``scan`` regression test lands here. Tests that need POSIX
facilities (chmod, real owner/group names, symlinks) are marked
``@pytest.mark.posix``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pkgforge.command import PkgForgeCmd
from pkgforge.dbdump import RpmSpecFiles

# --------------------------------------------------------------------------
# What scan records: below PATH, not PATH itself; '-' unless AUTO; replaces
# existing entries unless --missing.
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
        path: RpmSpecFiles().render_entry(path, entry).decode()
        for path, entry in db_entries.items()
        if entry is not None
    }
    for shared in ("/usr", "/usr/bin", "/usr/share", "/etc"):
        assert shared not in lines, lines
    assert (
        lines["/usr/share/tool/a"] == '%dir %attr(755,root,root) "/usr/share/tool/a"\n'
    )


# --------------------------------------------------------------------------
# A missing PATH is a clean usage error; a symlink PATH is recorded as a
# symlink, never followed.
# --------------------------------------------------------------------------


def test_scan_missing_path_is_usage_error(tmp_path, cli):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"

    result = cli("--db", str(db), "--buildroot", str(root), "scan", "/nope")
    assert result.rc == 2
    lines = result.err.decode().splitlines()
    assert len(lines) == 1
    assert "does not exist" in lines[0]
    assert "/nope" in lines[0]
    assert not db.exists()


@pytest.mark.posix
@pytest.mark.parametrize("kind", ["relative", "absolute"])
def test_scan_symlinked_path_recorded_as_link(tmp_path, kind):
    from pkgforge.scan import ScanCmd

    root = tmp_path / "root"
    if kind == "relative":
        (root / "opt" / "real").mkdir(parents=True)
        (root / "opt" / "real" / "f").write_text("x")
        (root / "usr" / "lib").mkdir(parents=True)
        link = root / "usr" / "lib" / "app"
        link.symlink_to(Path("../../opt/real"))
        path = "/usr/lib/app"
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "f").write_text("x")
        (root / "usr" / "lib").mkdir(parents=True)
        link = root / "usr" / "lib" / "hostdir"
        link.symlink_to(outside)
        path = "/usr/lib/hostdir"

    db = tmp_path / "files.jsonl"
    parser = ScanCmd._parser_()
    inst = parser.parse_args(["--db", str(db), "--buildroot", str(root), path])
    inst()

    recorded = inst.loaddb()
    assert set(recorded) == {path}
    assert recorded[path]["type"] == "symlink"


@pytest.mark.posix
def test_scan_dangling_symlink_path_recorded(tmp_path, cli):
    # Regression guard: a dangling symlink PATH was, and stays, recordable.
    root = tmp_path / "root"
    root.mkdir()
    link = root / "dangling"
    link.symlink_to(root / "does-not-exist")
    db = tmp_path / "files.jsonl"

    assert cli("--db", str(db), "--buildroot", str(root), "scan", "/dangling").rc == 0
    recorded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert recorded["/dangling"]["type"] == "symlink"


@pytest.mark.posix
def test_scan_symlinked_ancestor_escape_rejected(tmp_path, cli):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "usr").mkdir(parents=True)
    link = root / "usr" / "lib"
    link.symlink_to(outside)
    db = tmp_path / "files.jsonl"

    result = cli("--db", str(db), "--buildroot", str(root), "scan", "/usr/lib/sub")
    assert result.rc == 2
    assert not db.exists()


@pytest.mark.posix
def test_scan_symlinked_buildroot_still_walked(tmp_path, cli):
    # Regression guard: PATH "/" is always walked, even through a
    # symlinked --buildroot.
    real_root = tmp_path / "real_root"
    (real_root / "usr").mkdir(parents=True)
    (real_root / "usr" / "a").write_text("x")
    root = tmp_path / "root_link"
    root.symlink_to(real_root)
    db = tmp_path / "files.jsonl"

    assert cli("--db", str(db), "--buildroot", str(root), "scan", "/").rc == 0
    recorded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert "/usr/a" in {k.replace("\\", "/") for k in recorded}


# --------------------------------------------------------------------------
# scan never records its own file DB (cross-platform: no chmod/owner
# involved, only path comparison).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt,ext", [("jsonl", "jsonl"), ("sqlite", "db")])
def test_scan_skips_file_db_inside_buildroot(tmp_path, monkeypatch, caplog, fmt, ext):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "usr" / "bin" / "tool").write_text("x")
    db_name = f"files.{ext}"

    from pkgforge.scan import ScanCmd

    PkgForgeCmd(db=Path(db_name), db_format=fmt, buildroot=Path(".")).initdb()

    caplog.set_level(logging.WARNING, logger="pkgforge")
    parser = ScanCmd._parser_()
    inst = parser.parse_args(["--db", db_name, "--db-format", fmt, "--missing", "/"])
    inst()

    recorded = {k.replace("\\", "/") for k in inst.loaddb()}
    assert f"/{db_name}" not in recorded
    assert "/usr/bin/tool" in recorded
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_scan_skips_sqlite_sidecars(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "usr").mkdir()
    (tmp_path / "usr" / "a").write_text("x")
    db_name = "files.db"

    from pkgforge.scan import ScanCmd

    PkgForgeCmd(db=Path(db_name), db_format="sqlite", buildroot=Path(".")).initdb()
    # A stale sidecar (e.g. left behind by a crashed run) must be skipped too.
    (tmp_path / (db_name + "-journal")).write_text("stale")

    caplog.set_level(logging.WARNING, logger="pkgforge")
    parser = ScanCmd._parser_()
    inst = parser.parse_args(["--db", db_name, "--db-format", "sqlite", "/"])
    inst()

    recorded = {k.replace("\\", "/") for k in inst.loaddb()}
    assert f"/{db_name}" not in recorded
    assert f"/{db_name}-journal" not in recorded
    assert "/usr/a" in recorded


def test_scan_db_outside_root_changes_nothing(tmp_path):
    # Regression guard: a DB outside --buildroot still records everything.
    root = tmp_path / "root"
    (root / "usr").mkdir(parents=True)
    (root / "usr" / "a").write_text("x")
    db = tmp_path / "db.jsonl"

    from pkgforge.scan import ScanCmd

    parser = ScanCmd._parser_()
    inst = parser.parse_args(["--db", str(db), "--buildroot", str(root), "/"])
    inst()

    recorded = {k.replace("\\", "/") for k in inst.loaddb()}
    assert recorded == {"/usr", "/usr/a"}


# --------------------------------------------------------------------------
# scan looks up user/group names only for fields set to AUTO, and caches
# each lookup for the process's life.
# --------------------------------------------------------------------------


class _CountingLookup:
    """Wraps a real ``pwd``/``grp``-shaped module, counting calls to one
    named function while still returning its real result."""

    def __init__(self, real, funcname: str):
        self._real = real
        self._funcname = funcname
        self.calls = 0

    def __getattr__(self, name):
        real_attr = getattr(self._real, name)
        if name != self._funcname:
            return real_attr

        def _counted(*args, **kwargs):
            self.calls += 1
            return real_attr(*args, **kwargs)

        return _counted


@pytest.mark.posix
def test_scan_default_makes_no_name_lookups(tmp_path, monkeypatch, cli):
    import grp
    import pwd

    import pkgforge.entry as common

    fake_pwd = _CountingLookup(pwd, "getpwuid")
    fake_grp = _CountingLookup(grp, "getgrgid")
    monkeypatch.setattr(common, "pwd", fake_pwd)
    monkeypatch.setattr(common, "grp", fake_grp)

    root = tmp_path / "root"
    (root / "usr" / "share" / "app").mkdir(parents=True)
    for i in range(50):
        (root / "usr" / "share" / "app" / f"f{i}").write_text("x")
    db = tmp_path / "files.jsonl"

    assert (
        cli("--db", str(db), "--buildroot", str(root), "scan", "/usr/share/app").rc == 0
    )
    assert fake_pwd.calls == 0
    assert fake_grp.calls == 0


@pytest.mark.posix
def test_scan_auto_owner_looks_up_each_id_once(tmp_path, monkeypatch, cli):
    import grp
    import pwd

    import pkgforge.entry as common

    fake_pwd = _CountingLookup(pwd, "getpwuid")
    fake_grp = _CountingLookup(grp, "getgrgid")
    monkeypatch.setattr(common, "pwd", fake_pwd)
    monkeypatch.setattr(common, "grp", fake_grp)

    root = tmp_path / "root"
    (root / "usr" / "share" / "app").mkdir(parents=True)
    for i in range(20):
        (root / "usr" / "share" / "app" / f"f{i}").write_text("x")
    db = tmp_path / "files.jsonl"

    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "--owner=--",
            "--group=--",
            "/usr/share/app",
        ).rc
        == 0
    )
    # Every staged file shares the same uid/gid (the test runner's), so at
    # most one real lookup per distinct id, however many files are scanned.
    assert fake_pwd.calls <= 1
    assert fake_grp.calls <= 1

    recorded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert recorded
    for entry in recorded.values():
        assert entry["owner"] not in ("-", "--")
        assert entry["group"] not in ("-", "--")


@pytest.mark.posix
def test_scan_exclude_does_not_repeat_lookups(tmp_path, monkeypatch, cli):
    import grp
    import pwd

    import pkgforge.entry as common

    fake_pwd = _CountingLookup(pwd, "getpwuid")
    fake_grp = _CountingLookup(grp, "getgrgid")
    monkeypatch.setattr(common, "pwd", fake_pwd)
    monkeypatch.setattr(common, "grp", fake_grp)

    root = tmp_path / "root"
    (root / "usr" / "share" / "app").mkdir(parents=True)
    for i in range(20):
        (root / "usr" / "share" / "app" / f"f{i}").write_text("x")
    db = tmp_path / "files.jsonl"

    # The glob matches every file (an inline type test forces PathMatch to
    # build an entry for it), but the test itself never excludes a file, so
    # every file is still recorded -- exercising both the lookup inside
    # PathMatch.match and the one scan makes to actually record the entry.
    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "--owner=--",
            "--group=--",
            "-X",
            "(?type:directory)*",
            "/usr/share/app",
        ).rc
        == 0
    )
    assert fake_pwd.calls <= 1
    assert fake_grp.calls <= 1

    recorded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert len(recorded) == 20


# --------------------------------------------------------------------------
# -m applies to regular files only; directories take --dir-mode; symlinks
# never record a mode.
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_scan_mode_applies_to_files_only(tmp_path, cli):
    root = tmp_path / "root"
    (root / "usr" / "share" / "app" / "sub").mkdir(parents=True)
    (root / "usr" / "share" / "app" / "sub" / "f").write_text("x")
    link = root / "usr" / "share" / "app" / "link"
    link.symlink_to("sub/f")
    db = tmp_path / "files.jsonl"

    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "-m",
            "644",
            "-o",
            "root",
            "-g",
            "root",
            "/usr/share/app",
        ).rc
        == 0
    )
    recorded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert recorded["/usr/share/app/sub"]["mode"] == "-"
    assert recorded["/usr/share/app/sub"]["type"] == "directory"
    assert recorded["/usr/share/app/link"]["mode"] == "-"
    assert recorded["/usr/share/app/link"]["type"] == "symlink"
    assert recorded["/usr/share/app/sub/f"]["mode"] == "644"
    assert recorded["/usr/share/app/sub/f"]["type"] == "file"
    for entry in recorded.values():
        assert entry["owner"] == "root"
        assert entry["group"] == "root"


@pytest.mark.posix
def test_scan_dir_mode_sets_directories(tmp_path, cli):
    root = tmp_path / "root"
    (root / "usr" / "share" / "app" / "sub").mkdir(parents=True)
    (root / "usr" / "share" / "app" / "sub" / "f").write_text("x")
    db = tmp_path / "files.jsonl"

    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "-m",
            "644",
            "--dir-mode",
            "755",
            "/usr/share/app",
        ).rc
        == 0
    )
    recorded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert recorded["/usr/share/app/sub"]["mode"] == "755"
    assert recorded["/usr/share/app/sub/f"]["mode"] == "644"


@pytest.mark.posix
def test_scan_auto_mode_reads_dirs_skips_links(tmp_path, cli):
    root = tmp_path / "root"
    (root / "usr" / "share" / "app" / "sub").mkdir(parents=True)
    (root / "usr" / "share" / "app" / "sub").chmod(0o750)
    (root / "usr" / "share" / "app" / "sub" / "f").write_text("x")
    link = root / "usr" / "share" / "app" / "link"
    link.symlink_to("sub/f")
    db = tmp_path / "files.jsonl"

    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "--mode=--",
            "/usr/share/app",
        ).rc
        == 0
    )
    recorded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert recorded["/usr/share/app/sub"]["mode"] == "750"
    assert recorded["/usr/share/app/link"]["mode"] == "-"


@pytest.mark.posix
def test_scan_dir_mode_auto_sentinel(tmp_path, cli):
    # --dir-mode=-- must resolve to the on-disk mode, never argparse's
    # Python-3.9 stripped "[]" artifact for an attached "--" value.
    root = tmp_path / "root"
    (root / "usr" / "share" / "app" / "sub").mkdir(parents=True)
    (root / "usr" / "share" / "app" / "sub").chmod(0o750)
    (root / "usr" / "share" / "app" / "sub" / "f").write_text("x")
    db = tmp_path / "files.jsonl"

    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "--dir-mode=--",
            "/usr/share/app",
        ).rc
        == 0
    )
    recorded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert recorded["/usr/share/app/sub"]["mode"] == "750"


# --------------------------------------------------------------------------
# scan --drop-stale tombstones a DB entry whose file is gone from the build
# root, bounded to PATH and honoring -X.
# --------------------------------------------------------------------------


@pytest.mark.posix
@pytest.mark.parametrize(
    "fmt,ext", [("jsonl", "jsonl"), ("yaml", "yaml"), ("sqlite", "db")]
)
def test_scan_drop_stale_tombstones_deleted(tmp_path, cli, fmt, ext):
    root = tmp_path / "root"
    (root / "usr" / "share" / "app").mkdir(parents=True)
    (root / "usr" / "share" / "app" / "a").write_text("x")
    (root / "usr" / "share" / "app" / "b").write_text("y")
    db = tmp_path / f"files.{ext}"

    assert (
        cli(
            "--db",
            str(db),
            "--db-format",
            fmt,
            "--buildroot",
            str(root),
            "scan",
            "/usr/share/app",
        ).rc
        == 0
    )
    (root / "usr" / "share" / "app" / "b").unlink()

    assert (
        cli(
            "--db",
            str(db),
            "--db-format",
            fmt,
            "--buildroot",
            str(root),
            "scan",
            "--drop-stale",
            "/usr/share/app",
        ).rc
        == 0
    )

    a_key, b_key = "/usr/share/app/a", "/usr/share/app/b"
    loaded = PkgForgeCmd(db=db, db_format=fmt, buildroot=root).loaddb()
    assert loaded[b_key] is None
    assert loaded[a_key] is not None

    lines = {
        path: RpmSpecFiles().render_entry(path, entry).decode()
        for path, entry in loaded.items()
        if entry is not None
    }
    assert b_key not in lines
    assert a_key in lines


@pytest.mark.posix
def test_scan_drop_stale_bounded_to_path(tmp_path, cli):
    root = tmp_path / "root"
    (root / "usr" / "share" / "app").mkdir(parents=True)
    (root / "usr" / "share" / "app" / "a").write_text("x")
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "tool").write_text("y")
    db = tmp_path / "files.jsonl"

    assert cli("--db", str(db), "--buildroot", str(root), "scan", "/usr").rc == 0
    (root / "usr" / "bin" / "tool").unlink()

    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "--drop-stale",
            "/usr/share/app",
        ).rc
        == 0
    )

    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    # /usr/bin/tool's file is gone too, but it's outside the drop-stale
    # PATH, so its entry is left alone.
    assert loaded["/usr/bin/tool"] is not None
    assert loaded["/usr/share/app/a"] is not None


@pytest.mark.posix
def test_scan_drop_stale_keeps_excluded(tmp_path, cli):
    root = tmp_path / "root"
    (root / "usr" / "share" / "app").mkdir(parents=True)
    (root / "usr" / "share" / "app" / "a").write_text("x")
    (root / "usr" / "share" / "app" / "ghost").write_text("y")
    db = tmp_path / "files.jsonl"

    assert (
        cli("--db", str(db), "--buildroot", str(root), "scan", "/usr/share/app").rc == 0
    )
    (root / "usr" / "share" / "app" / "ghost").unlink()

    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "--drop-stale",
            "-X",
            "*ghost",
            "/usr/share/app",
        ).rc
        == 0
    )

    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert loaded["/usr/share/app/ghost"] is not None
    assert loaded["/usr/share/app/a"] is not None


def test_scan_drop_stale_needs_file_db(tmp_path, cli):
    root = tmp_path / "root"
    root.mkdir()

    result = cli("--buildroot", str(root), "scan", "--drop-stale", "/")
    assert result.rc == 2


@pytest.mark.posix
def test_scan_missing_readds_removed_file(tmp_path, cli):
    # Regression guard: --missing's own "not yet recorded" rule is
    # unaffected by --drop-stale -- a tombstoned path is still re-added.
    root = tmp_path / "root"
    (root / "usr" / "share" / "app").mkdir(parents=True)
    (root / "usr" / "share" / "app" / "a").write_text("x")
    db = tmp_path / "files.jsonl"

    assert (
        cli("--db", str(db), "--buildroot", str(root), "scan", "/usr/share/app").rc == 0
    )
    PkgForgeCmd(db=db, db_format=None, buildroot=root).remove_entry("/usr/share/app/a")
    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert loaded["/usr/share/app/a"] is None

    assert (
        cli(
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "scan",
            "--missing",
            "/usr/share/app",
        ).rc
        == 0
    )
    loaded = PkgForgeCmd(db=db, db_format=None, buildroot=root).loaddb()
    assert loaded["/usr/share/app/a"] is not None
