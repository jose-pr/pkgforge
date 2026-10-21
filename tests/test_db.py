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

SRC_DIR = Path(__file__).resolve().parents[1] / "src"

import pkgforge.db as dbmod
from pkgforge.exceptions import DbError
from pkgforge.db import (
    DbProvider,
    JSONLinesDb,
    SQLiteDb,
    YAMLDb,
    format_for_suffix,
    open_db,
)

ALL_FORMATS = ["jsonl", "yaml", "sqlite"]


def _has_libyaml() -> bool:
    try:
        import yaml
    except ImportError:
        return False
    return bool(getattr(yaml, "__with_libyaml__", False))


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
    return open_db(provider.path, provider.NAME).load()


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
    provider.add("/x", make_entry(meta={"note": "%config"}))
    assert _reload(provider)["/x"]["meta"] == {"note": "%config"}


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
# selection: --db-format, else suffix; content never decides
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
    assert isinstance(open_db(tmp_path / "a.jsonl"), JSONLinesDb)
    assert isinstance(open_db(tmp_path / "a.yaml"), YAMLDb)
    assert isinstance(open_db(tmp_path / "a.db"), SQLiteDb)


def test_explicit_format_overrides_suffix(tmp_path):
    # A .yaml suffix but forced sqlite.
    assert isinstance(open_db(tmp_path / "a.yaml", "sqlite"), SQLiteDb)


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
        "from pkgforge.cli import main\n"
        "from pkgforge.db import JSONLinesDb\n"
        f"p = JSONLinesDb(pathlib.Path({str(db_path)!r}))\n"
        "p.init()\n"
        "p.add('/a', {'mode': '644', 'owner': '-', 'group': '-', 'type': 'file', 'kind': 'file', 'caps': '-', 'package': '-', 'meta': {}})\n"
        "loaded = JSONLinesDb(p.path).load()\n"
        "assert loaded['/a']['mode'] == '644'\n"
        "try:\n"
        "    rc = main(['--help'])\n"
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


def test_yaml_extra_missing_names_the_extra(tmp_path):
    # PyYAML is the optional ``yaml`` extra: hidden, the package imports, the
    # jsonl backend works and the yaml backend fails naming the extra.
    script = (
        "import os, sys\n"
        "sys.modules['yaml'] = None\n"
        "import pkgforge\n"
        "from pkgforge.cli import main\n"
        "os.chdir(sys.argv[1])\n"
        "print('jsonl', main(['--db', 'x.jsonl', 'initdb']) or 0)\n"
        "print('yaml', main(['--db', 'x.yaml', 'initdb']))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["jsonl 0", "yaml 1"]
    assert "pip install 'pkgforge[yaml]'" in result.stderr
    assert (tmp_path / "x.jsonl").exists()


@pytest.mark.parametrize(
    "block, ext, fmt, action",
    [
        ("_sqlite3", "db", "sqlite", "p.init()"),
        (
            "yaml",
            "yaml",
            "yaml",
            "p.add('/a', {'mode': '644', 'owner': '-', 'group': '-', "
            "'type': 'file', 'kind': 'file', 'caps': '-', 'package': '-', 'meta': {}})",
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
        "from pkgforge.exceptions import PkgForgeError\n"
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


def test_yaml_uses_libyaml_when_available(yaml_module):
    yaml = yaml_module
    if not getattr(yaml, "__with_libyaml__", False):
        pytest.skip("PyYAML installed without libyaml (no CSafeLoader/CSafeDumper)")

    from pkgforge.db._yaml import _yaml_io

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
                not _has_libyaml(),
                reason="PyYAML installed without libyaml",
            ),
        ),
    ],
)
def test_yaml_loader_parity(tmp_path, monkeypatch, make_entry, which, yaml_module):
    yaml = yaml_module
    # Whichever loader/dumper _yaml_io() picks, behavior (last-wins,
    # tombstones) and the exact bytes compact() writes must be unchanged.
    import pkgforge.db._yaml as db_yaml

    if which == "pure":
        loader, dumper = yaml.SafeLoader, yaml.SafeDumper
    else:
        loader, dumper = yaml.CSafeLoader, yaml.CSafeDumper
    monkeypatch.setattr(db_yaml, "_yaml_io", lambda: (loader, dumper))

    p = open_db(tmp_path / "f.yaml", "yaml")
    p.init()
    p.add("/a", make_entry(mode="644"))
    p.add("/a", make_entry(mode="600"))  # last write for a path wins
    p.add("/b", make_entry(mode="755"))
    p.remove("/b")  # tombstone

    db = open_db(p.path, "yaml").load()
    assert db["/a"]["mode"] == "600"
    assert db["/b"] is None

    p.compact()
    compacted = p.path.read_text()
    assert compacted == yaml.safe_dump({"/a": make_entry(mode="600")})


