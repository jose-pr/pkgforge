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
import typing
from pathlib import Path

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

    def install(self, src: Path, dst: Path):
        self._logger_.info(
            "Installing %s at %s", DEFAULT if src is None else src, self.buildpath(dst)
        )
        if (
            dst.exists()
            and src not in [DEFAULT, None]
            and src.resolve() == dst.resolve()
        ):
            return
        if self.type == FileType.File:
            dst.unlink(True)
            if not src:
                dst.touch()
            elif self.decompress:
                argv, _ = _DECOMPRESSORS[self.decompress]
                with dst.open("wb") as f:
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
                    # Copy src -> dst. The staged file is independent of the
                    # source: -m/--chown apply to the copy only, and the
                    # source keeps its own content, mode and ownership.
                    shutil.copy2(os.fspath(src), os.fspath(dst))
                else:
                    # A FIFO or non-terminal character device (a named pipe,
                    # /dev/stdin, process substitution): stream its bytes.
                    # copy2/copystat do not apply to a non-regular file.
                    with open(src, "rb") as input, dst.open("wb") as output:
                        shutil.copyfileobj(input, output)
            else:
                self._logger_.info("Obtaining data from stdin")
                with dst.open("wb") as output:
                    shutil.copyfileobj(_require_stdin(), output)
        elif self.type == FileType.Symlink:
            if str(src) == DEFAULT or not src:
                target = self.meta.get("target")
                if not target:
                    # __call__ already checks this right after type
                    # resolution (before the destination is even computed);
                    # this raise stays for a direct Python-API caller of
                    # install() itself.
                    raise UsageError("a symlink with no source needs -O target=PATH")
                dst.symlink_to(target)
            else:
                target = src.readlink()
                self.meta["target"] = os.fspath(target)
                dst.symlink_to(target)
                shutil.copystat(src, dst, follow_symlinks=False)

        elif self.type == FileType.Directory:
            dst.mkdir(exist_ok=True)
            if not src:
                ...
            elif src == DEFAULT or not src.is_dir():
                # Extract an archive source. Prefer stdlib tarfile for the tar
                # family (no external binary, cross-platform, safe `data`
                # filter); fall back to bsdtar for stdin and formats tarfile
                # can't open (e.g. iso).
                if src != DEFAULT and _is_tar_source(src):
                    self._logger_.debug("Extracting %s via tarfile", src)
                    _extract_tar(src, dst)
                else:
                    self._logger_.debug("Extracting %s via bsdtar", src)
                    subprocess.run(
                        [
                            "bsdtar",
                            "-x",
                            "-C",
                            os.fspath(dst),
                            "-f",
                            os.fspath(src) if src != DEFAULT else "-",
                        ],
                        stdin=sys.stdin if src == DEFAULT else None,
                        check=True,
                    )
            elif src.is_dir():
                shutil.copystat(src, dst, follow_symlinks=False)
                if self.exclude:
                    filter = PathMatch(self.exclude, src)

                    def _ignore(_dir: str, _files: typing.List[str]):
                        return [
                            file for file in _files if filter.match(Path(_dir, file))
                        ]

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

        else:
            raise NotImplementedError(self.type)

    def _preflight(self) -> None:
        """Validate/normalize this clone's arguments, before resolution or
        any staging. Runs once per clone (a multi-source ``__call__`` fans
        out into one single-source clone per source first)."""
        self.mode = normalize_mode(self.mode)

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
        return dest

    def _stage(self, dest: Path) -> None:
        """Stage this clone's source at ``dest`` and record its entry."""
        if self.parents:
            dest.parent.mkdir(parents=True, exist_ok=True)

        self.install(self.source, dest)

        if self.remove_source and self.source not in [DEFAULT, None]:
            # A directory source needs rmtree; unlink only removes files/symlinks.
            if self.source.is_dir() and not self.source.is_symlink():
                shutil.rmtree(self.source)
            else:
                self.source.unlink()

        fileentry = entry_from_args(self)
        fileentry = resolve_entry(fileentry, dest)
        apply_entry(fileentry, dest, chown=self.chown, logger=self._logger_)

        if not self.noentry:
            fspath = os.fspath(self.buildpath(dest))
            self.add_entry(fspath, fileentry)

    def __call__(self):
        if isinstance(self.source, list):
            if sum(1 for s in self.source if str(s) == DEFAULT) > 1:
                raise UsageError("only one source may read stdin ('-') per invocation")
            for source in self.source:
                cloned = dict(self._get_kwargs())
                cloned["source"] = source
                Install(**cloned)()
            return

        self._preflight()
        dest = self._resolve()
        self._stage(dest)


Install._register()
