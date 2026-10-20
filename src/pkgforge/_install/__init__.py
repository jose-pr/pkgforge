"""``Project.install``'s per-source worker: stage one source into the build
root and record its entry.

This is the workhorse of an unattended build: it copies/extracts a source
into place under the build root, applies the requested mode (and,
optionally, ownership), and records the resulting :class:`FileEntry`.
"""

from __future__ import annotations

import logging
import os
import shutil
import typing
from pathlib import Path

try:  # Unix-only; see entry.py's identical guard.
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-Unix
    grp = pwd = None

from ..entry import (
    AUTO,
    DEFAULT,
    FileRecords,
    FileEntry,
    FileType,
    _filetype,
    _is_auto,
    _new_entry,
    apply_entry,
    check_meta,
    normalize_mode,
    normalize_name,
    resolve_entry,
)
from ..exceptions import PkgForgeError, UsageError
from ..exclude import PathMatchStmt
from .archive import _archive_dir_name
from .decompress import _DECOMPRESSORS, _SUFFIX_TO_KIND, _looks_like_path, _resolve_kind
from .record import _RecordTree
from .staging import _Staging, _detect_source_type, _require_stdin

if typing.TYPE_CHECKING:
    from ..project import Project


class _Installer(_RecordTree, _Staging):
    """Installs one source of a :meth:`~pkgforge.project.Project.install` call.

    A plain object holding the attributes the staging mixins read; one is
    built per source, so each owns its ``meta`` and per-clone flags.
    """

    _logger_ = logging.getLogger("pkgforge.install")

    def __init__(
        self,
        project: "Project",
        source: typing.Union[str, Path, None],
        dest: str,
        *,
        mode: str,
        owner: str,
        group: str,
        type: typing.Optional[FileType],
        meta: typing.Optional[typing.Mapping[str, str]],
        into: bool,
        parents: bool,
        decompress: typing.Optional[str],
        method: str,
        exclude: typing.Sequence[PathMatchStmt],
        record: bool,
        record_tree: bool,
        chown: bool,
        remove_source: bool,
    ) -> None:
        self.project = project
        self.buildroot = project.buildroot
        self.db: typing.Optional[Path] = (
            None if project.db is None else Path(project.db.path)
        )
        #: A :class:`Path`, ``"-"`` (standard input) or ``None`` (no source).
        self.source = source
        self.destination = Path(dest)
        self.mode = mode
        self.owner = owner
        self.group = group
        self.type: typing.Any = DEFAULT if type is None else type
        # Each clone owns its meta dict: the symlink branch rebinds rather
        # than mutates, but a fresh copy per clone is cheap defense in depth
        # against any other future writer.
        self.meta = dict(meta or {})
        self.no_target_directory = not into
        self.parents = parents
        # False (off), True (infer from the suffix) or a kind.
        self.decompress: typing.Union[bool, str] = (
            False
            if decompress is None
            else True if decompress == "auto" else decompress
        )
        self.method = method
        self.exclude = list(exclude)
        self.noentry = not record
        self.record_tree = record_tree
        self.chown = chown
        self.remove_source = remove_source
        #: What this clone wrote, in write order.
        self.records: FileRecords = []

    def _no_file_db(self) -> bool:
        """True when there is no file DB, so nothing is recorded to a file."""
        return self.db is None

    def loaddb(self) -> typing.Dict[str, typing.Optional[FileEntry]]:
        return self.project.load()

    def add_entry(self, dbpath: str, entry: FileEntry) -> None:
        """Record ``entry`` at ``dbpath`` through the project."""
        self.records.append((dbpath, self.project._write(dbpath, entry)))

    def _preflight(self) -> None:
        """Validate/normalize this clone's arguments, before resolution or
        any staging. Runs once per clone (a multi-source ``__call__`` fans
        out into one single-source clone per source first).

        Checks mostly what depends on arguments alone, never requiring the
        source to exist (so it also validates a direct Python-API
        construction, which skips the CLI's own converters entirely): the
        mode, --chown's owner/group names (only when --chown is set; a
        recorded-only name is never looked up, since it may be created
        later by a package's own scriptlets), and the --db directory. It
        never auto-creates the DB directory or checks euid -- CAP_CHOWN,
        user namespaces and fakeroot all make a chown that a plain euid
        check would reject. The one exception: with ``--exclude`` set, it
        classifies the source via :meth:`_source_kind` (a non-raising
        ``Path.is_dir``/``is_symlink`` stat, never :func:`_detect_source_type`,
        which raises for a missing/special path) to warn once for a file or
        symlink source, the only kinds ``-X`` never filters (a directory or
        archive source is filtered during staging).
        """
        self.mode = normalize_mode(self.mode)
        self.owner = normalize_name(self.owner, "owner")
        self.group = normalize_name(self.group, "group")
        check_meta(self.meta)

        if self.chown:
            if pwd is None or grp is None:
                raise RuntimeError("chown requires the Unix pwd/grp modules")
            owner, group = self.owner, self.group
            if owner not in (DEFAULT, AUTO):
                try:
                    pwd.getpwnam(owner)
                except KeyError:
                    raise UsageError(f"--chown: unknown owner {owner!r}") from None
            if group not in (DEFAULT, AUTO):
                try:
                    grp.getgrnam(group)
                except KeyError:
                    raise UsageError(f"--chown: unknown group {group!r}") from None

        if not self.noentry and not self._no_file_db():
            if not Path(self.db).parent.is_dir():
                raise UsageError(f"--db {self.db}: directory does not exist")

        if self.exclude:
            kind = self._source_kind()
            if kind in ("file", "symlink"):
                self._logger_.warning(
                    "-X/--exclude has no effect on a %s source %s; it "
                    "only filters a directory or archive source",
                    kind,
                    self.source,
                )

    def _source_kind(self) -> str:
        """Classify this clone's source the way :meth:`_resolve` ultimately
        will -- ``"archive"``, ``"copy"`` (a real directory), ``"file"`` or
        ``"symlink"`` -- without ever raising for a missing source (that is
        :meth:`_resolve`'s own ``UsageError`` to give, not this one's).

        Auto-detection (an unset/``-``/``--`` ``--type``) never yields
        :attr:`FileType.Directory` for anything but a real on-disk
        directory (:func:`_detect_source_type`), so only an explicit
        ``-d``/``--type directory`` can put a non-directory source (an
        archive) in a directory-typed install at all -- checking
        ``self.type`` first, before ever stat-ing the source, is what lets
        this skip :func:`_detect_source_type` and its stricter, raising
        checks entirely.
        """
        source = self.source
        if not _is_auto(self.type):
            type_ = _filetype(self.type)
        elif source and source != DEFAULT and Path(source).is_symlink():
            type_ = FileType.Symlink
        elif source and source != DEFAULT and Path(source).is_dir():
            type_ = FileType.Directory
        else:
            type_ = FileType.File

        if type_ == FileType.Symlink:
            return "symlink"
        if type_ != FileType.Directory:
            return "file"
        if source not in (DEFAULT, None) and Path(source).is_dir():
            return "copy"
        return "archive"

    def _resolve(self) -> Path:
        """Resolve this clone's type, decompress kind and destination.

        Writes no file. Returns the local (buildroot-joined) destination
        path that :meth:`_stage` will install into.
        """
        if (
            isinstance(self.source, Path)
            and not self.source.exists()
            and not self.source.is_symlink()
        ):
            raise UsageError(f"source {self.source} does not exist")

        if not self.no_target_directory and str(self.source) == DEFAULT:
            raise UsageError(
                "a '-' (stdin) source needs -T (or -D) and an explicit "
                "destination file name"
            )

        if _is_auto(self.type):
            self._logger_.debug("Determining type from source")
            if self.source and self.source != DEFAULT:
                self.type = _detect_source_type(self.source)
            else:
                self.type = FileType.File
        else:
            self.type = _filetype(self.type)

        if self.type == FileType.Symlink and (
            str(self.source) == DEFAULT or not self.source
        ):
            # Checked here, right after type resolution: before the
            # destination is computed, before any -p mkdir, and before
            # _install() reaches its own check. On Windows a "/"-rooted
            # destination fails inside Project.dbpath() (and -p would mkdir
            # outside the build root) before _install() ever runs.
            if not self.meta.get("target"):
                raise UsageError("a symlink with no source needs -O target=PATH")

        if self.type != FileType.Symlink and str(self.source) == DEFAULT:
            # A file or directory "-" source actually reads stdin; a
            # symlink type/source never does (its "-" is just the
            # positional placeholder -O target=PATH uses). Validated here,
            # once, before any staging -- both the plain-copy path and the
            # decompress/bsdtar subprocess paths below rely on this having
            # already run.
            _require_stdin()

        if self.decompress is True:
            # Bare -x means "infer the compression from the source suffix",
            # which stdin does not have (a "-" source stays a plain str).
            if not self.source or str(self.source) == DEFAULT:
                raise UsageError("cannot infer compression from stdin; pass -x TYPE")
            suffix = self.source.suffix.lower()
            kind = _SUFFIX_TO_KIND.get(suffix)
            if kind is None:
                raise UsageError(
                    f"cannot infer compression from {self.source.name!r}; "
                    "pass -x KIND"
                )
            self.decompress = kind
        elif self.decompress:
            if isinstance(self.decompress, str) and _looks_like_path(self.decompress):
                # argparse gave -x the next positional (see _looks_like_path);
                # say so instead of trying to resolve that path as a kind.
                raise UsageError(
                    f"--decompress got {self.decompress!r}, which looks like a "
                    "path, not a compression kind; write the kind (-x gz), or "
                    "put a bare -x after the source and destination to infer it"
                )
            self.decompress = _resolve_kind(self.decompress)

        if self.decompress:
            argv, _ = _DECOMPRESSORS[self.decompress]
            if shutil.which(argv[0]) is None:
                raise PkgForgeError(f"decompressor {argv[0]!r} not found on PATH")

        if not self.no_target_directory:
            self.destination = self.destination / self.source.name
            if self.decompress:
                suffix = _DECOMPRESSORS[self.decompress][1]
                name = self.destination.name
                if name.lower().endswith(suffix):
                    name = name[: -len(suffix)]
                self.destination = self.destination.with_name(name)
            if self.type == FileType.Directory and not self.source.is_dir():
                # An archive being extracted (not a real directory source,
                # which keeps its own name unchanged): drop its archive
                # suffix from the destination name.
                self.destination = self.destination.with_name(
                    _archive_dir_name(self.destination.name)
                )

        dest = self.project._rootpath(
            self.destination, follow_final=(self.type == FileType.Directory)
        )

        if not self.parents and not dest.parent.is_dir():
            raise UsageError(
                f"destination directory {self.project.dbpath(dest.parent)} does "
                "not exist (use -p)"
            )

        if self.remove_source and self.source not in [DEFAULT, None]:
            if self.source.is_dir() and not self.source.is_symlink():
                self._refuse_remove_source_containment(dest)

        return dest

    def _refuse_remove_source_containment(self, dest: Path) -> None:
        """Refuse ``--remove-source`` when the resolved destination, or the
        DB file, sits at or inside a directory source -- removing the
        source afterwards would delete what was just staged (or the DB
        itself). Checked before any staging happens.
        """
        src_real = os.path.realpath(self.source)
        targets = [("destination", dest)]
        if not self.noentry and not self._no_file_db():
            targets.append(("DB", Path(self.db)))
        for label, target in targets:
            target_real = os.path.realpath(target)
            if target_real == src_real or target_real.startswith(src_real + os.sep):
                raise UsageError(
                    f"--remove-source: the {label} is inside the source "
                    f"directory {self.source}; refusing to remove it"
                )

    def _stage(self, dest: Path) -> None:
        """Stage this clone's source at ``dest`` and record its entry.

        ``_install()`` returns either ``dest`` itself (nothing further to
        swap) or a sibling temp; the entry is applied to whichever of the
        two actually holds the content, and only then is the temp (if any)
        replaced onto ``dest`` -- so a failed chmod/chown/record leaves an
        earlier good ``dest`` exactly as it was, never a partial temp.
        """
        # Re-checked here, right before any mkdir: a multi-source __call__
        # resolves every clone (and its containment check) before staging
        # any of them, so an earlier clone's own staging -- e.g. an absolute
        # in-root symlink left by copytree(symlinks=True) -- can plant a new
        # escape between this clone's own _resolve() and this call.
        dest = self.project._rootpath(
            self.project.dbpath(dest), follow_final=(self.type == FileType.Directory)
        )
        created: typing.List[Path] = []
        if self.parents:
            ancestor = dest.parent
            while not ancestor.exists() and ancestor != ancestor.parent:
                created.append(ancestor)
                ancestor = ancestor.parent
            dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._stage_into(dest)
        except BaseException:
            # Deepest first; a directory something else has since written
            # into is not empty and stays.
            for parent in created:
                try:
                    os.rmdir(parent)
                except OSError:
                    break
            raise

    def _stage_into(self, dest: Path) -> None:
        """Stage this clone's source at ``dest`` (whose parent exists), apply
        and record its entry, then honour ``--remove-source``."""
        staged = self._install(self.source, dest)
        try:
            fileentry = _new_entry(
                mode=self.mode,
                owner=self.owner,
                group=self.group,
                type=FileType(self.type),
                meta=self.meta,
            )
            fileentry = resolve_entry(fileentry, staged)
            apply_entry(fileentry, staged, chown=self.chown, logger=self._logger_)
            if staged != dest:
                os.replace(staged, dest)
        except BaseException:
            if self._move_atomic:
                # --method move already consumed self.source into `staged`
                # (or, for a symlink/no-op staging that reused dest itself,
                # never set this at all); restore it instead of deleting the
                # only copy.
                self._rollback_move(staged)
            elif staged != dest and (staged.is_symlink() or staged.exists()):
                if staged.is_dir() and not staged.is_symlink():
                    shutil.rmtree(staged, ignore_errors=True)
                else:
                    staged.unlink(missing_ok=True)
            raise

        if not self.noentry:
            fspath = self.project.dbpath(dest)
            # Batched (a no-op except sqlite) so --record-tree's children
            # share one connection with the top entry below.
            with self.project._batch():
                try:
                    self.add_entry(fspath, fileentry)
                except BaseException:
                    if self._move_atomic:
                        # The entry is already applied and (if it needed
                        # one) a temp already replaced onto dest -- the
                        # moved data now lives at dest itself.
                        self._rollback_move(dest)
                    raise
                # After the top entry is committed: a --record-tree failure
                # exits 1 with the tree staged and the top entry recorded --
                # nothing to roll back, staging itself already succeeded.
                self._record_tree(dest)

        if self.remove_source and self.source not in [DEFAULT, None]:
            # Runs LAST: after apply/replace/record succeed, so a failure
            # anywhere above (a bad --chown name past preflight, a disk-full
            # DB append) leaves the source in place and the command
            # re-runnable. Skipped, not refused, only when the source IS
            # dest (an in-place build): a --method link hardlink shares the
            # data, so removing the source name leaves the staged copy intact.
            if not os.path.lexists(self.source):
                # --method move already consumed it -- redundant with move,
                # so this is a no-op rather than an error. A source
                # --exclude left partially in place (the merge path) still
                # reaches the branches below, same as without move.
                pass
            elif os.path.realpath(self.source) == os.path.realpath(dest):
                self._logger_.warning(
                    "--remove-source: %s is the destination; not removing",
                    self.source,
                )
            elif self.source.is_dir() and not self.source.is_symlink():
                # A directory source needs rmtree; unlink only removes files/symlinks.
                shutil.rmtree(self.source)
            else:
                self.source.unlink()
