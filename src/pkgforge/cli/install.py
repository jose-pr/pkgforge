"""``install``: stage a source into the build root and record its entry."""

from __future__ import annotations

import argparse
import typing
from pathlib import Path

import duho

from ..entry import (
    AUTO,
    DEFAULT,
    FileType,
    _filetype,
    _is_auto,
)
from ..exceptions import UsageError
from ..project import _INSTALL_METHODS
from ._common import EntryArgs, ExcludeArgs, ProjectCmd, _refuse_mcp_stdin

__all__ = ["Install"]


def _env_method(value: str) -> str:
    """Env-var or CLI value to an install method; empty means ``copy``.

    A non-``str`` ``type=`` converter is what makes duho treat an empty env
    value as unset, where a bare ``str`` field would keep it as a value.
    """
    return value if value else "copy"


def _parse_filetype(text: str) -> typing.Union[FileType, str]:
    """The ``type=`` converter of ``--type``."""
    try:
        return _filetype(text)
    except UsageError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _source(text: str) -> typing.Union[str, Path]:
    """The ``type=`` converter of a source: ``-`` stays text (standard input),
    anything else is a path."""
    return text if text in ("", DEFAULT) else Path(text)


class Install(EntryArgs, ExcludeArgs, ProjectCmd):
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
        duho.Meta(
            env="PKGFORGE_INSTALL_METHOD",
            type=_env_method,
            choices=_INSTALL_METHODS,
            metavar="METHOD",
        ),
    ] = "copy"
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
    record_tree: duho.Arg[bool, duho.Meta(env="PKGFORGE_INSTALL_RECORD_TREE")] = False
    (
        "after installing a directory or archive DESTINATION, also record "
        "every path below it not already in the DB: owner/group/meta from "
        "this install, mode and type from disk (symlinks '-'), honouring "
        "-X (env PKGFORGE_INSTALL_RECORD_TREE); ignored for a file/symlink "
        "source, and a no-op under --noentry"
    )
    ("--record-tree",)
    type: duho.Arg[
        typing.Union[FileType, str],
        duho.Meta(
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
        typing.Union[str, bool], duho.Meta(nargs="?", metavar="KIND")
    ] = False
    "decompress the source (gz, xz, bz2, zst or lzma); KIND is optional: write -x KIND SRC DST, or a bare -x after the paths to infer it from the suffix"
    ("-x", "--decompress")
    remove_source: bool = False
    "delete the source after staging (files or directories)"
    ("--remove-source",)
    D: bool = False
    "shortcut for -Tp"
    ("-D",)
    d: duho.Arg[bool, duho.Meta(conflicts="type")] = False
    "shortcut for --type directory (not allowed with -t/--type)"
    ("-d",)
    # A plain List[Path]: duho rejects a collection inside a Union.
    source: duho.Arg[
        typing.List[Path],
        duho.Meta(type=_source, nargs="+"),
    ] = []
    "one or more files, directories or archives to stage"
    ("source",)
    destination: Path
    "destination path under the build root"
    ("destination",)

    def __init__(self, **kwargs: typing.Any) -> None:
        if kwargs.pop("D", False):
            kwargs["no_target_directory"] = True
            kwargs["parents"] = True
        if kwargs.pop("d", False):
            kwargs["type"] = "directory"
        # The parser always supplies decompress: None for a bare -x, which
        # infers the kind. Leaving it out means off.
        decompress = kwargs.get("decompress", False)
        if decompress is None or decompress == DEFAULT:
            kwargs["decompress"] = True
        super().__init__(**kwargs)
        if self.type == []:  # argparse before 3.12 strips an attached "--"
            self.type = AUTO

    def _decompress(self) -> typing.Optional[str]:
        """``--decompress`` as the library spells it: ``None``, ``"auto"`` or a kind."""
        return "auto" if self.decompress is True else self.decompress or None

    def __call__(self) -> int:
        reads_stdin = any(str(item) == DEFAULT for item in self.source)
        if reads_stdin and (
            _is_auto(self.type) or _filetype(self.type) != FileType.Symlink
        ):
            _refuse_mcp_stdin("a '-' source")
        records = self._project().install(
            self.source,
            self.destination.as_posix(),
            mode=self.mode,
            owner=self.owner,
            group=self.group,
            type=self.type,
            meta=self.meta,
            into=not self.no_target_directory,
            parents=self.parents,
            decompress=self._decompress(),
            method=self.method,
            exclude=self.exclude,
            record=not self.noentry,
            record_tree=self.record_tree,
            chown=self.chown,
            remove_source=self.remove_source,
        )
        self._emit(records)
        return 0
