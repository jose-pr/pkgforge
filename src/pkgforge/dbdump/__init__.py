"""Render a file DB into packaging manifests (:func:`dump`).

Two shapes of format are supported, each a :class:`DumpFormat` subclass:

* :class:`PerEntryFormat` (e.g. :class:`~pkgforge.dbdump.RPMSpecFiles`)
  renders one line per DB entry into one ``bytes`` value, written to a file;
* :class:`MultiArtifactFormat` (e.g. :class:`~pkgforge.dbdump.DebianFiles`)
  renders several related files (e.g. an ``install`` path list plus a
  ``fixperms`` script) into a ``{name: bytes}`` dict, written into a
  directory.

A third party adds a format by subclassing one of the two with its own
``NAME`` (and, optionally, ``ALIASES``): defining the subclass registers it,
and listing it in the ``pkgforge.dump_formats`` entry-point group
(``name = module:Class``) makes it selectable by ``dbdump -f`` once its
package is installed. A name or alias another format holds is a
:class:`ValueError`.
"""

from __future__ import annotations

import abc
import contextlib
import logging
import os
import re
import typing
from pathlib import Path

from .._registry import Registered
from ..entry import (
    _CONTROL_PATTERN,
    _RPM_EXPANSION_PATTERN,
    _MODE_PATTERN,
    _NAME_PATTERN,
    FileEntries,
    FileEntry,
    FileType,
    StrPath,
)
from ..exceptions import DumpError, InvalidEntryError, UnsupportedOutputError
from ..exclude import PathMatch, _statements

#: Every C0 control character (0x00-0x1f) plus DEL (0x7f): rpm and debhelper
#: both choke on these one way or another (a bare newline splits a line, a
#: literal DEL is simply illegal), and neither format has an escape for them.
_CONTROL_RE = re.compile(_CONTROL_PATTERN)
_RPM_EXPANSION_RE = re.compile(_RPM_EXPANSION_PATTERN)

_NAME_RE = re.compile(_NAME_PATTERN)
_MODE_RE = re.compile(_MODE_PATTERN)
#: Finds a line of newline-joined values that is not a valid name / mode.
#: Empty values (rendered as ``-``) never match. Run only after
#: :data:`_CONTROL_RE` found no newline in the values.
_BAD_NAMES_RE = re.compile(rf"^(?!(?:{_NAME_PATTERN})$).+", re.M)
_BAD_MODES_RE = re.compile(rf"^(?!(?:{_MODE_PATTERN})$).+", re.M)
#: Finds a key that is not an absolute path with no empty, ``.`` or ``..``
#: segment (the key ``/`` alone is allowed), over newline-joined keys.
_BAD_KEYS_RE = re.compile(r"^(?!/)|//|/\.\.?(?:/|$)|(?<=.)/$", re.M)
_FILE_TYPES = frozenset(member.value for member in FileType)


def _reject_control(path: str, fmt: str) -> None:
    if _CONTROL_RE.search(path):
        raise DumpError(
            f"{fmt}: a DB entry's path contains a control character that "
            "cannot be represented in this format"
        )


def _has_control(texts: typing.Iterable[str]) -> bool:
    """Whether any string in ``texts`` contains a C0/DEL control character.

    A single :data:`_CONTROL_RE` search over every string joined together,
    reading the module global at call time (so a test's monkeypatch of
    ``pkgforge.dbdump._CONTROL_RE`` is honored) -- the batch pre-check a
    format's ``render`` uses to decide whether any per-entry rejection is
    needed at all. Concatenation cannot hide or fabricate a match: every
    character this regex looks for is matched on its own, never as part of a
    multi-character sequence, so a hit anywhere in the joined text always
    means some individual string actually contains that character.
    """
    return _CONTROL_RE.search("".join(texts)) is not None


def _entry_error(path: str, field: str, value: object = None) -> InvalidEntryError:
    shown = "" if value is None else f" {value!r}"
    return InvalidEntryError(f"dbdump: entry {path!r}: invalid {field}{shown}")


