"""The common command base and its file-DB plumbing.

Defines the CLI path parser (:func:`parsepath`), the common command base
(:class:`PkgForgeCmd`: the ``--db``/``--db-format``/``--buildroot`` options
every subcommand shares, plus the file-DB read/write helpers and the
build-root <-> local-path translation) that every subcommand extends, and the
CLI root (:class:`PkgForge`).

Storage is pluggable (see :mod:`pkgforge.db`: ``jsonl`` by default, plus
``yaml`` and ``sqlite``, and a third party can add its own by subclassing
:class:`~pkgforge.db.DbProvider`). On load, the last record for a path wins.
An existing file is content-sniffed on both read and write, so a legacy or
mislabeled DB (e.g. a single-document YAML file, whatever its ``--db``
suffix) keeps loading, and keeps being appended to, in its own format
rather than being silently misread or corrupted. To convert one to a
different backend, ``initdb --db-format FMT`` a new path and re-record.
"""

from __future__ import annotations

import contextlib
import os
import posixpath
import typing
from pathlib import Path, PurePath, PurePosixPath

import duho
from duho import Cli, Cmd, LoggingArgs

from .entry import FileEntry
from .errors import UsageError


def parsepath(path: str) -> typing.Optional[typing.Union[str, Path]]:
    """Parse a CLI path argument.

    ``"-"`` (stdin/stdout) is returned as-is; the empty string becomes
    ``None``; anything else becomes a :class:`~pathlib.Path`.
    """
    if path == "-":
        return "-"
    elif not path:
        return None
    else:
        return Path(path)


def _env_path(value: str) -> typing.Optional[Path]:
    """Env-var/CLI value -> Path, or None if empty/unset (see PkgForgeCmd.db)."""
    return Path(value) if value else None


def _env_str(value: str) -> typing.Optional[str]:
    """Env-var/CLI value -> str, or None if empty/unset (PkgForgeCmd.db_format)."""
    return value if value else None


def _env_root(value: str) -> Path:
    """Env-var/CLI value -> Path, defaulting to '.' if empty (buildroot)."""
    return Path(value) if value else Path(".")


def _check_db_format(value: typing.Optional[str]) -> typing.Optional[str]:
    """Validate a ``db_format`` value against the registered providers,
    returning its canonical :attr:`~pkgforge.db.DbProvider.NAME`.

    A falsy value (``None`` or ``""``) means "auto-detect" and is never
    checked here -- ``open_db`` still resolves it from the ``--db`` suffix or,
    for an existing file, by sniffing its content. Anything else must already
    be a registered :class:`~pkgforge.db.DbProvider` subclass's ``NAME`` or
    ``ALIASES`` entry, checked eagerly so a typo fails before any file is
    staged, instead of surfacing as a traceback the first time the DB is
    touched.
    """
    if not value:
        return value
    # Function-local, like PkgForgeCmd._provider() below: DbProvider._registry
    # is read fresh on every call, so a subclass defined after this module is
    # imported (a third-party backend) is still recognized.
    from .db import DbProvider

    return DbProvider.lookup(value).NAME


