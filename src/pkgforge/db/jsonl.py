"""``jsonl``: append-only JSON Lines, one object per line, last record wins."""

from __future__ import annotations

import json
import re
import typing

from . import Db, DbError, _fields
from ._appendlog import AppendLogDb, _normalize

if typing.TYPE_CHECKING:
    from ..entry import FileEntry


def _jsonl_line(path: str, entry: typing.Optional["FileEntry"]) -> str:
    """One JSON Lines record for ``path``/``entry`` (or a removal marker),
    terminated with a single trailing newline."""
    return json.dumps({"path": path, **_fields(entry)}, sort_keys=True) + "\n"


class JsonlDb(AppendLogDb):
    """Append-only JSON Lines: one JSON object per line, last per path wins."""

    NAME = "jsonl"
    SUFFIXES = (".jsonl", ".ndjson")

    @staticmethod
    def sniff(head: bytes) -> bool:
        # A quoted first key: claims pkgforge's own output ({"group": ...)
        # and a hand-written record ({ "path": ...}), never a flow-style
        # YAML mapping (yaml.safe_dump's plain-key output, e.g.
        # {/usr/bin/x: ...}, or {}).
        return re.match(rb'\s*\{\s*"', head) is not None

    def load(self) -> Db:
        if not self.path.exists():
            return {}
        db: Db = {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise DbError(f"{self.path}: {exc}") from exc
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DbError(
                    f"{self.path}:{lineno}: invalid JSON Lines record: "
                    f"{exc.msg} (column {exc.colno})"
                ) from exc
            if not isinstance(rec, dict):
                raise DbError(
                    f"{self.path}:{lineno}: invalid JSON Lines record: not an object"
                )
            try:
                path = rec.pop("path")
            except KeyError:
                raise DbError(
                    f'{self.path}:{lineno}: invalid JSON Lines record: missing "path"'
                ) from None
            db[path] = (
                None if rec.pop("_removed", False) else _normalize(self.path, path, rec)
            )
        return db

    def _record(self, path: str, entry: typing.Optional["FileEntry"]) -> str:
        return _jsonl_line(path, entry)

    def _render_all(self, live: typing.Dict[str, "FileEntry"]) -> str:
        return "".join(_jsonl_line(p, e) for p, e in live.items())
