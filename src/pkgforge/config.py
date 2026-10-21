"""The project configuration file, ``pkgforge.cfg``: a TOML document describing
one package, read into the frozen dataclasses below.

:func:`load` reads and validates a file; :meth:`ProjectConfig.from_dict` checks
a mapping that is already loaded. Every problem is a
:class:`~pkgforge.exceptions.UsageError` naming the file and the key.
``${table.key}`` in any text value is replaced by that key of the file; ``$${``
is a literal ``${``. The ``anycfg`` library is imported only when a file is read.
"""

from __future__ import annotations

import os
import posixpath
import re
import typing
from dataclasses import dataclass, field
from pathlib import Path

from .entry import (
    AUTO,
    DEFAULT,
    FileType,
    StrPath,
    _filetype,
    check_meta,
    normalize_mode,
    normalize_name,
)
from .exceptions import ExcludeSyntaxError, UsageError
from .exclude import PathMatchStmt

__all__ = [
    "EntryDefaults",
    "EntryRule",
    "PackageInfo",
    "ProjectConfig",
    "StageStep",
    "load",
]

_SENTINELS = (DEFAULT, AUTO)


class _Text(str):
    """The type of a text field: it refuses any other TOML value.

    ``anycfg.hydrate`` otherwise turns the integer ``0o644`` into ``"420"`` and
    ``true`` into ``"True"``, so a mode written without quotes would be recorded
    as a different mode. The value kept is a plain ``str``: YAML's safe dumper
    cannot write a ``str`` subclass into the file DB.
    """

    @classmethod
    def _load_(cls, value: typing.Any) -> str:
        if not isinstance(value, str):
            raise UsageError(f"expected a quoted string, not {type(value).__name__}")
        return str(value)


def _mode(value: typing.Optional[str], field_name: str) -> typing.Optional[str]:
    """``value`` as 1-4 octal digits; a sentinel is refused, because a file
    states values and never asks for one to be read from disk."""
    if value is None:
        return None
    try:
        mode = normalize_mode(value)
    except UsageError as exc:
        raise UsageError(f"{field_name}: {exc}") from None
    if mode in _SENTINELS:
        raise UsageError(
            f"{field_name}: invalid mode {value!r}: expected 1-4 octal digits"
        )
    return mode


def _name(value: typing.Optional[str], field_name: str) -> typing.Optional[str]:
    """``value`` as an account name; a sentinel is refused as for :func:`_mode`."""
    if value is None:
        return None
    try:
        name = normalize_name(value, field_name)
    except UsageError as exc:
        raise UsageError(f"{field_name}: {exc}") from None
    if name in _SENTINELS:
        raise UsageError(
            f"{field_name}: invalid {field_name} {value!r}: expected an account name"
        )
    return name


def _statements(
    values: typing.Iterable[str], field_name: str
) -> typing.Tuple[str, ...]:
    """``values`` unchanged once every one parses as a ``-X`` statement."""
    out = tuple(values)
    for text in out:
        try:
            PathMatchStmt.parse(text)
        except ExcludeSyntaxError as exc:
            raise UsageError(f"{field_name}: {exc}") from None
    return out


def _set(obj: object, name: str, value: object) -> None:
    """Store a normalized value on a frozen dataclass."""
    object.__setattr__(obj, name, value)


@dataclass(frozen=True)
class PackageInfo:
    """The ``[package]`` table: facts about the package that other values can
    name once with ``${package.name}``. ``name`` is required."""

    name: _Text
    version: typing.Optional[_Text] = None
    release: typing.Optional[_Text] = None
    arch: typing.Optional[_Text] = None
    summary: typing.Optional[_Text] = None
    license: typing.Optional[_Text] = None
    maintainer: typing.Optional[_Text] = None
    description: typing.Optional[_Text] = None
    depends: typing.Tuple[_Text, ...] = ()


@dataclass(frozen=True)
class EntryDefaults:
    """The ``[defaults]`` table: the owner, group and modes recorded for an
    entry that no flag and no rule sets. ``file_mode`` applies to a regular
    file, ``exec_mode`` to one with an execute bit and ``dir_mode`` to a
    directory. Modes are 1-4 octal digits; owner and group are account names."""

    owner: typing.Optional[_Text] = None
    group: typing.Optional[_Text] = None
    file_mode: typing.Optional[_Text] = None
    exec_mode: typing.Optional[_Text] = None
    dir_mode: typing.Optional[_Text] = None

    def __post_init__(self) -> None:
        _set(self, "owner", _name(self.owner, "owner"))
        _set(self, "group", _name(self.group, "group"))
        for name in ("file_mode", "exec_mode", "dir_mode"):
            _set(self, name, _mode(getattr(self, name), name))