def test_open_db_unknown_format_has_no_context(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        open_db(tmp_path / "x", "toml")
    assert excinfo.value.__suppress_context__ is True


def test_stdout_records_are_the_jsonl_lines(tmp_path, cli):
    # With no --db the command prints exactly the lines JSONLinesDb would
    # append to a real file for the records the operation returned.
    from pkgforge import Project
    from pkgforge.db import jsonl_line

    root = tmp_path / "root"
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "x").write_text("x")
    records = Project(root).scan("/usr/bin", mode="755")
    assert records

    result = cli("--buildroot", str(root), "scan", "-m", "755", "/usr/bin")
    assert result.rc == 0
    assert result.out.decode() == "".join(
        jsonl_line(path, entry) for path, entry in records
    )


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


def test_yaml_reads_legacy_written_db(tmp_path, make_entry, yaml_module):
    # A hand-written legacy YAML mapping still loads via the yaml provider.
    path = tmp_path / "legacy.yaml"
    path.write_text(
        yaml_module.safe_dump({"/usr/bin/x": make_entry(mode="755"), "/gone": None})
    )
    db = open_db(path).load()
    assert db["/usr/bin/x"]["mode"] == "755"
    assert db["/gone"] is None


def test_yaml_content_under_a_jsonl_suffix_fails_to_load(
    tmp_path, make_entry, yaml_module
):
    path = tmp_path / "legacy.jsonl"
    path.write_text(yaml_module.safe_dump({"/old": make_entry(mode="755")}))

    with pytest.raises(DbError, match="legacy.jsonl.*--db-format"):
        open_db(path).load()
    assert open_db(path, "yaml").load()["/old"]["mode"] == "755"


# --------------------------------------------------------------------------
# Project delegation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ["jsonl", "yaml", "db"])
def test_project_delegates_to_provider(project, make_entry, ext):
    proj = project(name=f"files.{ext}")
    proj.init()
    proj.db.add("/usr/bin/x", make_entry(mode="755"))
    proj.db.add("/usr/bin/x", make_entry(mode="700"))
    proj.db.remove("/tmp/y")
    db = proj.load()
    assert db["/usr/bin/x"]["mode"] == "700"
    assert db["/tmp/y"] is None


def test_project_db_format_override(project, make_entry):
    # Suffix says yaml, db_format forces sqlite.
    proj = project(name="files.yaml", db_format="sqlite")
    proj.init()
    proj.db.add("/a", make_entry())
    assert proj.db.path.read_bytes().startswith(b"SQLite format 3\x00")


# --------------------------------------------------------------------------
# DbProvider subclass extension seam
# --------------------------------------------------------------------------


class _TsvDb(DbProvider):
    """A toy tab-separated backend for provider-subclass tests.

    Declares no ``NAME`` of its own, so subclassing it directly registers
    nothing (mirrors an abstract intermediate class such as
    ``AppendLogDb``); a test builds a real, registered backend with
    ``type("TsvDb", (_TsvDb,), {"NAME": "tsv", ...})``.
    """

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
                    "kind": "file",
                    "caps": "-",
                    "package": "-",
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


def test_db_provider_subclass_selectable_by_name(
    restore_registries, tmp_path, make_entry
):
    Tsv = type("TsvDb", (_TsvDb,), {"NAME": "tsv", "SUFFIXES": (".tsv",)})
    p = open_db(tmp_path / "f.out", "tsv")
    assert isinstance(p, Tsv)
    p.init()
    p.add("/a", make_entry(mode="644"))
    assert open_db(tmp_path / "f.out", "tsv").load()["/a"]["mode"] == "644"


def test_db_provider_subclass_selectable_by_suffix(restore_registries, tmp_path):
    Tsv = type("TsvDb", (_TsvDb,), {"NAME": "tsv", "SUFFIXES": (".tsv",)})
    assert isinstance(open_db(tmp_path / "f.tsv"), Tsv)
    assert format_for_suffix(tmp_path / "f.tsv") == "tsv"


