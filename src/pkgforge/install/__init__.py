"""``install`` subcommand: stage a source into the build root + record its entry.

The staging itself is :meth:`pkgforge.project.Project.install`; this command
maps the command line onto it.
"""

from __future__ import annotations

import typing
from pathlib import Path

import duho

from .._install.transfer import _env_method
from ..command import PkgForgeCmd, _as_path, _refuse_mcp_stdin, parsepath
from ..entry import (
    DEFAULT,
    FileEntryArgs,
    FileType,
    _filetype,
    _is_auto,
    _parse_filetype,
)
from ..exceptions import UsageError
from ..exclude import ExcludeArgs
from ..project import _INSTALL_METHODS


class Install(FileEntryArgs, ExcludeArgs, PkgForgeCmd):
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
    # Declared as a plain ``List[Path]``, not a ``Union[List[Path], Path]``:
    # duho resolves a union by composing its members' scalar factories, so a
    # collection member (which needs its own argparse action) is rejected at
    # parser-build time.
    #: One or more source paths.
    source: duho.Arg[
        typing.List[Path],
        duho.Meta(type=parsepath, nargs="+"),
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
        if not isinstance(self.source, list):
            raise UsageError("source must be a list of paths")
        self.source = [_as_path(item, "source") for item in self.source]
        if hasattr(self, "destination"):
            self.destination = _as_path(self.destination, "destination")

    def __call__(self):
        sources = ["" if item is None else item for item in self.source]
        reads_stdin = any(str(item) == DEFAULT for item in sources)
        if reads_stdin and (
            _is_auto(self.type) or _filetype(self.type) != FileType.Symlink
        ):
            _refuse_mcp_stdin("a '-' source")
        if self.decompress is True:
            decompress = "auto"
        else:
            decompress = self.decompress or None
        records = self._project().install(
            sources,
            self.destination.as_posix(),
            mode=self.mode,
            owner=self.owner,
            group=self.group,
            type=self.type,
            meta=self.meta,
            into=not self.no_target_directory,
            parents=self.parents,
            decompress=decompress,
            method=self.method,
            exclude=self.exclude,
            record=not self.noentry,
            record_tree=self.record_tree,
            chown=self.chown,
            remove_source=self.remove_source,
        )
        self._emit(records)


Install._register()
