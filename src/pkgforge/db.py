"""Provider-agnostic file DB backends.

A file DB maps a build-relative *path* to a :class:`~pkgforge.common.FileEntry`
(or ``None`` for a removed path). pkgforge supports three interchangeable
storage backends behind one :class:`DbProvider` interface:

* :class:`JsonlDb` -- append-only JSON Lines (one JSON object per line);
* :class:`YamlDb`  -- append-only YAML (concatenated single-key documents);
* :class:`SqliteDb` -- a real SQLite store (upsert in place, no log to compact).

All three return the **same** ``load()`` shape, so the rest of pkgforge is
backend-agnostic. Pick a backend with :func:`open_db`: by the ``--db`` file
suffix, an explicit ``--db-format``, or -- when reading an existing file --
by sniffing its actual content (so a legacy/mislabeled file still loads).
"""

from __future__ import annotations

import abc
import contextlib
import functools
import json
import os
import re
import stat
import tempfile
import typing
from pathlib import Path

try:  # POSIX-only; pkgforge's runtime is Linux-only, but this module (and
    # the parser/--help built on it) must still import on Windows.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows dev box)
    fcntl = None

from .common import DEFAULT, PkgForgeError

if typing.TYPE_CHECKING:
    from .common import FileEntry


class DbError(PkgForgeError, ValueError):
    """A file DB's on-disk content could not be used as one.

    Raised for a parse error, non-UTF-8 bytes, a JSON Lines line or YAML
    top-level document that is not a mapping (jsonl: or ``null``, its
    tombstone spelling), or a field with a value of the wrong type. Caught by
    :func:`pkgforge.main`'s error boundary like any
    :class:`~pkgforge.common.PkgForgeError` (one stderr line, exit 1); also a
    :class:`ValueError`, so an existing ``except ValueError`` caller keeps
    working unchanged.
    """


#: A loaded DB: build path -> entry, or ``None`` for a removed path.
Db = typing.Dict[str, "typing.Optional[FileEntry]"]

#: Content sniffer: given a file's first bytes, return True if this format owns it.
Sniffer = typing.Callable[[bytes], bool]

#: Registered backends: format name -> provider class. Populated by
#: :func:`register_provider` (the built-in three register themselves at import).
PROVIDERS: typing.Dict[str, typing.Type[DbProvider]] = {}

#: File-suffix (lowercased) -> format name. Populated by :func:`register_provider`.
SUFFIX_FORMATS: typing.Dict[str, str] = {}

#: Content sniffers, newest-registered first: (format-name, sniffer).
_SNIFFERS: typing.List[typing.Tuple[str, Sniffer]] = []

#: Format used when the suffix is unknown / absent.
DEFAULT_FORMAT = "jsonl"

#: SQLite file magic (first 16 bytes of any SQLite 3 database).
_SQLITE_MAGIC = b"SQLite format 3\x00"

#: Rows per commit inside :meth:`SqliteDb.batch` -- bounds how much work a
#: killed batch loses, without paying a durable (fsync-backed) commit per row.
_BATCH_ROWS = 1000

#: The one YAML 1.1 implicit-resolver tag :func:`_yaml_io`'s loader keeps
#: (every other implicit tag -- int, float, bool, timestamp -- is dropped so
#: an unquoted scalar loads as the text it was written as).
_YAML_NULL_TAG = "tag:yaml.org,2002:null"


