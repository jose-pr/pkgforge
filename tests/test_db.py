"""Tests for the JSON Lines file DB format (and legacy-YAML read fallback)."""

from __future__ import annotations

import json

import yaml

from buildutils.common import BuildUtils, FileType


def _cmd(tmp_path, name="files.jsonl"):
    inst = BuildUtils.__new__(BuildUtils)
    inst.db = tmp_path / name
    inst.buildroot = tmp_path
    return inst


def _entry(mode="644", type="file", **over):
    e = {"mode": mode, "owner": "root", "group": "root", "type": type, "meta": {}}
    e.update(over)
    return e


# --------------------------------------------------------------------------
# write shape
# --------------------------------------------------------------------------


def test_write_is_one_json_line_per_entry(tmp_path):
    cmd = _cmd(tmp_path)
    cmd.add_entry("/usr/bin/a", _entry())
    cmd.add_entry("/usr/bin/b", _entry(mode="755"))
    lines = cmd.db.read_text().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["path"] == "/usr/bin/a"
    assert rec["mode"] == "644"


def test_filetype_enum_coerced_to_str_on_write(tmp_path):
    cmd = _cmd(tmp_path)
    # A fresh entry may carry the FileType enum (not yet round-tripped).
    cmd.add_entry("/d", _entry(type=FileType.Directory))
    rec = json.loads(cmd.db.read_text().splitlines()[0])
    assert rec["type"] == "directory"


# --------------------------------------------------------------------------
# read: last-wins + removal
# --------------------------------------------------------------------------


def test_last_write_wins(tmp_path):
    cmd = _cmd(tmp_path)
    cmd.add_entry("/x", _entry(mode="644"))
    cmd.add_entry("/x", _entry(mode="600"))
    db = cmd.loaddb()
    assert db["/x"]["mode"] == "600"


def test_removal_marks_none(tmp_path):
    cmd = _cmd(tmp_path)
    cmd.add_entry("/x", _entry())
    cmd.remove_entry("/x")
    assert cmd.loaddb()["/x"] is None


def test_load_missing_is_empty(tmp_path):
    assert _cmd(tmp_path).loaddb() == {}


# --------------------------------------------------------------------------
# legacy YAML read fallback
# --------------------------------------------------------------------------


def test_reads_legacy_yaml_db(tmp_path):
    # A DB written in the old single-document YAML format still loads.
    cmd = _cmd(tmp_path, name="legacy.yaml")
    cmd.db.write_text(
        yaml.safe_dump(
            {
                "/usr/bin/x": _entry(mode="755"),
                "/gone": None,
            }
        )
    )
    db = cmd.loaddb()
    assert db["/usr/bin/x"]["mode"] == "755"
    assert db["/gone"] is None


def test_append_upgrades_legacy_yaml_to_jsonl(tmp_path):
    cmd = _cmd(tmp_path, name="legacy.yaml")
    cmd.db.write_text(yaml.safe_dump({"/old": _entry(mode="600")}))
    # Appending must rewrite the file as JSON Lines, preserving the old entry.
    cmd.add_entry("/new", _entry(mode="644"))
    text = cmd.db.read_text()
    # Now valid JSON Lines: every non-blank line parses as a JSON object.
    for line in text.splitlines():
        if line.strip():
            json.loads(line)
    db = cmd.loaddb()
    assert db["/old"]["mode"] == "600"
    assert db["/new"]["mode"] == "644"


# --------------------------------------------------------------------------
# compaction
# --------------------------------------------------------------------------


def test_compact_drops_superseded_and_removed(tmp_path):
    cmd = _cmd(tmp_path)
    cmd.add_entry("/x", _entry(mode="644"))
    cmd.add_entry("/x", _entry(mode="600"))  # supersedes
    cmd.add_entry("/y", _entry())
    cmd.remove_entry("/y")  # removed
    assert len(cmd.db.read_text().splitlines()) == 4  # log has all 4 records

    cmd.compactdb()
    lines = [l for l in cmd.db.read_text().splitlines() if l.strip()]
    assert len(lines) == 1  # only live /x remains
    db = cmd.loaddb()
    assert db["/x"]["mode"] == "600"
    assert "/y" not in db
