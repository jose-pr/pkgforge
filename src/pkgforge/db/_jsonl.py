"""``jsonl``: append-only JSON Lines, one object per line, last record wins."""

from __future__ import annotations

import json
import typing
from pathlib import Path

from ..entry import FileEntry
from ..exceptions import DbError
from . import Db, _check_record, _fields, _format_hint, _record_ok
from ._appendlog import AppendLogDb, _read_text

#: Reused across every line of every load: a bare :func:`json.loads` call
#: builds a fresh ``JSONDecoder`` (and, for an object, its own regexes) each
#: time, which dominated the cost of loading a large, entirely-valid file.
#: :meth:`~json.JSONDecoder.raw_decode` is the same C scanner without that
#: per-call setup or the trailing-whitespace re-match ``loads`` also does.
_DECODER = json.JSONDecoder()


def jsonl_line(path: str, entry: typing.Optional["FileEntry"]) -> str:
    """One JSON Lines record for ``path``/``entry`` (``None`` writes a removal
    marker), terminated with a single newline: the exact text
    :class:`JSONLinesDb` appends."""
    return json.dumps({"path": path, **_fields(entry)}, sort_keys=True) + "\n"


def _parse_jsonl(text: str, source: typing.Union["Path", str], hint: str = "") -> Db:
    """Parse JSON Lines ``text`` into a :data:`~pkgforge.db.Db` mapping.

    Shared by :meth:`JSONLinesDb.load` (``source`` is the DB file's own path)
    and ``dbdump --db -`` (``source`` is the literal string ``"<stdin>"``):
    every error is tagged ``{source}:{lineno}:``. ``hint`` is appended to an
    error that means the text is not JSON Lines at all.

    Lines split on ``\\n`` only (``str.splitlines`` would also split inside
    a string on U+2028, U+2029 and NEL). Each stripped line is first tried
    with the shared :data:`_DECODER`'s :meth:`~json.JSONDecoder.raw_decode`,
    which on a stripped line is ``json.loads`` without its per-call decoder
    and trailing-whitespace re-match whenever the value ends at the line's
    end; anything else falls back to ``json.loads`` for its error message.
    A line ``{"path": P, "_removed": true}`` is a tombstone; any other line
    is checked by :func:`~pkgforge.db._check_record`.
    """
    db: Db = {}
    for lineno, line in enumerate(text.split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        ok = True
        try:
            rec, end = _DECODER.raw_decode(line)
        except json.JSONDecodeError:
            ok = False
        except RecursionError as exc:
            raise DbError(
                f"{source}:{lineno}: invalid JSON Lines record: nested too deeply"
            ) from exc
        if not ok or end != len(line):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DbError(
                    f"{source}:{lineno}: invalid JSON Lines record: "
                    f"{exc.msg} (column {exc.colno}){hint}"
                ) from exc
            except RecursionError as exc:
                raise DbError(
                    f"{source}:{lineno}: invalid JSON Lines record: nested too deeply"
                ) from exc
        if type(rec) is not dict:
            raise DbError(
                f"{source}:{lineno}: invalid JSON Lines record: not an object{hint}"
            )
        try:
            path = rec.pop("path")
        except KeyError:
            raise DbError(
                f'{source}:{lineno}: invalid JSON Lines record: missing "path"{hint}'
            ) from None
        if "_removed" in rec:
            if rec["_removed"] is not True or len(rec) != 1:
                raise DbError(
                    f"{source}:{lineno}: {path!r}: a removal is exactly "
                    '{"path": ..., "_removed": true}'
                )
            rec = None
        elif _record_ok(path, rec):
            db[path] = rec
            continue
        db[path] = _check_record(path, rec, f"{source}:{lineno}")
    return db


def parse_jsonl(text: str, source: str = "text") -> Db:
    """Parse JSON Lines ``text`` (what :func:`jsonl_line` writes) into a
    :data:`~pkgforge.db.Db`; the last record for a path wins.

    ``source`` names the text in errors, which read ``{source}:{line}: ...``.
    Raises :class:`~pkgforge.exceptions.DbError` for a malformed record.
    """
    return _parse_jsonl(text, source, "")


class JSONLinesDb(AppendLogDb):
    """Append-only JSON Lines: one JSON object per line, last per path wins."""

    NAME = "jsonl"
    SUFFIXES = (".jsonl", ".ndjson")

    def load(self) -> Db:
        if not self.path.exists():
            return {}
        hint = _format_hint(self.NAME)
        return _parse_jsonl(_read_text(self.path, hint), self.path, hint)

    def _record(self, path: str, entry: typing.Optional["FileEntry"]) -> str:
        return jsonl_line(path, entry)

    def _render_all(self, live: typing.Dict[str, "FileEntry"]) -> str:
        return "".join(jsonl_line(p, e) for p, e in live.items())
