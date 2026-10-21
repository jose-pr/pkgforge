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
    BASE_GROUPS,
    BASE_USERS,
    DEFAULT,
    FileType,
    StrPath,
    _KIND_TYPES,
    _filetype,
    check_meta,
    normalize_caps,
    normalize_kind,
    normalize_mode,
    normalize_name,
    normalize_package,
)
from .exceptions import ExcludeSyntaxError, UsageError
from .exclude import PathMatchStmt

__all__ = [
    "ApkInfo",
    "EntryDefaults",
    "DebianInfo",
    "EntryRule",
    "GroupAccount",
    "PackageInfo",
    "PacmanInfo",
    "ProjectConfig",
    "RpmInfo",
    "StageStep",
    "SubPackageInfo",
    "UserAccount",
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


def _kind(value: typing.Optional[str]) -> typing.Optional[str]:
    """``value`` as a :class:`~pkgforge.entry.FileKind` value; ``-`` is refused
    as for :func:`_mode`."""
    if value is None:
        return None
    try:
        kind = normalize_kind(value)
    except UsageError as exc:
        raise UsageError(f"kind: {exc}") from None
    if kind == DEFAULT:
        raise UsageError(f"kind: invalid kind {value!r}: expected a kind name")
    return kind


def _caps(value: typing.Optional[str]) -> typing.Optional[str]:
    """``value`` as one ``setcap`` clause; ``-`` is refused as for :func:`_mode`."""
    if value is None:
        return None
    try:
        caps = normalize_caps(value)
    except UsageError as exc:
        raise UsageError(f"caps: {exc}") from None
    if caps == DEFAULT:
        raise UsageError(f"caps: invalid caps {value!r}: expected a setcap clause")
    return caps


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


def _package(value: typing.Optional[str]) -> typing.Optional[str]:
    """``value`` as a package name; ``-`` is refused as for :func:`_mode`."""
    if value is None:
        return None
    try:
        name = normalize_package(value)
    except UsageError as exc:
        raise UsageError(f"package: {exc}") from None
    if name == DEFAULT:
        raise UsageError(f"package: invalid package {value!r}: expected a package name")
    return name


_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]+")
_VERSION = re.compile(r"[0-9][A-Za-z0-9.+~]*")
_RELEASE = re.compile(r"[A-Za-z0-9.+~]+")
_MAINTAINER = re.compile(r"[^<>,\x00-\x1f\x7f]+ <[^<>\s]+@[^<>\s]+>")
_SECTION = re.compile(r"[a-z0-9][a-z0-9/+.-]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s")
_DESCRIPTION_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
_PRIORITIES = ("required", "important", "standard", "optional", "extra")


def _matches(
    value: typing.Optional[str],
    field_name: str,
    pattern: "re.Pattern[str]",
    expected: str,
) -> None:
    """Raise unless ``value`` is ``None`` or fully matches ``pattern``."""
    if value is not None and not pattern.fullmatch(value):
        raise UsageError(f"{field_name}: {value!r} is not {expected}")


def _one_line(value: typing.Optional[str], field_name: str) -> None:
    """Raise if ``value`` holds a control character, a newline included."""
    if value is not None and _CONTROL.search(value):
        raise UsageError(f"{field_name}: must be one line with no control character")


def _items(
    values: typing.Iterable[str], field_name: str, comma: bool = True
) -> typing.Tuple[str, ...]:
    """``values`` unchanged when each item is non-empty, one line and, unless
    ``comma``, free of ``,``."""
    out = tuple(values)
    for item in out:
        if not item:
            raise UsageError(f"{field_name}: an item must not be empty")
        _one_line(item, field_name)
        if not comma and "," in item:
            raise UsageError(f"{field_name}: {item!r} must not contain ','")
    return out


def _description(value: typing.Optional[str]) -> None:
    """Raise if ``value`` holds a control character other than newline."""
    if value is not None and _DESCRIPTION_CONTROL.search(value):
        raise UsageError("description: must hold no control character but newline")


