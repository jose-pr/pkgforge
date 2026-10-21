"""Render a file DB into packaging manifests (:func:`dump`).

Two shapes of format are supported, each a :class:`DumpFormat` subclass:

* :class:`PerEntryFormat` (e.g. :class:`~pkgforge.dbdump.RPMSpecFiles`)
  renders one line per DB entry into one ``bytes`` value, written to a file
  (or, given a directory, one ``files-PACKAGE.txt`` per package);
* :class:`MultiArtifactFormat` (e.g. :class:`~pkgforge.dbdump.DebianFiles`)
  renders several related files (e.g. an ``install`` path list plus a
  ``fixperms`` script) into a ``{name: bytes}`` dict, written into a
  directory.

Every format renders with a :class:`DumpContext`: the project configuration
that names the packages, and the output being written.

A format can also write a skeleton, a file its user is meant to edit
(:attr:`DumpFormat.SKELETON`, ``dbdump --skeleton``; :attr:`DumpFormat.KEEP_EXISTING`
for a format whose whole output is one). A skeleton file that already exists is
kept, with a warning, unless ``force`` is given.

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
import dataclasses
import datetime
import logging
import os
import re
import typing
from pathlib import Path

from .._registry import Registered
from ..entry import (
    _CAPS_PATTERN,
    _CONTROL_PATTERN,
    _KIND_TYPES,
    _MODE_PATTERN,
    _NAME_PATTERN,
    _PACKAGE_PATTERN,
    DEFAULT,
    FileEntries,
    FileEntry,
    FileType,
    StrPath,
)
from ..exceptions import (
    DumpError,
    InvalidEntryError,
    UnsupportedOutputError,
    UsageError,
)
from ..config import ProjectConfig, _owned
from ..exclude import PathMatch, _statements

#: Every C0 control character (0x00-0x1f) plus DEL (0x7f): rpm and debhelper
#: both choke on these one way or another (a bare newline splits a line, a
#: literal DEL is simply illegal), and neither format has an escape for them.
_CONTROL_RE = re.compile(_CONTROL_PATTERN)

_NAME_RE = re.compile(_NAME_PATTERN)
_MODE_RE = re.compile(_MODE_PATTERN)
_CAPS_RE = re.compile(_CAPS_PATTERN)
_PACKAGE_RE = re.compile(_PACKAGE_PATTERN)
#: Finds a line of newline-joined values that is not a valid name / mode.
#: Empty values (rendered as ``-``) never match. Run only after
#: :data:`_CONTROL_RE` found no newline in the values.
_BAD_NAMES_RE = re.compile(rf"^(?!(?:{_NAME_PATTERN})$).+", re.M)
_BAD_MODES_RE = re.compile(rf"^(?!(?:{_MODE_PATTERN})$).+", re.M)
_BAD_CAPS_RE = re.compile(rf"^(?!(?:{_CAPS_PATTERN}|-)$).*", re.M)
_BAD_PACKAGES_RE = re.compile(rf"^(?!(?:{_PACKAGE_PATTERN})$).*", re.M)
#: The ``(type, kind)`` pairs an entry may hold, and the ones that may carry
#: ``caps`` (a regular file that is not a ghost).
_KIND_FITS = frozenset(
    (ftype, kind) for kind, types in _KIND_TYPES.items() for ftype in types
)
_CAPS_FITS = frozenset(("file", kind) for kind in _KIND_TYPES if kind != "ghost")
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
    package = entry["package"]
    if not (isinstance(package, str) and _PACKAGE_RE.fullmatch(package)):
        raise _entry_error(path, "package", package)
    fkind = entry["kind"]
    if fkind not in _KIND_TYPES:
        raise _entry_error(path, "kind", fkind)
    if (kind, fkind) not in _KIND_FITS:
        raise _entry_error(path, "kind for type", fkind)
    caps = entry["caps"]
    if caps != DEFAULT and not (
        isinstance(caps, str)
        and _CAPS_RE.fullmatch(caps)
        and (kind, fkind) in _CAPS_FITS
    ):
        raise _entry_error(path, "caps", caps)


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
        for field in ("mode", "owner", "group", "caps", "package")
    ]
    kinds = set()
    pairs = set()
    capped = set()
    for _path, entry in entries:
        ftype = entry["type"]
        if isinstance(ftype, FileType):
            ftype = ftype.value
        kinds.add(ftype)
        pair = (ftype, entry["kind"])
        pairs.add(pair)
        if entry["caps"] != DEFAULT:
            capped.add(pair)
    if (
        _CONTROL_RE.search(keys)
        or any(_CONTROL_RE.search(column) for column in columns)
        or _BAD_KEYS_RE.search(keys)
        or _BAD_MODES_RE.search(columns[0])
        or _BAD_NAMES_RE.search(columns[1])
        or _BAD_NAMES_RE.search(columns[2])
        or _BAD_CAPS_RE.search(columns[3])
        or _BAD_PACKAGES_RE.search(columns[4])
        or not kinds <= _FILE_TYPES
        or not pairs <= _KIND_FITS
        or not capped <= _CAPS_FITS
    ):
        for path, entry in entries:
            _check_entry(path, entry)


def _source_date_epoch() -> typing.Optional[int]:
    """``SOURCE_DATE_EPOCH`` as whole seconds, or ``None`` when it is unset or
    empty.

    Raises :class:`~pkgforge.exceptions.UsageError` for a value that is not a
    number of seconds a timestamp can hold.
    """
    text = os.environ.get("SOURCE_DATE_EPOCH")
    if not text:
        return None
    try:
        seconds = int(text)
        datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise UsageError(
            f"SOURCE_DATE_EPOCH={text!r} is not a number of seconds"
        ) from None
    return seconds


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


def _require_keys(
    fmt: str,
    config: ProjectConfig,
    keys: typing.Sequence[str],
    sub_keys: typing.Sequence[str],
) -> None:
    """Raise :class:`~pkgforge.exceptions.UsageError` naming every ``[package]``
    key in ``keys`` (and ``name``) that ``config`` leaves unset, and every
    ``[[packages]]`` element missing one of ``sub_keys``."""
    package = config.package
    missing = [] if package is not None else ["name"]
    missing.extend(
        key for key in keys if package is None or not getattr(package, key, None)
    )
    parts = [f"[package] {', '.join(missing)}"] if missing else []
    for index, sub in enumerate(config.packages):
        parts.extend(
            f"[[packages]] {index} {key}"
            for key in sub_keys
            if not getattr(sub, key, None)
        )
    if not parts:
        return
    listed = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
    raise UsageError(f"{fmt} needs {listed}")


def _keep_existing(
    files: typing.Dict[Path, bytes], force: bool
) -> typing.Dict[Path, bytes]:
    """``files`` without the skeleton files that already exist, each logged as
    kept; all of them with ``force``."""
    if force:
        return files
    kept = {}
    for path, data in files.items():
        if path.exists():
            _LOGGER.warning("kept existing %s (pass --force to replace it)", path)
        else:
            kept[path] = data
    return kept


#: What a format renders: one file's bytes, or ``{filename: bytes}``.
Rendered = typing.Union[bytes, typing.Dict[str, bytes]]

_LOGGER = logging.getLogger("pkgforge.dbdump")


@dataclasses.dataclass(frozen=True)
class DumpContext:
    """What a format renders against besides the entries.

    ``config`` is the project configuration (its ``[package]`` and
    ``[[packages]]`` name the packages), ``config_path`` the file it was read
    from when known, and ``output`` the path being written (``None`` for
    stdout or when nothing is written).
    """

    config: ProjectConfig = dataclasses.field(default_factory=ProjectConfig)
    config_path: typing.Optional[Path] = None
    output: typing.Optional[Path] = None


def _packages(
    entries: FileEntries, config: ProjectConfig
) -> typing.List[typing.Tuple[str, FileEntries]]:
    """Group ``entries`` by package: the main package first (its name, or
    ``-`` when the configuration has no ``[package]``), then each
    ``[[packages]]`` name in order. An entry recorded as ``-`` belongs to the
    main package; an empty group is kept.

    Raises :class:`~pkgforge.exceptions.UsageError` for an entry whose package
    the configuration does not declare.
    """
    main = config.package.name if config.package is not None else DEFAULT
    groups: typing.Dict[str, FileEntries] = {main: []}
    for sub in config.packages:
        groups[sub.name] = []
    for path, entry in entries:
        package = entry["package"]
        if package == DEFAULT:
            package = main
        group = groups.get(package)
        if group is None:
            raise UsageError(
                f"dbdump: entry {path!r} ships in package {package!r}, "
                "which pkgforge.cfg does not declare"
            )
        group.append((path, entry))
    return list(groups.items())


class DumpFormat(Registered, abc.ABC):
    """A packaging-manifest format, selected by ``dbdump -f NAME``.

    A subclass registers by declaring its own ``NAME`` (plus, optionally,
    ``ALIASES``); see :class:`pkgforge._registry.Registered`. Subclass
    :class:`PerEntryFormat` or :class:`MultiArtifactFormat` -- they supply
    :meth:`check_output` and :meth:`write` for their respective output shape --
    or this class, supplying both, for a format with another shape.

    ``skeleton`` (``dbdump --skeleton``) asks a format with :attr:`SKELETON` to
    write the files its user edits as well.
    """

    NAME: typing.ClassVar[str] = ""
    ALIASES: typing.ClassVar[typing.Tuple[str, ...]] = ()
    #: Whether the format writes a skeleton with ``skeleton=True``: files its
    #: user edits, written only when missing (or with ``force``).
    SKELETON: typing.ClassVar[bool] = False
    #: Whether everything the format writes is a skeleton: an existing OUTPUT
    #: is kept unless ``force`` is given.
    KEEP_EXISTING: typing.ClassVar[bool] = False
    _registry: typing.ClassVar[typing.Dict[str, type]] = {}
    _KIND = "format"
    _GROUP = "pkgforge.dump_formats"

    def __init__(self, *, skeleton: bool = False) -> None:
        if skeleton and not self.SKELETON:
            raise UsageError(f"{self.NAME} has no skeleton")
        self.skeleton = skeleton

    @abc.abstractmethod
    def render(self, entries: FileEntries, context: DumpContext) -> Rendered:
        """Render the (already validated) ``entries`` to bytes, writing nothing."""

    @abc.abstractmethod
    def check_output(self, output: Path) -> None:
        """Raise :class:`UnsupportedOutputError` if ``output`` is the wrong
        shape for this format."""

    @abc.abstractmethod
    def write(
        self, rendered: Rendered, output: Path, *, force: bool = False
    ) -> typing.List[Path]:
        """Write ``rendered`` to ``output``, whole or not at all; return the
        files written. A skeleton file that exists is kept (and not returned)
        unless ``force``."""


class PerEntryFormat(DumpFormat):
    """A format that renders one line per DB entry.

    Given a file (or stdout) it renders the entries of the one package that
    has any; more than one package is a
    :class:`~pkgforge.exceptions.UsageError`. Given an existing directory it
    renders :attr:`PACKAGE_FILE` for every declared package, so a package with
    no entry still gets its (possibly empty) list.
    """

    #: The file written into a directory, per package.
    PACKAGE_FILE: typing.ClassVar[str] = "files-{package}.txt"
    #: What an empty package's file holds.
    EMPTY: typing.ClassVar[bytes] = b""

    @abc.abstractmethod
    def render_entry(self, path: str, entry: FileEntry) -> bytes:
        """Render one DB entry to its line of bytes."""

    def _render_lines(self, entries: FileEntries) -> bytes:
        return b"".join(self.render_entry(path, entry) for path, entry in entries)

    def render(
        self, entries: FileEntries, context: DumpContext
    ) -> typing.Union[bytes, typing.Dict[str, bytes]]:
        groups = _packages(entries, context.config)
        if context.output is not None and context.output.is_dir():
            if context.config.package is None:
                raise UsageError(
                    f"writing {self.PACKAGE_FILE.replace('{package}', 'PACKAGE')} "
                    "needs [package] name"
                )
            return {
                self.PACKAGE_FILE.format(package=name): (
                    self._render_lines(group) if group else self.EMPTY
                )
                for name, group in groups
            }
        used = [(name, group) for name, group in groups if group]
        if len(used) > 1:
            names = ", ".join(name for name, _group in used)
            raise UsageError(
                f"dbdump: the DB holds entries of {len(used)} packages ({names}): "
                f"give a directory OUTPUT for one "
                f"{self.PACKAGE_FILE.replace('{package}', 'PACKAGE')} each"
            )
        return self._render_lines(used[0][1]) if used else self._render_lines([])

    def check_output(self, output: Path) -> None:
        """Any path fits: a file, or a directory for one list per package."""

    def write(
        self, rendered: Rendered, output: Path, *, force: bool = False
    ) -> typing.List[Path]:
        if isinstance(rendered, bytes):
            _write_files({output: rendered})
            return [output]
        files = {output / name: data for name, data in rendered.items()}
        output.mkdir(parents=True, exist_ok=True)
        _write_files(files)
        return list(files)


class MultiArtifactFormat(DumpFormat):
    """A format that renders several named artifacts into a directory."""

    @abc.abstractmethod
    def render(
        self, entries: FileEntries, context: DumpContext
    ) -> typing.Dict[str, bytes]:
        """Render every artifact this format produces: ``{filename: bytes}``."""

    def check_output(self, output: Path) -> None:
        if output.exists() and not output.is_dir():
            raise UnsupportedOutputError(
                f"{self.NAME} writes several files; OUTPUT must be a "
                f"directory or '-', got existing file {output}"
            )

    def write(
        self, rendered: Rendered, output: Path, *, force: bool = False
    ) -> typing.List[Path]:
        artifacts = typing.cast(typing.Dict[str, bytes], rendered)
        files = {output / name: data for name, data in artifacts.items()}
        for directory in {path.parent for path in files}:
            directory.mkdir(parents=True, exist_ok=True)
        _write_files(files)
        return list(files)


def _surviving_entries(
    db: typing.Mapping[str, typing.Optional[FileEntry]],
    exclude: typing.Sequence[str],
    own: typing.Optional[typing.Sequence[str]] = None,
) -> FileEntries:
    """The live entries of ``db`` that no ``exclude`` statement drops,
    sorted by DB path. With ``own``, a directory outside every prefix is
    dropped first."""
    matcher = PathMatch(_statements(exclude))
    entries: FileEntries = []
    for path, entry in db.items():
        if entry is None:
            continue
        if own is not None and entry["type"] == FileType.Directory:
            if not _owned(path, own):
                continue
        if exclude and matcher.match(path, entry):
            continue
        entries.append((path, entry))
    if db and exclude and not entries:
        live = sum(1 for entry in db.values() if entry is not None)
        _LOGGER.warning(
            "0 of %d entries survived --exclude filtering; dumping an empty manifest",
            live,
        )
    # Code-point order by DB path, which rpm and dh_install ignore, so a
    # staged tree gives byte-identical manifests on any filesystem/backend.
    entries.sort(key=lambda pathentry: pathentry[0])
    return entries


def _prepare(
    fmt: str,
    output: typing.Optional[StrPath],
    *,
    skeleton: bool = False,
    force: bool = False,
) -> typing.Tuple[DumpFormat, typing.Optional[Path]]:
    """The format instance for ``fmt`` and the OUTPUT path (``None`` for
    stdout), after checking the flags and the output fit the format.

    Raises :class:`~pkgforge.exceptions.UsageError` for an unknown format,
    ``skeleton`` with a format that has none or without a directory OUTPUT, and
    ``force`` where nothing is a skeleton;
    :class:`~pkgforge.exceptions.UnsupportedOutputError` for an OUTPUT of the
    wrong shape.
    """
    fmt_cls = DumpFormat.get(fmt)
    if force and not (skeleton or fmt_cls.KEEP_EXISTING):
        raise UsageError("--force applies to -f rpmspec and --skeleton")
    instance = fmt_cls(skeleton=True) if skeleton else fmt_cls()
    target = None if output is None else Path(os.fspath(output))
    if skeleton and target is None:
        raise UsageError("--skeleton writes into a directory")
    if target is not None:
        instance.check_output(target)
    return instance, target


def dump(
    db: typing.Mapping[str, typing.Optional[FileEntry]],
    fmt: str,
    output: typing.Optional[StrPath] = None,
    *,
    exclude: typing.Sequence[str] = (),
    own: typing.Optional[typing.Sequence[str]] = None,
    config: typing.Optional[ProjectConfig] = None,
    config_path: typing.Optional[StrPath] = None,
    skeleton: bool = False,
    force: bool = False,
) -> Rendered:
    """Render the live entries of ``db`` with the format ``fmt``.

    ``fmt`` is a registered format name or alias. Removed entries (``None``)
    and entries matched by an ``exclude`` statement are dropped. With ``own``
    (``/``-rooted prefixes), so is every directory entry that is neither a
    prefix nor below one; entries of other types are untouched. The rest are
    sorted by path and validated. ``config`` names the packages the entries
    ship in (default: an empty configuration, one unnamed package) and
    ``config_path`` is the file it was read from, for a format that points at
    it. With ``output`` the result is also written whole: a file for a
    per-entry format (or a directory holding one list per package), a
    directory (created if missing) for a multi-artifact one.

    With ``skeleton`` a format that has one (``debian``) also writes the files
    its user edits, into a directory ``output``. A skeleton file, or for
    ``rpmspec`` the spec, that already exists is kept with a warning unless
    ``force``.

    Returns the rendered bytes (a ``bytes`` value, or ``{filename: bytes}``).
    Raises :class:`~pkgforge.exceptions.UsageError` for an unknown format or
    exclude statement, an entry of a package ``config`` does not declare, a
    format that needs ``[package] name`` or other keys, ``skeleton`` with a
    format that has none or without a directory ``output``, or ``force`` where
    nothing is a skeleton;
    :class:`~pkgforge.exceptions.UnsupportedOutputError`
    for an ``output`` of the wrong shape (checked before anything is
    rendered), :class:`~pkgforge.exceptions.InvalidEntryError` for an entry no
    format can render and :class:`~pkgforge.exceptions.DumpError` for one this
    format cannot.
    """
    instance, target = _prepare(fmt, output, skeleton=skeleton, force=force)
    entries = _surviving_entries(db, exclude, own)
    _validate_entries(entries)
    context = DumpContext(
        config=config if config is not None else ProjectConfig(),
        config_path=None if config_path is None else Path(os.fspath(config_path)),
        output=target,
    )
    rendered = instance.render(entries, context)
    if target is not None:
        for written in instance.write(rendered, target, force=force):
            _LOGGER.info("Wrote %s", written)
    return rendered


# --------------------------------------------------------------------------
# Built-in formats: importing each module registers its format class.
# --------------------------------------------------------------------------

from ._rpm import RPMSpecFiles, RPMSpecFilesPre419  # noqa: E402
from ._rpmspec import RPMSpec, RPMSpecPre419  # noqa: E402
from ._debian import DebianFiles  # noqa: E402

__all__ = [
    "DebianFiles",
    "DumpContext",
    "DumpFormat",
    "MultiArtifactFormat",
    "PerEntryFormat",
    "RPMSpec",
    "RPMSpecFiles",
    "RPMSpecFilesPre419",
    "RPMSpecPre419",
    "Rendered",
    "dump",
]