@dataclass(frozen=True)
class EntryRule:
    """One ``[[rules]]`` element: ``mode``, ``owner`` and ``group`` for the
    entries whose DB path matches a ``match`` statement (the ``-X`` grammar).
    ``match`` is not empty."""

    match: typing.Tuple[_Text, ...]
    mode: typing.Optional[_Text] = None
    owner: typing.Optional[_Text] = None
    group: typing.Optional[_Text] = None

    def __post_init__(self) -> None:
        if not self.match:
            raise UsageError("match: needs at least one statement")
        _set(self, "match", _statements(self.match, "match"))
        _set(self, "mode", _mode(self.mode, "mode"))
        _set(self, "owner", _name(self.owner, "owner"))
        _set(self, "group", _name(self.group, "group"))


@dataclass(frozen=True)
class StageStep:
    """One ``[[stage]]`` element: a step that stages one thing into the build
    root. Exactly one of ``src`` (install a source at ``dest``), ``dir``,
    ``file`` or ``symlink`` (with ``target``) names it. ``type``, ``into``,
    ``decompress``, ``method``, ``exclude`` and ``record_tree`` are the
    ``install`` options and apply to ``src`` only."""

    src: typing.Optional[_Text] = None
    dest: typing.Optional[_Text] = None
    dir: typing.Optional[_Text] = None
    file: typing.Optional[_Text] = None
    symlink: typing.Optional[_Text] = None
    target: typing.Optional[_Text] = None
    mode: typing.Optional[_Text] = None
    owner: typing.Optional[_Text] = None
    group: typing.Optional[_Text] = None
    meta: typing.Dict[_Text, _Text] = field(default_factory=dict)
    type: typing.Optional[_Text] = None
    into: bool = True
    decompress: typing.Optional[_Text] = None
    method: typing.Optional[_Text] = None
    exclude: typing.Tuple[_Text, ...] = ()
    record_tree: bool = False

    def __post_init__(self) -> None:
        # Imported here: project.py imports this module.
        from ._install.decompress import _resolve_kind
        from .project import _INSTALL_METHODS

        named = [
            n for n in ("src", "dir", "file", "symlink") if getattr(self, n) is not None
        ]
        if len(named) != 1:
            raise UsageError("step needs exactly one of src, dir, file, symlink")
        for name in (*named, "dest", "target"):
            if getattr(self, name) == "":
                raise UsageError(f"{name}: must not be empty")
        has_src = self.src is not None
        if has_src and self.dest is None:
            raise UsageError("dest: required with src")
        if not has_src and self.dest is not None:
            raise UsageError("dest: only valid with src")
        if self.symlink is not None and self.target is None:
            raise UsageError("target: required with symlink")
        if self.symlink is None and self.target is not None:
            raise UsageError("target: only valid with symlink")
        if self.symlink is not None and self.mode is not None:
            raise UsageError("mode: not valid with symlink")
        if self.symlink is not None and "target" in self.meta:
            raise UsageError("meta: must not hold 'target' with symlink")
        if not has_src:
            for name, default in (
                ("type", None),
                ("into", True),
                ("decompress", None),
                ("method", None),
                ("exclude", ()),
                ("record_tree", False),
            ):
                if getattr(self, name) != default:
                    raise UsageError(f"{name}: only valid with src")

        _set(self, "mode", _mode(self.mode, "mode"))
        _set(self, "owner", _name(self.owner, "owner"))
        _set(self, "group", _name(self.group, "group"))
        check_meta(self.meta)
        if self.type is not None:
            try:
                kind = _filetype(self.type)
            except UsageError as exc:
                raise UsageError(f"type: {exc}") from None
            if not isinstance(kind, FileType):
                raise UsageError(f"type: invalid type {self.type!r}")
            _set(self, "type", kind.value)
        if self.decompress is not None and self.decompress != "auto":
            try:
                _set(self, "decompress", _resolve_kind(self.decompress))
            except UsageError as exc:
                raise UsageError(f"decompress: {exc}") from None
        if self.method is not None and self.method not in _INSTALL_METHODS:
            raise UsageError(
                f"method: unknown install method {self.method!r}; choose from "
                f"{', '.join(_INSTALL_METHODS)}"
            )
        _set(self, "exclude", _statements(self.exclude, "exclude"))