def register_provider(
    name: str,
    provider_cls: typing.Type[DbProvider],
    *,
    suffixes: typing.Iterable[str] = (),
    sniff: typing.Optional[Sniffer] = None,
) -> typing.Type[DbProvider]:
    """Register a file-DB backend so :func:`open_db` can select it.

    This is the extension seam that keeps third-party backends OUT of core
    pkgforge: an external package calls ``register_provider`` at import time
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


def _fields(entry: typing.Optional[FileEntry]) -> dict:
    """``entry``'s fields alone (no ``path``), FileType coerced to its plain
    string value. ``None`` (a removal) becomes ``{"_removed": True}``."""
    if entry is None:
        return {"_removed": True}
    return {k: (str(v.value) if hasattr(v, "value") else v) for k, v in entry.items()}


def _jsonl_line(path: str, entry: typing.Optional[FileEntry]) -> str:
    """One JSON Lines record for ``path``/``entry`` (or a removal marker),
    terminated with a single trailing newline."""
    return json.dumps({"path": path, **_fields(entry)}, sort_keys=True) + "\n"


def _is_flow_style_yaml(path: Path) -> bool:
    """True if an existing, non-empty YAML file's first substantive line
    (skipping blank lines, ``#`` comments and a lone ``---`` document-start
    marker) opens a flow-style mapping (``{``).

    ``YamlDb._append`` only ever writes a block-style top-level mapping;
    appending that onto an existing flow-style document (e.g.
    ``{/usr/bin/x: {...}}``, or the empty mapping ``{}``) produces invalid
    YAML. pkgforge itself never writes flow style, so this only matters for
    a hand-written or third-party file.
    """
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped == "---":
                    continue
                return stripped.startswith("{")
    except (OSError, UnicodeDecodeError):
        return False
    return False


@contextlib.contextmanager
def _locked(path: Path, mode: str) -> typing.Iterator[typing.BinaryIO]:
    """Open ``path`` in ``mode`` and, on POSIX, hold an exclusive ``flock``
    on it for the duration -- serializing ``jsonl``/``yaml`` writers
    (``_append_text``, ``init``) against each other and against ``compact``.

    After the lock is taken, the open file descriptor is compared against a
    fresh ``stat`` of ``path`` (device + inode): a mismatch means a
    concurrent ``compact`` already replaced the file with a new inode (via
    ``os.replace``) while this call was waiting on the old inode's lock, so
    the stale handle is closed and ``path`` reopened -- a lock is never held,
    and a write never lands, on an inode that has already been unlinked.
    Without this re-check, a writer queued on the pre-compact inode would
    acquire the lock only after ``compact`` released it, and would then
    append to a file nothing else can ever see again.

    A no-op lock (the handle is still opened and yielded, just never
    ``flock``-ed) when ``fcntl`` is unavailable -- off POSIX, where
    pkgforge's runtime doesn't run anyway.
    """
    while True:
        fh = path.open(mode)
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                live = os.stat(path)
            except FileNotFoundError:
                fh.close()
                continue
            held = os.fstat(fh.fileno())
            if (held.st_dev, held.st_ino) != (live.st_dev, live.st_ino):
                fh.close()
                continue
        try:
            yield fh
        finally:
            fh.close()
        return


def _compact_lock(path: Path):
    """The lock :meth:`JsonlDb.compact`/:meth:`YamlDb.compact` hold across
    their load-then-replace, so a concurrent ``_append`` (also holding this
    lock) can never be overwritten by a compact that already read the file.

    Only :func:`_locked` when ``fcntl`` exists (POSIX): off POSIX (the
    Windows dev box), ``fcntl`` provides no actual locking, and merely
    holding our own open handle to the DB file for the whole load-then-
    replace would risk the later ``os.replace`` failing with a Windows
    sharing violation for no benefit -- a plain no-op context there instead
    guarantees no handle of ours is open when the replace runs.
    """
    if fcntl is not None:
        return _locked(path, "ab")
    return contextlib.nullcontext()


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace ``path``'s content with ``text`` (UTF-8).

    Writes ``text`` to a new temp file next to ``path``, fsyncs it, copies
    ``path``'s existing mode (and tries its owner/group), then
    ``os.replace``s the temp file over ``path`` -- so an ``ENOSPC``, a kill,
    or any other failure mid-write leaves the original file completely
    unchanged, unlike :meth:`~pathlib.Path.write_text`'s truncate-then-write
    (which can leave a torn, half-written file if it fails partway).

    ``path`` is resolved via ``os.path.realpath`` first, so a *symlinked*
    ``--db`` keeps its link -- the link's target is replaced, matching what
    ``write_text`` did before (``os.replace`` itself would instead replace
    the link with a plain file). A hardlinked DB is not preserved: the
    replace swaps in a new inode, so a hardlink to the old one is detached.
    On any failure the temp file is removed and the exception re-raised;
    nothing is left behind either way.
    """
    target = Path(os.path.realpath(path))
    existing = os.stat(target) if target.exists() else None
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        if existing is not None:
            os.chmod(tmp_name, stat.S_IMODE(existing.st_mode))
            if hasattr(os, "chown"):
                with contextlib.suppress(OSError):
                    os.chown(tmp_name, existing.st_uid, existing.st_gid)
        os.replace(tmp_name, target)
        with contextlib.suppress(OSError):  # best-effort: durability, not correctness
            dirfd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _append_text(path: Path, text: str) -> None:
    """Append ``text`` to ``path`` as UTF-8, first repairing a missing
    trailing newline on the file's existing content.

    An append-log DB written or touched by something other than pkgforge (a
    hand edit, a ``printf``/``echo -n`` generator, a torn write) can lack a
    final newline; appending straight onto it would otherwise fuse the new
    record onto the old last line, silently corrupting the DB. Opening
    ``"a+b"`` (rather than checking ``st_size`` separately) keeps the repair
    and the append itself inside one open handle. Holds :func:`_locked`
    across the whole thing, serializing concurrent writers (and `compact`).
    """
    with _locked(path, "a+b") as fh:
        if fh.seek(0, os.SEEK_END):
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                fh.write(b"\n")
        fh.write(text.encode("utf-8"))


def _normalize(dbfile: Path, path: str, rec: object) -> dict:
    """Normalize one already-non-``None`` loaded record for the ``jsonl``/
    ``yaml`` built-in backends: default missing fields, and coerce a JSONL
    literal ``int`` mode/owner/group to the string pkgforge itself always
    writes (the ``yaml`` backend's null-only loader already keeps every
    other scalar as the string it was written as, so this mostly matters
    for ``jsonl``, where JSON's native ints are unaffected by that loader).

    Uses built-ins only (no field-shape knowledge beyond dict/str/int),
    since a third-party ``load()`` is never routed through this.

    * missing ``meta`` -> ``{}``; missing ``mode``/``owner``/``group`` ->
      :data:`~pkgforge.common.DEFAULT` (``"-"``); missing ``type`` -> ``None``
      (pkgforge's own writer records an explicit ``null`` for an unset
      ``type``, so a *missing* key means the same thing here).
    * ``type(v) is int`` (never a ``bool`` -- ``type(True) is bool``, not
      ``int``, so a bool is never silently accepted here) for ``owner``/
      ``group`` -> ``str(v)``; for ``mode`` -> ``str(v)`` only when that
      string is 1-4 octal digits, matching what a hand-typed ``-m`` value
      would be.
    * Anything else of the wrong type (a ``bool``, a ``float``, an ``int``
      that isn't 1-4 octal digits for ``mode``, a non-``str``/non-``None``
      ``type``, or a record that is not a mapping at all) raises
      :class:`DbError` naming ``dbfile`` and ``path``.

    No regex is applied to a mode that is *already* a string -- pkgforge's
    own CLI (``normalize_mode``) accepts spellings such as ``"0o755"`` that
    wouldn't match a strict octal-digit pattern, and a DB written by an
    older pkgforge must keep loading.
    """
    if not isinstance(rec, dict):
        raise DbError(f"{dbfile}: {path}: record is not a mapping")
    rec = dict(rec)
    rec.setdefault("meta", {})
    rec.setdefault("mode", DEFAULT)
    rec.setdefault("owner", DEFAULT)
    rec.setdefault("group", DEFAULT)
    if "type" not in rec:
        rec["type"] = None

    for field in ("owner", "group"):
        value = rec[field]
        if isinstance(value, str):
            continue
        if type(value) is int:
            rec[field] = str(value)
        else:
            raise DbError(f"{dbfile}: {path}: {field} must be a string, got {value!r}")

    mode = rec["mode"]
    if not isinstance(mode, str):
        if type(mode) is int and re.fullmatch(r"[0-7]{1,4}", str(mode)):
            rec["mode"] = str(mode)
        else:
            raise DbError(
                f'{dbfile}: {path}: mode must be a string such as "0755", '
                f"got {mode!r}"
            )

    type_ = rec["type"]
    if type_ is not None and not isinstance(type_, str):
        raise DbError(f"{dbfile}: {path}: type must be a string or null, got {type_!r}")

    return rec


class DbProvider(abc.ABC):
    """Storage backend for a file DB, bound to a filesystem ``path``."""

    format: str = ""

    def __init__(self, path: Path):
        self.path = path

    @abc.abstractmethod
    def load(self) -> Db:
        """Return the full DB as ``{path: entry-or-None}``."""

    @abc.abstractmethod
    def add(self, path: str, entry: FileEntry) -> None:
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

    @contextlib.contextmanager
    def batch(self) -> typing.Iterator[DbProvider]:
        """Optionally batch a run of ``add``/``remove`` calls for efficiency.

        A context manager yielding ``self``. The default implementation
        (this one) does nothing extra -- every ``add``/``remove`` inside it
        still writes (and, for the append-log backends, still locks)
        exactly as it would outside one -- so an existing or third-party
        provider that doesn't override this keeps working unchanged.
        :class:`SqliteDb` overrides it to hold one connection open across
        the whole batch and commit periodically instead of connecting,
        creating the schema and committing once per call (see
        :data:`_BATCH_ROWS`).
        """
        yield self


# --------------------------------------------------------------------------
# Append-log text backends (jsonl, yaml)
# --------------------------------------------------------------------------


class JsonlDb(DbProvider):
    """Append-only JSON Lines: one JSON object per line, last per path wins."""

    format = "jsonl"

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

    def add(self, path: str, entry: FileEntry) -> None:
        _append_text(self.path, _jsonl_line(path, entry))

    def remove(self, path: str) -> None:
        _append_text(self.path, _jsonl_line(path, None))

    def compact(self) -> None:
        if not self.path.exists():
            return
        with _compact_lock(self.path):
            db = self.load()
            lines = [_jsonl_line(p, e) for p, e in db.items() if e is not None]
            _atomic_write_text(self.path, "".join(lines))

    def init(self) -> None:
        with _locked(self.path, "ab") as fh:
            fh.truncate(0)


@functools.lru_cache(maxsize=None)
def _yaml_io() -> typing.Tuple[type, type]:
    """Lazily import PyYAML and return its ``(Loader, Dumper)`` classes.

    The import is deferred here rather than a module-level ``import yaml``,
    so ``import pkgforge``, ``--help`` and the ``jsonl`` backend all work on
    an interpreter that lacks PyYAML; only actually touching the ``yaml``
    backend pays for the import. A missing module raises one clear
    :class:`~pkgforge.common.PkgForgeError` instead of a bare ``ImportError``
    surfacing from wherever this was first called.

    Prefers PyYAML's libyaml-backed ``CSafeLoader``/``CSafeDumper`` (several
    times faster than the pure-Python ``SafeLoader``/``SafeDumper``) when the
    installed PyYAML build has them, falling back to the pure-Python classes
    otherwise. Both give identical results: the C loader only swaps the
    scanner/parser (its constructor is still ``SafeConstructor``, so
    duplicate-key last-wins is unaffected), and the C dumper's output is
    byte-identical to the pure-Python one.

    The returned Loader is a subclass with every *implicit* resolver but
    ``null`` removed, so an unquoted scalar loads as the text it was
    written as (``mode: 0755`` is ``"0755"``, not the int ``493``) instead
    of PyYAML's YAML-1.1 int/float/bool/timestamp guessing; ``~``/``null``
    still load as ``None`` so tombstones are unaffected, and an explicitly
    quoted or tagged scalar is untouched either way. This changes nothing
    for anything pkgforge itself writes (its own dumps already quote every
    value pkgforge cares about type-fidelity for).
    """
    try:
        import yaml
    except ImportError as exc:
        raise PkgForgeError(
            "the yaml DB backend needs PyYAML, which is not installed; "
            "use --db-format jsonl or sqlite"
        ) from exc
    base_loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    dumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)

    class _StrLoader(base_loader):
        pass

    _StrLoader.yaml_implicit_resolvers = {
        first_char: [
            (tag, regexp) for tag, regexp in resolvers if tag == _YAML_NULL_TAG
        ]
        for first_char, resolvers in base_loader.yaml_implicit_resolvers.items()
    }
    return _StrLoader, dumper