def test_db_provider_subclass_accepted_as_db_format(restore_registries, tmp_path):
    from pkgforge import Project
    from pkgforge.exceptions import UsageError

    with pytest.raises(UsageError) as excinfo:
        Project(tmp_path, tmp_path / "x.jsonl", db_format="toml")
    assert isinstance(excinfo.value, ValueError)

    type("TsvDb", (_TsvDb,), {"NAME": "custom"})
    proj = Project(tmp_path, tmp_path / "x.jsonl", db_format="custom")
    assert proj.db.NAME == "custom"


def test_db_provider_subclass_adds_leading_dot(restore_registries, tmp_path):
    Tsv = type(
        "TsvDb", (_TsvDb,), {"NAME": "tsv", "SUFFIXES": ("TSV",)}
    )  # no dot, upper-case
    assert format_for_suffix(tmp_path / "x.tsv") == "tsv"
    assert isinstance(open_db(tmp_path / "x.tsv"), Tsv)


def test_db_provider_subclass_rejects_multi_dot_suffix(restore_registries):
    with pytest.raises(ValueError):
        type("TargzDb", (_TsvDb,), {"NAME": "targz", "SUFFIXES": (".tar.gz",)})
    assert "targz" not in DbProvider._registry


def test_db_provider_subclass_inherits_neither_aliases_nor_suffixes(
    restore_registries, tmp_path
):
    # A subclass that does not redeclare ALIASES/SUFFIXES in its OWN body
    # must not silently claim its parent's: registration (and therefore
    # get/format_for_suffix) reads only what a class's own __dict__ sets,
    # never what plain attribute access would show as inherited.
    Base = type(
        "TsvDb",
        (_TsvDb,),
        {"NAME": "tsv", "ALIASES": ("tabsep",), "SUFFIXES": (".tsv",)},
    )
    type("TsvDbSub", (Base,), {"NAME": "tsv2"})

    assert DbProvider.get("tabsep") is Base
    assert format_for_suffix(tmp_path / "x.tsv") == "tsv"


# --------------------------------------------------------------------------
# UTF-8 text I/O, newline repair on append, and DbError
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["jsonl", "yaml"])
def test_append_repairs_missing_newline(tmp_path, fmt, make_entry):
    path = tmp_path / f"f.{fmt}"
    if fmt == "jsonl":
        path.write_bytes(
            b'{"path": "/old", "mode": "644", "owner": "-", "group": "-", '
            b'"type": "file", "kind": "file", "caps": "-", "package": "-", "meta": {}}'
        )
    else:
        path.write_bytes(b"/old: null")  # a tombstone, no trailing newline

    p = open_db(path, fmt)
    p.add("/new", make_entry(mode="600"))

    db = open_db(path, fmt).load()
    assert db["/new"]["mode"] == "600"
    if fmt == "jsonl":
        assert db["/old"]["mode"] == "644"
    else:
        assert db["/old"] is None


def test_jsonl_error_names_file_and_line(tmp_path):
    from pkgforge.exceptions import DbError

    path = tmp_path / "f.jsonl"
    path.write_text(
        '{"path": "/a", "mode": "644", "owner": "-", "group": "-", '
        '"type": "file", "kind": "file", "caps": "-", "package": "-", "meta": {}}\n'
        '{"path": "/b", "mode": "644", "owner": "-", "group": "-", '
        '"type": "file", "kind": "file", "caps": "-", "package": "-", "meta": {}}\n'
        '{"path": "/c", not valid json\n',
        encoding="utf-8",
    )
    with pytest.raises(DbError) as excinfo:
        open_db(path, "jsonl").load()
    assert f"{path}:3:" in str(excinfo.value)


def test_yaml_error_names_file(tmp_path):
    from pkgforge.exceptions import DbError

    path = tmp_path / "f.yaml"
    path.write_text("a:\n  b: [1, 2\n", encoding="utf-8")  # unterminated flow seq
    with pytest.raises(DbError) as excinfo:
        open_db(path, "yaml").load()
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
    from pkgforge.exceptions import DbError

    path, fmt = _write_malformed(tmp_path, kind)
    with pytest.raises(DbError) as excinfo:
        open_db(path, fmt).load()
    assert str(path) in str(excinfo.value)