def _arch(value: typing.Optional[str]) -> None:
    """Raise unless ``value`` is ``None``, ``all`` or ``any``."""
    if value is not None and value not in ("all", "any"):
        raise UsageError(f"arch: {value!r} is not 'all' or 'any'")


def _package_name(value: str) -> None:
    _matches(value, "name", _PACKAGE_NAME, "a package name ([a-z0-9][a-z0-9+.-]+)")


@dataclass(frozen=True)
class RpmInfo:
    """The ``rpm`` table of a package: facts for the RPM format only.
    ``requires`` lists one ``Requires:`` item each."""

    requires: typing.Tuple[_Text, ...] = ()

    def __post_init__(self) -> None:
        _set(self, "requires", _items(self.requires, "requires"))


@dataclass(frozen=True)
class DebianInfo:
    """The ``debian`` table of a package: facts for the Debian format only.
    ``depends`` lists one dependency each (no ``,``); ``section`` and
    ``priority`` are control fields."""

    depends: typing.Tuple[_Text, ...] = ()
    section: typing.Optional[_Text] = None
    priority: typing.Optional[_Text] = None

    def __post_init__(self) -> None:
        _set(self, "depends", _items(self.depends, "depends", comma=False))
        _matches(self.section, "section", _SECTION, "a section name")
        if self.priority is not None and self.priority not in _PRIORITIES:
            raise UsageError(
                f"priority: {self.priority!r} is not one of {', '.join(_PRIORITIES)}"
            )


def _one_word_items(values: typing.Iterable[str], name: str) -> typing.Tuple[str, ...]:
    items = _items(values, name)
    for item in items:
        if _WHITESPACE.search(item):
            raise UsageError(f"{name}: {item!r} must be one word")
    return items


@dataclass(frozen=True)
class PacmanInfo:
    """The ``pacman`` table of a package: facts for the Arch format only.
    ``depends`` lists one ``depend`` item each, a single word such as
    ``glibc`` or ``bash>=5``."""

    depends: typing.Tuple[_Text, ...] = ()

    def __post_init__(self) -> None:
        _set(self, "depends", _one_word_items(self.depends, "depends"))


@dataclass(frozen=True)
class ApkInfo:
    """The ``apk`` table of a package: facts for the Alpine format only.
    ``depends`` lists one ``depend`` item each, a single word such as
    ``musl`` or ``bash>=5``."""

    depends: typing.Tuple[_Text, ...] = ()

    def __post_init__(self) -> None:
        _set(self, "depends", _one_word_items(self.depends, "depends"))


@dataclass(frozen=True)
class PackageInfo:
    """The ``[package]`` table: the main (and source) package, whose facts other
    values can name once with ``${package.KEY}``. ``name`` is required and a
    package name (``[a-z0-9][a-z0-9+.-]+``); ``arch`` is ``all`` or ``any``;
    ``maintainer`` reads ``Name <address>``. ``rpm``, ``debian``, ``pacman`` and
    ``apk`` hold the facts only that format uses."""

    name: _Text
    version: typing.Optional[_Text] = None
    release: typing.Optional[_Text] = None
    arch: typing.Optional[_Text] = None
    summary: typing.Optional[_Text] = None
    license: typing.Optional[_Text] = None
    maintainer: typing.Optional[_Text] = None
    description: typing.Optional[_Text] = None
    homepage: typing.Optional[_Text] = None
    rpm: RpmInfo = field(default_factory=RpmInfo)
    debian: DebianInfo = field(default_factory=DebianInfo)
    pacman: PacmanInfo = field(default_factory=PacmanInfo)
    apk: ApkInfo = field(default_factory=ApkInfo)

    def __post_init__(self) -> None:
        _package_name(self.name)
        _matches(self.version, "version", _VERSION, "a version ([0-9][A-Za-z0-9.+~]*)")
        _matches(self.release, "release", _RELEASE, "a release ([A-Za-z0-9.+~]+)")
        _arch(self.arch)
        _matches(self.maintainer, "maintainer", _MAINTAINER, "'Name <address>'")
        for name in ("summary", "license", "homepage"):
            _one_line(getattr(self, name), name)
        _description(self.description)


