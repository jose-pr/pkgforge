"""The Python API for build scripts: :class:`Project` stages files into a build
root, records their install metadata in a file DB and dumps that DB into
packaging manifests.

Everything here is plain Python: nothing prints, exits or reads the command
line, and no file is opened until an operation runs. The ``pkgforge`` command
line is a wrapper over this class.
"""

from __future__ import annotations

import contextlib
import logging
import os
import posixpath
import typing
from pathlib import Path, PurePath, PurePosixPath

from . import dbdump
from ._install import _Installer
from ._scan import scan as _scan
from ._sysfiles import SYSUSERS_DIR, TMPFILES_DIR, sysusers_text, tmpfiles_text
from ._verify import verify as _verify
from .config import (
    EntryDefaults,
    EntryRule,
    ProjectConfig,
    StageStep,
    _accounts,
    _owned,
)
from .config import load as _load_config
from .db import Db, DbProvider, open_db
from .entry import (
    AUTO,
    DEFAULT,
    FileEntries,
    FileEntry,
    FileRecords,
    FileType,
    StrPath,
    _check_kind_caps,
)
from .exceptions import UsageError
from .exclude import PathMatch, _statements

__all__ = ["Project", "VerifyIssue"]

#: Recognized ``method`` values of :meth:`Project.install`.
_INSTALL_METHODS = ("copy", "link", "move")

#: The entry fields ``[[rules]]`` fill when they are unset (``[defaults]`` has no
#: ``kind``, ``caps`` or ``package``).
_RESOLVED = ("mode", "owner", "group", "kind", "caps", "package")
_UNSET = frozenset((DEFAULT, AUTO))

_INIT_LOGGER = logging.getLogger("pkgforge.initdb")
_COMPACT_LOGGER = logging.getLogger("pkgforge.compact")
_DUMP_LOGGER = logging.getLogger("pkgforge.dbdump")
_MKDIR_LOGGER = logging.getLogger("pkgforge.mkdir")
_SYMLINK_LOGGER = logging.getLogger("pkgforge.symlink")
_STAGE_LOGGER = logging.getLogger("pkgforge.stage")
_SYSFILES_LOGGER = logging.getLogger("pkgforge.sysfiles")
_VERIFY_LOGGER = logging.getLogger("pkgforge.verify")


def _path(value: object, what: str) -> Path:
    """``value`` as a :class:`~pathlib.Path`; ``TypeError`` for a non-path."""
    if isinstance(value, (str, os.PathLike)):
        return Path(os.fspath(value))
    raise TypeError(
        f"{what} must be a str or os.PathLike, not {value.__class__.__name__}"
    )


def _check_db_format(value: typing.Optional[str]) -> typing.Optional[str]:
    """Validate a ``db_format`` against the registered providers, returning
    its canonical name. A falsy value means "from the suffix" and is never
    checked; anything else must be a registered ``NAME`` or alias, so a typo
    fails before any file is staged."""
    if not value:
        return None
    return DbProvider.get(value).NAME


class VerifyIssue(typing.NamedTuple):
    """One difference :meth:`Project.verify` found between the build root and
    the file DB."""

    #: The DB path (``/``-rooted POSIX text).
    path: str
    #: ``"unrecorded"`` (on disk, not in the DB), ``"missing"`` (in the DB, not
    #: on disk), ``"type"`` (the on-disk type differs) or ``"target"`` (a
    #: symlink points elsewhere than the recorded ``target``).
    problem: str
    #: What the DB holds: the entry's type, or the symlink target; ``None`` for
    #: ``"unrecorded"``.
    recorded: typing.Optional[str]
    #: What the disk holds: the type (``"special"`` for a fifo, socket or
    #: device), or the symlink target; ``None`` for ``"missing"``.
    found: typing.Optional[str]


