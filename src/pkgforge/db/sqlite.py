"""``sqlite``: a real SQLite store, upserted in place (no append log)."""

from __future__ import annotations

import contextlib
import json
import os
import typing

from ..errors import PkgForgeError
from . import Db, DbError, DbProvider, _fields

if typing.TYPE_CHECKING:
    from ..entry import FileEntry

#: SQLite file magic (first 16 bytes of any SQLite 3 database).
_SQLITE_MAGIC = b"SQLite format 3\x00"

#: Rows per commit inside :meth:`SqliteDb.batch` -- bounds how much work a
#: killed batch loses, without paying a durable (fsync-backed) commit per row.
_BATCH_ROWS = 1000


def _ensure_utf8(path: str, rec: dict) -> None:
    """Raise :class:`~pkgforge.db.DbError` if ``path`` or any text field in
    ``rec`` is not valid UTF-8.

    ``sqlite3`` encodes ``str`` parameters strictly as UTF-8 and raises a
    raw ``UnicodeEncodeError`` for a lone surrogate (an undecodable byte in
    a path name, or a ``pwd``/``grp`` entry containing one) -- checked here,
    before any SQL runs, so a batch that hits one partway through a scan
    doesn't leave rows from before it committed and the rest silently gone.
    ``jsonl``/``yaml`` have no such restriction (JSON/YAML both escape a
    surrogate and round-trip it), so this is sqlite-only.
    """
    candidates = [("path", path)]
    for field in ("mode", "owner", "group", "type"):
        value = rec.get(field)
        if isinstance(value, str):
            candidates.append((field, value))
    for name, value in candidates:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise DbError(
                f"{value!r}: {name} is not valid UTF-8; the sqlite backend "
                "cannot store it"
            ) from exc


