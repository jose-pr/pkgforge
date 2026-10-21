"""The one place a ``/``-rooted path is mapped under a build root."""

from __future__ import annotations

import os
import posixpath
import typing
from pathlib import Path, PurePath, PurePosixPath

from .exceptions import UsageError


def under_root(
    root: Path,
    real_root: str,
    path: typing.Union[str, os.PathLike],
    *,
    follow_final: bool = False,
) -> Path:
    """The path ``path`` names under ``root``, whose ``os.path.realpath`` is
    ``real_root``.

    Raises :class:`~pkgforge.exceptions.UsageError` for a ``..`` that climbs
    above the root and for a symlinked component that leads outside it. The
    leaf is followed only when ``follow_final`` is true and it already names a
    directory; otherwise just its parent is resolved. The check is not a
    defence against a concurrent writer.
    """
    # A PurePath converts with its own as_posix(): os.fspath() would use the
    # native separator, a backslash on Windows, which PurePosixPath misreads.
    if isinstance(path, PurePath):
        posix_path = path.as_posix()
    else:
        posix_path = PurePosixPath(os.fspath(path)).as_posix()
    rel = posixpath.normpath(posix_path.lstrip("/") or ".")
    if rel == ".." or rel.startswith("../"):
        raise UsageError(f"{path}: resolves outside --buildroot")
    if rel == ".":
        return root
    local = Path(root, rel)
    check = local if (follow_final and os.path.isdir(local)) else local.parent
    try:
        common = os.path.commonpath([real_root, os.path.realpath(check)])
    except ValueError:
        # Windows: a drive-qualified path replaces the root's drive.
        raise UsageError(f"{path}: resolves outside --buildroot") from None
    if common != real_root:
        raise UsageError(f"{path}: a symlink leads outside --buildroot")
    return local
