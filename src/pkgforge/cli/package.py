"""``package``: build one package per declared package from the file DB."""

from __future__ import annotations

import typing
from pathlib import Path

from ..exceptions import UsageError
from ._common import ProjectCmd, _open_stdout

__all__ = ["Package"]


class Package(ProjectCmd):
    """Build one package per declared package from the file DB and the build root"""

    _parsername_ = "package"
    _logger_name_ = "pkgforge.package"

    format: str
    "package format: deb or tar"
    ("--format", "-f")
    output: Path = Path(".")
    "directory the packages are written into, created if missing"
    ("--output", "-o")
    #: Not ``arch``: duho layers the ``[package]`` table of pkgforge.cfg onto this
    #: command, and its ``arch`` (``all`` or ``any``) would fill the flag.
    arch_name: typing.Optional[str] = None
    (
        "architecture name of an 'any' package, in the format's own spelling; "
        "default: this machine's"
    )
    ("--arch",)
    compression: typing.Optional[str] = None
    "payload compression the format offers; default: the format's own"
    ("--compression",)

    def __call__(self) -> int:
        if str(self.db) == "-":
            raise UsageError(
                "package reads the build root and the file DB; --db - has no build root"
            )
        paths = self._project().package(
            self.format, self.output, arch=self.arch_name, compression=self.compression
        )
        with _open_stdout() as out:
            for path in paths:
                out.write(f"{path}\n".encode("utf-8", "surrogateescape"))
        return 0
