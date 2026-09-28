"""Provider-agnostic file DB backends.

A file DB maps a build-relative *path* to a :class:`~pkgforge.entry.FileEntry`
(or ``None`` for a removed path). pkgforge supports three interchangeable
storage backends behind one :class:`DbProvider` interface, each in its own
module: :class:`~pkgforge.db.jsonl.JsonlDb`, :class:`~pkgforge.db.yaml.YamlDb`
and :class:`~pkgforge.db.sqlite.SqliteDb`.

All three return the **same** ``load()`` shape, so the rest of pkgforge is
backend-agnostic. Pick a backend with :func:`open_db`: by the ``--db`` file
suffix, an explicit ``--db-format``, or -- when reading an existing file --
by sniffing its actual content (so a legacy/mislabeled file still loads).

A third party adds a backend by subclassing :class:`DbProvider` with its own
``NAME`` (and, optionally, ``ALIASES``/``SUFFIXES``/``sniff``) -- no
registration call is needed, subclassing alone registers it (see
:mod:`pkgforge._registry`).
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

    Raised for a parse error, non-UTF-8 bytes, a JSON Lines line or YAML
    top-level document that is not a mapping (jsonl: or ``null``, its
    tombstone spelling), or a field with a value of the wrong type. Caught by
    :func:`pkgforge.main`'s error boundary like any
    :class:`~pkgforge.errors.PkgForgeError` (one stderr line, exit 1); also a
    :class:`ValueError`, so an existing ``except ValueError`` caller keeps
    working unchanged.
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
    rather than half-registered. An optional ``sniff(head: bytes) -> bool``
    staticmethod inspects a file's first 16 bytes to claim it by content;
    it counts only when the class's own body defines it (never inherited).
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


def sniff_format(path: Path) -> typing.Optional[str]:
    """Detect an existing file's format from its content, or ``None`` if unknown.

    Registered classes are tried newest-first (their own ``sniff``
    staticmethod, when their own body defines one); if none claims the
    file, the fallback treats any other non-empty content as YAML (the
    most permissive text format).
    """
    try:
        # Read only the 16 bytes a sniffer looks at -- an append-log DB can
        # be arbitrarily large, and read_bytes() would pull all of it in.
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    for registered in DbProvider._unique():
        if "sniff" not in registered.__dict__:
            continue
        try:
            if registered.sniff(head):
                return registered.NAME
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

    ``for_read=True`` means "sniff an existing file's content", not
    "this call only reads" -- :meth:`~pkgforge.command.PkgForgeCmd._write_entry`
    passes it on *writes* too, on purpose: it keeps an append in the file's
    actual format (e.g. legacy YAML content under a ``.jsonl`` suffix stays
    YAML) instead of appending JSON Lines into a file sniffing would have
    read back as something else.
    """
    if fmt is None:
        detected = sniff_format(path) if (for_read and path.exists()) else None
        fmt = detected or format_for_suffix(path)
    provider_cls = DbProvider.lookup(fmt)
    return provider_cls(path)


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
    "sniff_format",
]