@dataclass(frozen=True)
class SubPackageInfo:
    """One ``[[packages]]`` element: a subpackage built from the same source.
    ``name`` is its full package name (``hello-doc``); the other keys are as in
    :class:`PackageInfo`, and an unset ``arch`` is the main package's."""

    name: _Text
    summary: typing.Optional[_Text] = None
    description: typing.Optional[_Text] = None
    arch: typing.Optional[_Text] = None
    rpm: RpmInfo = field(default_factory=RpmInfo)
    debian: DebianInfo = field(default_factory=DebianInfo)
    pacman: PacmanInfo = field(default_factory=PacmanInfo)
    apk: ApkInfo = field(default_factory=ApkInfo)

    def __post_init__(self) -> None:
        _package_name(self.name)
        _one_line(self.summary, "summary")
        _description(self.description)
        _arch(self.arch)


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
    """One ``[[rules]]`` element: ``mode``, ``owner``, ``group``, ``kind``,
    ``caps`` and ``package`` for the entries whose DB path matches a ``match`` statement (the
    ``-X`` grammar). ``match`` is not empty. A ``kind`` other than ``file`` or
    ``ghost`` fits regular files only, so such a rule's ``match`` should say
    ``(?type:file)``."""

    match: typing.Tuple[_Text, ...]
    mode: typing.Optional[_Text] = None
    owner: typing.Optional[_Text] = None
    group: typing.Optional[_Text] = None
    kind: typing.Optional[_Text] = None
    caps: typing.Optional[_Text] = None
    package: typing.Optional[_Text] = None

    def __post_init__(self) -> None:
        if not self.match:
            raise UsageError("match: needs at least one statement")
        _set(self, "match", _statements(self.match, "match"))
        _set(self, "mode", _mode(self.mode, "mode"))
        _set(self, "owner", _name(self.owner, "owner"))
        _set(self, "group", _name(self.group, "group"))
        _set(self, "kind", _kind(self.kind))
        _set(self, "caps", _caps(self.caps))
        _set(self, "package", _package(self.package))


