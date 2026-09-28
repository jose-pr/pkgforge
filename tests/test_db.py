"""Tests for the provider-agnostic file DB (jsonl / yaml / sqlite)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
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
        # A null-only-resolver subclass of CSafeLoader, not the bare class.
        assert issubclass(loader, yaml.CSafeLoader)
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


# --------------------------------------------------------------------------
# UTF-8 text I/O, newline repair on append, and DbError
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["jsonl", "yaml"])
def test_append_repairs_missing_newline(tmp_path, fmt, make_entry):
    path = tmp_path / f"f.{fmt}"
    if fmt == "jsonl":
        path.write_bytes(
            b'{"path": "/old", "mode": "644", "owner": "-", "group": "-", '
            b'"type": "file", "meta": {}}'
        )
    else:
        path.write_bytes(b"/old: null")  # a tombstone, no trailing newline

    p = open_db(path, fmt)
    p.add("/new", make_entry(mode="600"))

    db = open_db(path, fmt, for_read=True).load()
    assert db["/new"]["mode"] == "600"
    if fmt == "jsonl":
        assert db["/old"]["mode"] == "644"
    else:
        assert db["/old"] is None


def test_jsonl_error_names_file_and_line(tmp_path):
    from pkgforge.db import DbError

    path = tmp_path / "f.jsonl"
    path.write_text(
        '{"path": "/a", "mode": "644", "owner": "-", "group": "-", '
        '"type": "file", "meta": {}}\n'
        '{"path": "/b", "mode": "644", "owner": "-", "group": "-", '
        '"type": "file", "meta": {}}\n'
        '{"path": "/c", not valid json\n',
        encoding="utf-8",
    )
    with pytest.raises(DbError) as excinfo:
        open_db(path, "jsonl", for_read=True).load()
    assert f"{path}:3:" in str(excinfo.value)


def test_yaml_error_names_file(tmp_path):
    from pkgforge.db import DbError

    path = tmp_path / "f.yaml"
    path.write_text("a:\n  b: [1, 2\n", encoding="utf-8")  # unterminated flow seq
    with pytest.raises(DbError) as excinfo:
        open_db(path, "yaml", for_read=True).load()
    assert str(path) in str(excinfo.value)


def _write_malformed(tmp_path: Path, kind: str):
    if kind == "yaml_list":
        path = tmp_path / "f.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        fmt = "yaml"
    elif kind == "yaml_scalar":
        path = tmp_path / "f.yaml"
        path.write_text("just a scalar\n", encoding="utf-8")
        fmt = "yaml"
    elif kind == "jsonl_array":
        path = tmp_path / "f.jsonl"
        path.write_text("[1, 2, 3]\n", encoding="utf-8")
        fmt = "jsonl"
    else:  # latin1_bytes
        path = tmp_path / "f.jsonl"
        path.write_bytes(b'{"path": "/x", "mode": "\xe9"}\n')
        fmt = "jsonl"
    return path, fmt


@pytest.mark.parametrize(
    "kind", ["yaml_list", "yaml_scalar", "jsonl_array", "latin1_bytes"]
)
def test_malformed_db_raises(tmp_path, kind):
    from pkgforge.db import DbError

    path, fmt = _write_malformed(tmp_path, kind)
    with pytest.raises(DbError) as excinfo:
        open_db(path, fmt, for_read=True).load()
    assert str(path) in str(excinfo.value)


@pytest.mark.posix
@pytest.mark.parametrize("fmt", ["jsonl", "yaml"])
def test_utf8_db_under_ascii_locale(tmp_path, fmt):
    db_path = tmp_path / f"f.{fmt}"
    if fmt == "jsonl":
        db_path.write_bytes(
            b'{"group": "-", "meta": {}, "mode": "-", "owner": "-", '
            b'"path": "/etc/caf\xc3\xa9.conf", "type": "file"}\n'
        )
    else:
        db_path.write_bytes(b"/etc/caf\xc3\xa9.conf:\n  type: file\n")

    script = (
        "import locale, sys\n"
        "enc = locale.getpreferredencoding(False)\n"
        "if 'utf' in enc.lower():\n"
        "    print('LOCALE_STILL_UTF8:' + enc)\n"
        "    sys.exit(0)\n"
        "import pathlib\n"
        "from pkgforge.db import open_db\n"
        f"path = pathlib.Path({str(db_path)!r})\n"
        f"p = open_db(path, {fmt!r}, for_read=True)\n"
        "db = p.load()\n"
        "assert '/etc/caf\\u00e9.conf' in db, sorted(db)\n"
        "p.compact()\n"
        f"p2 = open_db(path, {fmt!r}, for_read=True)\n"
        "db2 = p2.load()\n"
        "assert '/etc/caf\\u00e9.conf' in db2, sorted(db2)\n"
        "print('OK')\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC_DIR),
        "LC_ALL": "en_GB.iso885915",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
    }
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    if out.startswith("LOCALE_STILL_UTF8"):
        pytest.skip(f"could not apply a non-UTF-8 locale ({out})")
    assert out == "OK", result.stdout


# --------------------------------------------------------------------------
# yaml scalars load as strings; missing fields default; bad types raise
# --------------------------------------------------------------------------


def test_yaml_scalars_load_as_strings(tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text(
        "/usr/bin/tool:\n"
        "  mode: 0755\n"
        "  owner: 0\n"
        "  group: root\n"
        "  type: file\n"
        "  meta: {enabled: yes}\n"
        "/gone: null\n",
        encoding="utf-8",
    )
    db = open_db(path, "yaml", for_read=True).load()
    entry = db["/usr/bin/tool"]
    assert entry["mode"] == "0755"
    assert entry["owner"] == "0"
    assert entry["meta"] == {"enabled": "yes"}
    assert db["/gone"] is None


@pytest.mark.parametrize("fmt", ["jsonl", "yaml"])
def test_missing_fields_normalized(tmp_path, fmt):
    path = tmp_path / f"f.{fmt}"
    if fmt == "jsonl":
        path.write_text('{"path": "/a"}\n', encoding="utf-8")
    else:
        path.write_text("/a: {}\n", encoding="utf-8")

    db = open_db(path, fmt, for_read=True).load()
    entry = db["/a"]
    assert entry["meta"] == {}
    assert entry["mode"] == "-"
    assert entry["owner"] == "-"
    assert entry["group"] == "-"
    assert entry["type"] is None


def test_null_type_loads(tmp_path):
    # Pin: FileEntry.from_args writes an explicit "type": null when unset
    # (not a missing key) -- _normalize must accept that, not reject it.
    path = tmp_path / "f.jsonl"
    path.write_text(
        '{"path": "/a", "mode": "644", "owner": "-", "group": "-", '
        '"type": null, "meta": {}}\n',
        encoding="utf-8",
    )
    db = open_db(path, "jsonl", for_read=True).load()
    assert db["/a"]["type"] is None


def test_jsonl_int_mode_is_str(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text(
        '{"path": "/a", "mode": 755, "owner": 0, "group": 0, '
        '"type": "file", "meta": {}}\n',
        encoding="utf-8",
    )
    db = open_db(path, "jsonl", for_read=True).load()
    entry = db["/a"]
    assert entry["mode"] == "755"
    assert entry["owner"] == "0"
    assert entry["group"] == "0"


def _write_bad_field(tmp_path: Path, kind: str) -> Path:
    base = (
        '{{"path": "/a", "owner": "-", "group": "-", "type": "file", '
        '"meta": {{}}, "mode": {mode}}}\n'
    )
    if kind == "float_mode":
        path = tmp_path / "f.jsonl"
        path.write_text(base.format(mode="7.5"), encoding="utf-8")
    elif kind == "bool_mode":
        path = tmp_path / "f.jsonl"
        path.write_text(base.format(mode="true"), encoding="utf-8")
    elif kind == "bool_owner":
        path = tmp_path / "f.jsonl"
        path.write_text(
            '{"path": "/a", "mode": "-", "owner": true, "group": "-", '
            '"type": "file", "meta": {}}\n',
            encoding="utf-8",
        )
    elif kind == "non_octal_int":
        path = tmp_path / "f.jsonl"
        path.write_text(base.format(mode="8"), encoding="utf-8")
    elif kind == "int_type":
        path = tmp_path / "f.jsonl"
        path.write_text(
            '{"path": "/a", "mode": "-", "owner": "-", "group": "-", '
            '"type": 5, "meta": {}}\n',
            encoding="utf-8",
        )
    else:  # record_list: a per-path YAML record that is a list, not a mapping
        path = tmp_path / "f.yaml"
        path.write_text("/a:\n  - 1\n  - 2\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "kind",
    [
        "float_mode",
        "bool_mode",
        "bool_owner",
        "non_octal_int",
        "int_type",
        "record_list",
    ],
)
def test_bad_field_types_raise(tmp_path, kind):
    from pkgforge.db import DbError

    path = _write_bad_field(tmp_path, kind)
    fmt = "yaml" if kind == "record_list" else "jsonl"
    with pytest.raises(DbError) as excinfo:
        open_db(path, fmt, for_read=True).load()
    assert "/a" in str(excinfo.value)


# --------------------------------------------------------------------------
# sniffing: a quoted-first-key jsonl sniffer; flow-style YAML append refusal
# --------------------------------------------------------------------------


def test_flow_yaml_sniffs_as_yaml(tmp_path):
    path = tmp_path / "flow.yaml"
    path.write_text(
        "{/usr/bin/x: {mode: '0755', owner: root, group: root, type: file, "
        "meta: {}}}\n",
        encoding="utf-8",
    )
    assert sniff_format(path) == "yaml"
    db = open_db(path, for_read=True).load()
    assert db["/usr/bin/x"]["mode"] == "0755"


def test_spaced_jsonl_sniffs_as_jsonl(tmp_path):
    # Pin: a hand-written record with a space before the quoted key must
    # keep sniffing as jsonl, not fall through to the yaml fallback.
    path = tmp_path / "noext"
    path.write_text('{ "path": "/a", "mode": "644"}\n', encoding="utf-8")
    assert sniff_format(path) == "jsonl"


@pytest.mark.parametrize(
    "content",
    [
        "{/usr/bin/x: {mode: '0755'}}\n",
        "{}\n",
        "# a comment\n---\n{/usr/bin/x: {mode: '0755'}}\n",
    ],
    ids=["flow", "empty", "comment_then_flow"],
)
def test_append_to_flow_yaml_refused(tmp_path, make_entry, content):
    from pkgforge.db import DbError

    path = tmp_path / "flow.yaml"
    path.write_bytes(content.encode("utf-8"))
    before = path.read_bytes()

    p = open_db(path, "yaml")
    with pytest.raises(DbError):
        p.add("/new", make_entry())

    assert path.read_bytes() == before


def test_compact_rewrites_flow_yaml_to_block(tmp_path, make_entry):
    path = tmp_path / "flow.yaml"
    path.write_text(
        "{/usr/bin/x: {mode: '0755', owner: root, group: root, type: file, "
        "meta: {}}}\n",
        encoding="utf-8",
    )
    p = open_db(path, "yaml")
    p.compact()

    rewritten = path.read_text(encoding="utf-8").lstrip()
    assert not rewritten.startswith("{")

    # An append now succeeds against the rewritten block-style file.
    p.add("/new", make_entry())
    db = open_db(path, "yaml", for_read=True).load()
    assert db["/usr/bin/x"]["mode"] == "0755"
    assert db["/new"] is not None


# --------------------------------------------------------------------------
# locked, atomic compaction of the append-log DBs
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["jsonl", "yaml"])
def test_compact_failure_leaves_db_intact(tmp_path, monkeypatch, make_entry, fmt):
    import pkgforge.db as dbmod

    p = open_db(tmp_path / f"f.{fmt}", fmt)
    p.init()
    p.add("/keep", make_entry())
    p.add("/gone", make_entry())
    p.remove("/gone")
    before = p.path.read_bytes()

    def _raise(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(dbmod.os, "replace", _raise)

    with pytest.raises(OSError):
        p.compact()

    assert p.path.read_bytes() == before
    assert not any(name.endswith(".tmp") for name in os.listdir(tmp_path))
    db = open_db(p.path, fmt, for_read=True).load()
    assert db["/keep"] is not None
    assert db["/gone"] is None


@pytest.mark.posix
@pytest.mark.parametrize("fmt", ["jsonl", "yaml"])
def test_compact_keeps_mode_and_symlink(tmp_path, make_entry, fmt):
    # Pin: this already held before the atomic-write fix (write_text follows
    # a symlink too), and must keep holding after it.
    real = tmp_path / f"real.{fmt}"
    link = tmp_path / f"link.{fmt}"
    p = open_db(real, fmt)
    p.init()
    p.add("/keep", make_entry())
    p.add("/gone", make_entry())
    p.remove("/gone")
    os.chmod(real, 0o640)
    link.symlink_to(real)

    open_db(link, fmt).compact()

    assert link.is_symlink()
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    db = open_db(link, fmt, for_read=True).load()
    assert "/keep" in db
    assert "/gone" not in db


@pytest.mark.posix
@pytest.mark.parametrize("fmt", ["jsonl", "yaml"])
def test_compact_keeps_concurrent_append(tmp_path, monkeypatch, make_entry, fmt):
    import pkgforge.db as dbmod

    db_path = tmp_path / f"f.{fmt}"
    p = open_db(db_path, fmt)
    p.init()
    p.add("/usr/a", make_entry())

    ready = tmp_path / "ready"
    script = (
        "import pathlib\n"
        "from pkgforge.db import open_db\n"
        f"p = open_db(pathlib.Path({str(db_path)!r}), {fmt!r})\n"
        f"pathlib.Path({str(ready)!r}).write_text('1')\n"
        "p.add('/usr/b', {'mode': '644', 'owner': '-', 'group': '-', "
        "'type': 'file', 'meta': {}})\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
    )
    provider_cls = dbmod.JsonlDb if fmt == "jsonl" else dbmod.YamlDb
    real_load = provider_cls.load

    def _waiting_load(self):
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        assert proc.poll() is None, "child appended before compact held the lock"
        return real_load(self)

    try:
        monkeypatch.setattr(provider_cls, "load", _waiting_load)
        open_db(db_path, fmt).compact()
    finally:
        monkeypatch.undo()  # this test's own final load() below must not re-trigger it
        proc.wait(timeout=10)

    db = open_db(db_path, fmt, for_read=True).load()
    assert "/usr/a" in db
    assert "/usr/b" in db


def test_db_imports_without_fcntl(tmp_path):
    # Pin: pkgforge.db must still import (and its locking helpers still
    # work, unlocked) on an interpreter without fcntl (e.g. Windows).
    db_path = tmp_path / "f.jsonl"
    script = (
        "import sys\n"
        "sys.modules['fcntl'] = None\n"
        "import pathlib\n"
        "import pkgforge\n"
        "from pkgforge.db import JsonlDb\n"
        f"p = JsonlDb(pathlib.Path({str(db_path)!r}))\n"
        "p.init()\n"
        "p.add('/a', {'mode': '644', 'owner': '-', 'group': '-', 'type': 'file', 'meta': {}})\n"
        "p.compact()\n"
        "assert JsonlDb(p.path).load()['/a']['mode'] == '644'\n"
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


# --------------------------------------------------------------------------
# batched writes: one sqlite connection/transaction per command (perf)
# --------------------------------------------------------------------------


def test_batch_commits_every_n_rows(tmp_path, monkeypatch, make_entry):
    import sqlite3

    monkeypatch.setattr(dbmod, "_BATCH_ROWS", 2)
    db_path = tmp_path / "f.db"
    p = open_db(db_path, "sqlite")
    p.init()

    def _count_via_second_connection() -> int:
        conn = sqlite3.connect(str(db_path))
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM entries").fetchone()
        finally:
            conn.close()
        return count

    with p.batch() as batch:
        batch.add("/a", make_entry())
        assert _count_via_second_connection() == 0  # 1 row: not yet due

        batch.add("/b", make_entry())
        assert _count_via_second_connection() == 2  # 2 rows: committed

    assert len(open_db(db_path, "sqlite", for_read=True).load()) == 2


def test_batch_keeps_rows_on_error(tmp_path, make_entry):
    db_path = tmp_path / "f.db"
    p = open_db(db_path, "sqlite")
    p.init()

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with p.batch() as batch:
            batch.add("/a", make_entry())
            raise Boom()

    # Partial work is kept (append logs keep it too) and the connection is
    # closed even on the exception path -- an open handle would keep this
    # unlink() from succeeding on Windows.
    loaded = open_db(db_path, "sqlite", for_read=True).load()
    assert "/a" in loaded
    db_path.unlink()


@pytest.mark.posix
def test_scan_with_provider_without_batch(tmp_path, cli, restore_registries):
    # A third-party provider that never overrides batch() (inherits the
    # DbProvider default, a no-op) still works under scan.
    register_provider("tsv", _TsvDb, suffixes=(".tsv",))

    root = tmp_path / "root"
    tree = root / "usr" / "share" / "tool"
    tree.mkdir(parents=True)
    (tree / "a").write_text("x")
    (tree / "b").write_text("y")

    # --db-format is explicit so open_db's for_read=True sniffing (which
    # would otherwise re-sniff and silently fall back to yaml once the file
    # exists, since _TsvDb registered no sniff=) never kicks in.
    db = tmp_path / "files.tsv"
    result = cli(
        "--db",
        str(db),
        "--db-format",
        "tsv",
        "--buildroot",
        str(root),
        "scan",
        "/usr/share/tool",
    )
    assert result.rc == 0

    loaded = _TsvDb(db).load()
    assert "/usr/share/tool/a" in loaded
    assert "/usr/share/tool/b" in loaded


def test_scan_sqlite_one_connection(tmp_path, cli, monkeypatch):
    import sqlite3

    root = tmp_path / "root"
    tree = root / "usr" / "share" / "tool"
    tree.mkdir(parents=True)
    for i in range(200):
        (tree / f"f{i:03d}").write_text("x")

    calls = []
    real_connect = sqlite3.connect

    def counting_connect(*a, **k):
        calls.append(1)
        return real_connect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)

    db = tmp_path / "files.db"
    result = cli("--db", str(db), "--buildroot", str(root), "scan", "/usr/share/tool")
    assert result.rc == 0
    assert len(calls) == 1

    loaded = open_db(db, "sqlite", for_read=True).load()
    assert len(loaded) == 200


# --------------------------------------------------------------------------
# reading a SQLite DB never writes; non-UTF-8 text fails cleanly
# --------------------------------------------------------------------------


def test_sqlite_load_leaves_empty_file(tmp_path):
    path = tmp_path / "empty.db"
    path.write_bytes(b"")

    assert open_db(path, "sqlite", for_read=True).load() == {}
    assert path.stat().st_size == 0


@pytest.mark.parametrize("op", ["load", "compact"])
def test_sqlite_foreign_db_rejected(tmp_path, op):
    import sqlite3

    from pkgforge.db import DbError

    path = tmp_path / "foreign.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
    finally:
        conn.close()

    p = open_db(path, "sqlite", for_read=True)
    with pytest.raises(DbError):
        getattr(p, op)()

    conn = sqlite3.connect(str(path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert tables == {"t"}  # never given an `entries` table of our own


@pytest.mark.parametrize("which", ["path", "owner"])
def test_sqlite_rejects_non_utf8_text(tmp_path, make_entry, which):
    from pkgforge.db import DbError

    p = open_db(tmp_path / "f.db", "sqlite")
    p.init()
    bad = "raw\udce9"  # a lone surrogate: undecodable bytes from os.walk
    if which == "path":
        path, entry = bad, make_entry()
    else:
        path, entry = "/a", make_entry(owner=bad)

    with pytest.raises(DbError) as excinfo:
        p.add(path, entry)
    assert "UTF-8" in str(excinfo.value)
    assert open_db(p.path, "sqlite", for_read=True).load() == {}
