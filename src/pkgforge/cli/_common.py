"""Arguments and helpers the commands share."""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import typing
from pathlib import Path

import duho
from duho import Cmd, LoggingArgs

from ..db import jsonl_line
from ..entry import (
    _KINDS,
    AUTO,
    DEFAULT,
    FileRecords,
    normalize_caps,
    normalize_mode,
    normalize_name,
)
from ..exceptions import ExcludeSyntaxError, UsageError
from ..exclude import PathMatchStmt

if typing.TYPE_CHECKING:
    from ..project import Project

__all__: typing.List[str] = []


def _silence_broken_pipe() -> None:
    """Point stdout at ``os.devnull`` so a later implicit flush or close does
    not raise a second time (the recipe in the Python signal-handling docs).

    Gives up quietly when stdout has no real file descriptor (a captured,
    in-memory stream).
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


def _refuse_mcp_stdin(what: str) -> None:
    """Raise :class:`UsageError` when ``what`` would be read from standard
    input inside an MCP tool call, where stdin is the protocol channel."""
    if duho.running_mode() == "mcp":
        raise UsageError(
            f"reading {what} from standard input is not available in an MCP tool call"
        )


def _env_path(value: str) -> typing.Optional[Path]:
    """Env-var or CLI value to a path; empty means unset."""
    return Path(value) if value else None


def _env_str(value: str) -> typing.Optional[str]:
    """Env-var or CLI value to text; empty means unset."""
    return value if value else None


def _env_root(value: str) -> Path:
    """Env-var or CLI value to a path; empty means the current directory."""
    return Path(value) if value else Path(".")


def _parse_name(field: str) -> typing.Callable[[str], str]:
    """The ``type=`` converter of ``--owner``/``--group``: a bad name exits 2
    before anything is staged."""

    def parse(text: str) -> str:
        try:
            return normalize_name(text, field)
        except UsageError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    return parse


def _parse_mode(text: str) -> str:
    """The ``type=`` converter of ``--mode``/``--dir-mode``.

    Unlike :func:`~pkgforge.entry.normalize_mode`, an empty value is refused.
    """
    if text == "":
        raise argparse.ArgumentTypeError(
            "mode must not be empty; use '-' for the default, or '--'/'auto' "
            "to resolve from the staged file"
        )
    try:
        return normalize_mode(text)
    except UsageError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _parse_caps(text: str) -> str:
    """The ``type=`` converter of ``--caps``: a bad clause exits 2 before
    anything is staged."""
    try:
        return normalize_caps(text)
    except UsageError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _own(text: str) -> str:
    """The ``type=`` converter of ``--own``: a bad prefix exits 2; the
    normalized prefix is returned."""
    from ..config import _own_prefix

    try:
        return _own_prefix(text)
    except UsageError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _key_value(text: str) -> typing.Dict[str, str]:
    """The ``type=`` converter of ``-O``/``--meta``: one ``KEY=VALUE`` pair as
    a single-entry dict, merged by :class:`duho.UpdateAction`."""
    if "=" not in text:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    key, value = text.split("=", maxsplit=1)
    return {key: value}


def _exclude_stmt(text: str) -> str:
    """The ``type=`` converter of ``--exclude``: a bad statement exits 2; the
    text is returned for the library to parse again."""
    try:
        PathMatchStmt.parse(text)
    except ExcludeSyntaxError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return text


class OwnershipArgs(Cmd):
    """``--group``, ``--owner`` and ``--meta``: the fields of the commands that
    record an entry with no mode of its own."""

    group: duho.Arg[str, duho.Meta(type=_parse_name("group"))] = DEFAULT
    "group to record: an account name, '-' (the OS default) or '--' (resolve from the staged file)"
    ("--group", "-g")
    owner: duho.Arg[str, duho.Meta(type=_parse_name("owner"))] = DEFAULT
    "owner to record: an account name, '-' (the OS default) or '--' (resolve from the staged file)"
    ("--owner", "-o")
    meta: duho.Arg[
        typing.Dict[str, str],
        duho.Meta(
            action=duho.UpdateAction,
            type=_key_value,
            metavar="KEY=VALUE",
        ),
    ] = {}
    "extra metadata KEY=VALUE (repeatable)"
    ("-O", "--meta")

    def __init__(self, **kwargs: typing.Any) -> None:
        super().__init__(**kwargs)
        # argparse before 3.12 strips an attached "--" (--owner=--, -o--) to []
        # before any converter runs; it means AUTO.
        for name in ("owner", "group"):
            if getattr(self, name) == []:
                setattr(self, name, AUTO)


class EntryArgs(OwnershipArgs):
    """``--mode`` and the ownership fields: the commands that record a file
    entry."""

    mode: duho.Arg[str, duho.Meta(type=_parse_mode)] = DEFAULT
    "permission mode: 1-4 octal digits, '-' (leave default), '--' or 'auto' (resolve from the staged file)"
    ("--mode", "-m")

    def __init__(self, **kwargs: typing.Any) -> None:
        super().__init__(**kwargs)
        if self.mode == []:  # argparse before 3.12, as for owner and group
            self.mode = AUTO


class KindArgs(Cmd):
    """``--kind``: the commands that record an entry packaging may treat
    specially (a config file, a doc, a ghost)."""

    kind: duho.Arg[str, duho.Meta(choices=(DEFAULT, *_KINDS))] = DEFAULT
    (
        "how packaging treats the entry: file, config, config-noreplace, doc, "
        "license or ghost ('-': a matching rule's kind, else file)"
    )
    ("--kind", "-k")


class ExcludeArgs(Cmd):
    """``--exclude``/``-X``, shared by the commands that filter paths."""

    # duho.Append collects one statement per occurrence, where a bare list
    # field would gather one occurrence's tokens. Its help goes through
    # duho.Meta: duho.Append rejects help= at parser build.
    exclude: duho.Arg[
        typing.List[str],
        duho.Append(_exclude_stmt, metavar="STMT"),
        duho.Meta(
            help="exclude paths matching STMT (repeatable); "
            "STMT is [!][(?[!]test:arg)...]GLOB; an absolute GLOB is "
            "relative to the source (install), PATH (scan) or the DB path "
            "(dbdump); see the exclude-pattern guide for the grammar"
        ),
    ] = []
    ("--exclude", "-X")


def _load_config(path: Path, source: str) -> typing.Dict[str, typing.Any]:
    """The ``_config_loader_`` of the root: read ``path``, check the whole file
    before any command runs and return its mapping for duho to layer.

    ``source`` is where duho found the path (``"env"`` for ``PKGFORGE_CONFIG``).
    """
    from ..config import ProjectConfig, _read

    try:
        data = _read(path)
    except FileNotFoundError as exc:
        if source == "env":
            raise UsageError(f"PKGFORGE_CONFIG names no file: {path}") from exc
        raise
    try:
        ProjectConfig.from_dict(data)
    except UsageError as exc:
        raise UsageError(f"{path}: {exc}") from exc
    return data


class ProjectCmd(LoggingArgs, Cmd):
    """The options every command shares, and the bridge to
    :class:`~pkgforge.project.Project`."""

    #: The configuration file's mapping, set when a file was read.
    _config_data: typing.ClassVar[typing.Optional[typing.Mapping[str, typing.Any]]] = (
        None
    )

    db: duho.Arg[
        typing.Optional[Path],
        duho.Meta(env="PKGFORGE_DB", type=_env_path, metavar="PATH"),
    ] = None
    "file DB path (env PKGFORGE_DB); '-' (or unset, for install/scan) writes records to stdout, and dbdump reads them from stdin"
    ("--db",)
    db_format: duho.Arg[
        typing.Optional[str],
        duho.Meta(env="PKGFORGE_DB_FORMAT", type=_env_str, metavar="FORMAT"),
    ] = None
    "storage backend: jsonl, yaml, sqlite, or a registered DbProvider subclass's NAME (env PKGFORGE_DB_FORMAT); else inferred from the --db suffix"
    ("--db-format",)
    buildroot: duho.Arg[
        Path, duho.Meta(env="PKGFORGE_ROOT", type=_env_root, metavar="DIR")
    ] = Path(".")
    "staging root that maps to '/' in the DB (env PKGFORGE_ROOT; default '.')"
    ("--buildroot", "-r")
    own: duho.Arg[
        typing.List[str],
        duho.Append(_own, metavar="PREFIX"),
        duho.Meta(
            help="directory prefix the package owns (repeatable; replaces the "
            "file's own list): every recorded parent at or below it is recorded "
            "too, and dbdump --dirs owned keeps only those"
        ),
    ] = []
    ("--own",)
    debug: bool = False
    "print the traceback of an error before its one-line message"
    ("--debug",)

    def _no_file_db(self) -> bool:
        """Whether records go to stdout: ``--db`` is unset or ``-``."""
        return self.db is None or str(self.db) == "-"

    def _config_validate_(self, config: typing.Mapping[str, typing.Any]) -> None:
        """Keep the configuration file's mapping on the command.

        duho calls this on the selected command after a parse that read a file;
        it is how a command gets the file's tables.
        """
        self._config_data = config

    def _project(self) -> "Project":
        """The project these options describe."""
        from ..config import ProjectConfig
        from ..project import Project

        config = (
            ProjectConfig()
            if self._config_data is None
            else ProjectConfig.from_dict(self._config_data)
        )
        return Project(
            self.buildroot,
            None if self._no_file_db() else self.db,
            db_format=self.db_format or None,
            # duho has layered the command line over the file's own list.
            config=dataclasses.replace(config, own=tuple(self.own)),
        )

    def _emit(self, records: FileRecords) -> None:
        """Print ``records`` as JSON Lines when there is no file DB, so a
        pipeline can capture what would have been recorded."""
        if self._no_file_db():
            for path, entry in records:
                print(jsonl_line(path, entry), end="")