class SqliteDb(DbProvider):
    """SQLite store: one row per path, upserted in place (no append log)."""

    NAME = "sqlite"
    SUFFIXES = (".db", ".sqlite", ".sqlite3")

    @staticmethod
    def sniff(head: bytes) -> bool:
        return head.startswith(_SQLITE_MAGIC)

    _SCHEMA = (
        "CREATE TABLE IF NOT EXISTS entries ("
        "path TEXT PRIMARY KEY, mode TEXT, owner TEXT, "
        '"group" TEXT, type TEXT, meta_json TEXT, removed INTEGER DEFAULT 0)'
    )

    #: Set only while a :meth:`batch` is active; ``add``/``remove`` use it
    #: instead of opening (and committing/closing) their own connection.
    _batch_conn = None
    _batch_count = 0

    @contextlib.contextmanager
    def batch(self) -> typing.Iterator["SqliteDb"]:
        """Hold one connection open across many ``add``/``remove`` calls,
        committing every :data:`_BATCH_ROWS` rows and once more on exit
        (exception included, so a killed batch keeps whatever it already
        committed -- the same partial-progress behavior the append-log
        backends have always had) instead of connecting, creating the
        schema and committing once per call.

        Not reentrant (a nested ``with provider.batch():`` reuses the same
        connection and row counter, which is harmless but pointless);
        callers only ever open one at a time (see ``PkgForgeCmd._db_batch``).
        """
        try:
            import sqlite3
        except ImportError as exc:
            raise PkgForgeError(
                "the sqlite DB backend needs Python's sqlite3 module "
                "(_sqlite3), which this interpreter lacks; use "
                "--db-format jsonl or yaml"
            ) from exc
        conn = sqlite3.connect(os.fspath(self.path))
        conn.execute(self._SCHEMA)
        self._batch_conn = conn
        self._batch_count = 0
        try:
            yield self
        finally:
            try:
                conn.commit()
            finally:
                self._batch_conn = None
                conn.close()

    @contextlib.contextmanager
    def _connect(self, *, ensure_schema: bool = True):
        """Yield a connection that is committed on success and always closed.

        ``sqlite3``'s own context manager commits/rolls back but does NOT close
        the connection -- leaving the file handle open, which blocks deletion on
        Windows. This wrapper guarantees ``close()``.

        ``sqlite3`` is imported here, not at module top, so ``import pkgforge``,
        ``--help`` and the ``jsonl``/``yaml`` backends all work on an
        interpreter built without it (``_sqlite3`` missing, e.g. some pyenv or
        source builds) -- only actually connecting pays for the import, and a
        missing module raises one clear error instead of an ``ImportError``
        deep in the stdlib's own ``sqlite3`` package.
        """
        try:
            import sqlite3
        except ImportError as exc:
            raise PkgForgeError(
                "the sqlite DB backend needs Python's sqlite3 module "
                "(_sqlite3), which this interpreter lacks; use "
                "--db-format jsonl or yaml"
            ) from exc
        conn = sqlite3.connect(os.fspath(self.path))
        try:
            if ensure_schema:
                conn.execute(self._SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _has_entries_table(self, conn) -> bool:
        """Return ``True`` if ``conn``'s database already has the
        ``entries`` table; ``False`` for a schema-less file (empty, or a
        zero-byte file sqlite happily opens); raise :class:`~pkgforge.db.DbError`
        for a SQLite file that holds OTHER tables but not ``entries`` -- some
        other program's database, not one of ours."""
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "entries" in tables:
            return True
        if tables:
            raise DbError(
                f"{self.path}: not a pkgforge sqlite DB (no 'entries' "
                f"table; has {', '.join(sorted(tables))})"
            )
        return False

    def load(self) -> Db:
        if not self.path.exists():
            return {}
        db: Db = {}
        # ensure_schema=False: a read must never write, not even the no-op
        # `CREATE TABLE IF NOT EXISTS` -- pointing a read at an empty or
        # foreign SQLite file must not add a table to it.
        with self._connect(ensure_schema=False) as conn:
            if not self._has_entries_table(conn):
                return {}
            for row in conn.execute(
                'SELECT path, mode, owner, "group", type, meta_json, removed FROM entries'
            ):
                path, mode, owner, group, type_, meta_json, removed = row
                if removed:
                    db[path] = None
                else:
                    # pkgforge always writes an empty meta as the literal
                    # string "{}" (json.dumps({}, sort_keys=True)); skip
                    # json.loads for that known value (and a falsy/legacy
                    # empty value) and hand each row its own fresh dict,
                    # never one shared object across rows.
                    if meta_json and meta_json != "{}":
                        meta = json.loads(meta_json)
                    else:
                        meta = {}
                    db[path] = {
                        "mode": mode,
                        "owner": owner,
                        "group": group,
                        "type": type_,
                        "meta": meta,
                    }
        return db

    def _insert_add(self, conn, path: str, entry: "FileEntry") -> None:
        rec = _fields(entry)
        _ensure_utf8(path, rec)
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

    def _insert_remove(self, conn, path: str) -> None:
        _ensure_utf8(path, {})
        conn.execute(
            "INSERT INTO entries (path, removed) VALUES (?, 1) "
            "ON CONFLICT(path) DO UPDATE SET removed=1",
            (path,),
        )

    def _batch_commit_if_due(self) -> None:
        self._batch_count += 1
        if self._batch_count % _BATCH_ROWS == 0:
            self._batch_conn.commit()

    def add(self, path: str, entry: "FileEntry") -> None:
        if self._batch_conn is not None:
            self._insert_add(self._batch_conn, path, entry)
            self._batch_commit_if_due()
            return
        with self._connect() as conn:
            self._insert_add(conn, path, entry)

    def remove(self, path: str) -> None:
        if self._batch_conn is not None:
            self._insert_remove(self._batch_conn, path)
            self._batch_commit_if_due()
            return
        with self._connect() as conn:
            self._insert_remove(conn, path)

    def compact(self) -> None:
        if not self.path.exists():
            return
        with self._connect(ensure_schema=False) as conn:
            if not self._has_entries_table(conn):
                return
            conn.execute("DELETE FROM entries WHERE removed=1")
            # VACUUM must run outside a transaction; commit what we have first.
            conn.commit()
            conn.execute("VACUUM")

    def init(self) -> None:
        with self._connect(ensure_schema=False) as conn:
            conn.execute("DROP TABLE IF EXISTS entries")
            conn.execute(self._SCHEMA)
