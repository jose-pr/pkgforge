"""``install`` subcommand: stage a source into the build root + record its entry.

This is the workhorse of an unattended build: it copies/extracts a source
into place under the build root, applies the requested mode (and,
optionally, ownership), and records the resulting :class:`FileEntry` in the DB.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import typing
from pathlib import Path

try:  # Unix-only; see common.py's identical guard.
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-Unix
    grp = pwd = None

import duho

from .common import (
    AUTO,
    DEFAULT,
    PkgForgeCmd,
    PkgForgeError,
    FileEntryArgs,
    FileType,
    UsageError,
    apply_entry,
    entry_from_args,
    normalize_mode,
    parsepath,
    resolve_entry,
    _filetype,
    _normalize_field,
    _parse_filetype,
)
from .exclude import PathMatch, PathMatchStmt

#: Kind -> (argv prefix, canonical suffix). ``argv`` always includes ``-d``
#: (or the tool's own always-decompressing form), so a compressor's name
#: passed as a KIND can never compress instead of decompress; the source (or
#: ``-`` for stdin) is appended by :meth:`Install.install`.
_DECOMPRESSORS: typing.Dict[str, typing.Tuple[typing.List[str], str]] = {
    "gz": (["gzip", "-dc"], ".gz"),
    "xz": (["xz", "-dc"], ".xz"),
    "bz2": (["bzip2", "-dc"], ".bz2"),
    "zst": (["zstd", "-dcq"], ".zst"),
    "lzma": (["xz", "--format=lzma", "-dc"], ".lzma"),
}
#: Decompressor tool name -> canonical kind key in :data:`_DECOMPRESSORS`, so
#: ``-x gunzip``/``-x unxz``/etc keep working as aliases for the kind.
_KIND_ALIASES: typing.Dict[str, str] = {
    "gzip": "gz",
    "gunzip": "gz",
    "xz": "xz",
    "unxz": "xz",
    "bzip2": "bz2",
    "bunzip2": "bz2",
    "zstd": "zst",
    "unzstd": "zst",
    "lzma": "lzma",
    "unlzma": "lzma",
}
#: Canonical suffix (lowercased) -> kind key, for inferring KIND from a bare
#: ``-x``'s source suffix.
_SUFFIX_TO_KIND: typing.Dict[str, str] = {
    suffix: kind for kind, (_, suffix) in _DECOMPRESSORS.items()
}


def _resolve_kind(kind: str) -> str:
    """Resolve a ``--decompress`` value (a kind or a decompressor tool name,
    matched case-insensitively) to a canonical key in :data:`_DECOMPRESSORS`.

    Raises :class:`UsageError` for anything else -- never falls back to
    running the value as an arbitrary command.
    """
    key = kind.lower()
    key = _KIND_ALIASES.get(key, key)
    if key not in _DECOMPRESSORS:
        raise UsageError(
            f"unknown compression kind {kind!r}; use one of "
            f"{', '.join(sorted(_DECOMPRESSORS))}"
        )
    return key


#: Archive suffixes handled by stdlib :mod:`tarfile` (tar family + compression).
#: Anything else (e.g. ``.iso``) falls back to ``bsdtar``.
TAR_SUFFIXES = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tbz",
    ".tar.xz",
    ".txz",
)

#: True when this interpreter's ``tarfile`` supports the ``filter=`` extraction
#: argument (PEP 706, added in 3.12; backported to 3.9.17+). Passing ``filter=``
#: on an interpreter without it raises ``TypeError``, so we only opt in when safe.
_TARFILE_HAS_FILTER = hasattr(tarfile, "data_filter")


def _looks_like_path(kind: str) -> bool:
    """True if a ``--decompress`` value looks like a path instead of a kind.

    ``-x`` takes an *optional* argument, so argparse hands it the next token:
    ``install -x SRC DST`` parses ``SRC`` as the compression kind (and then
    errors out about a missing destination, or, with more sources, silently
    shifts every positional along by one). A kind is a bare word — ``gz`` or a
    decompressor command name — so a separator or a suffix means the misparse.
    """
    return bool(kind) and any(sep in kind for sep in (".", "/", os.sep))


def _is_tar_source(src: Path | str) -> bool:
    """True if ``src`` is a tar-family archive stdlib :mod:`tarfile` can extract."""
    name = os.fspath(src).lower()
    return name.endswith(TAR_SUFFIXES)


def _require_stdin() -> typing.BinaryIO:
    """Return stdin's binary buffer for a ``-`` source, after checking it is
    actually readable data (piped, or ``/dev/null``) rather than a terminal
    or a closed fd. Never closes stdin: the caller reads from the returned
    buffer without wrapping fd 0 in a new file object.
    """
    if sys.stdin is None:
        raise UsageError("'-' reads stdin, but stdin is closed")
    if sys.stdin.isatty():
        raise UsageError(
            "'-' reads stdin, but stdin is a terminal; pipe or redirect the data"
        )
    return sys.stdin.buffer


def _detect_source_type(path: Path) -> FileType:
    """Auto-detect a source's :class:`FileType`.

    A FIFO, or a character device that is not itself a terminal, is typed
    :attr:`FileType.File` so it is streamed like a regular file -- this is
    what makes a named pipe, ``/dev/stdin`` and process substitution
    (``<(cmd)``, which is a magic symlink to an anonymous pipe on Linux)
    work as install sources, instead of staging a dangling symlink to the
    pipe's ``pipe:[N]``/``/proc/self/fd/N`` target. This check runs before
    ``is_symlink()`` so it applies even though such a path often *is* one.
    A terminal character device, a socket or a block device is refused. An
    explicit ``-t symlink`` bypasses this entirely (it never calls this
    function) and always copies the link text.
    """
    try:
        followed = os.stat(path)
    except OSError:
        followed = None
    if followed is not None and stat.S_ISFIFO(followed.st_mode):
        return FileType.File
    if followed is not None and stat.S_ISCHR(followed.st_mode):
        fd = os.open(os.fspath(path), os.O_RDONLY | os.O_NOCTTY)
        try:
            is_tty = os.isatty(fd)
        finally:
            os.close(fd)
        if is_tty:
            raise UsageError(f"{path}: source is a terminal; pipe or redirect the data")
        return FileType.File
    if path.is_symlink():
        return FileType.Symlink
    if followed is None:
        raise TypeError(
            f"{path}: not a regular file, directory or symlink "
            "(missing or special file)"
        )
    if stat.S_ISDIR(followed.st_mode):
        return FileType.Directory
    if stat.S_ISREG(followed.st_mode):
        return FileType.File
    raise UsageError(f"{path}: unsupported source type (socket or block device)")


def _umask() -> int:
    """Read the process umask without changing it (``os.umask`` has no
    read-only form: setting it is the only way to read it)."""
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _refuse_real_directory_dest(dst: Path) -> None:
    """Refuse to replace a real (non-symlink) directory with a file or
    symlink -- a clear error instead of a confusing OSError deep in a
    copy/rename call."""
    if dst.is_dir() and not dst.is_symlink():
        raise UsageError(f"{dst}: cannot replace a directory with a file or symlink")


def _extract_tar(fileobj_or_name, dst: Path) -> None:
    """Extract a tar-family archive into ``dst`` using stdlib :mod:`tarfile`.

    Uses the safe ``data`` extraction filter where the interpreter supports it
    (guards against absolute paths / traversal / special files); older
    interpreters without ``filter=`` extract without it.
    """
    kwargs = {}
    if isinstance(fileobj_or_name, (str, os.PathLike)):
        opener = tarfile.open(name=os.fspath(fileobj_or_name), mode="r:*")
    else:
        opener = tarfile.open(fileobj=fileobj_or_name, mode="r|*")
    with opener as tar:
        if _TARFILE_HAS_FILTER:
            kwargs["filter"] = "data"
        tar.extractall(os.fspath(dst), **kwargs)


class Install(FileEntryArgs, PkgForgeCmd):
    """Install a source into the build root and record its file entry."""

    _parsername_ = "install"
    _logger_name_ = "pkgforge.install"

    noentry: bool = False
    "stage the file but do not record a DB entry"
    ("--noentry",)
    chown: bool = False
    "apply the recorded owner/group with chown (off by default)"
    ("--chown",)
    type: duho.Arg[
        typing.Union[FileType, str],
        duho.NS(type=_parse_filetype, metavar="{file,directory,symlink}"),
    ] = DEFAULT
    "file, directory or symlink, in any case (auto-detected from the source if unset, or given as '--')"
    ("--type", "-t")
    exclude: duho.Arg[
        typing.List[PathMatchStmt],
        duho.Append(PathMatchStmt.parse, metavar="STMT"),
    ] = []
    "exclude paths matching STMT when copying a directory source (repeatable); see the exclude-pattern guide for the grammar"
    ("--exclude", "-X")
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
    #: One or more sources. Declared as a plain ``List[Path]``, not a
    #: ``Union[List[Path], Path]``: duho resolves a union by composing its
    #: members' scalar factories, so a collection member (which needs its own
    #: argparse action) is rejected at parser-build time. ``__call__`` still
    #: accepts a bare ``Path`` from the Python API — it fans a list out into
    #: one clone per source and each clone carries a scalar.
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

    @classmethod
    def _parser_(cls, subparser=None, name=None, parents=(), **kwargs):
        parser = super()._parser_(subparser, name, parents, **kwargs)
        # Convenience shortcuts, translated in __init__:
        #   -D  ->  -Tp (no-target-directory + parents)
        #   -d  ->  --type directory (mutually exclusive with -t/--type)
        parser.add_argument(
            "-D", help="shortcut for -Tp", action="store_true", default=False
        )
        parser.add_argument(
            "-d",
            dest="d",
            help="shortcut for --type directory",
            action="store_true",
            default=False,
        )
        return parser

    def _stage_file(self, src: Path, dst: Path) -> Path:
        """Write this clone's file content into a temp file next to ``dst``.

        Never touches ``dst`` itself; the caller applies the entry and
        ``os.replace``s the returned temp onto ``dst``. On any failure the
        temp is removed and the exception re-raised.
        """
        fd, tmp_name = tempfile.mkstemp(
            dir=os.fspath(dst.parent), prefix=f".{dst.name}.", suffix=".pkgforge-tmp"
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            if not src:
                # An empty file (-t file, no source). mkstemp already
                # created it 0600; give it the umask-adjusted default mode
                # a plain touch() would have, since apply_entry() skips
                # chmod entirely when mode is left at DEFAULT.
                os.chmod(tmp, 0o666 & ~_umask())
            elif self.decompress:
                os.chmod(tmp, 0o666 & ~_umask())
                argv, _ = _DECOMPRESSORS[self.decompress]
                with tmp.open("wb") as f:
                    subprocess.run(
                        [
                            *argv,
                            # An absolute path so a source starting with "-"
                            # (e.g. "-v.gz") is never read as an option.
                            os.fspath(src.absolute()) if str(src) != DEFAULT else "-",
                        ],
                        # Only a "-" source reads stdin; for a real file the
                        # child inherits ours. Passing the file object (not
                        # a bare .fileno()) lets subprocess resolve it even
                        # when stdin has been replaced with a wrapped file
                        # object (as the tests do).
                        stdin=sys.stdin if str(src) == DEFAULT else None,
                        stdout=f,
                        check=True,
                    )
            elif str(src) != DEFAULT:
                if stat.S_ISREG(os.stat(src).st_mode):
                    # Copy src -> tmp. mkstemp already created tmp; copy2
                    # wants to create the file itself (to also copy the
                    # source's own mode/mtime), so remove the placeholder.
                    tmp.unlink()
                    shutil.copy2(os.fspath(src), os.fspath(tmp))
                else:
                    # A FIFO or non-terminal character device (a named pipe,
                    # /dev/stdin, process substitution): stream its bytes.
                    # copy2/copystat do not apply to a non-regular file.
                    os.chmod(tmp, 0o666 & ~_umask())
                    with open(src, "rb") as input, tmp.open("wb") as output:
                        shutil.copyfileobj(input, output)
            else:
                self._logger_.info("Obtaining data from stdin")
                os.chmod(tmp, 0o666 & ~_umask())
                with tmp.open("wb") as output:
                    shutil.copyfileobj(_require_stdin(), output)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return tmp

    def _stage_symlink(self, src: Path, dst: Path) -> Path:
        """Reserve a temp name next to ``dst`` and symlink it to the
        resolved target. Never touches ``dst`` itself; see :meth:`_stage_file`.
        """
        if str(src) == DEFAULT or not src:
            target = self.meta.get("target")
            if not target:
                # __call__ already checks this right after type resolution
                # (before the destination is even computed); this raise
                # stays for a direct Python-API caller of install() itself.
                raise UsageError("a symlink with no source needs -O target=PATH")
            copystat_from = None
        else:
            target = os.fspath(src.readlink())
            # Rebind, never mutate: self.meta may be the caller's own dict,
            # a shared class-level default, or a parser -O action's stored
            # default. Mutating it in place leaked "target" into every
            # entry recorded after this one, in the same multi-source
            # install and beyond it.
            self.meta = {**self.meta, "target": target}
            copystat_from = src

        fd, tmp_name = tempfile.mkstemp(
            dir=os.fspath(dst.parent), prefix=f".{dst.name}.", suffix=".pkgforge-tmp"
        )
        os.close(fd)
        tmp = Path(tmp_name)
        tmp.unlink()  # mkstemp's placeholder; replaced with the symlink below
        try:
            os.symlink(target, tmp)
            if copystat_from is not None:
                shutil.copystat(copystat_from, tmp, follow_symlinks=False)
        except BaseException:
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink(missing_ok=True)
            raise
        return tmp

    def _stage_directory(self, src: Path, dst: Path) -> Path:
        """Stage a directory (or archive) source at ``dst``.

        Returns ``dst`` once its final content is in place (a merge, or
        creating an empty directory, leaves nothing further for the caller
        to swap); returns a sibling temp directory when the caller must
        still ``os.replace`` it onto ``dst`` (a fresh, non-merging archive
        extraction).
        """
        if not src:
            # -t directory, no source: an empty directory. May already
            # exist (a re-run); nothing to extract or merge.
            dst.mkdir(exist_ok=True)
            return dst

        if src == DEFAULT or not src.is_dir():
            # Extract an archive source into a fresh temp directory first,
            # so a filter rejection or a truncated archive never reaches
            # dst directly.
            tmp = Path(
                tempfile.mkdtemp(
                    dir=os.fspath(dst.parent),
                    prefix=f".{dst.name}.",
                    suffix=".pkgforge-tmp",
                )
            )
            os.chmod(tmp, 0o777 & ~_umask())
            try:
                # Prefer stdlib tarfile for the tar family (no external
                # binary, cross-platform, safe `data` filter); fall back to
                # bsdtar for stdin and formats tarfile can't open (e.g. iso).
                if src != DEFAULT and _is_tar_source(src):
                    self._logger_.debug("Extracting %s via tarfile", src)
                    _extract_tar(src, tmp)
                else:
                    self._logger_.debug("Extracting %s via bsdtar", src)
                    subprocess.run(
                        [
                            "bsdtar",
                            "-x",
                            "-C",
                            os.fspath(tmp),
                            "-f",
                            os.fspath(src) if src != DEFAULT else "-",
                        ],
                        stdin=sys.stdin if src == DEFAULT else None,
                        check=True,
                    )
                if dst.exists():
                    shutil.copytree(tmp, dst, symlinks=True, dirs_exist_ok=True)
                    shutil.rmtree(tmp)
                    return dst
                return tmp
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise

        # A directory source: copytree onto dst, merging if dst already
        # exists. Only roll back dst on failure if this call created it --
        # a partial merge into a pre-existing directory is not rolled back.
        created = not dst.exists()
        try:
            dst.mkdir(exist_ok=True)
            shutil.copystat(src, dst, follow_symlinks=False)
            if self.exclude:
                filter = PathMatch(self.exclude, src)

                def _ignore(_dir: str, _files: typing.List[str]):
                    return [file for file in _files if filter.match(Path(_dir, file))]

            else:
                _ignore = None
            shutil.copytree(
                src,
                dst,
                symlinks=True,
                ignore=_ignore,
                ignore_dangling_symlinks=True,
                dirs_exist_ok=True,
            )
        except BaseException:
            if created:
                shutil.rmtree(dst, ignore_errors=True)
            raise
        return dst

    def install(self, src: Path, dst: Path) -> Path:
        """Stage ``src`` at ``dst`` and return the path holding the final
        content: either ``dst`` itself (nothing further to do -- a merge,
        an in-place no-op, or an already-empty directory) or a sibling temp
        the caller must ``os.replace`` onto ``dst`` after applying the
        entry. Writes nothing to ``dst`` directly for a file or symlink
        type, so a failure here never touches a previously staged copy.
        """
        self._logger_.info(
            "Installing %s at %s", DEFAULT if src is None else src, self.buildpath(dst)
        )
        if (
            src not in [DEFAULT, None]
            and os.path.lexists(dst)
            and os.path.samestat(os.lstat(src), os.lstat(dst))
        ):
            # src already IS the staged dst (e.g. an in-place build, or a
            # hardlink left by an earlier run): nothing to stage. Uses
            # samestat on lstat, not resolve()/exists(), so a stale host
            # symlink that merely resolves to src does not count as "same".
            if self.type == FileType.Symlink:
                self.meta = {**self.meta, "target": os.fspath(src.readlink())}
            return dst

        if self.type == FileType.File:
            _refuse_real_directory_dest(dst)
            return self._stage_file(src, dst)
        elif self.type == FileType.Symlink:
            _refuse_real_directory_dest(dst)
            return self._stage_symlink(src, dst)
        elif self.type == FileType.Directory:
            return self._stage_directory(src, dst)
        else:
            raise NotImplementedError(self.type)

    def _preflight(self) -> None:
        """Validate/normalize this clone's arguments, before resolution or
        any staging. Runs once per clone (a multi-source ``__call__`` fans
        out into one single-source clone per source first).

        Checks only what depends on arguments alone, never on the source
        (so it also validates a direct Python-API construction, which
        skips the CLI's own converters entirely): the mode, --chown's
        owner/group names (only when --chown is set; a recorded-only name
        is never looked up, since it may be created later by a package's
        own scriptlets), and the --db directory. It never auto-creates the
        DB directory or checks euid -- CAP_CHOWN, user namespaces and
        fakeroot all make a chown that a plain euid check would reject.
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
            if self.type == FileType.Directory:
                name = self.destination.name
                parts = name.split(".")
                if len(parts) > 1:
                    suffixes = parts[1:]
                    suffixes.reverse()
                    for ty in ["tar", "iso"]:
                        if ty in suffixes:
                            idx = suffixes.index(ty)
                            name = ".".join([parts[0], *reversed(suffixes[idx + 1 :])])
                            self.destination = self.destination.with_name(name)
                            break

        if not self.destination.is_absolute() and not self.buildroot:
            raise ValueError(self.destination)

        dest = self.destination
        if self.buildroot:
            if dest.is_absolute():
                dest = Path(self.buildroot, *dest.parts[1:])
            else:
                dest = self.buildroot / dest

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
            if staged != dest and (staged.is_symlink() or staged.exists()):
                if staged.is_dir() and not staged.is_symlink():
                    shutil.rmtree(staged, ignore_errors=True)
                else:
                    staged.unlink(missing_ok=True)
            raise

        if not self.noentry:
            fspath = os.fspath(self.buildpath(dest))
            self.add_entry(fspath, fileentry)

        if self.remove_source and self.source not in [DEFAULT, None]:
            # Runs LAST: after apply/replace/record succeed, so a failure
            # anywhere above (a bad --chown name past preflight, a disk-full
            # DB append) leaves the source in place and the command
            # re-runnable. Skipped, not refused, when the source IS dest
            # (an in-place build): the containment check in _resolve already
            # refused the case where removing a directory source would
            # delete dest or the DB out from under it.
            if os.path.lexists(dest) and os.path.samestat(
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