@pytest.mark.posix
@pytest.mark.parametrize("fmt", ["jsonl", "yaml"])
def test_utf8_db_under_ascii_locale(tmp_path, fmt):
    db_path = tmp_path / f"f.{fmt}"
    if fmt == "jsonl":
        db_path.write_bytes(
            b'{"group": "-", "meta": {}, "mode": "-", "owner": "-", '
            b'"path": "/etc/caf\xc3\xa9.conf", "type": "file", '
            b'"kind": "file", "caps": "-", "package": "-"}\n'
        )
    else:
        db_path.write_bytes(
            b"/etc/caf\xc3\xa9.conf:\n  mode: '-'\n  owner: '-'\n  group: '-'\n"
            b"  type: file\n  kind: file\n  caps: '-'\n  package: '-'\n  meta: {}\n"
        )

    script = (
        "import locale, sys\n"
        "enc = locale.getpreferredencoding(False)\n"
        "if 'utf' in enc.lower():\n"
        "    print('LOCALE_STILL_UTF8:' + enc)\n"
        "    sys.exit(0)\n"
        "import pathlib\n"
        "from pkgforge.db import open_db\n"
        f"path = pathlib.Path({str(db_path)!r})\n"
        f"p = open_db(path, {fmt!r})\n"
        "db = p.load()\n"
        "assert '/etc/caf\\u00e9.conf' in db, sorted(db)\n"
        "p.compact()\n"
        f"p2 = open_db(path, {fmt!r})\n"
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
# yaml scalars load as strings
# --------------------------------------------------------------------------


def test_yaml_scalars_load_as_strings(tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text(
        "/usr/bin/tool:\n"
        "  mode: 0755\n"
        "  owner: 0\n"
        "  group: root\n"
        "  type: file\n"
        "  kind: file\n"
        "  caps: '-'\n"
        "  package: '-'\n"
        "  meta: {enabled: yes}\n"
        "/gone: null\n",
        encoding="utf-8",
    )
    db = open_db(path, "yaml").load()
    entry = db["/usr/bin/tool"]
    assert entry["mode"] == "0755"
    assert entry["owner"] == "0"
    assert entry["meta"] == {"enabled": "yes"}
    assert db["/gone"] is None


# --------------------------------------------------------------------------
# pin JSON Lines and SQLite load results and errors, byte for byte
# --------------------------------------------------------------------------


def test_jsonl_load_errors_pinned(tmp_path):
    from pkgforge.exceptions import DbError

    path = tmp_path / "f.jsonl"

    def _err(text: str) -> str:
        path.write_text(text, encoding="utf-8")
        with pytest.raises(DbError) as excinfo:
            open_db(path, "jsonl").load()
        return str(excinfo.value)

    hint = " (read as jsonl; pass --db-format to choose another format)"
    # bad JSON with a column
    assert _err('{"path": "/a", not valid json\n') == (
        f"{path}:1: invalid JSON Lines record: "
        f"Expecting property name enclosed in double quotes (column 16){hint}"
    )
    # a BOM on a line after the first is not dropped
    assert _err("\n\ufeff" + '{"path": "/a"}\n') == (
        f"{path}:2: invalid JSON Lines record: "
        f"Unexpected UTF-8 BOM (decode using utf-8-sig) (column 1){hint}"
    )
    # two objects on one line
    assert _err("{}{}\n") == (
        f"{path}:1: invalid JSON Lines record: Extra data (column 3){hint}"
    )
    # trailing garbage after a valid object
    assert _err('{"a":1}xyz\n') == (
        f"{path}:1: invalid JSON Lines record: Extra data (column 8){hint}"
    )
    # a JSON array is not an object
    assert _err("[1]\n") == (
        f"{path}:1: invalid JSON Lines record: not an object{hint}"
    )
    # a JSON null is not an object
    assert _err("null\n") == (
        f"{path}:1: invalid JSON Lines record: not an object{hint}"
    )
    # a JSON string is not an object
    assert _err('"text"\n') == (
        f"{path}:1: invalid JSON Lines record: not an object{hint}"
    )
    # a valid object missing "path"
    assert _err('{"mode": "644"}\n') == (
        f'{path}:1: invalid JSON Lines record: missing "path"{hint}'
    )
    # non-UTF-8 bytes fail before any JSON parsing is attempted
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(DbError) as excinfo:
        open_db(path, "jsonl").load()
    assert str(path) in str(excinfo.value)


def test_jsonl_load_tolerant_lines_pinned(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_bytes(
        b"\n"
        b'  {"path": "/a", "mode": "644", "owner": "-", "group": "-", '
        b'"type": "file", "kind": "file", "caps": "-", "package": "-", "meta": {}}  \r\n'
        b"\t \r\n"
        b'{"path": "/a", "mode": "755", "owner": "-", "group": "-", '
        b'"type": "file", "kind": "file", "caps": "-", "package": "-", "meta": {}}\r\n'
        b'{"path": "/b", "_removed": true}\n'
        b'{"path": "/c", "mode": "600", "owner": "-", "group": "-", '
        b'"type": "file", "kind": "file", "caps": "-", "package": "-", "meta": {}}'  # no final newline
    )
    db = open_db(path, "jsonl").load()
    assert db["/a"]["mode"] == "755"  # duplicate path: last record wins
    assert db["/b"] is None  # "_removed" tombstone
    assert db["/c"]["mode"] == "600"  # last line has no trailing newline


def test_sqlite_load_meta_pinned(tmp_path, make_entry):
    db_path = tmp_path / "f.db"
    p = open_db(db_path, "sqlite")
    p.init()
    p.add("/empty", make_entry(meta={}))
    p.add("/full", make_entry(meta={"k": "v"}))

    db = open_db(db_path, "sqlite").load()
    assert db["/empty"]["meta"] == {}
    assert db["/full"]["meta"] == {"k": "v"}

    # each row's meta is its own dict object: mutating one never leaks
    db["/empty"]["meta"]["mutated"] = True
    assert db["/full"]["meta"] == {"k": "v"}

    # a fresh load hands back a fresh object too, not one cached anywhere
    db2 = open_db(db_path, "sqlite").load()
    assert db2["/empty"]["meta"] == {}
    assert db2["/empty"]["meta"] is not db["/empty"]["meta"]


def _counting_json_loads(monkeypatch):
    """Wrap the shared :mod:`json` module's ``loads`` with a call counter,
    for every module that does ``import json`` -- there is only ever one
    ``json`` module object, so patching it here reaches ``pkgforge.db._jsonl``
    and ``pkgforge.db._sqlite`` too without patching either by name."""
    calls = []
    real_loads = json.loads

    def _wrapped(s, *a, **kw):
        calls.append(s)
        return real_loads(s, *a, **kw)

    monkeypatch.setattr(json, "loads", _wrapped)
    return calls


def test_jsonl_load_skips_json_loads_for_valid_lines(tmp_path, monkeypatch, make_entry):
    path = tmp_path / "f.jsonl"
    lines = [
        json.dumps({"path": f"/f{i}", **make_entry()}, sort_keys=True)
        for i in range(50)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    calls = _counting_json_loads(monkeypatch)
    db = open_db(path, "jsonl").load()
    assert len(db) == 50
    assert calls == []

    # the fallback path is still exercised, and still raises the pinned
    # message, for a line the fast path can't parse on its own.
    from pkgforge.exceptions import DbError

    path.write_text('{"path": "/a", not valid json\n', encoding="utf-8")
    with pytest.raises(DbError) as excinfo:
        open_db(path, "jsonl").load()
    assert str(excinfo.value) == (
        f"{path}:1: invalid JSON Lines record: "
        "Expecting property name enclosed in double quotes (column 16)"
        " (read as jsonl; pass --db-format to choose another format)"
    )
    assert calls  # the fallback did call json.loads


def test_sqlite_load_skips_json_for_empty_meta(tmp_path, monkeypatch, make_entry):
    db_path = tmp_path / "f.db"
    p = open_db(db_path, "sqlite")
    p.init()
    for i in range(50):
        p.add(f"/f{i}", make_entry(meta={}))

    calls = _counting_json_loads(monkeypatch)
    db = open_db(db_path, "sqlite").load()
    assert len(db) == 50
    assert all(entry["meta"] == {} for entry in db.values())
    assert calls == []


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
            '"type": "file", "kind": "file", "caps": "-", "package": "-", "meta": {}}\n',
            encoding="utf-8",
        )
    elif kind == "non_octal_int":
        path = tmp_path / "f.jsonl"
        path.write_text(base.format(mode="8"), encoding="utf-8")
    elif kind == "int_type":
        path = tmp_path / "f.jsonl"
        path.write_text(
            '{"path": "/a", "mode": "-", "owner": "-", "group": "-", '
            '"type": 5, "kind": "file", "caps": "-", "package": "-", "meta": {}}\n',
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
    from pkgforge.exceptions import DbError

    path = _write_bad_field(tmp_path, kind)
    fmt = "yaml" if kind == "record_list" else "jsonl"
    with pytest.raises(DbError) as excinfo:
        open_db(path, fmt).load()
    assert "/a" in str(excinfo.value)


# --------------------------------------------------------------------------
# flow-style YAML append refusal
# --------------------------------------------------------------------------


def test_flow_yaml_loads(tmp_path):
    path = tmp_path / "flow.yaml"
    path.write_text(
        "{/usr/bin/x: {mode: '0755', owner: root, group: root, type: file, kind: file, caps: '-', package: '-', "
        "meta: {}}}\n",
        encoding="utf-8",
    )
    db = open_db(path).load()
    assert db["/usr/bin/x"]["mode"] == "0755"


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
    from pkgforge.exceptions import DbError

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
        "{/usr/bin/x: {mode: '0755', owner: root, group: root, type: file, kind: file, caps: '-', package: '-', "
        "meta: {}}}\n",
        encoding="utf-8",
    )
    p = open_db(path, "yaml")
    p.compact()

    rewritten = path.read_text(encoding="utf-8").lstrip()
    assert not rewritten.startswith("{")

    # An append now succeeds against the rewritten block-style file.
    p.add("/new", make_entry())
    db = open_db(path, "yaml").load()
    assert db["/usr/bin/x"]["mode"] == "0755"
    assert db["/new"] is not None


# --------------------------------------------------------------------------
# locked, atomic compaction of the append-log DBs
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["jsonl", "yaml"])
def test_compact_failure_leaves_db_intact(tmp_path, monkeypatch, make_entry, fmt):
    import pkgforge.db._appendlog as db_appendlog

    p = open_db(tmp_path / f"f.{fmt}", fmt)
    p.init()
    p.add("/keep", make_entry())
    p.add("/gone", make_entry())
    p.remove("/gone")
    before = p.path.read_bytes()

    def _raise(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(db_appendlog.os, "replace", _raise)

    with pytest.raises(OSError):
        p.compact()

    assert p.path.read_bytes() == before
    assert not any(name.endswith(".tmp") for name in os.listdir(tmp_path))
    db = open_db(p.path, fmt).load()
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
    db = open_db(link, fmt).load()
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
        "'type': 'file', 'kind': 'file', 'caps': '-', 'package': '-', 'meta': {}})\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
    )
    provider_cls = dbmod.JSONLinesDb if fmt == "jsonl" else dbmod.YAMLDb
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

    db = open_db(db_path, fmt).load()
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
        "from pkgforge.db import JSONLinesDb\n"
        f"p = JSONLinesDb(pathlib.Path({str(db_path)!r}))\n"
        "p.init()\n"
        "p.add('/a', {'mode': '644', 'owner': '-', 'group': '-', 'type': 'file', 'kind': 'file', 'caps': '-', 'package': '-', 'meta': {}})\n"
        "p.compact()\n"
        "assert JSONLinesDb(p.path).load()['/a']['mode'] == '644'\n"
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

    import pkgforge.db._sqlite as db_sqlite

    monkeypatch.setattr(db_sqlite, "_BATCH_ROWS", 2)
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

    assert len(open_db(db_path, "sqlite").load()) == 2


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
    loaded = open_db(db_path, "sqlite").load()
    assert "/a" in loaded
    db_path.unlink()


@pytest.mark.posix
def test_scan_with_provider_without_batch(tmp_path, cli, restore_registries):
    # A third-party provider that never overrides batch() (inherits the
    # DbProvider default, a no-op) still works under scan.
    type("TsvDb", (_TsvDb,), {"NAME": "tsv", "SUFFIXES": (".tsv",)})

    root = tmp_path / "root"
    tree = root / "usr" / "share" / "tool"
    tree.mkdir(parents=True)
    (tree / "a").write_text("x")
    (tree / "b").write_text("y")

    # --db-format is explicit: the .tsv suffix alone would also resolve it.
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

    loaded = open_db(db, "sqlite").load()
    assert len(loaded) == 200


# --------------------------------------------------------------------------
# reading a SQLite DB never writes; non-UTF-8 text fails cleanly
# --------------------------------------------------------------------------


def test_sqlite_load_leaves_empty_file(tmp_path):
    path = tmp_path / "empty.db"
    path.write_bytes(b"")

    assert open_db(path, "sqlite").load() == {}
    assert path.stat().st_size == 0


@pytest.mark.parametrize("op", ["load", "compact"])
def test_sqlite_foreign_db_rejected(tmp_path, op):
    import sqlite3

    from pkgforge.exceptions import DbError

    path = tmp_path / "foreign.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
    finally:
        conn.close()

    p = open_db(path, "sqlite")
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
    from pkgforge.exceptions import DbError

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
    assert open_db(p.path, "sqlite").load() == {}
