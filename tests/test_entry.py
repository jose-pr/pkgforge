"""Tests for the typed ``FileEntry`` module functions (``common.py``).

``FileEntry`` is a ``TypedDict``: its values are plain dicts, so
``FileEntry.from_args``/``from_path``/``resolve_for``/``apply`` only ever
worked when called *unbound* through the class. These tests pin that the
typed module functions (``entry_from_args``, ``entry_from_path``,
``resolve_entry``, ``apply_entry``) exist, are exported, and agree with the
compat aliases.
"""

from __future__ import annotations

import pkgforge
from pkgforge.common import (
    DEFAULT,
    FileEntry,
    FileEntryArgs,
    FileType,
    apply_entry,
    entry_from_args,
    entry_from_path,
    resolve_entry,
)


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
