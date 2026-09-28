"""Shared append-log machinery for the ``jsonl``/``yaml`` text backends.

An append-log DB is a file that is only ever appended to (``add``/``remove``
write one new record) and, on :meth:`AppendLogDb.compact`, rewritten from
scratch with just the live records. :class:`AppendLogDb` owns that shared
shape; :mod:`pkgforge.db.jsonl` and :mod:`pkgforge.db.yaml` each supply only
their own text encoding via :meth:`AppendLogDb._record` (one record's text)
and :meth:`AppendLogDb._render_all` (the whole compacted file's text).
"""

from __future__ import annotations

import contextlib
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

from ..entry import DEFAULT
from . import Db, DbError, DbProvider

if typing.TYPE_CHECKING:
    from ..entry import FileEntry


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
    """The lock :meth:`AppendLogDb.compact` holds across its load-then-
    replace, so a concurrent ``_append_text`` (also holding this lock) can
    never be overwritten by a compact that already read the file.

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
      :data:`~pkgforge.entry.DEFAULT` (``"-"``); missing ``type`` -> ``None``
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
      :class:`~pkgforge.db.DbError` naming ``dbfile`` and ``path``.

    No regex is applied to a mode that is *already* a string -- pkgforge's
    own CLI (``normalize_mode``) accepts spellings such as ``"0o755"`` that
    wouldn't match a strict octal-digit pattern, and a DB written by an
    older pkgforge must keep loading.
    """
    # Fast path: every record pkgforge itself writes is already canonical, and
    # the loaders hand over a freshly decoded dict, so it can be returned as is.
    if (
        type(rec) is dict
        and type(rec.get("mode")) is str
        and type(rec.get("owner")) is str
        and type(rec.get("group")) is str
        and "meta" in rec
        and "type" in rec
        and (rec["type"] is None or type(rec["type"]) is str)
    ):
        return rec
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


class AppendLogDb(DbProvider):
    """Shared append-log behavior for the ``jsonl``/``yaml`` backends.

    Declares no ``NAME`` of its own (so subclassing it alone registers
    nothing); a concrete backend supplies ``NAME``, ``load()`` and the two
    hooks below:

    * :meth:`_record` -- one record's text (append-only, one call per
      ``add``/``remove``);
    * :meth:`_render_all` -- the whole file's text for the *live* (non-
      removed) records, used only by :meth:`compact`.
    """

    def _record(self, path: str, entry: typing.Optional["FileEntry"]) -> str:
        raise NotImplementedError

    def _render_all(self, live: typing.Dict[str, "FileEntry"]) -> str:
        raise NotImplementedError

    def add(self, path: str, entry: "FileEntry") -> None:
        _append_text(self.path, self._record(path, entry))

    def remove(self, path: str) -> None:
        _append_text(self.path, self._record(path, None))

    def compact(self) -> None:
        if not self.path.exists():
            return
        with _compact_lock(self.path):
            db: Db = self.load()
            live = {p: e for p, e in db.items() if e is not None}
            _atomic_write_text(self.path, self._render_all(live))

    def init(self) -> None:
        with _locked(self.path, "ab") as fh:
            fh.truncate(0)
