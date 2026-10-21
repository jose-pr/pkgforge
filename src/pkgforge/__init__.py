"""pkgforge: stage files into a build root and record their install metadata.

:class:`Project` is the library API; the command line is :mod:`pkgforge.cli`,
which importing this package never loads.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _version

from .db import DbProvider, open_db
from .dbdump import DumpFormat
from .entry import FileEntries, FileEntry, FileRecords, FileType, StrPath
from .exceptions import (
    DbError,
    DumpError,
    ExcludeSyntaxError,
    InvalidEntryError,
    PkgForgeError,
    UnsupportedOutputError,
    UsageError,
)
from .package import PackageFormat
from .project import Project, VerifyIssue

try:  # resolve the installed distribution version, if any
    __version__ = _version("pkgforge")
except PackageNotFoundError:  # not installed (running from a source checkout)
    __version__ = "0.0.0"

__all__ = [
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
    "PackageFormat",
    "PkgForgeError",
    "Project",
    "StrPath",
    "UnsupportedOutputError",
    "UsageError",
    "VerifyIssue",
    "__version__",
    "open_db",
]
