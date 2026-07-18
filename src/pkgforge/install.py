"""``install`` subcommand: stage a source into the build root + record its entry.

This is the workhorse of an unattended build: it copies/links/extracts a
source into place under the build root, applies the requested mode (and,
optionally, ownership), and records the resulting :class:`FileEntry` in the DB.
"""

from __future__ import annotations

import os
import shutil
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
    FileEntry,
    FileEntryArgs,
    FileType,
    parsepath,
)
from .exclude import PathMatch, PathMatchStmt

DECOMPRESS_CMDS = {"gz": "gunzip", "xz": "unxz", "bz2": "bunzip2"}

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


def _is_tar_source(src: "Path | str") -> bool:
    """True if ``src`` is a tar-family archive stdlib :mod:`tarfile` can extract."""
    name = os.fspath(src).lower()
    return name.endswith(TAR_SUFFIXES)


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

    noentry: bool = False
    ("--noentry",)
    chown: bool = False
    ("--chown",)
    type: typing.Union[FileType, str] = DEFAULT
    ("--type", "-t")
    exclude: duho.Arg[
        typing.List[PathMatchStmt],
        duho.NS(type=PathMatchStmt.parse, action="append"),
    ] = []
    ("--exclude", "-X")
    parents: bool
    ("--parents", "-p")
    no_target_directory: bool
    ("--no-target-directory", "-T")
    decompress: duho.Arg[typing.Union[str, bool], duho.NS(nargs="?")] = False
    ("-x", "--decompress")
    remove_source: bool = False
    ("--remove-source",)
    source: duho.Arg[
        typing.Union[typing.List[Path], Path],
        duho.NS(type=parsepath, nargs="+"),
    ] = []
    ("source",)
    destination: Path
    ("destination",)

    def __init__(self, **kwargs):
        if kwargs.pop("D", False):
            kwargs["no_target_directory"] = True
            kwargs["parents"] = True
        if kwargs.pop("d", False):
            kwargs["type"] = "directory"

        decompress = kwargs.get("decompress")
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
        if dst.exists() and src not in [DEFAULT, None] and src.resolve() == dst.resolve():
            return
        if self.type == FileType.File:
            dst.unlink(True)
            if not src:
                dst.touch()
            elif self.decompress:
                with dst.open("wb") as f:
                    subprocess.run(
                        [
                            DECOMPRESS_CMDS.get(self.decompress, self.decompress),
                            "-kc",
                            os.fspath(src),
                        ],
                        stdin=sys.stdin.fileno(),
                        stdout=f,
                        check=True,
                    )
            elif str(src) != DEFAULT:
                # Hardlink src -> dst. os.link works on every supported Python
                # (Path.link_to was removed in 3.12; Path.hardlink_to only
                # exists from 3.10), so use the stdlib os call directly.
                os.link(os.fspath(src), os.fspath(dst))
                shutil.copystat(src, dst, follow_symlinks=False)
            else:
                self._logger_.info("Obtaining data from stdin")
                with dst.open("wb") as output:
                    if not sys.stdin.isatty():
                        with os.fdopen(sys.stdin.fileno(), "rb") as input:
                            shutil.copyfileobj(input, output)
        elif self.type == FileType.Symlink:
            if str(src) == DEFAULT or not src:
                target = self.meta.get("target")
                if not target:
                    raise ValueError(src)
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
                        stdin=sys.stdin.fileno() if src == DEFAULT else None,
                        check=True,
                    )
            elif src.is_dir():
                shutil.copystat(src, dst, follow_symlinks=False)
                if self.exclude:
                    filter = PathMatch(self.exclude, src)

                    def _ignore(_dir: str, _files: "typing.List[str]"):
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

    def __call__(self):
        if isinstance(self.source, list):
            for source in self.source:
                cloned = dict(self._get_kwargs())
                cloned["source"] = source
                Install(**cloned)()
            return

        if self.type == DEFAULT:
            self._logger_.debug("Determining type from source")
            if self.source and self.source != DEFAULT:
                self.type = FileType.from_path(self.source)
            else:
                self.type = FileType.File
        else:
            self.type = FileType(self.type)

        if self.decompress is True:
            self.decompress = self.source.suffix[1:]

        if not self.no_target_directory:
            if str(self.source) == DEFAULT:
                raise ValueError(self.source)
            self.destination = self.destination / self.source.name
            if self.decompress:
                self.destination = self.destination.with_name(
                    self.destination.name.removesuffix(f".{self.decompress}")
                )
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

        if self.parents:
            dest.parent.mkdir(parents=True, exist_ok=True)

        self.install(self.source, dest)

        if self.remove_source and self.source not in [DEFAULT, None]:
            # A directory source needs rmtree; unlink only removes files/symlinks.
            if self.source.is_dir() and not self.source.is_symlink():
                shutil.rmtree(self.source)
            else:
                self.source.unlink()

        fileentry = FileEntry.from_args(self)
        fileentry = FileEntry.resolve_for(fileentry, dest)
        FileEntry.apply(fileentry, dest, chown=self.chown, logger=self._logger_)

        if not self.noentry:
            fspath = os.fspath(self.buildpath(dest))
            self.add_entry(fspath, fileentry)


Install._register()
