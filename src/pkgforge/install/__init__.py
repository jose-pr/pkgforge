"""``install`` subcommand: stage a source into the build root + record its entry.

This is the workhorse of an unattended build: it copies/extracts a source
into place under the build root, applies the requested mode (and,
optionally, ownership), and records the resulting :class:`FileEntry` in the DB.
"""

from __future__ import annotations

import os
import shutil
import typing
from pathlib import Path

#: Recognized `--method`/`PKGFORGE_INSTALL_METHOD` values.
_INSTALL_METHODS = ("copy", "link", "move")

try:  # Unix-only; see entry.py's identical guard.
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-Unix
    grp = pwd = None

import duho

from ..errors import PkgForgeError, UsageError
from ..entry import (
    AUTO,
    DEFAULT,
    FileEntryArgs,
    FileType,
    apply_entry,
    entry_from_args,
    normalize_mode,
    resolve_entry,
    _filetype,
    _normalize_field,
    _parse_filetype,
)
from ..command import PkgForgeCmd, parsepath
from ..exclude import ExcludeArgs
from .archive import _archive_dir_name
from .decompress import _DECOMPRESSORS, _SUFFIX_TO_KIND, _looks_like_path, _resolve_kind
from .record import _RecordTree
from .staging import _Staging, _detect_source_type, _require_stdin
from .transfer import _env_method


