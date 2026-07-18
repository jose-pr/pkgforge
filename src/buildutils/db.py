"""Provider-agnostic file DB backends.

A file DB maps a build-relative *path* to a :class:`~buildutils.common.FileEntry`
(or ``None`` for a removed path). buildutils supports three interchangeable
storage backends behind one :class:`DbProvider` interface:

* :class:`JsonlDb` -- append-only JSON Lines (one JSON object per line);
* :class:`YamlDb`  -- append-only YAML (concatenated single-key documents);
* :class:`SqliteDb` -- a real SQLite store (upsert in place, no log to compact).

All three return the **same** ``load()`` shape, so the rest of buildutils is
backend-agnostic. Pick a backend with :func:`open_db`: by the ``--db`` file
suffix, an explicit ``--db-format``, or -- when reading an existing file --
by sniffing its actual content (so a legacy/mislabeled file still loads).
"""

from __future__ import annotations

import abc
import contextlib
import json
import os
import sqlite3
import typing
from pathlib import Path

import yaml

if typing.TYPE_CHECKING:
    from .common import FileEntry

#: A loaded DB: build path -> entry, or ``None`` for a removed path.
Db = typing.Dict[str, "typing.Optional[FileEntry]"]

#: Content sniffer: given a file's first bytes, return True if this format owns it.
Sniffer = typing.Callable[[bytes], bool]

#: Registered backends: format name -> provider class. Populated by
#: :func:`register_provider` (the built-in three register themselves at import).
PROVIDERS: "typing.Dict[str, typing.Type[DbProvider]]" = {}

#: File-suffix (lowercased) -> format name. Populated by :func:`register_provider`.
SUFFIX_FORMATS: "typing.Dict[str, str]" = {}

#: Content sniffers, newest-registered first: (format-name, sniffer).
_SNIFFERS: "typing.List[typing.Tuple[str, Sniffer]]" = []

#: Format used when the suffix is unknown / absent.
DEFAULT_FORMAT = "jsonl"

#: SQLite file magic (first 16 bytes of any SQLite 3 database).
_SQLITE_MAGIC = b"SQLite format 3\x00"


def register_provider(
    name: str,
    provider_cls: "typing.Type[DbProvider]",
    *,
    suffixes: "typing.Iterable[str]" = (),
    sniff: "typing.Optional[Sniffer]" = None,
) -> "typing.Type[DbProvider]":
    """Register a file-DB backend so :func:`open_db` can select it.

    This is the extension seam that keeps third-party backends OUT of core
    buildutils: an external package calls ``register_provider`` at import time
    and its format becomes usable via ``--db-format <name>``, a matching file
    suffix, or content sniffing.

    * ``name`` -- the format name (used by ``--db-format`` and error messages).
    * ``provider_cls`` -- a :class:`DbProvider` subclass constructed as
      ``provider_cls(path)``.
    * ``suffixes`` -- file suffixes (e.g. ``(".toml",)``, leading dot,
      case-insensitive) that infer this format from a ``--db`` path.
    * ``sniff`` -- optional ``sniff(head: bytes) -> bool`` that inspects a file's
      first 16 bytes and returns True if this backend owns it. Registered
      sniffers are consulted newest-first, before the built-in heuristics, so a
      later registration can claim a shape an earlier one would.

    Returns ``provider_cls`` so it can be used as a decorator. Re-registering a
    name replaces the previous class for that name.
    """
    PROVIDERS[name] = provider_cls
    for suffix in suffixes:
        SUFFIX_FORMATS[suffix.lower()] = name
    if sniff is not None:
        _SNIFFERS.insert(0, (name, sniff))
    return provider_cls


def _record(path: str, entry: "typing.Optional[FileEntry]") -> dict:
    """A JSON/YAML-safe record dict for one entry (FileType coerced to str)."""
    if entry is None:
        return {"path": path, "_removed": True}
    rec = {"path": path}
    for k, v in entry.items():
        # FileType is a str-enum; store its plain string value.
        rec[k] = str(v.value) if hasattr(v, "value") else v
    return rec


