"""The command line and the library keep the same names for the same inputs."""

from __future__ import annotations

import inspect
import typing

import pytest

from pkgforge import Project
from pkgforge.cli._common import ProjectCmd
from pkgforge.cli.dbdump import DbDump
from pkgforge.cli.install import Install
from pkgforge.cli.scan import ScanCmd

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
    ],
    ids=["install", "scan", "dbdump"],
)
def test_command_fields_match_library_keywords(command, method):
    assert _command_fields(command) == _keywords(method)
