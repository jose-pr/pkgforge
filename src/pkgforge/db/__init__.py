"""Provider-agnostic file DB backends.

A file DB maps a build-relative *path* to a :class:`~pkgforge.entry.FileEntry`
(or ``None`` for a removed path). pkgforge supports three interchangeable
storage backends behind one :class:`DbProvider` interface, each in its own
module: :class:`~pkgforge.db.jsonl.JsonlDb`, :class:`~pkgforge.db.yaml.YamlDb`
and :class:`~pkgforge.db.sqlite.SqliteDb`.

All three return the **same** ``load()`` shape, so the rest of pkgforge is
backend-agnostic. Pick a backend with :func:`open_db`: an explicit
``--db-format``, else the ``--db`` file suffix, else ``jsonl``. A file whose
content is not in the chosen format fails to load with a
:class:`DbError`; its content is never inspected to pick a backend.

A third party adds a backend by subclassing :class:`DbProvider` with its own
``NAME`` (and, optionally, ``ALIASES``/``SUFFIXES``) -- no registration call
is needed, subclassing alone registers it (see :mod:`pkgforge._registry`).
"""

from __future__ import annotations

import abc
import contextlib
import typing
from pathlib import Path

from .._registry import Registered
from ..errors import PkgForgeError

if typing.TYPE_CHECKING:
    from ..entry import FileEntry


class DbError(PkgForgeError, ValueError):
    """A file DB's on-disk content could not be used as one.

    Raised for a parse error, non-UTF-8 bytes, content that is not in the
    chosen format, a JSON Lines line or YAML top-level document that is not a
    mapping, or a record that breaks the schema (see :func:`_check_record`).
    Caught by :func:`pkgforge.main`'s error boundary like any
    :class:`~pkgforge.errors.PkgForgeError` (one stderr line, exit 1); also a
    :class:`ValueError`.
    """


#: A loaded DB: build path -> entry, or ``None`` for a removed path.
Db = typing.Dict[str, "typing.Optional[FileEntry]"]

#: Format used when the suffix is unknown / absent.
DEFAULT_FORMAT = "jsonl"


def _fields(entry: typing.Optional["FileEntry"]) -> dict:
    """``entry``'s fields alone (no ``path``), FileType coerced to its plain
    string value. ``None`` (a removal) becomes ``{"_removed": True}``."""
    if entry is None:
        return {"_removed": True}
    return {k: (str(v.value) if hasattr(v, "value") else v) for k, v in entry.items()}


class DbProvider(Registered, abc.ABC):
    """Storage backend for a file DB, bound to a filesystem ``path``.

    A subclass registers by declaring its own ``NAME`` (plus, optionally,
    ``ALIASES``); see :class:`pkgforge._registry.Registered`. ``SUFFIXES``
    (file suffixes that infer this format from a ``--db`` path -- case-
    insensitive; a missing leading dot is added, since :attr:`Path.suffix`
    always includes one and a dotless entry could otherwise never match; a
    suffix with more than one dot, e.g. ``".tar.gz"``, raises ``ValueError``
    at class-creation time, since :attr:`Path.suffix` only ever returns the
    last dot-segment and such an entry could never match either) are
    normalized -- and validated -- from the class's own body before
    registration, so a bad suffix leaves the class entirely unregistered
    rather than half-registered.
    """

    NAME: typing.ClassVar[str] = ""
    ALIASES: typing.ClassVar[typing.Tuple[str, ...]] = ()
    SUFFIXES: typing.ClassVar[typing.Tuple[str, ...]] = ()
    _registry: typing.ClassVar[typing.Dict[str, type]] = {}
    _KIND = "db format"

    def __init__(self, path: Path):
        self.path = path

    def __init_subclass__(cls, **kwargs: typing.Any) -> None:
        # Normalize/validate this class's OWN suffixes BEFORE Registered's
        # __init_subclass__ runs (which does the actual NAME/ALIASES
        # registration): a bad suffix must raise before anything about this
        # class is registered, never leaving a name half-registered.
        if "SUFFIXES" in cls.__dict__:
            normalized = []
            for suffix in cls.__dict__["SUFFIXES"]:
                suffix = suffix.lower()
                if suffix and not suffix.startswith("."):
                    suffix = "." + suffix
                if suffix.count(".") > 1:
                    raise ValueError(
                        f"suffix {suffix!r} has more than one dot; Path.suffix "
                        "only ever returns the last dot-segment, so this could "
                        "never match a --db path"
                    )
                normalized.append(suffix)
            cls.SUFFIXES = tuple(normalized)
        super().__init_subclass__(**kwargs)

    @abc.abstractmethod
    def load(self) -> Db:
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

    @contextlib.contextmanager
    def batch(self) -> typing.Iterator["DbProvider"]:
        """Optionally batch a run of ``add``/``remove`` calls for efficiency.

        A context manager yielding ``self``. The default implementation
        (this one) does nothing extra -- every ``add``/``remove`` inside it
        still writes (and, for the append-log backends, still locks)
        exactly as it would outside one -- so an existing or third-party
        provider that doesn't override this keeps working unchanged.
        :class:`~pkgforge.db.sqlite.SqliteDb` overrides it to hold one
        connection open across the whole batch and commit periodically
        instead of connecting, creating the schema and committing once per
        call.
        """
        yield self


