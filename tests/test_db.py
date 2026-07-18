"""Tests for the provider-agnostic file DB (jsonl / yaml / sqlite)."""

from __future__ import annotations

import json

import pytest
import yaml

import pkgforge.db as dbmod
from pkgforge.common import PkgForge, FileType
from pkgforge.db import (
    DbProvider,
    JsonlDb,
    SqliteDb,
    YamlDb,
    format_for_suffix,
    open_db,
    register_provider,
    sniff_format,
)

ALL_FORMATS = ["jsonl", "yaml", "sqlite"]


def _entry(mode="644", type="file", **over):
    e = {"mode": mode, "owner": "root", "group": "root", "type": type, "meta": {}}
    e.update(over)
    return e


# --------------------------------------------------------------------------
# provider parity: every backend behaves identically through load()
# --------------------------------------------------------------------------


@pytest.fixture(params=ALL_FORMATS)
def provider(request, tmp_path):
    path = tmp_path / f"files.{request.param}"
    p = open_db(path, request.param)
    p.init()
    return p


def _reload(provider):
    # A fresh provider instance, as a separate CLI invocation would use.
    return open_db(provider.path, provider.format, for_read=True).load()


def test_roundtrip(provider):
    provider.add("/usr/bin/x", _entry(mode="755"))
    assert _reload(provider)["/usr/bin/x"]["mode"] == "755"


def test_last_write_wins(provider):
    provider.add("/x", _entry(mode="644"))
    provider.add("/x", _entry(mode="600"))
    assert _reload(provider)["/x"]["mode"] == "600"


def test_removal_marks_none(provider):
    provider.add("/x", _entry())
    provider.remove("/x")
    assert _reload(provider)["/x"] is None


def test_meta_preserved(provider):
    provider.add("/x", _entry(meta={"rpmprefix": "%config"}))
    assert _reload(provider)["/x"]["meta"] == {"rpmprefix": "%config"}


def test_filetype_enum_stored_as_str(provider):
    provider.add("/d", _entry(type=FileType.Directory))
    assert _reload(provider)["/d"]["type"] == "directory"


def test_compact_drops_removed(provider):
    provider.add("/keep", _entry())
    provider.add("/gone", _entry())
    provider.remove("/gone")
    provider.compact()
    db = _reload(provider)
    assert "/keep" in db
    assert "/gone" not in db


def test_load_missing_is_empty(tmp_path):
    for fmt in ALL_FORMATS:
        assert open_db(tmp_path / f"nope.{fmt}", fmt).load() == {}


# --------------------------------------------------------------------------
# selection: suffix, override, content sniff
# --------------------------------------------------------------------------


def test_format_for_suffix():
    from pathlib import Path

    assert format_for_suffix(Path("f.jsonl")) == "jsonl"
    assert format_for_suffix(Path("f.ndjson")) == "jsonl"
    assert format_for_suffix(Path("f.yaml")) == "yaml"
    assert format_for_suffix(Path("f.yml")) == "yaml"
    assert format_for_suffix(Path("f.db")) == "sqlite"
    assert format_for_suffix(Path("f.sqlite3")) == "sqlite"
    assert format_for_suffix(Path("f.unknown")) == "jsonl"  # default


def test_open_db_by_suffix(tmp_path):
    assert isinstance(open_db(tmp_path / "a.jsonl"), JsonlDb)
    assert isinstance(open_db(tmp_path / "a.yaml"), YamlDb)
    assert isinstance(open_db(tmp_path / "a.db"), SqliteDb)


def test_explicit_format_overrides_suffix(tmp_path):
    # A .yaml suffix but forced sqlite.
    assert isinstance(open_db(tmp_path / "a.yaml", "sqlite"), SqliteDb)


def test_sniff_detects_content_over_suffix(tmp_path):
    # Write YAML content into a suffix-less file; a read auto-detects it as yaml.
    path = tmp_path / "noext"
    open_db(path, "yaml").add("/a", _entry())
    assert sniff_format(path) == "yaml"
    assert open_db(path, for_read=True).format == "yaml"


def test_sniff_detects_sqlite(tmp_path):
    path = tmp_path / "store"  # no .db suffix
    open_db(path, "sqlite").init()
    assert sniff_format(path) == "sqlite"
    assert open_db(path, for_read=True).format == "sqlite"


def test_sniff_detects_jsonl(tmp_path):
    path = tmp_path / "log"
    open_db(path, "jsonl").add("/a", _entry())
    assert sniff_format(path) == "jsonl"


def test_unknown_format_raises(tmp_path):
    with pytest.raises(ValueError):
        open_db(tmp_path / "x", "toml")


# --------------------------------------------------------------------------
# backend specifics
# --------------------------------------------------------------------------