class YamlDb(DbProvider):
    """Append-only YAML: concatenated single-key documents, last key wins.

    Kept for compatibility and as an explicitly-selectable backend. Reading
    relies on the YAML loader letting a later duplicate mapping key win -- a
    property of this backend's format, not a general guarantee.
    """

    format = "yaml"

    def load(self) -> Db:
        if not self.path.exists():
            return {}
        loader, _ = _yaml_io()
        import yaml

        try:
            with self.path.open(encoding="utf-8") as fh:
                data = yaml.load(fh, Loader=loader)
        except UnicodeDecodeError as exc:
            raise DbError(f"{self.path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise DbError(f"{self.path}: {exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise DbError(
                f"{self.path}: invalid YAML file DB: top-level document is "
                "not a mapping"
            )
        return {
            path: (None if rec is None else _normalize(self.path, path, rec))
            for path, rec in data.items()
        }

    def _append(self, path: str, entry: typing.Optional[FileEntry]) -> None:
        if self.path.exists() and _is_flow_style_yaml(self.path):
            raise DbError(
                f"{self.path}: a flow-style YAML DB cannot be appended to; "
                "run `pkgforge --db-format yaml compact` first to rewrite "
                "it in block style"
            )
        _, dumper = _yaml_io()
        import yaml

        value = None if entry is None else _fields(entry)
        _append_text(self.path, yaml.dump({path: value}, Dumper=dumper))

    def add(self, path: str, entry: FileEntry) -> None:
        self._append(path, entry)

    def remove(self, path: str) -> None:
        self._append(path, None)

    def compact(self) -> None:
        if not self.path.exists():
            return
        with _compact_lock(self.path):
            db = self.load()
            _, dumper = _yaml_io()
            import yaml

            live = {p: _fields(e) for p, e in db.items() if e is not None}
            text = yaml.dump(live, Dumper=dumper) if live else ""
            _atomic_write_text(self.path, text)

    def init(self) -> None:
        with _locked(self.path, "ab") as fh:
            fh.truncate(0)


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

    def load(self) -> Db:
        if not self.path.exists():
            return {}
        db: Db = {}
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

    def _insert_add(self, conn, path: str, entry: FileEntry) -> None:
        rec = _fields(entry)
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
        conn.execute(
            "INSERT INTO entries (path, removed) VALUES (?, 1) "
            "ON CONFLICT(path) DO UPDATE SET removed=1",
            (path,),
        )

    def _batch_commit_if_due(self) -> None:
        self._batch_count += 1
        if self._batch_count % _BATCH_ROWS == 0:
            self._batch_conn.commit()

    def add(self, path: str, entry: FileEntry) -> None:
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
    # A quoted first key: claims pkgforge's own output ({"group": ...) and a
    # hand-written record ({ "path": ...}), never a flow-style YAML mapping
    # (yaml.safe_dump's plain-key output, e.g. {/usr/bin/x: ...}, or {}).
    sniff=lambda head: re.match(rb'\s*\{\s*"', head) is not None,
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


def sniff_format(path: Path) -> typing.Optional[str]:
    """Detect an existing file's format from its content, or ``None`` if unknown.

    Registered sniffers are tried newest-first (the registered ``jsonl``
    sniffer is what claims a quoted-first-key ``{``; see
    :func:`register_provider`'s built-in registration below); if none claims
    the file, the fallback treats any other non-empty content as YAML (the
    most permissive text format).
    """
    try:
        # Read only the 16 bytes the sniffers look at — an append-log DB can
        # be arbitrarily large, and read_bytes() would pull all of it in.
        with path.open("rb") as fh:
            head = fh.read(16)
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
    path: Path, fmt: typing.Optional[str] = None, *, for_read: bool = False
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
        ) from None
    return provider_cls(path)