def format_for_suffix(path: Path) -> str:
    """Infer a format from ``path``'s suffix, else :data:`DEFAULT_FORMAT`.

    Registered classes are tried newest-first, so a suffix claimed by two
    classes resolves to whichever registered more recently.
    """
    suffix = path.suffix.lower()
    for registered in DbProvider._unique():
        # Own class dict only: a subclass that does not redeclare SUFFIXES
        # itself must not silently claim its parent's (plain attribute
        # access would find the inherited value instead).
        if suffix in registered.__dict__.get("SUFFIXES", ()):
            return registered.NAME
    return DEFAULT_FORMAT


def open_db(path: Path, fmt: typing.Optional[str] = None) -> DbProvider:
    """Resolve and construct the :class:`DbProvider` for ``path``.

    An explicit ``fmt`` wins; otherwise the suffix decides, defaulting to JSON
    Lines. The file's content is never inspected: a file that is not in the
    resolved format fails when it is loaded.
    """
    return DbProvider.lookup(fmt or format_for_suffix(path))(path)


def _format_hint(name: str) -> str:
    """The suffix a loader appends to an error caused by content that is not
    in its format."""
    return f" (read as {name}; pass --db-format to choose another format)"


_RECORD_FIELDS = ("mode", "owner", "group", "type", "meta")
_RECORD_TYPES = frozenset(("file", "directory", "symlink"))


def _record_ok(path: object, rec: object) -> bool:
    """Whether ``path`` and the non-tombstone ``rec`` pass :func:`_check_record`.

    The loaders' per-record fast path: ``type(x) is str`` checks, no regex and
    no message formatting.
    """
    if type(rec) is not dict or len(rec) != 5:
        return False
    kind = rec.get("type")
    meta = rec.get("meta")
    return (
        type(path) is str
        and path.startswith("/")
        and type(rec.get("mode")) is str
        and type(rec.get("owner")) is str
        and type(rec.get("group")) is str
        and type(kind) is str
        and kind in _RECORD_TYPES
        and type(meta) is dict
        and (
            not meta or all(type(k) is str and type(v) is str for k, v in meta.items())
        )
    )


def _check_record(
    path: object, rec: object, where: object
) -> typing.Optional["FileEntry"]:
    """Validate one loaded record and return it, or ``None`` for a tombstone.

    ``path`` must be a ``str`` starting with ``/``. ``rec`` is ``None`` (a
    tombstone) or a ``dict`` with exactly ``mode``, ``owner``, ``group``,
    ``type`` and ``meta``: the first three ``str``, ``type`` one of ``file``,
    ``directory`` and ``symlink``, ``meta`` a ``dict`` of ``str`` to ``str``.
    Nothing is coerced and nothing is defaulted. ``where`` (a file, or
    ``file:line``) prefixes the :class:`DbError` raised for anything else.
    """
    if rec is not None and _record_ok(path, rec):
        return rec
    if type(path) is not str or not path.startswith("/"):
        raise DbError(f"{where}: {path!r}: path must be a string starting with '/'")
    if rec is None:
        return None
    if type(rec) is not dict:
        raise DbError(f"{where}: {path!r}: record is not a mapping")
    extra = sorted(str(key) for key in rec if key not in _RECORD_FIELDS)
    if extra:
        raise DbError(f"{where}: {path!r}: unknown field {', '.join(extra)}")
    for field in _RECORD_FIELDS:
        if field not in rec:
            raise DbError(f"{where}: {path!r}: missing field {field}")
    for field in ("mode", "owner", "group"):
        if type(rec[field]) is not str:
            raise DbError(
                f"{where}: {path!r}: {field} must be a string, got {rec[field]!r}"
            )
    if type(rec["type"]) is not str or rec["type"] not in _RECORD_TYPES:
        raise DbError(
            f"{where}: {path!r}: type must be one of "
            f"{', '.join(sorted(_RECORD_TYPES))}, got {rec['type']!r}"
        )
    raise DbError(f"{where}: {path!r}: meta must map strings to strings")


# --------------------------------------------------------------------------
# Built-in backends: importing each module registers its provider class.
# --------------------------------------------------------------------------

from .jsonl import JsonlDb  # noqa: E402
from .yaml import YamlDb  # noqa: E402
from .sqlite import SqliteDb  # noqa: E402

__all__ = [
    "Db",
    "DbError",
    "DbProvider",
    "DEFAULT_FORMAT",
    "JsonlDb",
    "YamlDb",
    "SqliteDb",
    "format_for_suffix",
    "open_db",
]