class DbProvider(abc.ABC):
    """Storage backend for a file DB, bound to a filesystem ``path``."""

    format: str = ""

    def __init__(self, path: Path):
        self.path = path

    @abc.abstractmethod
    def load(self) -> "Db":
        """Return the full DB as ``{path: entry-or-None}``."""

    @abc.abstractmethod
    def add(self, path: str, entry: "FileEntry") -> None:
        """Record ``entry`` for ``path``."""

    @abc.abstractmethod
    def remove(self, path: str) -> None:
        """Mark ``path`` removed."""

    @abc.abstractmethod
    def compact(self) -> None:
        """Collapse redundant history (a no-op for backends without any)."""

    @abc.abstractmethod
    def init(self) -> None:
        """Create or reset an empty DB."""


# --------------------------------------------------------------------------
# Append-log text backends (jsonl, yaml)
# --------------------------------------------------------------------------


class JsonlDb(DbProvider):
    """Append-only JSON Lines: one JSON object per line, last per path wins."""

    format = "jsonl"

    def load(self) -> "Db":
        if not self.path.exists():
            return {}
        db: "Db" = {}
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            path = rec.pop("path")
            db[path] = None if rec.pop("_removed", False) else rec
        return db

    def _append(self, record: dict) -> None:
        line = json.dumps(record, sort_keys=True) + "\n"
        with self.path.open("a") as fh:
            fh.write(line)

    def add(self, path: str, entry: "FileEntry") -> None:
        self._append(_record(path, entry))

    def remove(self, path: str) -> None:
        self._append(_record(path, None))

    def compact(self) -> None:
        db = self.load()
        lines = [
            json.dumps(_record(p, e), sort_keys=True) + "\n"
            for p, e in db.items()
            if e is not None
        ]
        self.path.write_text("".join(lines))

    def init(self) -> None:
        self.path.write_text("")


class YamlDb(DbProvider):
    """Append-only YAML: concatenated single-key documents, last key wins.

    Kept for compatibility and as an explicitly-selectable backend. Reading
    relies on the YAML loader letting a later duplicate mapping key win -- a
    property of this backend's format, not a general guarantee.
    """

    format = "yaml"

    def load(self) -> "Db":
        if not self.path.exists():
            return {}
        return yaml.safe_load(self.path.read_text()) or {}

    def _append(self, path: str, entry: "typing.Optional[FileEntry]") -> None:
        rec = _record(path, entry)
        rec.pop("path")
        value = None if entry is None else rec
        with self.path.open("a") as fh:
            fh.write(yaml.safe_dump({path: value}))

    def add(self, path: str, entry: "FileEntry") -> None:
        self._append(path, entry)

    def remove(self, path: str) -> None:
        self._append(path, None)

    def compact(self) -> None:
        db = self.load()
        live = {p: _dict_no_path(_record(p, e)) for p, e in db.items() if e is not None}
        self.path.write_text(yaml.safe_dump(live) if live else "")

    def init(self) -> None:
        self.path.write_text("")


def _dict_no_path(record: dict) -> dict:
    record = dict(record)
    record.pop("path", None)
    return record


# --------------------------------------------------------------------------
# SQLite backend
# --------------------------------------------------------------------------


