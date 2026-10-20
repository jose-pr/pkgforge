"""The command line and the library keep the same names for the same inputs."""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import subprocess
import sys
import typing
from pathlib import Path

import pytest

from pkgforge import Project
from pkgforge.cli._common import ProjectCmd
from pkgforge.cli.dbdump import DbDump
from pkgforge.cli.install import Install
from pkgforge.cli.mkdir import Mkdir
from pkgforge.cli.scan import ScanCmd
from pkgforge.cli.stage import Stage
from pkgforge.cli.symlink import Symlink

SRC = str(Path(__file__).resolve().parents[1] / "src")
#: The names of the root package: the library API, nothing of the command line.
ROOT_ALL = (
    "DbError",
    "DbProvider",
    "DumpError",
    "DumpFormat",
    "ExcludeSyntaxError",
    "FileEntries",
    "FileEntry",
    "FileRecords",
    "FileType",
    "InvalidEntryError",
    "PkgForgeError",
    "Project",
    "StrPath",
    "UnsupportedOutputError",
    "UsageError",
    "__version__",
    "open_db",
)

#: Command fields whose library keyword is spelled differently.
RENAMED = {
    "noentry": "record",
    "no_target_directory": "into",
    "source": "sources",
    "destination": "dest",
    "format": "fmt",
}
#: Command-line spellings with no keyword of their own: -D and -d fold into
#: other fields.
CLI_ONLY = {"D", "d"}


def _command_fields(cls: type) -> typing.Set[str]:
    own = set(typing.get_type_hints(cls)) - set(typing.get_type_hints(ProjectCmd))
    return {RENAMED.get(name, name) for name in own - CLI_ONLY}


def _keywords(method: typing.Callable[..., object]) -> typing.Set[str]:
    return set(inspect.signature(method).parameters) - {"self"}


@pytest.mark.parametrize(
    "command, method",
    [
        (Install, Project.install),
        (ScanCmd, Project.scan),
        (DbDump, Project.dump),
        (Stage, Project.stage),
        (Mkdir, Project.mkdir),
        (Symlink, Project.symlink),
    ],
    ids=["install", "scan", "dbdump", "stage", "mkdir", "symlink"],
)
def test_command_fields_match_library_keywords(command, method):
    assert _command_fields(command) == _keywords(method)


def _public_modules() -> typing.List[str]:
    import pkgforge

    names = ["pkgforge"]
    for info in pkgutil.walk_packages(pkgforge.__path__, "pkgforge."):
        if info.name == "pkgforge.__main__":
            continue
        if any(part.startswith("_") for part in info.name.split(".")):
            continue
        names.append(info.name)
    return names


@pytest.mark.parametrize("name", _public_modules())
def test_public_module_declares_all(name):
    module = importlib.import_module(name)
    assert hasattr(module, "__all__"), name
    unresolved = [item for item in module.__all__ if not hasattr(module, item)]
    assert not unresolved, unresolved


def test_importing_the_library_does_not_load_the_command_line():
    code = (
        "import sys, pkgforge\n"
        "loaded = [m for m in ('duho', 'pkgforge.cli', 'yaml', 'sqlite3',"
        " 'anycfg', 'pathapi') if m in sys.modules]\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": SRC},
    )
    assert result.stdout.strip() == ""


def test_importing_the_command_line_does_not_load_the_config_reader():
    code = (
        "import sys, pkgforge.cli\n"
        "loaded = [m for m in ('anycfg', 'pathapi') if m in sys.modules]\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": SRC},
    )
    assert result.stdout.strip() == ""


def test_root_all_is_the_documented_surface():
    import pkgforge

    assert sorted(pkgforge.__all__) == sorted(ROOT_ALL)
