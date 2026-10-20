"""The strict record schema every file-DB loader enforces, the format hint on
content that is not in the chosen format, and the BOM / line-splitting rules.
"""

from __future__ import annotations

import sqlite3

import pytest

from pkgforge.db import DbError, SqliteDb, _check_record, open_db
from pkgforge.db.jsonl import _jsonl_line


def _record(**over):
    rec = {"mode": "644", "owner": "-", "group": "-", "type": "file", "meta": {}}
    rec.update(over)
    return rec


@pytest.mark.parametrize("fmt", ["jsonl", "yaml"])
def test_missing_fields_are_rejected(tmp_path, fmt):
    path = tmp_path / f"f.{fmt}"
    if fmt == "jsonl":
        path.write_text('{"path": "/a"}\n', encoding="utf-8")
    else:
        path.write_text("/a: {}\n", encoding="utf-8")

    with pytest.raises(DbError, match="'/a': missing field") as excinfo:
        open_db(path, fmt).load()
    assert str(path) in str(excinfo.value)


@pytest.mark.parametrize(
    "path,rec,message",
    [
        (5, _record(), "path must be a string"),
        (None, _record(), "path must be a string"),
        ("rel/x", _record(), "path must be a string starting with '/'"),
        ("/a", "text", "record is not a mapping"),
        ("/a", _record(mode=644), "mode must be a string"),
        ("/a", _record(owner=0), "owner must be a string"),
        ("/a", _record(group=None), "group must be a string"),
        ("/a", _record(mode=True), "mode must be a string"),
        ("/a", _record(type=None), "type must be one of"),
        ("/a", _record(type="bogus"), "type must be one of"),
        ("/a", _record(type=["file"]), "type must be one of"),
        ("/a", _record(type="--"), "type must be one of"),
        ("/a", _record(meta="x"), "meta must map strings to strings"),
        ("/a", _record(meta={"k": 1}), "meta must map strings to strings"),
        ("/a", _record(meta={1: "v"}), "meta must map strings to strings"),
        ("/a", {**_record(), "extra": "x"}, "unknown field extra"),
    ],
)
def test_check_record_rejects(path, rec, message):
    with pytest.raises(DbError, match=message) as excinfo:
        _check_record(path, rec, "where")
    assert str(excinfo.value).startswith("where: ")


def test_check_record_accepts_and_returns_the_record():
    rec = _record(meta={"k": "v"})
    assert _check_record("/a", rec, "where") is rec
    assert _check_record("/a", None, "where") is None


_JSONL = '{"path": "/a", "mode": "644", "owner": "-", "group": "-", %s}'


@pytest.mark.parametrize(
    "line,message",
    [
        (
            '{"path": 5, "mode": "644", "owner": "-", "group": "-", '
            '"type": "file", "meta": {}}',
            "path must be a string",
        ),
        (
            '{"path": null, "mode": "644", "owner": "-", "group": "-", '
            '"type": "file", "meta": {}}',
            "path must be a string",
        ),
        (_JSONL % '"type": "file", "meta": "x"', "meta must map"),
        (_JSONL % '"type": "bogus", "meta": {}', "type must be one of"),
        (_JSONL % '"type": null, "meta": {}', "type must be one of"),
        (
            '{"path": "/a", "mode": 755, "owner": "-", "group": "-", '
            '"type": "file", "meta": {}}',
            "mode must be a string",
        ),
        ('{"path": "/a", "_removed": false}', "a removal is exactly"),
        ('{"path": "/a", "_removed": true, "mode": "644"}', "a removal is exactly"),
    ],
)
def test_jsonl_load_rejects_schema_violations(tmp_path, line, message):
    path = tmp_path / "f.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(DbError, match=message) as excinfo:
        open_db(path, "jsonl").load()
    assert str(excinfo.value).startswith(f"{path}:1: ")