@dataclass(frozen=True)
class StageStep:
    """One ``[[stage]]`` element: a step that stages one thing into the build
    root. Exactly one of ``src`` (install a source at ``dest``), ``dir``,
    ``file`` or ``symlink`` (with ``target``) names it. ``kind`` and
    ``package`` apply to every step, ``caps`` to ``src`` and ``file`` steps; ``type``, ``into``,
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
    kind: typing.Optional[_Text] = None
    caps: typing.Optional[_Text] = None
    package: typing.Optional[_Text] = None
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
        if self.caps is not None and self.src is None and self.file is None:
            raise UsageError("caps: only valid with src or file")
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
        _set(self, "kind", _kind(self.kind))
        _set(self, "caps", _caps(self.caps))
        _set(self, "package", _package(self.package))
        no_source = "symlink" if self.symlink is not None else "directory"
        if (
            (self.symlink is not None or self.dir is not None)
            and self.kind is not None
            and no_source not in _KIND_TYPES[self.kind]
        ):
            raise UsageError(
                f"kind: {self.kind} needs a regular file, not a {no_source}"
            )
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


#: The names a package may create: what ``systemd-sysusers`` accepts.
_CREATED_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,30}")


def _declared_name(value: str, created: bool, base: typing.FrozenSet[str]) -> str:
    """``value`` as the name of a declared account; ``base`` is the set of
    base-passwd names of its kind, which need no declaration."""
    name = _name(value, "name")
    assert name is not None  # value is not None
    if created and not _CREATED_NAME.fullmatch(name):
        raise UsageError(
            f"name: {name!r} cannot be created: expected "
            "[a-z_][a-z0-9_-]{0,30}; use create = false for an existing account"
        )
    if name in base:
        raise UsageError(f"name: {name!r} is a base-passwd account: it needs no entry")
    return name


def _absolute_word(
    value: typing.Optional[str], field_name: str
) -> typing.Optional[str]:
    """``value`` when it is an absolute path with no whitespace, backslash or
    control character (``systemd-sysusers`` drops an unquoted backslash)."""
    if value is None:
        return None
    if not value.startswith("/") or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise UsageError(
            f"{field_name}: {value!r} must be an absolute path with no "
            "whitespace or backslash"
        )
    return value


@dataclass(frozen=True)
class UserAccount:
    """One ``[[users]]`` element: an account that owns files of the package.

    With ``create = true`` the package creates it at install time (a
    ``systemd-sysusers`` ``u`` line, which also makes the group of the same
    name) with ``description`` as its GECOS text, ``home`` and ``shell``, and
    adds it to the supplementary ``groups``. With ``create = false`` the
    account exists on the target already, made by a dependency: no line is
    written and only ``name`` may be set, but its files are still owned by it
    at install time. Either way the name may own recorded files.
    """

    name: _Text
    description: typing.Optional[_Text] = None
    home: typing.Optional[_Text] = None
    shell: typing.Optional[_Text] = None
    groups: typing.Tuple[_Text, ...] = ()
    create: bool = True

    def __post_init__(self) -> None:
        name = _declared_name(self.name, self.create, BASE_USERS)
        if self.create and name in BASE_GROUPS:
            raise UsageError(
                f"name: {name!r} is a base-passwd group, which its own group "
                "would clash with"
            )
        _set(self, "name", name)
        if not self.create:
            # No sysusers line is written for an existing account, so these
            # would change nothing.
            for field_name in ("description", "home", "shell", "groups"):
                if getattr(self, field_name):
                    raise UsageError(
                        f"{field_name}: only valid with create = true: an "
                        "existing account is not changed"
                    )
        if self.description is not None and re.search(
            r'["\\\x00-\x1f\x7f]', self.description
        ):
            # systemd-sysusers ends the quoted text at `"` and drops a backslash.
            raise UsageError(
                "description: must hold no control character, double quote or "
                "backslash"
            )
        _set(self, "home", _absolute_word(self.home, "home"))
        _set(self, "shell", _absolute_word(self.shell, "shell"))
        _set(self, "groups", tuple(_name(group, "groups") for group in self.groups))


@dataclass(frozen=True)
class GroupAccount:
    """One ``[[groups]]`` element: a group that owns files of the package.

    With ``create = true`` the package creates it at install time (a
    ``systemd-sysusers`` ``g`` line); with ``create = false`` it exists on the
    target already, and its files are still owned by it at install time. The
    group of a created user needs no element: it is implied.
    """

    name: _Text
    create: bool = True

    def __post_init__(self) -> None:
        _set(self, "name", _declared_name(self.name, self.create, BASE_GROUPS))


def _accounts(
    config: "ProjectConfig",
) -> typing.Tuple[typing.FrozenSet[str], typing.FrozenSet[str]]:
    """The owner names and the group names ``config`` allows an entry to
    carry: the base-passwd sets, what is declared, and the group of each
    created user."""
    users = BASE_USERS | {user.name for user in config.users}
    groups = (
        BASE_GROUPS
        | {group.name for group in config.groups}
        | {user.name for user in config.users if user.create}
    )
    return frozenset(users), frozenset(groups)


def _check_account_names(config: "ProjectConfig") -> None:
    """Refuse a declared name that is repeated or clashes, a supplementary
    group nothing defines, and an owner or group the tables name that is
    neither base-passwd nor declared."""
    for table, items in (("users", config.users), ("groups", config.groups)):
        names: typing.Set[str] = set()
        for index, item in enumerate(items):
            if item.name in names:
                raise UsageError(f"{table}[{index}]: {item.name!r} is declared twice")
            names.add(item.name)
    created = {user.name for user in config.users if user.create}
    for index, group in enumerate(config.groups):
        if group.name in created:
            raise UsageError(
                f"groups[{index}]: {group.name!r} is the group of the created user "
                "of the same name: drop the [[groups]] element"
            )
    users, groups = _accounts(config)
    for index, user in enumerate(config.users):
        for name in user.groups:
            if name not in groups:
                raise UsageError(
                    f"users[{index}]: group {name!r} is neither a base-passwd "
                    "group nor declared in [[groups]]"
                )
    if (created or any(g.create for g in config.groups)) and config.package is None:
        raise UsageError(
            "[[users]] and [[groups]] that create an account need [package] name"
        )

    def check(where: str, owner: typing.Optional[str], group: typing.Optional[str]):
        if owner is not None and owner not in users:
            raise UsageError(
                f"{where}: owner {owner!r} is neither a base-passwd account "
                "nor declared in [[users]]"
            )
        if group is not None and group not in groups:
            raise UsageError(
                f"{where}: group {group!r} is neither a base-passwd group "
                "nor declared in [[groups]]"
            )

    check("defaults", config.defaults.owner, config.defaults.group)
    for index, rule in enumerate(config.rules):
        check(f"rules[{index}]", rule.owner, rule.group)
    for index, step in enumerate(config.stage):
        check(f"stage[{index}]", step.owner, step.group)


def _check_packages(config: "ProjectConfig") -> None:
    """Refuse a subpackage that is repeated, named like the main package or
    declared without ``[package]``, and a rule or step that names a package
    nothing declares."""
    if config.packages and config.package is None:
        raise UsageError("[[packages]] need [package]")
    main = None if config.package is None else config.package.name
    names = [] if main is None else [main]
    for index, sub in enumerate(config.packages):
        if sub.name == main:
            raise UsageError(f"packages[{index}]: {sub.name!r} is the [package] name")
        if sub.name in names:
            raise UsageError(f"packages[{index}]: {sub.name!r} is declared twice")
        names.append(sub.name)
    for table, items in (("rules", config.rules), ("stage", config.stage)):
        for index, item in enumerate(items):
            if item.package is not None and item.package not in names:
                raise UsageError(
                    f"{table}[{index}]: package {item.package!r} is neither "
                    "[package] name nor a [[packages]] name"
                )


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
    ``[[packages]]``, ``[[rules]]``, ``[[stage]]``, ``[[users]]`` and
    ``[[groups]]``. A key that
    is not listed here is an error. An owner or group named by ``[defaults]``,
    a rule or a step must be a base-passwd account (``BASE_USERS``,
    ``BASE_GROUPS``) or declared in ``[[users]]``/``[[groups]]``; a created
    account needs ``[package] name``. Build one with :func:`load` or
    :meth:`from_dict`.
    """

    buildroot: typing.Optional[_Text] = None
    db: typing.Optional[_Text] = None
    db_format: typing.Optional[_Text] = None
    own: typing.Tuple[_Text, ...] = ()
    package: typing.Optional[PackageInfo] = None
    packages: typing.Tuple[SubPackageInfo, ...] = ()
    defaults: EntryDefaults = field(default_factory=EntryDefaults)
    rules: typing.Tuple[EntryRule, ...] = ()
    stage: typing.Tuple[StageStep, ...] = ()
    users: typing.Tuple[UserAccount, ...] = ()
    groups: typing.Tuple[GroupAccount, ...] = ()

    def __post_init__(self) -> None:
        _set(self, "own", tuple(_own_prefix(prefix) for prefix in self.own))
        _check_account_names(self)
        _check_packages(self)

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