class Install(FileEntryArgs, ExcludeArgs, _RecordTree, PkgForgeCmd, _Staging):
    """Install a source into the build root and record its file entry."""

    _parsername_ = "install"
    _logger_name_ = "pkgforge.install"

    noentry: bool = False
    "stage the file but do not record a DB entry"
    ("--noentry",)
    chown: bool = False
    "apply the recorded owner/group with chown (off by default)"
    ("--chown",)
    method: duho.Arg[
        str,
        duho.NS(
            env="PKGFORGE_INSTALL_METHOD",
            type=_env_method,
            choices=_INSTALL_METHODS,
            metavar="METHOD",
        ),
    ] = _env_method(os.environ.get("PKGFORGE_INSTALL_METHOD", ""))
    (
        "how to stage a file or directory source: copy (default) or, to "
        "avoid re-copying a build output you already have on disk, link "
        "(hardlink -- shares the source's inode, so -m/-o/-g/--chown then "
        "change the source too) or move (consumes the source; a failure "
        "after staging restores it). Ignored for a '-' (stdin) source, -x "
        "decompression, an archive source and a symlink source/type (env "
        "PKGFORGE_INSTALL_METHOD)"
    )
    ("--method",)
    type: duho.Arg[
        typing.Union[FileType, str],
        duho.NS(
            type=_parse_filetype,
            metavar="{file,directory,symlink}",
            conflicts="type",
        ),
    ] = DEFAULT
    "file, directory or symlink, in any case (auto-detected from the source if unset, or given as '--')"
    ("--type", "-t")
    parents: bool
    "create missing parent directories of the destination"
    ("--parents", "-p")
    no_target_directory: bool
    "treat destination as the final path, not a directory"
    ("--no-target-directory", "-T")
    decompress: duho.Arg[
        typing.Union[str, bool], duho.NS(nargs="?", metavar="KIND")
    ] = False
    "decompress the source (gz/xz/bz2); KIND is optional: write -x KIND SRC DST, or a bare -x after the paths to infer it from the suffix"
    ("-x", "--decompress")
    remove_source: bool = False
    "delete the source after staging (files or directories)"
    ("--remove-source",)
    D: bool = False
    "shortcut for -Tp"
    ("-D",)
    d: duho.Arg[bool, duho.NS(conflicts="type")] = False
    "shortcut for --type directory (not allowed with -t/--type)"
    ("-d",)
    # Declared as a plain ``List[Path]``, not a ``Union[List[Path], Path]``:
    # duho resolves a union by composing its members' scalar factories, so a
    # collection member (which needs its own argparse action) is rejected at
    # parser-build time. ``__call__`` still accepts a bare ``Path`` from the
    # Python API -- it fans a list out into one clone per source and each
    # clone carries a scalar.
    #: One or more source paths; a bare ``Path`` is accepted from the Python API.
    source: duho.Arg[
        typing.List[Path],
        duho.NS(type=parsepath, nargs="+"),
    ] = []
    "one or more files, directories or archives to stage"
    ("source",)
    destination: Path
    "destination path under the build root"
    ("destination",)

    def __init__(self, **kwargs):
        if kwargs.pop("D", False):
            kwargs["no_target_directory"] = True
            kwargs["parents"] = True
        if kwargs.pop("d", False):
            kwargs["type"] = "directory"

        # `.get("decompress", False)`, not `.get("decompress")`: the CLI
        # parser always supplies the key (None for a bare -x, since the
        # field declares no `const`), but a direct Python-API construction
        # that simply omits `decompress` must NOT be treated the same as a
        # bare -x -- it means "off", matching the field's documented
        # default. An explicitly passed `decompress=None` is still treated
        # as "infer", same as the CLI's bare -x.
        decompress = kwargs.get("decompress", False)
        if decompress is None or decompress == "-":
            kwargs["decompress"] = True
        super().__init__(**kwargs)

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

        if self.chown:
            if pwd is None or grp is None:
                raise RuntimeError("chown requires the Unix pwd/grp modules")
            # Normalize py3.9's stripped `--owner=--` -> [] to AUTO first
            # (the same conversion entry_from_args does later): checking
            # the raw list against DEFAULT/AUTO would never match, and
            # pwd.getpwnam([]) crashes with TypeError instead of a clean
            # UsageError.
            owner = _normalize_field(self.owner)
            group = _normalize_field(self.group)
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
            # Side-effect free for a read: this only sniffs/validates the
            # format (surfacing an unknown --db-format or PKGFORGE_DB_FORMAT
            # before anything is staged) and never creates the file.
            self._provider(for_read=True)
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
        if self.type not in (DEFAULT, AUTO, FileType._AUTO, []):
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

        if self.type in (DEFAULT, AUTO, FileType._AUTO, []):
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
            # install() reaches its own check. On Windows a "/"-rooted
            # destination fails inside buildpath() (and -p would mkdir
            # outside the build root) before install() ever runs.
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
            # which stdin does not have (parsepath keeps "-" as a plain str).
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

        dest = self._rootpath(
            self.destination, follow_final=(self.type == FileType.Directory)
        )

        if not self.parents and not dest.parent.is_dir():
            raise UsageError(
                f"destination directory {self.buildpath(dest.parent)} does "
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

        ``install()`` returns either ``dest`` itself (nothing further to
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
        dest = self._rootpath(
            self.buildpath(dest), follow_final=(self.type == FileType.Directory)
        )
        if self.parents:
            dest.parent.mkdir(parents=True, exist_ok=True)

        staged = self.install(self.source, dest)
        try:
            fileentry = entry_from_args(self)
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
            fspath = os.fspath(self.buildpath(dest))
            # Batched (a no-op except sqlite) so --record-tree's children
            # share one connection with the top entry below.
            with self._db_batch():
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
            # re-runnable. Skipped, not refused, when the source IS dest
            # (an in-place build): the containment check in _resolve already
            # refused the case where removing a directory source would
            # delete dest or the DB out from under it.
            if not os.path.lexists(self.source):
                # --method move already consumed it -- redundant with move,
                # so this is a no-op rather than an error. A source
                # --exclude left partially in place (the merge path) still
                # reaches the branches below, same as without move.
                pass
            elif os.path.lexists(dest) and os.path.samestat(
                os.lstat(self.source), os.lstat(dest)
            ):
                self._logger_.warning(
                    "--remove-source: %s is the staged file; not removing",
                    self.source,
                )
            elif self.source.is_dir() and not self.source.is_symlink():
                # A directory source needs rmtree; unlink only removes files/symlinks.
                shutil.rmtree(self.source)
            else:
                self.source.unlink()

    def __call__(self):
        if isinstance(self.source, list):
            if sum(1 for s in self.source if str(s) == DEFAULT) > 1:
                raise UsageError("only one source may read stdin ('-') per invocation")

            # Resolve every clone (type, decompress kind, destination --
            # writes no file) before staging any of them, so a collision
            # between two sources -- or a bad argument on a later source --
            # is caught with nothing on disk yet.
            resolved: typing.List[typing.Tuple["Install", Path]] = []
            for source in self.source:
                cloned = dict(self._get_kwargs())
                cloned["source"] = source
                # Each clone owns its meta dict: the symlink branch already
                # rebinds rather than mutates, but a fresh copy per clone is
                # cheap defense in depth against any other future writer.
                cloned["meta"] = dict(cloned.get("meta") or {})
                inst = Install(**cloned)
                inst._preflight()
                resolved.append((inst, inst._resolve()))

            by_dest: typing.Dict[Path, typing.List["Install"]] = {}
            for inst, dest in resolved:
                by_dest.setdefault(dest, []).append(inst)
            for dest, insts in by_dest.items():
                if len(insts) < 2:
                    continue
                names = sorted({os.fspath(i.source) for i in insts})
                if len(names) == 1:
                    continue  # the same source path, harmlessly repeated
                if all(i.type == FileType.Directory for i in insts):
                    continue  # directory (and archive) sources merge
                raise UsageError(
                    f"{', '.join(names)} all resolve to {dest}; only "
                    "directory sources may share a destination"
                )

            for inst, dest in resolved:
                inst._stage(dest)
            return

        self._preflight()
        dest = self._resolve()
        self._stage(dest)


Install._register()
