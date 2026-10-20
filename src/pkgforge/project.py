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
from .config import EntryDefaults, EntryRule, ProjectConfig, _owned
from .config import load as _load_config
from .db import Db, DbProvider, open_db
from .entry import (
    DEFAULT,
    FileEntries,
    FileEntry,
    FileRecords,
    FileType,
    StrPath,
)
from .exceptions import UsageError
from .exclude import PathMatch, _statements

__all__ = ["Project"]

#: Recognized ``method`` values of :meth:`Project.install`.
_INSTALL_METHODS = ("copy", "link", "move")

#: The entry fields ``[[rules]]`` and ``[defaults]`` fill when they are unset.
_RESOLVED = ("mode", "owner", "group")

_INIT_LOGGER = logging.getLogger("pkgforge.initdb")
_COMPACT_LOGGER = logging.getLogger("pkgforge.compact")
_DUMP_LOGGER = logging.getLogger("pkgforge.dbdump")


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

    def _configured(
        self, dbpath: str, entry: FileEntry, local: typing.Optional[Path] = None
    ) -> FileEntry:
        """``entry`` with the ``mode``, ``owner`` and ``group`` it leaves at
        ``"-"`` filled in from the configuration; ``entry`` itself is never
        changed.

        A field that is not ``"-"`` is a flag and stays. The others come from
        the first ``[[rules]]`` element whose ``match`` fits ``dbpath``, then
        from ``[defaults]`` (``dir_mode`` for a directory, ``exec_mode`` for a
        file with an execute bit on ``local``, else ``file_mode``). A symlink
        never takes a mode. ``local`` is the file to test for the execute bit;
        by default its place under the build root.
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
            entry = self._configured(
                parent,
                {
                    "mode": DEFAULT,
                    "owner": DEFAULT,
                    "group": DEFAULT,
                    "type": FileType.Directory.value,
                    "meta": {},
                },
            )
            if self.db is not None:
                (self._batch_provider or self.db).add(parent, entry)
            self._written.add(parent)
            if records is not None:
                records.append((parent, entry))

    def _record(
        self,
        dbpath: str,
        entry: FileEntry,
        records: typing.Optional[FileRecords] = None,
    ) -> FileEntry:
        """The entry actually recorded for ``dbpath``; every write goes
        through here. The owned parents of ``dbpath`` are written first and
        appended to ``records``."""
        entry = self._configured(dbpath, entry)
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

        self._begin_operation()
        records: FileRecords = []
        for inst, target in resolved:
            inst._stage(target)
            records.extend(inst.records)
        return records

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

        With ``output``, also write it whole: a file for a per-entry format, a
        directory (created if missing) for a multi-artifact one. ``dirs`` is
        ``"all"`` or ``"owned"``: the latter keeps a directory entry only at or
        below a prefix of ``config.own``.

        Returns:
            The rendered bytes (``bytes``, or ``{filename: bytes}``).

        Raises:
            UsageError: ``fmt`` is unknown, ``dirs`` is neither ``"all"`` nor
                ``"owned"`` or is ``"owned"`` with no ``own`` prefixes, or the
                file DB is missing.
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
        return dbdump.dump(loaded, fmt_cls.NAME, target, exclude=exclude, own=own)

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