def _own_prefix(prefix: str) -> str:
    """``prefix`` normalized, or :class:`UsageError` unless it is a ``/``-rooted
    path below ``/`` with no ``..``."""
    if not prefix.startswith("/") or ".." in prefix.split("/"):
        raise UsageError(f"own: {prefix!r} must be a '/'-rooted path without '..'")
    normal = re.sub("^/+", "/", posixpath.normpath(prefix))
    if normal == "/":
        raise UsageError("own: '/' is not a package directory")
    return normal


def _owned(path: str, prefixes: typing.Sequence[str]) -> bool:
    """Whether the DB path ``path`` is one of ``prefixes`` or below one."""
    return any(path == p or path.startswith(p + "/") for p in prefixes)


@dataclass(frozen=True)
class ProjectConfig:
    """The whole ``pkgforge.cfg`` document.

    ``buildroot``, ``db`` and ``db_format`` set the options of the same name
    (``db = "-"`` means no file DB), ``own`` lists the directory prefixes the
    package owns, and the tables are ``[package]``, ``[defaults]``,
    ``[[rules]]`` and ``[[stage]]``. A key that is not listed here is an error.
    Build one with :func:`load` or :meth:`from_dict`.
    """

    buildroot: typing.Optional[_Text] = None
    db: typing.Optional[_Text] = None
    db_format: typing.Optional[_Text] = None
    own: typing.Tuple[_Text, ...] = ()
    package: typing.Optional[PackageInfo] = None
    defaults: EntryDefaults = field(default_factory=EntryDefaults)
    rules: typing.Tuple[EntryRule, ...] = ()
    stage: typing.Tuple[StageStep, ...] = ()

    def __post_init__(self) -> None:
        _set(self, "own", tuple(_own_prefix(prefix) for prefix in self.own))

    @classmethod
    def from_dict(cls, data: typing.Mapping[str, typing.Any]) -> "ProjectConfig":
        """The configuration described by ``data``, a mapping shaped like the
        file (``${...}`` already replaced).

        Raises:
            UsageError: a key is unknown, a value has the wrong type or is not
                allowed; the text ends with ``[at KEY (ProjectConfig)]``.
        """
        import anycfg

        try:
            return anycfg.hydrate(cls, data, extra="forbid")
        except anycfg.ConfigError as exc:
            raise UsageError(str(exc)) from exc


def _join(base: Path, value: typing.Any) -> typing.Any:
    """``value`` joined to ``base`` when it is a non-empty relative path text;
    anything else is left for validation to report."""
    if isinstance(value, str) and value and not Path(value).is_absolute():
        return str(base / value)
    return value


def _read(path: StrPath) -> typing.Dict[str, typing.Any]:
    """The mapping in the file at ``path``, ``${...}`` replaced and the paths
    it holds (``buildroot``, ``db``, each ``stage`` ``src``) made relative to
    the file's directory.

    Raises:
        UsageError: the file is malformed or names an undefined ``${...}``.
        OSError: the file cannot be read.
    """
    import anycfg

    # One file describes one package: a YAML !include would read a file the
    # user never named, so includes are refused.
    loader = anycfg.Loader(
        interpolate=anycfg.VarsInterpolator(strict=True),
        policy=anycfg.Policy.untrusted(allow_includes=False),
    )
    try:
        data = loader.load(path, format="cfg")
    except anycfg.ParseError as exc:
        raise UsageError(str(exc)) from exc
    except anycfg.ConfigError as exc:
        raise UsageError(f"{os.fspath(path)}: {exc}") from exc
    if not isinstance(data, dict):
        raise UsageError(f"{os.fspath(path)}: the top level must be a table")

    base = Path(path).parent
    out = dict(data)
    for key in ("buildroot", "db"):
        if key in out and out[key] != "-":
            out[key] = _join(base, out[key])
    stage = out.get("stage")
    if isinstance(stage, list):
        steps = []
        for step in stage:
            if isinstance(step, dict) and "src" in step:
                step = {**step, "src": _join(base, step["src"])}
            steps.append(step)
        out["stage"] = steps
    return out


def load(path: StrPath) -> ProjectConfig:
    """Read and validate the configuration file at ``path``.

    Relative ``buildroot``, ``db`` (unless ``"-"``) and ``[[stage]]`` ``src``
    values are taken relative to the file's directory.

    Raises:
        UsageError: the file is malformed, names an undefined ``${...}``, holds
            an unknown key or a value that is not allowed; the text starts
            with ``path``.
        FileNotFoundError: there is no such file.
        OSError: the file cannot be read.
    """
    data = _read(path)
    try:
        return ProjectConfig.from_dict(data)
    except UsageError as exc:
        raise UsageError(f"{os.fspath(path)}: {exc}") from exc