def test_yaml_load_rejects_schema_violations(tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text(
        "/a: {mode: '644', owner: '-', group: '-', type: bogus, meta: {}}\n"
    )
    with pytest.raises(DbError, match="'/a': type must be one of"):
        open_db(path, "yaml").load()
    path.write_text("rel: null\n")
    with pytest.raises(DbError, match="path must be a string starting with"):
        open_db(path, "yaml").load()


def _sqlite_rows(path, *rows):
    conn = sqlite3.connect(str(path))
    conn.execute(SqliteDb._SCHEMA)
    for row in rows:
        conn.execute("INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, ?)", row)
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "row,message",
    [
        (("/a", None, "-", "-", "file", "{}", 0), "mode must be a string"),
        (("/a", "644", "-", "-", None, "{}", 0), "type must be one of"),
        (("/a", "644", "-", "-", "file", None, 0), "meta must map strings"),
        (("/a", "644", "-", "-", "file", '{"k": 1}', 0), "meta must map strings"),
        (("/a", "644", "-", "-", "file", "{bad", 0), "invalid meta_json"),
        (("rel", None, None, None, None, None, 1), "path must be a string"),
    ],
)
def test_sqlite_load_rejects_schema_violations(tmp_path, row, message):
    path = tmp_path / "f.db"
    _sqlite_rows(path, row)
    with pytest.raises(DbError, match=message) as excinfo:
        open_db(path, "sqlite").load()
    assert str(path) in str(excinfo.value)


def test_sqlite_removed_row_loads_as_tombstone(tmp_path):
    path = tmp_path / "f.db"
    _sqlite_rows(path, ("/gone", None, None, None, None, None, 1))
    assert open_db(path, "sqlite").load() == {"/gone": None}


def test_no_coercion_of_a_valid_record(tmp_path, make_entry):
    for fmt in ("jsonl", "yaml", "sqlite"):
        path = tmp_path / f"f.{fmt}"
        entry = make_entry(mode="0755", owner="0", meta={"k": "v"})
        open_db(path, fmt).add("/a", entry)
        assert open_db(path, fmt).load()["/a"] == entry


def test_jsonl_ignores_a_leading_bom_and_splits_on_newline_only(tmp_path, make_entry):
    path = tmp_path / "f.jsonl"
    odd = "/a b\u0085c d"
    text = _jsonl_line(odd, make_entry()) + _jsonl_line("/b", make_entry())
    path.write_bytes(b"\xef\xbb\xbf" + text.encode())

    assert sorted(open_db(path, "jsonl").load()) == sorted([odd, "/b"])
    # An append keeps the BOM file loadable.
    open_db(path, "jsonl").add("/c", make_entry())
    assert "/c" in open_db(path, "jsonl").load()


def test_a_literal_u2028_in_a_json_string_is_one_record(tmp_path, make_entry):
    # json.dumps escapes U+2028 by default; a hand-written file may carry it raw.
    path = tmp_path / "f.jsonl"
    path.write_text(
        '{"path": "/a b", "mode": "644", "owner": "-", "group": "-", '
        '"type": "file", "meta": {}}\n',
        encoding="utf-8",
    )
    assert list(open_db(path, "jsonl").load()) == ["/a b"]


def test_yaml_ignores_a_leading_bom(tmp_path, make_entry):
    path = tmp_path / "f.yaml"
    open_db(path, "yaml").add("/a", make_entry())
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert "/a" in open_db(path, "yaml").load()


def test_content_never_picks_the_format(tmp_path, make_entry):
    path = tmp_path / "noext"
    open_db(path, "yaml").add("/a", make_entry())
    assert open_db(path).NAME == "jsonl"
    with pytest.raises(DbError, match="--db-format"):
        open_db(path).load()
    assert open_db(path, "yaml").load()["/a"]["mode"] == "644"


def test_sqlite_content_read_as_jsonl_or_text_read_as_sqlite(tmp_path):
    store = tmp_path / "store.jsonl"
    open_db(store, "sqlite").init()
    with pytest.raises(DbError, match="--db-format"):
        open_db(store).load()
    text = tmp_path / "text.db"
    text.write_text("this is not a database\n" * 20)
    with pytest.raises(DbError, match="--db-format"):
        open_db(text).load()


def test_jsonl_content_read_as_yaml(tmp_path, make_entry):
    path = tmp_path / "f.yaml"
    open_db(tmp_path / "f.jsonl", "jsonl").add("/a", make_entry())
    open_db(tmp_path / "f.jsonl", "jsonl").add("/b", make_entry())
    path.write_bytes((tmp_path / "f.jsonl").read_bytes())
    with pytest.raises(DbError, match="--db-format"):
        open_db(path).load()