def _check_entry(path: str, entry: FileEntry) -> None:
    """Raise :class:`InvalidEntryError` for the first bad field of one entry."""
    if _BAD_KEYS_RE.search(path) or _CONTROL_RE.search(path):
        raise _entry_error(path, "path")
    kind = entry["type"]
    kind = kind.value if isinstance(kind, FileType) else kind
    if kind not in _FILE_TYPES:
        raise _entry_error(path, "type", entry["type"])
    for field, pattern in (
        ("mode", _MODE_RE),
        ("owner", _NAME_RE),
        ("group", _NAME_RE),
    ):
        value = entry[field]
        if value and not (isinstance(value, str) and pattern.fullmatch(value)):
            raise _entry_error(path, field, value)
    prefix = entry["meta"].get("rpmprefix")
    if prefix and (_CONTROL_RE.search(prefix) or _RPM_EXPANSION_RE.search(prefix)):
        raise _entry_error(path, "meta rpmprefix", prefix)


def _validate_entries(entries: FileEntries) -> None:
    """Refuse the first entry (in entry order) that no format can render
    safely: see :class:`InvalidEntryError`.

    One regex search per field over the newline-joined values decides whether
    anything is wrong; only then does a per-entry pass find and name it. A
    control character anywhere (a newline included) goes to the per-entry
    pass too, since it would otherwise be indistinguishable from the join.
    """
    if not entries:
        return
    keys = "\n".join(path for path, _entry in entries)
    columns = [
        "\n".join(str(entry[field] or "") for _path, entry in entries)
        for field in ("mode", "owner", "group")
    ]
    prefixes = "".join(entry["meta"].get("rpmprefix") or "" for _path, entry in entries)
    kinds = {
        entry["type"].value if isinstance(entry["type"], FileType) else entry["type"]
        for _path, entry in entries
    }
    if (
        _CONTROL_RE.search(keys)
        or _CONTROL_RE.search(prefixes)
        or _RPM_EXPANSION_RE.search(prefixes)
        or any(_CONTROL_RE.search(column) for column in columns)
        or _BAD_KEYS_RE.search(keys)
        or _BAD_MODES_RE.search(columns[0])
        or _BAD_NAMES_RE.search(columns[1])
        or _BAD_NAMES_RE.search(columns[2])
        or not kinds <= _FILE_TYPES
    ):
        for path, entry in entries:
            _check_entry(path, entry)


def _write_files(files: typing.Dict[Path, bytes]) -> None:
    """Write every file whole or not at all: each goes to a sibling
    ``.<name>.pkgforge-tmp`` first, and only when all of those are written are
    they renamed over their targets. A failure removes the temp files and
    leaves every target as it was.
    """
    temps = {path: path.with_name(f".{path.name}.pkgforge-tmp") for path in files}
    try:
        for path, data in files.items():
            temps[path].write_bytes(data)
        for path, temp in temps.items():
            os.replace(temp, path)
    finally:
        for temp in temps.values():
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()


#: What a format renders: one file's bytes, or ``{filename: bytes}``.
Rendered = typing.Union[bytes, typing.Dict[str, bytes]]

_LOGGER = logging.getLogger("pkgforge.dbdump")


class DumpFormat(Registered, abc.ABC):
    """A packaging-manifest format, selected by ``dbdump -f NAME``.

    A subclass registers by declaring its own ``NAME`` (plus, optionally,
    ``ALIASES``); see :class:`pkgforge._registry.Registered`. Subclass
    :class:`PerEntryFormat` or :class:`MultiArtifactFormat`, not this class
    directly -- they supply :meth:`check_output` and :meth:`write` for their
    respective output shape.
    """

    NAME: typing.ClassVar[str] = ""
    ALIASES: typing.ClassVar[typing.Tuple[str, ...]] = ()
    _registry: typing.ClassVar[typing.Dict[str, type]] = {}
    _KIND = "format"
    _GROUP = "pkgforge.dump_formats"

    @abc.abstractmethod
    def render(self, entries: FileEntries) -> Rendered:
        """Render the (already validated) ``entries`` to bytes, writing nothing."""

    @abc.abstractmethod
    def check_output(self, output: Path) -> None:
        """Raise :class:`UnsupportedOutputError` if ``output`` is the wrong
        shape for this format."""

    @abc.abstractmethod
    def write(self, rendered: Rendered, output: Path) -> typing.List[Path]:
        """Write ``rendered`` to ``output``, whole or not at all; return the
        files written."""