def test_jsonl_is_one_line_per_record(tmp_path):
    p = open_db(tmp_path / "f.jsonl")
    p.add("/a", _entry())
    p.add("/b", _entry(mode="755"))
    lines = [l for l in p.path.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["path"] == "/a"


def test_sqlite_upserts_in_place_no_duplicate_rows(tmp_path):
    import sqlite3

    p = open_db(tmp_path / "f.db")
    p.init()
    p.add("/x", _entry(mode="644"))
    p.add("/x", _entry(mode="600"))
    conn = sqlite3.connect(str(p.path))
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM entries").fetchone()
    finally:
        conn.close()
    assert count == 1  # upserted, not appended


def test_yaml_reads_legacy_written_db(tmp_path):
    # A hand-written legacy YAML mapping still loads via the yaml provider.
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump({"/usr/bin/x": _entry(mode="755"), "/gone": None}))
    db = open_db(path, for_read=True).load()
    assert db["/usr/bin/x"]["mode"] == "755"
    assert db["/gone"] is None


# --------------------------------------------------------------------------
# PkgForgeCmd delegation
# --------------------------------------------------------------------------


def _cmd(tmp_path, name):
    inst = PkgForge.__new__(PkgForge)
    inst.db = tmp_path / name
    inst.db_format = None
    inst.buildroot = tmp_path
    return inst


@pytest.mark.parametrize("ext", ["jsonl", "yaml", "db"])
def test_buildutil_delegates_to_provider(tmp_path, ext):
    cmd = _cmd(tmp_path, f"files.{ext}")
    cmd.initdb()
    cmd.add_entry("/usr/bin/x", _entry(mode="755"))
    cmd.add_entry("/usr/bin/x", _entry(mode="700"))
    cmd.remove_entry("/tmp/y")
    db = cmd.loaddb()
    assert db["/usr/bin/x"]["mode"] == "700"
    assert db["/tmp/y"] is None


def test_buildutil_db_format_override(tmp_path):
    # Suffix says yaml, --db-format forces sqlite.
    cmd = _cmd(tmp_path, "files.yaml")
    cmd.db_format = "sqlite"
    cmd.initdb()
    cmd.add_entry("/a", _entry())
    assert sniff_format(cmd.db) == "sqlite"


# --------------------------------------------------------------------------
# register_provider extension seam
# --------------------------------------------------------------------------


@pytest.fixture
def restore_registries():
    """Snapshot/restore the provider registries around a registration test."""
    providers = dict(dbmod.PROVIDERS)
    suffixes = dict(dbmod.SUFFIX_FORMATS)
    sniffers = list(dbmod._SNIFFERS)
    try:
        yield
    finally:
        dbmod.PROVIDERS.clear()
        dbmod.PROVIDERS.update(providers)
        dbmod.SUFFIX_FORMATS.clear()
        dbmod.SUFFIX_FORMATS.update(suffixes)
        dbmod._SNIFFERS[:] = sniffers


class _TsvDb(DbProvider):
    """A toy tab-separated backend for the registration test."""

    format = "tsv"
    _MARK = "#pkgforge-tsv\n"

    def load(self):
        if not self.path.exists():
            return {}
        db = {}
        for line in self.path.read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            path, mode = line.split("\t")
            db[path] = None if mode == "-" else {
                "mode": mode, "owner": "-", "group": "-", "type": "file", "meta": {}
            }
        return db

    def add(self, path, entry):
        with self.path.open("a") as fh:
            fh.write(f"{path}\t{entry['mode']}\n")

    def remove(self, path):
        with self.path.open("a") as fh:
            fh.write(f"{path}\t-\n")

    def compact(self):
        db = self.load()
        self.init()
        for p, e in db.items():
            if e is not None:
                self.add(p, e)

    def init(self):
        self.path.write_text(self._MARK)


def test_register_provider_selectable_by_name(restore_registries, tmp_path):
    register_provider("tsv", _TsvDb, suffixes=(".tsv",))
    p = open_db(tmp_path / "f.out", "tsv")
    assert isinstance(p, _TsvDb)
    p.init()
    p.add("/a", _entry(mode="644"))
    assert open_db(tmp_path / "f.out", "tsv").load()["/a"]["mode"] == "644"


def test_register_provider_selectable_by_suffix(restore_registries, tmp_path):
    register_provider("tsv", _TsvDb, suffixes=(".tsv",))
    assert isinstance(open_db(tmp_path / "f.tsv"), _TsvDb)
    assert format_for_suffix((tmp_path / "f.tsv")) == "tsv"


def test_register_provider_sniffer_wins(restore_registries, tmp_path):
    register_provider(
        "tsv",
        _TsvDb,
        suffixes=(".tsv",),
        sniff=lambda head: head.startswith(b"#pkgforge-tsv"),
    )
    path = tmp_path / "noext"
    open_db(path, "tsv").init()
    # Content sniffed as tsv even though the suffix is unknown.
    assert sniff_format(path) == "tsv"
    assert open_db(path, for_read=True).format == "tsv"


def test_register_provider_returns_class_for_decorator(restore_registries):
    assert register_provider("tsv", _TsvDb) is _TsvDb


def test_provider_registration_does_not_leak(tmp_path):
    # After the restore fixture, "tsv" must be gone from the global registry.
    assert "tsv" not in dbmod.PROVIDERS