class Project:
    """A build root plus an optional file DB.

    ``buildroot`` is the local directory that maps to ``/`` in the DB. ``db``
    is the file DB: a path (its format is ``db_format``, else inferred from
    the suffix, else JSON Lines), a ready :class:`~pkgforge.db.DbProvider`, or
    ``None`` for no file DB, where operations write nothing and return the
    records they would have written.

    Constructing a :class:`Project` validates its arguments and opens
    nothing. It holds no open resource, so it is not a context manager, and
    it is not thread-safe.

    DB paths (``dest``, a scan ``path``, a DB key) are ``/``-rooted POSIX
    text; local paths are :data:`~pkgforge.entry.StrPath`.

    ``config`` is the parsed ``pkgforge.cfg``
    (:class:`~pkgforge.config.ProjectConfig`); :meth:`from_config` builds a
    project from a file.

    Raises:
        TypeError: ``buildroot`` or ``db`` is not a path (or provider), or
            ``config`` is not a :class:`~pkgforge.config.ProjectConfig`.
        ValueError: ``db`` is ``"-"``, or a provider is given with ``db_format``.
        UsageError: ``db_format`` names no registered format.
    """

    #: Local directory that maps to ``/`` in the DB.
    buildroot: Path
    #: The file DB, or ``None`` when there is none.
    db: typing.Optional[DbProvider]
    #: The project configuration; an empty one when none was given.
    config: ProjectConfig

    _batch_provider: typing.Optional[DbProvider] = None
    # The parsed [[rules]] and whether any rule or default is set, with the
    # config they came from.
    _rules: typing.Optional[
        typing.Tuple[
            ProjectConfig,
            typing.Tuple[typing.Tuple[PathMatch, EntryRule], ...],
            bool,
        ]
    ] = None
    # The owner and group names the config allows, with the config they came from.
    _names: typing.Optional[
        typing.Tuple[ProjectConfig, typing.FrozenSet[str], typing.FrozenSet[str]]
    ] = None
    # The package names the config declares, with the config they came from.
    _packages: typing.Optional[typing.Tuple[ProjectConfig, typing.FrozenSet[str]]] = (
        None
    )
    # Owned parents: the paths the DB held when the operation first needed to
    # know (None until then) and the paths the operation has written since.
    _held: typing.Optional[typing.Set[str]] = None
    _written: typing.Set[str]

    def __init__(
        self,
        buildroot: StrPath,
        db: typing.Optional[typing.Union[StrPath, DbProvider]] = None,
        *,
        db_format: typing.Optional[str] = None,
        config: typing.Optional[ProjectConfig] = None,
    ) -> None:
        if config is None:
            config = ProjectConfig()
        elif not isinstance(config, ProjectConfig):
            raise TypeError(
                f"config must be a ProjectConfig, not {config.__class__.__name__}"
            )
        self.config = config
        self._begin_operation()
        self.buildroot = _path(buildroot, "buildroot")
        fmt = _check_db_format(db_format)
        if isinstance(db, DbProvider):
            if db_format:
                raise ValueError("db_format cannot be given with a DbProvider")
            self.db = db
        elif db is None:
            self.db = None
        elif isinstance(db, str) and db == "-":
            raise ValueError(
                "db '-' means standard input/output on the command line; "
                "pass db=None for no file DB"
            )
        else:
            self.db = open_db(_path(db, "db"), fmt)

    @classmethod
    def from_config(
        cls,
        path: StrPath,
        *,
        buildroot: typing.Optional[StrPath] = None,
        db: typing.Optional[typing.Union[StrPath, DbProvider]] = None,
        db_format: typing.Optional[str] = None,
    ) -> "Project":
        """A project described by the configuration file at ``path``.

        An argument beats the file, and the file beats the default (build root
        ``"."``, no file DB). A ``db`` of ``"-"`` in the file means no file DB.
        The environment is not read.

        Raises:
            UsageError: the file is invalid (see :func:`pkgforge.config.load`).
            FileNotFoundError: there is no such file.
        """
        config = _load_config(path)
        if buildroot is None:
            buildroot = config.buildroot or "."
        if db is None and config.db not in (None, "-"):
            db = config.db
        if db_format is None and not isinstance(db, DbProvider):
            db_format = config.db_format
        return cls(buildroot, db, db_format=db_format, config=config)

    # -- paths -------------------------------------------------------------

    def localpath(self, dbpath: str) -> Path:
        """The local path under the build root for the DB path ``dbpath``."""
        p = PurePosixPath(os.fspath(dbpath))
        rel = p.relative_to("/") if p.is_absolute() else p
        return Path(self.buildroot, rel)

    def dbpath(self, localpath: StrPath) -> str:
        """The ``/``-rooted DB path for ``localpath``, which must lie under
        the build root (``ValueError`` otherwise)."""
        return Path("/", Path(localpath).relative_to(self.buildroot)).as_posix()

    def _rootpath(
        self, path: typing.Union[str, os.PathLike], *, follow_final: bool = False
    ) -> Path:
        """Resolve a ``/``-rooted DESTINATION/PATH to a path under
        ``--buildroot``, refusing one that would climb above it.

        A literal ``..`` that climbs above the root is refused -- checked on
        the root-relative form, since normalizing the ``/``-rooted form
        first (``posixpath.normpath("/../x")`` collapses to ``/``) would
        silently hide the escape instead of rejecting it. An in-root ``..``
        (``/usr/share/../lib/x``) is normalized in the returned path, never
        left for the caller to record verbatim.

        A symlinked path component is refused the same way: after building
        the local path, ``realpath`` of its parent (non-strict, so this runs
        before any mkdir/unlink -- it resolves the existing prefix without
        raising for a path that doesn't exist yet) must stay under the
        build root's own realpath. The leaf itself is checked too, but only
        when ``follow_final`` is true and it already names a directory --
        the case where the caller is about to walk or write *into* it, not
        merely replace it. A leaf that is a dangling symlink, or a symlink
        to a file, therefore has only its parent checked, and is recorded
        as the link itself rather than its (possibly out-of-root) target.

        Not a sandbox against a concurrent writer (no ``openat2``/TOCTOU
        guarantees) -- that isn't needed for a single build process, though
        ``install`` re-checks right before its own mkdir, since an earlier
        source in the same multi-source invocation can plant a new symlink
        after this method already ran for a later one.

        Also refuses a *relative* build root (the ordinary cwd default) whose
        realpath is ``/`` unless it was spelled explicitly (``--buildroot /``
        or ``PKGFORGE_ROOT=/``) -- otherwise an unattended run started from
        ``/`` with no build root configured would map straight onto the live
        filesystem, silently.
        """
        root = self.buildroot
        real_root = os.path.realpath(root)
        if not root.is_absolute() and real_root == os.path.realpath(os.sep):
            raise UsageError(
                f"build root {os.fspath(root)!r} resolves to '/'; pass "
                "--buildroot / (or set PKGFORGE_ROOT=/) to target the live "
                "filesystem"
            )

        # A PurePath (install's DESTINATION, always a Path) converts via its
        # own .as_posix(): stringifying it first (os.fspath) would render it
        # with the native separator, which is a backslash on Windows and
        # breaks PurePosixPath parsing. A plain str (scan's PATH) is already
        # posix-shaped text from the caller, so it goes through
        # PurePosixPath directly.
        if isinstance(path, PurePath):
            posix_path = path.as_posix()
        else:
            posix_path = PurePosixPath(os.fspath(path)).as_posix()
        rel = posixpath.normpath(posix_path.lstrip("/") or ".")
        if rel == ".." or rel.startswith("../"):
            raise UsageError(f"{path}: resolves outside --buildroot")
        if rel == ".":
            return root

        local = Path(root, rel)
        check = local if (follow_final and os.path.isdir(local)) else local.parent
        if os.path.commonpath([real_root, os.path.realpath(check)]) != real_root:
            raise UsageError(f"{path}: a symlink leads outside --buildroot")
        return local

    # -- the file DB -------------------------------------------------------

    def _existing_db(self) -> DbProvider:
        """The configured file DB, which must already exist.

        Raises :class:`UsageError` when there is no DB or its file is
        missing, so a command that only reads (``dump``) or rewrites
        (``compact``) never mistakes a missing DB for an empty one.
        """
        if self.db is None:
            raise UsageError("no file DB configured; pass --db or set PKGFORGE_DB")
        if not Path(self.db.path).exists():
            raise UsageError(f"no file DB at {self.db.path}; run pkgforge initdb")
        return self.db

    @contextlib.contextmanager
    def _batch(self) -> typing.Iterator[None]:
        """Batch a run of writes through :meth:`DbProvider.batch` where the
        backend supports it (currently only ``sqlite``; every other backend
        inherits the no-op default), so :meth:`_write` uses one provider
        instead of one per call; a no-op with no file DB.

        Exits -- committing -- before the caller does anything that needs to
        see those writes through a *fresh* ``load()`` (e.g. ``scan``'s
        ``drop_stale`` reload): a second connection can't see rows a first
        one hasn't committed yet.
        """
        if self.db is None:
            yield
            return
        with self.db.batch() as provider:
            self._batch_provider = provider
            try:
                yield
            finally:
                self._batch_provider = None

    def _begin_operation(self) -> None:
        """Forget which paths the DB holds: each writing operation starts
        its owned-parent bookkeeping afresh."""
        self._held = None
        self._written = set()

    def _matchers(
        self,
    ) -> typing.Tuple[typing.Tuple[typing.Tuple[PathMatch, EntryRule], ...], bool]:
        """The ``[[rules]]`` with their ``match`` statements parsed, once per
        configuration, and whether any rule or default is set."""
        cache = self._rules
        if cache is None or cache[0] is not self.config:
            config = self.config
            rules = tuple(
                (PathMatch(_statements(rule.match)), rule) for rule in config.rules
            )
            cache = (config, rules, bool(rules) or config.defaults != EntryDefaults())
            self._rules = cache
        return cache[1], cache[2]

    def _check_names(self, where: str, owner: str, group: str) -> None:
        """Refuse an ``owner`` or ``group`` that is neither unset (``-``,
        ``--``) nor a base-passwd name nor declared in ``[[users]]`` or
        ``[[groups]]``; ``where`` leads the message."""
        cache = self._names
        if cache is None or cache[0] is not self.config:
            users, groups = _accounts(self.config)
            cache = (self.config, users | _UNSET, groups | _UNSET)
            self._names = cache
        if owner not in cache[1]:
            raise UsageError(
                f"{where}: owner {owner!r} is neither a base-passwd account "
                "nor declared in [[users]]"
            )
        if group not in cache[2]:
            raise UsageError(
                f"{where}: group {group!r} is neither a base-passwd group "
                "nor declared in [[groups]]"
            )

    def _configured(
        self, dbpath: str, entry: FileEntry, local: typing.Optional[Path] = None
    ) -> FileEntry:
        """``entry`` with the ``mode``, ``owner``, ``group``, ``kind``, ``caps``
        and ``package`` it leaves at ``"-"`` filled in from the configuration;
        ``entry`` itself is never changed.

        A field that is not ``"-"`` is a flag and stays. The others come from
        the first ``[[rules]]`` element whose ``match`` fits ``dbpath``, then
        (mode, owner and group only) from ``[defaults]`` (``dir_mode`` for a
        directory, ``exec_mode`` for a file with an execute bit on ``local``,
        else ``file_mode``). A symlink never takes a mode. ``local`` is the
        file to test for the execute bit; by default its place under the
        build root.
        """
        rules, active = self._matchers()
        if not active:
            return entry
        defaults = self.config.defaults
        unset = [name for name in _RESOLVED if entry[name] == DEFAULT]
        kind = entry["type"]
        if kind == FileType.Symlink and "mode" in unset:
            unset.remove("mode")
        if not unset:
            return entry
        values: typing.Dict[str, str] = {}
        for matcher, rule in rules:
            if matcher.match(dbpath, entry) is True:
                for name in unset:
                    value = getattr(rule, name)
                    if value is not None:
                        values[name] = value
                break
        for name in ("owner", "group"):
            value = getattr(defaults, name)
            if name in unset and name not in values and value is not None:
                values[name] = value
        if "mode" in unset and "mode" not in values:
            mode = self._default_mode(kind, dbpath, local)
            if mode is not None:
                values["mode"] = mode
        return {**entry, **values} if values else entry

    def _default_mode(
        self, kind: object, dbpath: str, local: typing.Optional[Path]
    ) -> typing.Optional[str]:
        """The ``[defaults]`` mode for an entry of type ``kind``, or ``None``."""
        defaults = self.config.defaults
        if kind == FileType.Directory:
            return defaults.dir_mode
        if kind != FileType.File:
            return None
        if defaults.exec_mode is None and defaults.file_mode is None:
            return None
        try:
            executable = os.lstat(local or self.localpath(dbpath)).st_mode & 0o111
        except OSError:
            executable = 0
        return defaults.exec_mode if executable else defaults.file_mode

    def _record_parents(
        self, dbpath: str, records: typing.Optional[FileRecords]
    ) -> None:
        """Record, top-down and before the entry at ``dbpath``, each ancestor
        directory at or below an ``own`` prefix that the DB does not hold yet.

        The DB is read once per operation, at the first entry that needs it.
        An owned parent is recorded and never chmod'ed.
        """
        own = self.config.own
        if not any(dbpath.startswith(prefix + "/") for prefix in own):
            return
        parts = dbpath.split("/")
        for end in range(2, len(parts)):
            parent = "/".join(parts[:end])
            if not _owned(parent, own) or parent in self._written:
                continue
            if self._held is None:
                self._held = {
                    path for path, held in self.load().items() if held is not None
                }
            if parent in self._held:
                continue
            entry = self._resolved(
                parent,
                self._configured(
                    parent,
                    {
                        "mode": DEFAULT,
                        "owner": DEFAULT,
                        "group": DEFAULT,
                        "type": FileType.Directory.value,
                        "kind": DEFAULT,
                        "caps": DEFAULT,
                        "package": DEFAULT,
                        "meta": {},
                    },
                ),
            )
            if self.db is not None:
                (self._batch_provider or self.db).add(parent, entry)
            self._written.add(parent)
            if records is not None:
                records.append((parent, entry))

    def _declared_packages(self) -> typing.FrozenSet[str]:
        """The package names the configuration allows an entry to carry:
        ``{"-"}`` with no ``[package]``, else its name and each ``[[packages]]``
        name."""
        cache = self._packages
        if cache is None or cache[0] is not self.config:
            config = self.config
            if config.package is None:
                names = frozenset((DEFAULT,))
            else:
                names = frozenset(
                    (config.package.name, *(sub.name for sub in config.packages))
                )
            cache = (config, names)
            self._packages = cache
        return cache[1]

    def _resolved(self, dbpath: str, entry: FileEntry) -> FileEntry:
        """``entry`` with an unset ``kind`` made ``file`` and an unset
        ``package`` made the ``[package]`` name (``-`` with none), after
        checking that its kind and caps fit its type and that its package is
        declared."""
        kind = entry["kind"]
        if kind == DEFAULT:
            kind = "file"
            entry = {**entry, "kind": kind}
        package = entry["package"]
        if package == DEFAULT and self.config.package is not None:
            package = self.config.package.name
            entry = {**entry, "package": package}
        _check_kind_caps(dbpath, kind, entry["caps"], entry["type"])
        self._check_package(dbpath, package)
        return entry

    def _check_package(self, where: str, package: str) -> None:
        """Refuse a ``package`` the configuration does not declare; ``where``
        leads the message."""
        if package not in self._declared_packages():
            raise UsageError(
                f"{where}: package {package!r} is neither [package] name nor "
                "a [[packages]] name"
            )

    def _record(
        self,
        dbpath: str,
        entry: FileEntry,
        records: typing.Optional[FileRecords] = None,
    ) -> FileEntry:
        """The entry actually recorded for ``dbpath``; every write goes
        through here. The owned parents of ``dbpath`` are written first and
        appended to ``records``.

        A ``kind`` still unset becomes ``file`` and a ``package`` still unset the
        ``[package]`` name; a kind or caps that does not fit the entry's type,
        a package the configuration does not declare, or an owner or group that
        is neither a base-passwd name nor declared, is a :class:`UsageError`."""
        entry = self._configured(dbpath, entry)
        entry = self._resolved(dbpath, entry)
        self._check_names(dbpath, entry["owner"], entry["group"])
        if self.config.own:
            self._record_parents(dbpath, records)
        return entry

    def _write(
        self,
        dbpath: str,
        entry: typing.Optional[FileEntry],
        records: typing.Optional[FileRecords] = None,
    ) -> typing.Optional[FileEntry]:
        """Write ``entry`` (``None`` removes ``dbpath``) to the file DB, if any.

        Returns what was recorded, for the caller's records. The owned parents
        written before ``entry`` are appended to ``records`` when it is given.
        """
        if entry is not None:
            entry = self._record(dbpath, entry, records)
        if self.config.own:
            if entry is None:
                self._written.discard(dbpath)
                if self._held is not None:
                    self._held.discard(dbpath)
            else:
                self._written.add(dbpath)
        if self.db is None:
            return entry
        provider = self._batch_provider or self.db
        if entry is None:
            provider.remove(dbpath)
        else:
            provider.add(dbpath, entry)
        return entry

    def init(self) -> None:
        """Create or reset (truncate) an empty file DB, creating its parent
        directory.

        Raises:
            UsageError: there is no file DB.
        """
        if self.db is None:
            raise UsageError("no file DB configured; pass --db or set PKGFORGE_DB")
        Path(self.db.path).parent.mkdir(parents=True, exist_ok=True)
        self.db.init()  # provider-specific empty DB (truncate file / reset table)
        _INIT_LOGGER.info("Initialized empty DB at %s", self.db.path)

    def load(self) -> Db:
        """The whole DB as ``{dbpath: entry}``; a removed path maps to ``None``.
        ``{}`` when there is no file DB or its file does not exist yet.

        Raises:
            DbError: the file is not valid in its format.
        """
        if self.db is None:
            return {}
        return self.db.load()

    def entries(self) -> FileEntries:
        """The live entries as ``(dbpath, entry)`` pairs sorted by path."""
        live = [
            (path, entry) for path, entry in self.load().items() if entry is not None
        ]
        live.sort(key=lambda pathentry: pathentry[0])
        return live

    def compact(self) -> None:
        """Collapse the DB to one record per live path, dropping superseded and
        removed ones.

        Raises:
            UsageError: there is no file DB, or its file does not exist.
        """
        db = self._existing_db()
        before = self.load()
        live = sum(1 for entry in before.values() if entry is not None)
        db.compact()
        _COMPACT_LOGGER.info(
            "Compacted %s: %d live path(s), %d removal(s) dropped",
            db.path,
            live,
            len(before) - live,
        )

    # -- operations --------------------------------------------------------

    def install(
        self,
        sources: typing.Sequence[StrPath],
        dest: str,
        *,
        mode: str = DEFAULT,
        owner: str = DEFAULT,
        group: str = DEFAULT,
        type: typing.Optional[FileType] = None,
        kind: str = DEFAULT,
        caps: str = DEFAULT,
        package: str = DEFAULT,
        meta: typing.Optional[typing.Mapping[str, str]] = None,
        into: bool = True,
        parents: bool = False,
        decompress: typing.Optional[str] = None,
        method: str = "copy",
        exclude: typing.Sequence[str] = (),
        record: bool = True,
        record_tree: bool = False,
        chown: bool = False,
        remove_source: bool = False,
    ) -> FileRecords:
        """Stage each source under the build root at ``dest`` and record it.

        ``sources`` are files, directories or archives (a sequence, never one
        bare path). ``"-"`` reads standard input (only one source may), and
        ``""`` means no source: an empty file, or with ``type`` directory an
        empty directory. ``dest`` is the ``/``-rooted DB path: the directory
        to install into, or with ``into=False`` the final path (one source
        only).

        Args:
            mode: 1-4 octal digits, ``"-"`` to leave the default, or ``"--"``
                to read it from the staged file.
            owner: an account name, ``"-"`` or ``"--"`` as for ``mode``.
            group: a group name, ``"-"`` or ``"--"`` as for ``mode``.
            type: the entry's type; detected from each source when ``None``.
            kind: how packaging treats the entry: ``"file"``, ``"config"``,
                ``"config-noreplace"``, ``"doc"``, ``"license"`` or
                ``"ghost"``; ``"-"`` takes a matching rule's kind, else
                ``"file"``. All but ``file`` and ``ghost`` need a regular file.
            caps: file capabilities the package sets on install, one
                ``setcap`` clause such as ``"cap_net_bind_service=ep"``;
                ``"-"`` takes a matching rule's, else none. Regular files
                that are not ghosts only.
            package: the package the entry ships in: the ``[package]`` name or
                a ``[[packages]]`` name; ``"-"`` takes a matching rule's
                package, else the ``[package]`` name (``"-"`` with none).
            meta: extra ``{key: value}`` metadata recorded with the entry.
            into: install into the directory ``dest`` (False: ``dest`` is the
                final path).
            parents: create the missing parent directories of the destination.
            decompress: a compression kind (gz, xz, bz2, zst or lzma) to
                decompress the source with, or ``"auto"`` to infer it from the
                source's suffix; ``None`` for no decompression.
            method: ``"copy"``, ``"link"`` (hardlink) or ``"move"``.
            exclude: statements (``[!][(?[!]test:arg)...]GLOB``) for paths of a
                directory or archive source to skip; absolute globs are
                relative to the source.
            record: write the entry to the file DB.
            record_tree: also record every path below an installed directory
                that the DB does not hold yet.
            chown: apply the recorded owner and group with ``chown``.
            remove_source: delete each source after it is staged.

        Every source is validated and resolved before any is staged, so a bad
        argument or a destination collision leaves nothing on disk.

        Returns:
            What was written, in write order (also with no file DB, when
            nothing is written).

        Raises:
            TypeError: ``sources`` is not a sequence of paths, ``dest`` is
                not text, ``type`` is not a :class:`~pkgforge.entry.FileType`,
                or an ``exclude`` statement is not text.
            UsageError: an argument or a source is unusable, or ``dest``
                leaves the build root.
            PkgForgeError: staging failed (a missing decompressor or ``bsdtar``,
                an unsafe archive member).
            OSError: the file system refused.
        """
        return self._run_install(
            sources,
            dest,
            mode=mode,
            owner=owner,
            group=group,
            type=type,
            kind=kind,
            caps=caps,
            package=package,
            meta=meta,
            into=into,
            parents=parents,
            decompress=decompress,
            method=method,
            exclude=exclude,
            record=record,
            record_tree=record_tree,
            chown=chown,
            remove_source=remove_source,
        )

    def _run_install(
        self,
        sources: typing.Sequence[StrPath],
        dest: str,
        *,
        mode: str = DEFAULT,
        owner: str = DEFAULT,
        group: str = DEFAULT,
        type: typing.Optional[FileType] = None,
        kind: str = DEFAULT,
        caps: str = DEFAULT,
        package: str = DEFAULT,
        meta: typing.Optional[typing.Mapping[str, str]] = None,
        into: bool = True,
        parents: bool = False,
        decompress: typing.Optional[str] = None,
        method: str = "copy",
        exclude: typing.Sequence[str] = (),
        record: bool = True,
        record_tree: bool = False,
        chown: bool = False,
        remove_source: bool = False,
        logger: typing.Optional[logging.Logger] = None,
        fresh: bool = True,
    ) -> FileRecords:
        """:meth:`install`, logging as ``logger`` when given. With ``fresh``
        false the call joins the operation already running: the owned-parent
        bookkeeping is not reset."""
        if isinstance(sources, (str, bytes, os.PathLike)) or not isinstance(
            sources, typing.Sequence
        ):
            raise TypeError("sources must be a sequence of paths, not a single path")
        if not isinstance(dest, str):
            raise TypeError(f"dest must be str, not {dest.__class__.__name__}")
        if method not in _INSTALL_METHODS:
            raise UsageError(
                f"unknown install method {method!r}; choose from "
                f"{', '.join(_INSTALL_METHODS)}"
            )
        if decompress is not None and not isinstance(decompress, str):
            raise TypeError("decompress must be a kind, 'auto' or None")
        if type is not None and not isinstance(type, FileType):
            raise TypeError(
                f"type must be a FileType or None, not {type.__class__.__name__}"
            )
        stmts = _statements(exclude)
        items = [_source(item) for item in sources]
        if sum(1 for item in items if item == DEFAULT) > 1:
            raise UsageError("only one source may read stdin ('-') per invocation")

        # Resolve every clone (type, decompress kind, destination --
        # writes no file) before staging any of them, so a collision
        # between two sources -- or a bad argument on a later source --
        # is caught with nothing on disk yet.
        resolved: typing.List[typing.Tuple[_Installer, Path]] = []
        for item in items:
            inst = _Installer(
                self,
                item,
                dest,
                mode=mode,
                owner=owner,
                group=group,
                type=type,
                kind=kind,
                caps=caps,
                package=package,
                meta=meta,
                into=into,
                parents=parents,
                decompress=decompress,
                method=method,
                exclude=stmts,
                record=record,
                record_tree=record_tree,
                chown=chown,
                remove_source=remove_source,
                logger=logger,
            )
            inst._preflight()
            resolved.append((inst, inst._resolve()))

        by_dest: typing.Dict[Path, typing.List[_Installer]] = {}
        for inst, target in resolved:
            by_dest.setdefault(target, []).append(inst)
        for target, insts in by_dest.items():
            if len(insts) < 2:
                continue
            names = sorted({os.fspath(i.source) for i in insts})
            if len(names) == 1:
                continue  # the same source path, harmlessly repeated
            if all(i.type == FileType.Directory for i in insts):
                continue  # directory (and archive) sources merge
            raise UsageError(
                f"{', '.join(names)} all resolve to {target}; only "
                "directory sources may share a destination"
            )

        if fresh:
            self._begin_operation()
        records: FileRecords = []
        for inst, target in resolved:
            inst._stage(target)
            records.extend(inst.records)
        return records

    def mkdir(
        self,
        paths: typing.Sequence[str],
        *,
        mode: str = DEFAULT,
        owner: str = DEFAULT,
        group: str = DEFAULT,
        kind: str = DEFAULT,
        package: str = DEFAULT,
        meta: typing.Optional[typing.Mapping[str, str]] = None,
        parents: bool = False,
    ) -> FileRecords:
        """Record each of ``paths`` (``/``-rooted DB paths) as a directory,
        creating it under the build root.

        Nothing is read from a source, so this is how a directory with no
        files of its own is packaged. Running it again changes nothing.
        ``mode``, ``owner``, ``group``, ``kind`` (``file`` or ``ghost`` fit a
        directory), ``package`` and ``meta`` are as for :meth:`install`; ``parents``
        creates the missing parent directories.

        Returns:
            What was written, in write order.

        Raises:
            TypeError: ``paths`` is not a sequence, or a path is not text.
            UsageError: an argument is unusable, a path leaves the build root,
                or a parent is missing and ``parents`` is false.
            OSError: the file system refused.
        """
        return self._mkdir(
            paths,
            mode=mode,
            owner=owner,
            group=group,
            kind=kind,
            package=package,
            meta=meta,
            parents=parents,
            logger=_MKDIR_LOGGER,
            fresh=True,
        )

    def _mkdir(
        self,
        paths: typing.Sequence[str],
        *,
        mode: str,
        owner: str,
        group: str,
        kind: str,
        package: str,
        meta: typing.Optional[typing.Mapping[str, str]],
        parents: bool,
        logger: logging.Logger,
        fresh: bool,
    ) -> FileRecords:
        """:meth:`mkdir`, logging as ``logger`` and joining the running
        operation unless ``fresh``."""
        if isinstance(paths, (str, bytes)) or not isinstance(paths, typing.Sequence):
            raise TypeError("paths must be a sequence of paths, not a single path")
        if fresh:
            self._begin_operation()
        records: FileRecords = []
        for path in paths:
            records.extend(
                self._run_install(
                    [""],
                    path,
                    mode=mode,
                    owner=owner,
                    group=group,
                    type=FileType.Directory,
                    kind=kind,
                    package=package,
                    meta=meta,
                    into=False,
                    parents=parents,
                    logger=logger,
                    fresh=False,
                )
            )
        return records

    def symlink(
        self,
        target: str,
        link: str,
        *,
        owner: str = DEFAULT,
        group: str = DEFAULT,
        kind: str = DEFAULT,
        package: str = DEFAULT,
        meta: typing.Optional[typing.Mapping[str, str]] = None,
        parents: bool = False,
    ) -> FileRecords:
        """Record ``link`` (a ``/``-rooted DB path) as a symbolic link to
        ``target`` and create it under the build root.

        ``target`` is stored as written, relative or absolute, and may name
        nothing. A symlink has no mode. ``owner``, ``group``, ``kind`` (``file``
        or ``ghost`` fit a link), ``package`` and ``meta`` are as for :meth:`install`;
        ``parents`` creates the missing parent directories.

        Returns:
            What was written, in write order.

        Raises:
            TypeError: ``target`` is not text.
            UsageError: ``target`` is empty or ``meta`` already holds
                ``target``, an argument is unusable, ``link`` leaves the build
                root, or a parent is missing and ``parents`` is false.
            OSError: the file system refused.
        """
        return self._symlink(
            target,
            link,
            owner=owner,
            group=group,
            kind=kind,
            package=package,
            meta=meta,
            parents=parents,
            logger=_SYMLINK_LOGGER,
            fresh=True,
        )

    def _symlink(
        self,
        target: str,
        link: str,
        *,
        owner: str,
        group: str,
        kind: str,
        package: str,
        meta: typing.Optional[typing.Mapping[str, str]],
        parents: bool,
        logger: logging.Logger,
        fresh: bool,
    ) -> FileRecords:
        """:meth:`symlink`, logging as ``logger`` and joining the running
        operation unless ``fresh``."""
        if not isinstance(target, str):
            raise TypeError(f"target must be str, not {target.__class__.__name__}")
        if not target:
            raise UsageError("a symlink needs a non-empty target")
        if meta is not None and "target" in meta:
            raise UsageError("meta must not hold 'target': it is set from target")
        return self._run_install(
            [""],
            link,
            owner=owner,
            group=group,
            type=FileType.Symlink,
            kind=kind,
            package=package,
            meta={**(meta or {}), "target": target},
            into=False,
            parents=parents,
            logger=logger,
            fresh=fresh,
        )

    def stage(self, *, init: bool = False) -> FileRecords:
        """Run the ``[[stage]]`` steps of the configuration, in order.

        A step with ``src`` installs it at ``dest`` (as :meth:`install`, with
        the step's options); ``dir``, ``file`` and ``symlink`` steps record a
        directory, an empty file or a link with no source. Missing parent
        directories are created. Every ``src`` is checked before the first
        step runs, so a missing source stages nothing.

        Args:
            init: reset the file DB first, as :meth:`init` does; with no file
                DB, log a warning and go on.

        Returns:
            What was written, in write order.

        Raises:
            UsageError: the configuration has no ``[[stage]]`` steps, a source
                is missing (the text names the step), or a step is refused.
            PkgForgeError: staging failed.
            OSError: the file system refused.
        """
        steps = self.config.stage
        if not steps:
            raise UsageError("no [[stage]] steps in the configuration")
        for number, step in enumerate(steps, 1):
            if step.src is not None and not (
                os.path.exists(step.src) or os.path.islink(step.src)
            ):
                raise UsageError(
                    f"[[stage]] #{number}: source {step.src} does not exist"
                )
        if init:
            if self.db is None:
                _STAGE_LOGGER.warning("no file DB: nothing to initialize")
            else:
                self.init()
        self._begin_operation()
        records: FileRecords = []
        for number, step in enumerate(steps, 1):
            what = step.src or next(
                kind for kind in ("dir", "file", "symlink") if getattr(step, kind)
            )
            _STAGE_LOGGER.info(
                "[[stage]] #%d: %s -> %s",
                number,
                what,
                step.dest or step.dir or step.file or step.symlink,
            )
            records.extend(self._stage_step(step))
        live = dict(self.entries()) if self.db is not None else {}
        live.update({path: entry for path, entry in records if entry is not None})
        generated = self._sysfiles(sorted(live.items()), _STAGE_LOGGER)
        records.extend(generated)
        _STAGE_LOGGER.info(
            "generated %d system file(s)",
            sum(1 for _path, entry in generated if entry is not None),
        )
        _STAGE_LOGGER.info("staged %d step(s), %d record(s)", len(steps), len(records))
        return records

    def sysfiles(self) -> FileRecords:
        """Write the ``sysusers.d`` and ``tmpfiles.d`` files the package needs
        under the build root and record them.

        ``/usr/lib/sysusers.d/NAME.conf`` holds the accounts ``[[users]]`` and
        ``[[groups]]`` create; ``/usr/lib/tmpfiles.d/NAME.conf`` creates each
        ghost directory in the file DB at install time. NAME is ``[package]
        name``. A file with nothing to say is not written, and one recorded
        earlier is removed with its entry. :meth:`stage` runs this after its
        steps.

        Returns:
            What was written: the entry of each file, or ``None`` for a file
            removed.

        Raises:
            UsageError: there is something to write and no ``[package]
                name``, or a ghost directory's path cannot be spelled in
                ``tmpfiles.d``.
            DbError: the file DB cannot be read.
            OSError: the file system refused.
        """
        self._begin_operation()
        return self._sysfiles(self.entries(), _SYSFILES_LOGGER)

    def _sysfiles(self, entries: FileEntries, logger: logging.Logger) -> FileRecords:
        """:meth:`sysfiles` over ``entries``, the live entries of the DB, in
        the running operation."""
        package = self.config.package
        held = {path for path, _entry in entries}
        records: FileRecords = []
        for directory, text in (
            (SYSUSERS_DIR, sysusers_text(self.config)),
            (TMPFILES_DIR, tmpfiles_text(entries)),
        ):
            if package is None and text:
                raise UsageError(
                    f"generating {directory}/NAME.conf needs [package] name"
                )
            path = f"{directory}/{package.name if package else ''}.conf"
            if not text:
                if package is not None and path in held:
                    local = self._rootpath(path)
                    with contextlib.suppress(FileNotFoundError):
                        local.unlink()
                    self._write(path, None, records)
                    records.append((path, None))
                    logger.info("removed %s", path)
                continue
            local = self._rootpath(path)
            local.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(FileNotFoundError):
                local.unlink()
            local.write_bytes(text.encode("utf-8"))
            os.chmod(local, 0o644)
            entry = self._write(
                path,
                {
                    "mode": "644",
                    "owner": "root",
                    "group": "root",
                    "type": FileType.File.value,
                    "kind": "file",
                    "caps": DEFAULT,
                    "package": DEFAULT,
                    "meta": {},
                },
                records,
            )
            records.append((path, entry))
            logger.info("wrote %s", path)
        return records

    def _stage_step(self, step: StageStep) -> FileRecords:
        """Run one ``[[stage]]`` step inside the running operation."""
        mode = DEFAULT if step.mode is None else step.mode
        owner = DEFAULT if step.owner is None else step.owner
        group = DEFAULT if step.group is None else step.group
        kind = DEFAULT if step.kind is None else step.kind
        caps = DEFAULT if step.caps is None else step.caps
        package = DEFAULT if step.package is None else step.package
        meta = dict(step.meta)
        if step.symlink is not None:
            return self._symlink(
                step.target or "",
                step.symlink,
                owner=owner,
                group=group,
                kind=kind,
                package=package,
                meta=meta,
                parents=True,
                logger=_STAGE_LOGGER,
                fresh=False,
            )
        if step.dir is not None:
            return self._mkdir(
                [step.dir],
                mode=mode,
                owner=owner,
                group=group,
                kind=kind,
                package=package,
                meta=meta,
                parents=True,
                logger=_STAGE_LOGGER,
                fresh=False,
            )
        if step.src is None:
            return self._run_install(
                [""],
                step.file or "",
                mode=mode,
                owner=owner,
                group=group,
                kind=kind,
                caps=caps,
                package=package,
                meta=meta,
                into=False,
                parents=True,
                logger=_STAGE_LOGGER,
                fresh=False,
            )
        return self._run_install(
            [step.src],
            step.dest or "",
            mode=mode,
            owner=owner,
            group=group,
            type=None if step.type is None else FileType(step.type),
            kind=kind,
            caps=caps,
            package=package,
            meta=meta,
            into=step.into,
            parents=True,
            decompress=step.decompress,
            method=step.method or "copy",
            exclude=step.exclude,
            record_tree=step.record_tree,
            logger=_STAGE_LOGGER,
            fresh=False,
        )

    def scan(
        self,
        path: str,
        *,
        mode: str = DEFAULT,
        owner: str = DEFAULT,
        group: str = DEFAULT,
        meta: typing.Optional[typing.Mapping[str, str]] = None,
        dir_mode: typing.Optional[str] = None,
        missing: bool = False,
        drop_stale: bool = False,
        exclude: typing.Sequence[str] = (),
    ) -> FileRecords:
        """Walk ``path`` (a ``/``-rooted DB path, below the build root) and
        record every path below it with its on-disk type; ``path`` itself is
        not recorded.

        Args:
            mode: the mode of regular files: 1-4 octal digits, ``"-"`` or
                ``"--"`` (read it from disk). Never applies to a directory or
                a symlink.
            owner: an account name, ``"-"`` or ``"--"`` (read it from disk).
            group: a group name, ``"-"`` or ``"--"`` (read it from disk).
            meta: extra ``{key: value}`` metadata for every entry.
            dir_mode: the mode of directories; by default ``"--"`` when
                ``mode`` is ``"--"``, else ``"-"``.
            missing: only record paths the DB does not hold yet.
            drop_stale: afterwards remove each DB entry below ``path`` whose
                file is gone from the build root, unless ``exclude`` keeps it
                (needs a file DB).
            exclude: statements for paths to skip, as for :meth:`install`;
                absolute globs are relative to ``path``.

        Returns:
            What was written, in write order.

        Raises:
            UsageError: ``path`` is missing or leaves the build root, an
                argument is invalid, or ``drop_stale`` has no file DB.
            PkgForgeError: a path is neither a file, directory nor symlink and
                was not excluded.
        """
        return _scan(
            self,
            path,
            mode=mode,
            owner=owner,
            group=group,
            meta=meta,
            dir_mode=dir_mode,
            missing=missing,
            drop_stale=drop_stale,
            exclude=exclude,
        )

    def dump(
        self,
        fmt: str,
        output: typing.Optional[StrPath] = None,
        *,
        exclude: typing.Sequence[str] = (),
        dirs: str = "all",
    ) -> dbdump.Rendered:
        """Render the file DB with the format ``fmt`` (see
        :func:`pkgforge.dbdump.dump`).

        With ``output``, also write it whole: a file for a per-entry format
        (or an existing directory, which gets one list per package), a
        directory (created if missing) for a multi-artifact one. The packages
        are those of ``config`` (``[package]``, ``[[packages]]``). ``dirs`` is
        ``"all"`` or ``"owned"``: the latter keeps a directory entry only at or
        below a prefix of ``config.own``.

        Returns:
            The rendered bytes (``bytes``, or ``{filename: bytes}``).

        Raises:
            UsageError: ``fmt`` is unknown, ``dirs`` is neither ``"all"`` nor
                ``"owned"`` or is ``"owned"`` with no ``own`` prefixes, the
                file DB is missing, an entry ships in a package the
                configuration does not declare, a file ``output`` would hold
                the entries of two packages, or the format needs ``[package]
                name`` and the configuration has none.
            UnsupportedOutputError: ``output`` has the wrong shape for ``fmt``.
            InvalidEntryError: an entry no format can render.
            DumpError: an entry ``fmt`` cannot represent.
        """
        own = self._dump_own(dirs)
        fmt_cls = dbdump.DumpFormat.get(fmt)
        target = None if output is None else Path(os.fspath(output))
        if target is not None:
            fmt_cls().check_output(target)
        db = self._existing_db()
        loaded = self.load()
        if not loaded:
            _DUMP_LOGGER.warning(
                "DB %s holds no entries; dumping an empty manifest", db.path
            )
        return dbdump.dump(
            loaded,
            fmt_cls.NAME,
            target,
            exclude=exclude,
            own=own,
            config=self.config,
        )

    def verify(self, *, exclude: typing.Sequence[str] = ()) -> typing.List[VerifyIssue]:
        """Compare the build root with the file DB, the way a packaging tool
        will see them.

        Reports a file or symlink on disk with no live entry (``unrecorded``),
        a live entry whose path does not exist (``missing``; a ``ghost`` is
        never expected on disk), an on-disk type that differs from the recorded
        one (``type``) and a symlink whose target differs from the recorded
        ``target`` (``target``). A directory with no entry is not reported: the
        package owns only the directories it records. Mode, owner and group are
        not compared, since the DB's values are what every dump format ships
        (owned parents and scanned entries keep their on-disk mode by design).

        The file DB inside the build root is skipped. ``exclude`` holds
        ``--exclude`` statements matched against DB paths, as ``dump`` does; a
        matching path is dropped from both sides and a matching directory is
        not descended.

        Returns:
            The issues sorted by path, then problem; empty when the tree
            matches the DB.

        Raises:
            UsageError: there is no file DB or its file is missing, or the
                build root does not exist.
            ExcludeSyntaxError: an ``exclude`` statement is invalid.
        """
        self._existing_db()
        stmts = _statements(exclude)
        root = self._rootpath("/")
        if not root.exists():
            raise UsageError(f"build root {root} does not exist")
        live = {path: entry for path, entry in self.entries()}
        issues = [
            VerifyIssue(path, problem, recorded, found)
            for problem, path, recorded, found in _verify(
                self, live, stmts, logger=_VERIFY_LOGGER
            )
        ]
        _VERIFY_LOGGER.info("verified %d entries: %d issue(s)", len(live), len(issues))
        return issues

    def _dump_own(self, dirs: str) -> typing.Optional[typing.Tuple[str, ...]]:
        """The prefixes ``dump(dirs=...)`` filters directories by: ``None``
        for ``"all"``."""
        if dirs == "all":
            return None
        if dirs != "owned":
            raise UsageError(f"unknown dirs {dirs!r}; choose from all, owned")
        if not self.config.own:
            raise UsageError(
                "--dirs owned needs owned prefixes: set own in pkgforge.cfg or "
                "pass --own"
            )
        return self.config.own


def _source(item: object) -> typing.Union[str, Path, None]:
    """One install source: ``"-"`` stays text (standard input), ``""`` is no
    source, anything else a :class:`~pathlib.Path`."""
    if isinstance(item, str) and item == "":
        return None
    if isinstance(item, str) and item == DEFAULT:
        return DEFAULT
    return _path(item, "source")
