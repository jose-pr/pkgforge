"""``Project.verify``: compare a build root with the file DB."""

from __future__ import annotations

import logging
import os
import typing
from pathlib import Path

from ._tree import _TreeWalk
from .entry import FileEntry, FileKind, FileType, _file_type
from .exclude import PathMatch, PathMatchStmt

if typing.TYPE_CHECKING:
    from .project import Project

#: ``(problem, path, recorded, found)``; see :class:`pkgforge.project.VerifyIssue`.
Issue = typing.Tuple[str, str, typing.Optional[str], typing.Optional[str]]

#: The ``found`` value for a fifo, socket or device, which no entry can record.
_SPECIAL = "special"


def _type_of(path: Path) -> str:
    """The on-disk type name of ``path`` (``lstat``, never followed)."""
    try:
        return _file_type(path, path.lstat()).value
    except TypeError:
        return _SPECIAL


class _Checker(_TreeWalk):
    """Compare each walked path with its live entry, and collect the paths
    ``-X`` pruned."""

    def __init__(
        self,
        project: "Project",
        live: typing.Mapping[str, FileEntry],
        matcher: typing.Optional[PathMatch],
        *,
        logger: logging.Logger,
    ) -> None:
        super().__init__(project, matcher, logger=logger)
        self.live = live
        self.issues: typing.List[Issue] = []
        #: ``dbpath + "/"`` of every path ``-X`` pruned.
        self.pruned: typing.List[str] = []

    def record(self, path: Path) -> bool:
        if self._skips_db(path):
            return True
        dbpath = self.project.dbpath(path)
        entry = self.live.get(dbpath)
        # A DB path with no root, as dbdump matches it.
        if self.matcher is not None and self.matcher.match(dbpath, entry=entry):
            self.logger.debug("Excluding %s", path)
            self.pruned.append(dbpath + "/")
            return False
        found = _type_of(path)
        if entry is None:
            # A directory the package does not own is not a problem: only
            # files and links are reported, as rpmbuild and dh_missing do.
            if found != FileType.Directory.value:
                self.issues.append(("unrecorded", dbpath, None, found))
        elif found != entry["type"]:
            self.issues.append(("type", dbpath, entry["type"], found))
        elif found == FileType.Symlink.value and "target" in entry["meta"]:
            actual = os.readlink(path)
            if actual != entry["meta"]["target"]:
                self.issues.append(("target", dbpath, entry["meta"]["target"], actual))
        return True


def verify(
    project: "Project",
    live: typing.Mapping[str, FileEntry],
    stmts: typing.Sequence[PathMatchStmt],
    *,
    logger: logging.Logger,
) -> typing.List[Issue]:
    """The differences between the build root and the live entries ``live``,
    sorted by path then problem; ``stmts`` are the ``-X`` statements."""
    matcher = PathMatch(stmts) if stmts else None
    checker = _Checker(project, live, matcher, logger=logger)
    checker.walk(project._rootpath("/"))
    pruned = tuple(checker.pruned)
    ghost = FileKind.Ghost.value
    for dbpath, entry in live.items():
        if entry["kind"] == ghost or (pruned and dbpath.startswith(pruned)):
            continue
        if matcher is not None and matcher.match(dbpath, entry=entry):
            continue
        if not os.path.lexists(project.localpath(dbpath)):
            checker.issues.append(("missing", dbpath, entry["type"], None))
    checker.issues.sort(key=lambda issue: (issue[1], issue[0]))
    return checker.issues
