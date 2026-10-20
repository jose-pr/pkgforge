"""Registration: duplicate names are refused, and third-party classes are found
through the ``pkgforge.dbs`` / ``pkgforge.dump_formats`` entry-point groups."""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

from pkgforge.db import DbProvider, JSONLinesDb, format_for_suffix, open_db
from pkgforge.dbdump import DumpFormat, PerEntryFormat, RPMSpecFiles
from pkgforge.exceptions import PkgForgeError, UsageError

PLUGIN = """
import pkgforge


class TomlDb(pkgforge.DbProvider):
    NAME = "toml"
    SUFFIXES = (".toml",)

    def load(self):
        return {}

    def add(self, path, entry):
        with open(self.path, "a") as fh:
            fh.write(path + "\\n")

    def remove(self, path):
        pass

    def compact(self):
        pass

    def init(self):
        open(self.path, "w").close()


class LinesFormat(pkgforge.dbdump.PerEntryFormat):
    NAME = "lines"

    def render_entry(self, path, entry):
        return (path + "\\n").encode()
"""


def _distribution(root: Path, entry_points: str, source: str = PLUGIN) -> None:
    (root / "tomlplug.py").write_text(source, encoding="utf-8")
    info = root / "tomlplug-0.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: tomlplug\nVersion: 0.0.0\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(entry_points, encoding="utf-8")
    importlib.invalidate_caches()


@pytest.fixture
def plugin(tmp_path, monkeypatch, restore_registries):
    """Install ``tomlplug`` (a DB backend and a dump format) on ``sys.path``."""
    root = tmp_path / "site"
    root.mkdir()
    _distribution(
        root,
        "[pkgforge.dbs]\ntoml = tomlplug:TomlDb\n\n"
        "[pkgforge.dump_formats]\nlines = tomlplug:LinesFormat\n",
    )
    monkeypatch.delitem(sys.modules, "tomlplug", raising=False)
    monkeypatch.syspath_prepend(str(root))
    yield root
    sys.modules.pop("tomlplug", None)


def _db_class(name: str, **body):
    return type("ProbeDb", (JSONLinesDb,), {"NAME": name, **body})


def test_duplicate_db_name_is_refused(restore_registries):
    with pytest.raises(ValueError, match="'jsonl'.*JSONLinesDb"):
        _db_class("jsonl")
    assert DbProvider.get("jsonl") is JSONLinesDb


def test_duplicate_db_alias_is_refused(restore_registries):
    first = _db_class("probe", ALIASES=("pr",))
    with pytest.raises(ValueError, match="'pr'.*ProbeDb"):
        _db_class("probe2", ALIASES=("pr",))
    assert DbProvider.get("pr") is first
    assert "probe2" not in DbProvider._registry


def test_alias_clashing_with_a_name_is_refused(restore_registries):
    with pytest.raises(ValueError, match="'yaml'"):
        _db_class("probe", ALIASES=("yaml",))
    assert "probe" not in DbProvider._registry


def test_duplicate_suffix_is_refused(restore_registries, tmp_path):
    with pytest.raises(ValueError, match="'.jsonl'.*JSONLinesDb"):
        _db_class("probe", SUFFIXES=(".jsonl",))
    assert "probe" not in DbProvider._registry
    assert format_for_suffix(tmp_path / "x.jsonl") == "jsonl"


def test_duplicate_format_name_and_alias_are_refused(restore_registries):
    for body in ({"NAME": "rpm"}, {"NAME": "probe", "ALIASES": ("rpmspec",)}):
        with pytest.raises(ValueError, match="RPMSpecFiles"):
            type("ProbeFormat", (PerEntryFormat,), body)
    assert DumpFormat.get("rpm") is RPMSpecFiles
    assert "probe" not in DumpFormat._registry


def test_names_lists_an_advertised_plugin_before_it_is_loaded(plugin):
    assert "toml" in DbProvider.names()
    assert "lines" in DumpFormat.names()
    assert "toml" not in DbProvider._registry
    assert "tomlplug" not in sys.modules
    with pytest.raises(UsageError) as excinfo:
        DbProvider.get("nope")
    assert "toml" in str(excinfo.value)


def test_get_loads_an_advertised_class(plugin):
    cls = DbProvider.get("toml")
    assert cls.NAME == "toml"
    assert DbProvider._registry["toml"] is cls
    assert DumpFormat.get("lines").NAME == "lines"


def test_db_format_option_selects_a_plugin(plugin, cli, tmp_path):
    db = tmp_path / "x.db"
    result = cli("--db-format", "toml", "--db", str(db), "initdb")
    assert result.rc == 0, result.err
    assert db.exists()


def test_suffix_selects_a_plugin(plugin, cli, tmp_path):
    db = tmp_path / "y.toml"
    result = cli("--db", str(db), "initdb")
    assert result.rc == 0, result.err
    assert db.exists()
    assert type(open_db(db)).NAME == "toml"


def test_dump_selects_an_advertised_format(plugin, tmp_path, make_entry):
    from pkgforge.dbdump import dump

    out = tmp_path / "out.txt"
    dump({"/a": make_entry()}, "lines", out)
    assert out.read_bytes() == b"/a\n"


def test_broken_entry_point_is_a_clean_error(
    tmp_path, monkeypatch, restore_registries, cli
):
    root = tmp_path / "site"
    root.mkdir()
    _distribution(root, "[pkgforge.dbs]\ntoml = tomlplug:Missing\n")
    monkeypatch.delitem(sys.modules, "tomlplug", raising=False)
    monkeypatch.syspath_prepend(str(root))
    try:
        result = cli("--db-format", "toml", "--db", str(tmp_path / "x"), "initdb")
    finally:
        sys.modules.pop("tomlplug", None)
    assert result.rc == 1
    lines = result.err.decode().strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("pkgforge: error:")
    assert "tomlplug:Missing" in lines[0]


def test_entry_point_naming_the_wrong_class_is_refused(
    tmp_path, monkeypatch, restore_registries
):
    root = tmp_path / "site"
    root.mkdir()
    _distribution(root, "[pkgforge.dbs]\nother = tomlplug:TomlDb\n")
    monkeypatch.delitem(sys.modules, "tomlplug", raising=False)
    monkeypatch.syspath_prepend(str(root))
    try:
        with pytest.raises(PkgForgeError, match="entry point tomlplug:TomlDb"):
            DbProvider.get("other")
    finally:
        sys.modules.pop("tomlplug", None)


def test_a_failing_plugin_is_skipped_by_suffix_inference(
    tmp_path, monkeypatch, restore_registries, caplog
):
    root = tmp_path / "site"
    root.mkdir()
    _distribution(root, "[pkgforge.dbs]\ntoml = tomlplug:Missing\n")
    monkeypatch.delitem(sys.modules, "tomlplug", raising=False)
    monkeypatch.syspath_prepend(str(root))
    try:
        with caplog.at_level(logging.WARNING, logger="pkgforge.db"):
            assert format_for_suffix(tmp_path / "x.unknown") == "jsonl"
    finally:
        sys.modules.pop("tomlplug", None)
    assert [r.name for r in caplog.records] == ["pkgforge.db"]
    assert "tomlplug:Missing" in caplog.records[0].getMessage()