class PerEntryFormat(DumpFormat):
    """A format that renders one line per DB entry to a single file."""

    @abc.abstractmethod
    def render_entry(self, path: str, entry: FileEntry) -> bytes:
        """Render one DB entry to its line of bytes."""

    def render(self, entries: FileEntries) -> bytes:
        return b"".join(self.render_entry(path, entry) for path, entry in entries)

    def check_output(self, output: Path) -> None:
        if output.is_dir():
            raise UnsupportedOutputError(
                f"{self.NAME} writes one file; OUTPUT is a directory: {output}"
            )

    def write(self, rendered: Rendered, output: Path) -> typing.List[Path]:
        _write_files({output: typing.cast(bytes, rendered)})
        return [output]


class MultiArtifactFormat(DumpFormat):
    """A format that renders several named artifacts into a directory."""

    @abc.abstractmethod
    def render(self, entries: FileEntries) -> typing.Dict[str, bytes]:
        """Render every artifact this format produces: ``{filename: bytes}``."""

    def check_output(self, output: Path) -> None:
        if output.exists() and not output.is_dir():
            raise UnsupportedOutputError(
                f"{self.NAME} writes several files; OUTPUT must be a "
                f"directory or '-', got existing file {output}"
            )

    def write(self, rendered: Rendered, output: Path) -> typing.List[Path]:
        artifacts = typing.cast(typing.Dict[str, bytes], rendered)
        output.mkdir(parents=True, exist_ok=True)
        files = {output / name: data for name, data in artifacts.items()}
        _write_files(files)
        return list(files)


def _surviving_entries(
    db: typing.Mapping[str, typing.Optional[FileEntry]],
    exclude: typing.Sequence[typing.Any],
) -> FileEntries:
    """The live entries of ``db`` that no ``exclude`` statement drops,
    sorted by DB path."""
    matcher = PathMatch(_statements(exclude))
    entries: FileEntries = []
    for path, entry in db.items():
        if entry is None or (exclude and matcher.match(path, entry)):
            continue
        entries.append((path, entry))
    if db and exclude and not entries:
        live = sum(1 for entry in db.values() if entry is not None)
        _LOGGER.warning(
            "0 of %d entries survived --exclude filtering; dumping an empty manifest",
            live,
        )
    # Sorted by DB path (code-point order), not backend/insertion/readdir
    # order: neither rpm nor dh_install give the output order any
    # meaning, so this is what makes a staged tree give byte-identical
    # manifests on any filesystem and any backend.
    entries.sort(key=lambda pathentry: pathentry[0])
    return entries


def dump(
    db: typing.Mapping[str, typing.Optional[FileEntry]],
    fmt: str,
    output: typing.Optional[StrPath] = None,
    *,
    exclude: typing.Sequence[str] = (),
) -> Rendered:
    """Render the live entries of ``db`` with the format ``fmt``.

    ``fmt`` is a registered format name or alias. Removed entries (``None``)
    and entries matched by an ``exclude`` statement are dropped; the rest are
    sorted by path and validated. With ``output`` the result is also written
    whole: a file for a per-entry format, a directory (created if missing) for
    a multi-artifact one.

    Returns the rendered bytes (a ``bytes`` value, or ``{filename: bytes}``).
    Raises :class:`~pkgforge.exceptions.UsageError` for an unknown format or
    exclude statement, :class:`~pkgforge.exceptions.UnsupportedOutputError`
    for an ``output`` of the wrong shape (checked before anything is
    rendered), :class:`~pkgforge.exceptions.InvalidEntryError` for an entry no
    format can render and :class:`~pkgforge.exceptions.DumpError` for one this
    format cannot.
    """
    instance = DumpFormat.get(fmt)()
    target = None if output is None else Path(os.fspath(output))
    if target is not None:
        instance.check_output(target)
    entries = _surviving_entries(db, exclude)
    _validate_entries(entries)
    rendered = instance.render(entries)
    if target is not None:
        for written in instance.write(rendered, target):
            _LOGGER.info("Wrote %s", written)
    return rendered


# --------------------------------------------------------------------------
# Built-in formats: importing each module registers its format class.
# --------------------------------------------------------------------------

from ._rpm import RPMSpecFiles, RPMSpecFilesPre419  # noqa: E402
from ._debian import DebianFiles  # noqa: E402

__all__ = [
    "DebianFiles",
    "DumpFormat",
    "MultiArtifactFormat",
    "PerEntryFormat",
    "RPMSpecFiles",
    "RPMSpecFilesPre419",
    "Rendered",
    "dump",
]
