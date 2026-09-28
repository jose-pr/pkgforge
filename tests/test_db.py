"""Tests for the provider-agnostic file DB (jsonl / yaml / sqlite)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SRC_DIR = Path(__file__).resolve().parents[1] / "src"

import pkgforge.db as dbmod
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


def test_roundtrip(provider, make_entry):
    provider.add("/usr/bin/x", make_entry(mode="755"))
    assert _reload(provider)["/usr/bin/x"]["mode"] == "755"


def test_last_write_wins(provider, make_entry):
    provider.add("/x", make_entry(mode="644"))
    provider.add("/x", make_entry(mode="600"))
    assert _reload(provider)["/x"]["mode"] == "600"


def test_removal_marks_none(provider, make_entry):
    provider.add("/x", make_entry())
    provider.remove("/x")
    assert _reload(provider)["/x"] is None


def test_meta_preserved(provider, make_entry):
    provider.add("/x", make_entry(meta={"rpmprefix": "%config"}))
    assert _reload(provider)["/x"]["meta"] == {"rpmprefix": "%config"}


def test_filetype_enum_stored_as_str(provider, make_entry):
    provider.add("/d", make_entry(type="directory"))
    assert _reload(provider)["/d"]["type"] == "directory"


def test_compact_drops_removed(provider, make_entry):
    provider.add("/keep", make_entry())
    provider.add("/gone", make_entry())
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


def test_sniff_detects_content_over_suffix(tmp_path, make_entry):
    # Write YAML content into a suffix-less file; a read auto-detects it as yaml.
    path = tmp_path / "noext"
    open_db(path, "yaml").add("/a", make_entry())
    assert sniff_format(path) == "yaml"
    assert open_db(path, for_read=True).format == "yaml"


def test_sniff_detects_sqlite(tmp_path):
    path = tmp_path / "store"  # no .db suffix
    open_db(path, "sqlite").init()
    assert sniff_format(path) == "sqlite"
    assert open_db(path, for_read=True).format == "sqlite"


def test_sniff_detects_jsonl(tmp_path, make_entry):
    path = tmp_path / "log"
    open_db(path, "jsonl").add("/a", make_entry())
    assert sniff_format(path) == "jsonl"


def test_unknown_format_raises(tmp_path):
    with pytest.raises(ValueError):
        open_db(tmp_path / "x", "toml")


# --------------------------------------------------------------------------
# lazy backend imports: sqlite3/yaml are optional at import time
# --------------------------------------------------------------------------


def test_import_without_sqlite3_or_yaml(tmp_path):
    # A subprocess is required: sys.modules can't be poisoned in-process
    # without breaking every other test that needs sqlite3/yaml for real.
    db_path = tmp_path / "f.jsonl"
    script = (
        "import sys\n"
        "sys.modules['_sqlite3'] = None\n"
        "sys.modules['yaml'] = None\n"
        "import pathlib\n"
        "import pkgforge\n"
        "from pkgforge.db import JsonlDb\n"
        f"p = JsonlDb(pathlib.Path({str(db_path)!r}))\n"
        "p.init()\n"
        "p.add('/a', {'mode': '644', 'owner': '-', 'group': '-', 'type': 'file', 'meta': {}})\n"
        "loaded = JsonlDb(p.path).load()\n"
        "assert loaded['/a']['mode'] == '644'\n"
        "try:\n"
        "    rc = pkgforge.main(['--help'])\n"
        "except SystemExit as exc:\n"
        "    rc = exc.code or 0\n"
        "assert rc == 0\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout.splitlines()


@pytest.mark.parametrize(
    "block, ext, fmt, action",
    [
        ("_sqlite3", "db", "sqlite", "p.init()"),
        (
            "yaml",
            "yaml",
            "yaml",
            "p.add('/a', {'mode': '644', 'owner': '-', 'group': '-', "
            "'type': 'file', 'meta': {}})",
        ),
    ],
    ids=["sqlite", "yaml"],
)
def test_missing_backend_module_is_one_line_error(tmp_path, block, ext, fmt, action):
    path = tmp_path / f"f.{ext}"
    script = (
        "import sys\n"
        f"sys.modules[{block!r}] = None\n"
        "import pathlib\n"
        "from pkgforge.db import open_db\n"
        "from pkgforge.common import PkgForgeError\n"
        f"p = open_db(pathlib.Path({str(path)!r}), {fmt!r})\n"
        "try:\n"
        f"    {action}\n"
        "except PkgForgeError as exc:\n"
        f"    assert {block!r} in str(exc), str(exc)\n"
        "    print('caught: ' + str(exc))\n"
        "else:\n"
        "    raise SystemExit('did not raise PkgForgeError')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line]
    assert len(lines) == 1
    assert block in lines[0]


# --------------------------------------------------------------------------
# yaml loader/dumper: prefer libyaml's C variants when available
# --------------------------------------------------------------------------


def test_yaml_uses_libyaml_when_available():
    if not getattr(yaml, "__with_libyaml__", False):
        pytest.skip("PyYAML installed without libyaml (no CSafeLoader/CSafeDumper)")

    from pkgforge.db import _yaml_io

    _yaml_io.cache_clear()
    try:
        loader, dumper = _yaml_io()
        assert loader is yaml.CSafeLoader
        assert dumper is yaml.CSafeDumper
    finally:
        _yaml_io.cache_clear()


@pytest.mark.parametrize(
    "which",
    [
        "pure",
        pytest.param(
            "c",
            marks=pytest.mark.skipif(
                not getattr(yaml, "__with_libyaml__", False),
                reason="PyYAML installed without libyaml",
            ),
        ),
    ],
)
def test_yaml_loader_parity(tmp_path, monkeypatch, make_entry, which):
    # Whichever loader/dumper _yaml_io() picks, behavior (last-wins,
    # tombstones) and the exact bytes compact() writes must be unchanged.
    import pkgforge.db as dbmod

    if which == "pure":
        loader, dumper = yaml.SafeLoader, yaml.SafeDumper
    else:
        loader, dumper = yaml.CSafeLoader, yaml.CSafeDumper
    monkeypatch.setattr(dbmod, "_yaml_io", lambda: (loader, dumper))

    p = open_db(tmp_path / "f.yaml", "yaml")
    p.init()
    p.add("/a", make_entry(mode="644"))
    p.add("/a", make_entry(mode="600"))  # last write for a path wins
    p.add("/b", make_entry(mode="755"))
    p.remove("/b")  # tombstone

    db = open_db(p.path, "yaml", for_read=True).load()
    assert db["/a"]["mode"] == "600"
    assert db["/b"] is None

    p.compact()
    compacted = p.path.read_text()
    assert compacted == yaml.safe_dump({"/a": make_entry(mode="600")})


def test_open_db_unknown_format_has_no_context(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        open_db(tmp_path / "x", "toml")
    assert excinfo.value.__suppress_context__ is True


def test_stdout_record_matches_jsonl_line(cmd, make_entry, capsys):
    # _write_entry's stdout fallback (no --db) must emit exactly the same
    # bytes JsonlDb.add/remove would append to a real file.
    from pkgforge.db import _jsonl_line

    inst = cmd(db=None)
    entry = make_entry(mode="755")

    inst.add_entry("/usr/bin/x", entry)
    assert capsys.readouterr().out == _jsonl_line("/usr/bin/x", entry)

    inst.remove_entry("/usr/bin/y")
    assert capsys.readouterr().out == _jsonl_line("/usr/bin/y", None)


# --------------------------------------------------------------------------
# backend specifics
# --------------------------------------------------------------------------


def test_jsonl_is_one_line_per_record(tmp_path, make_entry):
    p = open_db(tmp_path / "f.jsonl")
    p.add("/a", make_entry())
    p.add("/b", make_entry(mode="755"))
    lines = [line for line in p.path.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["path"] == "/a"


def test_sqlite_upserts_in_place_no_duplicate_rows(tmp_path, make_entry):
    import sqlite3

    p = open_db(tmp_path / "f.db")
    p.init()
    p.add("/x", make_entry(mode="644"))
    p.add("/x", make_entry(mode="600"))
    conn = sqlite3.connect(str(p.path))
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM entries").fetchone()
    finally:
        conn.close()
    assert count == 1  # upserted, not appended


def test_yaml_reads_legacy_written_db(tmp_path, make_entry):
    # A hand-written legacy YAML mapping still loads via the yaml provider.
    path = tmp_path / "legacy.yaml"
    path.write_text(
        yaml.safe_dump({"/usr/bin/x": make_entry(mode="755"), "/gone": None})
    )
    db = open_db(path, for_read=True).load()
    assert db["/usr/bin/x"]["mode"] == "755"
    assert db["/gone"] is None


# --------------------------------------------------------------------------
# PkgForgeCmd delegation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ["jsonl", "yaml", "db"])
def test_cmd_delegates_to_provider(cmd, make_entry, ext):
    inst = cmd(name=f"files.{ext}")
    inst.initdb()
    inst.add_entry("/usr/bin/x", make_entry(mode="755"))
    inst.add_entry("/usr/bin/x", make_entry(mode="700"))
    inst.remove_entry("/tmp/y")
    db = inst.loaddb()
    assert db["/usr/bin/x"]["mode"] == "700"
    assert db["/tmp/y"] is None


def test_cmd_db_format_override(cmd, make_entry):
    # Suffix says yaml, --db-format forces sqlite.
    inst = cmd(name="files.yaml", db_format="sqlite")
    inst.initdb()
    inst.add_entry("/a", make_entry())
    assert sniff_format(inst.db) == "sqlite"


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
        assert "tsv" not in dbmod.PROVIDERS


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
            db[path] = (
                None
                if mode == "-"
                else {
                    "mode": mode,
                    "owner": "-",
                    "group": "-",
                    "type": "file",
                    "meta": {},
                }
            )
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


def test_register_provider_selectable_by_name(restore_registries, tmp_path, make_entry):
    register_provider("tsv", _TsvDb, suffixes=(".tsv",))
    p = open_db(tmp_path / "f.out", "tsv")
    assert isinstance(p, _TsvDb)
    p.init()
    p.add("/a", make_entry(mode="644"))
    assert open_db(tmp_path / "f.out", "tsv").load()["/a"]["mode"] == "644"


def test_register_provider_selectable_by_suffix(restore_registries, tmp_path):
    register_provider("tsv", _TsvDb, suffixes=(".tsv",))
    assert isinstance(open_db(tmp_path / "f.tsv"), _TsvDb)
    assert format_for_suffix(tmp_path / "f.tsv") == "tsv"


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


def test_register_provider_accepted_as_db_format(restore_registries, tmp_path):
    from pkgforge.common import UsageError
    from pkgforge.initdb import InitDb

    with pytest.raises(UsageError) as excinfo:
        InitDb(db=tmp_path / "x.jsonl", db_format="toml")
    assert isinstance(excinfo.value, ValueError)

    register_provider("custom", _TsvDb)
    inst = InitDb(db=tmp_path / "x.jsonl", db_format="custom")
    assert inst.db_format == "custom"