class PkgForgeCmd(LoggingArgs, Cmd):
    """Common base for every pkgforge subcommand.

    Carries the three app-wide options (``--db``, ``--db-format`` and
    ``--buildroot``), the file-DB read/write helpers, and the build-root
    <-> local-path translation. Each leaf
    command subclasses this and implements ``__call__``; a leaf attaches itself
    to the :class:`PkgForge` root's subcommand tree via :meth:`_register`.
    """

    db: duho.Arg[
        typing.Optional[Path],
        duho.NS(env="PKGFORGE_DB", type=_env_path, metavar="PATH"),
    ] = _env_path(os.environ.get("PKGFORGE_DB", ""))
    "file DB path (env PKGFORGE_DB); unset or '-' records to stdout"
    ("--db",)
    db_format: duho.Arg[
        typing.Optional[str],
        duho.NS(env="PKGFORGE_DB_FORMAT", type=_env_str, metavar="FORMAT"),
    ] = _env_str(os.environ.get("PKGFORGE_DB_FORMAT", ""))
    "storage backend: jsonl, yaml, sqlite, or a registered DbProvider subclass's NAME (env PKGFORGE_DB_FORMAT); else inferred from the --db suffix"
    ("--db-format",)
    buildroot: duho.Arg[
        Path, duho.NS(env="PKGFORGE_ROOT", type=_env_root, metavar="DIR")
    ] = _env_root(os.environ.get("PKGFORGE_ROOT", ""))
    "staging root that maps to '/' in the DB (env PKGFORGE_ROOT; default '.')"
    ("--buildroot", "-r")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Checked after construction (so env/CLI/default fill-in has already
        # happened) rather than as a `type=` converter: a converter only runs
        # for a value that actually goes through argparse, so a direct
        # Python-API construction (PkgForgeCmd(db_format="toml")) would
        # otherwise bypass it entirely, same as the mode/type converters
        # above.
        self.db_format = _check_db_format(self.db_format)

    def localpath(self, buildpath: typing.Union[str, os.PathLike]) -> Path:
        p = PurePosixPath(os.fspath(buildpath))
        rel = p.relative_to("/") if p.is_absolute() else p
        return Path(self.buildroot, rel)

    def buildpath(self, localpath: Path) -> Path:
        return Path("/", localpath.relative_to(self.buildroot))

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

        Also refuses an unusable ``--buildroot``: unset/empty (a falsy
        value, reachable only from a direct Python-API construction) always
        raises, and a *relative* build root (the ordinary cwd default)
        whose realpath is ``/`` raises unless it was spelled explicitly
        (``--buildroot /`` or ``PKGFORGE_ROOT=/``) -- otherwise an
        unattended run started from ``/`` with no build root configured
        would map straight onto the live filesystem, silently.
        """
        root = self.buildroot
        if not root:
            raise UsageError("no build root configured (--buildroot/PKGFORGE_ROOT)")
        root = Path(root)
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
        # breaks PurePosixPath parsing -- self.buildpath() builds exactly
        # such a "/"-rooted Path for _stage()'s re-check. A plain str (scan's
        # PATH) is already posix-shaped text from the caller, so it goes
        # through PurePosixPath directly, as before.
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

    def _no_file_db(self) -> bool:
        """True when there is no real DB file to operate on (unset / stdout)."""
        return self.db is None or str(self.db) == "-"

    def _no_db_reason(self) -> typing.Optional[str]:
        """Human-readable reason there is no real DB *file* configured
        (``--db`` unset, or ``-``), or ``None`` if ``self.db`` names one --
        a path that doesn't exist YET is a different case each caller
        checks for itself: it means something to `initdb` (create it) and
        something else to `dbdump`/`compact` (warn and act as if empty).

        Used by `dbdump`/`initdb`/`compact` to log a WARNING instead of
        silently treating a missing DB as empty, per the documented
        "resilient defaults" (a missing/unset/`-` DB reads as empty, exit
        codes unchanged) -- this makes that leniency visible instead of
        changing it.
        """
        if self.db is None:
            return "no file DB (--db / PKGFORGE_DB unset)"
        if str(self.db) == "-":
            return "--db - is write-only (stdout); reads see an empty DB"
        return None

    def _provider(self, *, for_read: bool = False):
        """Resolve the DB storage provider for the configured --db/--db-format."""
        # Function-local: db.py imports entry only under TYPE_CHECKING today,
        # so a module-top import here would be safe now, but db.py is expected
        # to import entry's types at runtime later; keeping this local
        # avoids introducing a cycle then.
        from .db import open_db

        # `self.db_format` can be "" from a direct Python-API construction
        # (e.g. PkgForgeCmd(db_format="")), which bypasses the `type=`
        # converter above entirely -- normalize it here too, so an empty
        # value means "auto-detect" everywhere, not only through argv/env.
        return open_db(self.db, self.db_format or None, for_read=for_read)

    @contextlib.contextmanager
    def _db_batch(self):
        """Batch a run of writes through :meth:`DbProvider.batch` where the
        backend supports it (currently only ``sqlite``; every other backend
        inherits the no-op default), instead of resolving the provider fresh
        for each ``add_entry``/``remove_entry`` call.

        Resolves the provider once (``for_read=True``, same as
        :meth:`loaddb`/:meth:`_write_entry` outside a batch -- it keeps a
        write in the file's sniffed format) and stores it so
        :meth:`_write_entry` uses it directly; a no-op (nothing to batch)
        for an unset/stdout DB. Exits -- committing -- before the caller does
        anything that needs to see those writes through a *fresh* ``load()``
        (e.g. ``scan --drop-stale``'s reload): a second connection can't see
        rows a first one hasn't committed yet.
        """
        if self._no_file_db():
            yield
            return
        provider = self._provider(for_read=True)
        with provider.batch() as batch_provider:
            self._batch_provider = batch_provider
            try:
                yield
            finally:
                self._batch_provider = None

    def loaddb(self) -> typing.Dict[str, typing.Optional[FileEntry]]:
        if self._no_file_db():
            return {}
        return self._provider(for_read=True).load()

    def compactdb(self) -> None:
        """Collapse the DB's redundant history (backend-specific; no-op if none)."""
        if self._no_file_db():
            return
        self._provider(for_read=True).compact()

    def initdb(self) -> None:
        """Create or reset an empty DB (no-op for a stdout / unset DB)."""
        if self._no_file_db():
            return
        self._provider().init()

    def _write_entry(self, buildpath: Path, entry: typing.Optional[FileEntry]):
        path = os.fspath(buildpath)
        if self._no_file_db():
            # No file: emit the record as a JSON Lines line to stdout.
            # Function-local for the same reason as _provider() above.
            from .db.jsonl import _jsonl_line

            print(_jsonl_line(path, entry), end="")
            return
        provider = getattr(self, "_batch_provider", None) or self._provider(
            for_read=True
        )
        if entry is None:
            provider.remove(path)
        else:
            provider.add(path, entry)

    def add_entry(self, buildpath: Path, entry: FileEntry):
        self._write_entry(buildpath, entry)

    def remove_entry(self, buildpath: Path):
        self._write_entry(buildpath, None)

    @classmethod
    def _register(cls):
        PkgForge._register_subcmd_(cls)


class PkgForge(PkgForgeCmd, Cli):
    """Stage files into a build root and record their intended install
    metadata (mode, owner, group, type) in a file DB. Dump that DB into
    packaging manifests (RPM %files, Debian install/permissions).
    """

    # Extends PkgForgeCmd (the shared --db/--buildroot options and the DB
    # helpers) and duho.Cli (the app-root layer: --version, completion, and
    # the subcommand tree).
    _version_ = duho.AUTO
    _distribution_ = "pkgforge"
    _completion_ = True
    # Without this, duho falls back to the class name ("PkgForge") for the
    # prog name: usage, errors, --version and completion would all say
    # "PkgForge" instead of the invoked command, and shell completion (which
    # binds by exact, case-sensitive name) would never fire on bash/zsh/fish.
    _parsername_ = "pkgforge"