class SqliteDb(DbProvider):
    """SQLite store: one row per path, upserted in place (no append log)."""

    format = "sqlite"

    _SCHEMA = (
        "CREATE TABLE IF NOT EXISTS entries ("
        "path TEXT PRIMARY KEY, mode TEXT, owner TEXT, "
        '"group" TEXT, type TEXT, meta_json TEXT, removed INTEGER DEFAULT 0)'
    )

    @contextlib.contextmanager
    def _connect(self, *, ensure_schema: bool = True):
        """Yield a connection that is committed on success and always closed.

        ``sqlite3``'s own context manager commits/rolls back but does NOT close
        the connection -- leaving the file handle open, which blocks deletion on
        Windows. This wrapper guarantees ``close()``.
        """
        conn = sqlite3.connect(os.fspath(self.path))
        try:
            if ensure_schema:
                conn.execute(self._SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def load(self) -> "Db":
        if not self.path.exists():
            return {}
        db: "Db" = {}
        with self._connect() as conn:
            for row in conn.execute(
                'SELECT path, mode, owner, "group", type, meta_json, removed FROM entries'
            ):
                path, mode, owner, group, type_, meta_json, removed = row
                if removed:
                    db[path] = None
                else:
                    db[path] = {
                        "mode": mode,
                        "owner": owner,
                        "group": group,
                        "type": type_,
                        "meta": json.loads(meta_json) if meta_json else {},
                    }
        return db

    def add(self, path: str, entry: "FileEntry") -> None:
        rec = _record(path, entry)
        with self._connect() as conn:
            conn.execute(
                'INSERT INTO entries (path, mode, owner, "group", type, meta_json, removed) '
                "VALUES (?, ?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(path) DO UPDATE SET "
                'mode=excluded.mode, owner=excluded.owner, "group"=excluded."group", '
                "type=excluded.type, meta_json=excluded.meta_json, removed=0",
                (
                    path,
                    rec.get("mode"),
                    rec.get("owner"),
                    rec.get("group"),
                    rec.get("type"),
                    json.dumps(rec.get("meta") or {}, sort_keys=True),
                ),
            )

    def remove(self, path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO entries (path, removed) VALUES (?, 1) "
                "ON CONFLICT(path) DO UPDATE SET removed=1",
                (path,),
            )

    def compact(self) -> None:
        if not self.path.exists():
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM entries WHERE removed=1")
            # VACUUM must run outside a transaction; commit what we have first.
            conn.commit()
            conn.execute("VACUUM")

    def init(self) -> None:
        with self._connect(ensure_schema=False) as conn:
            conn.execute("DROP TABLE IF EXISTS entries")
            conn.execute(self._SCHEMA)


# --------------------------------------------------------------------------
# Built-in registration
# --------------------------------------------------------------------------

register_provider(
    "jsonl",
    JsonlDb,
    suffixes=(".jsonl", ".ndjson"),
    sniff=lambda head: head.lstrip()[:1] == b"{",
)
register_provider("yaml", YamlDb, suffixes=(".yaml", ".yml"))
register_provider(
    "sqlite",
    SqliteDb,
    suffixes=(".db", ".sqlite", ".sqlite3"),
    sniff=lambda head: head.startswith(_SQLITE_MAGIC),
)


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def format_for_suffix(path: Path) -> str:
    """Infer a format from ``path``'s suffix, else :data:`DEFAULT_FORMAT`."""
    return SUFFIX_FORMATS.get(path.suffix.lower(), DEFAULT_FORMAT)


def sniff_format(path: Path) -> "typing.Optional[str]":
    """Detect an existing file's format from its content, or ``None`` if unknown.

    Registered sniffers are tried newest-first; if none claims the file, the
    built-in fallback treats a leading ``{`` as JSON Lines and any other
    non-empty content as YAML.
    """
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return None
    for name, sniffer in _SNIFFERS:
        try:
            if sniffer(head):
                return name
        except Exception:  # pragma: no cover - a broken sniffer must not abort
            continue
    stripped = head.lstrip()
    if not stripped:
        return None
    # Fallback: anything non-empty that no sniffer claimed is treated as YAML
    # (the most permissive text format).
    return "yaml"


def open_db(
    path: Path, fmt: "typing.Optional[str]" = None, *, for_read: bool = False
) -> DbProvider:
    """Resolve and construct the :class:`DbProvider` for ``path``.

    Precedence: an explicit ``fmt`` wins; otherwise, when ``for_read`` and the
    file already exists, its content is sniffed (so a mislabeled or legacy file
    still loads); otherwise the suffix decides (defaulting to JSON Lines).
    """
    if fmt is None:
        detected = sniff_format(path) if (for_read and path.exists()) else None
        fmt = detected or format_for_suffix(path)
    try:
        provider_cls = PROVIDERS[fmt]
    except KeyError:
        raise ValueError(
            f"unknown db format {fmt!r}; choose from {', '.join(sorted(PROVIDERS))}"
        )
    return provider_cls(path)
