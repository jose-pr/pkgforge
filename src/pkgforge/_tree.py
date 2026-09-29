"""Shared tree walker for ``scan`` and ``install --record-tree``: records
every path under a root into the DB, skipping the file DB itself and an
optional ``-X`` matcher, and filling gaps only -- never overwriting a key
``known`` already holds a (non-``None``) entry for.
"""

from __future__ import annotations

import os
import typing
from pathlib import Path

from .entry import AUTO, FileEntry, FileType, _file_type, _resolve_stat
from .errors import PkgForgeError
from .exclude import PathMatch

if typing.TYPE_CHECKING:
    from .command import PkgForgeCmd


class TreeRecorder:
    """Walk a real, already-on-disk tree and record each path's
    :class:`~pkgforge.entry.FileEntry` into ``cmd``'s file DB.

    Shared by ``scan`` (``known`` is the loaded DB only under ``--missing``,
    else ``{}``, so a plain scan replaces existing entries) and
    ``install --record-tree`` (``known`` is always a fresh
    :meth:`~pkgforge.command.PkgForgeCmd.loaddb`, so record-tree only ever
    fills gaps): a key already present (a non-``None`` value) in ``known``
    is never overwritten.

    ``bases`` supplies the un-resolved (``AUTO``/``DEFAULT``-valued) entry
    template per :class:`~pkgforge.entry.FileType`; :meth:`record` resolves
    each ``AUTO``-valued field from the path's own ``lstat``. ``matcher`` is
    ``None`` when there is nothing to filter (no ``-X`` given); callers build
    their own -- ``scan``'s anchored at PATH, ``install --record-tree``'s at
    DESTINATION -- since each warns about an unreachable statement
    differently (or, for the latter, not at all -- staging's own matcher
    already warned once).
    """

    def __init__(
        self,
        cmd: "PkgForgeCmd",
        bases: typing.Mapping[FileType, FileEntry],
        matcher: typing.Optional[PathMatch],
        meta: typing.Dict[str, str],
        known: typing.Mapping[str, typing.Optional[FileEntry]],
    ) -> None:
        self.cmd = cmd
        self.bases = bases
        self.matcher = matcher
        self.meta = meta
        self.known = known
        #: Paths actually written this run -- never a path skipped by -X,
        #: the file-DB skip, or a key `known` already holds.
        self.recorded = 0
        # Precomputed once (never per walked path): the file DB's own
        # directory (realpath) and the basenames that matter -- the DB file
        # itself, and a sqlite backend's -journal/-wal/-shm sidecars, which
        # exist only transiently during a write but cost nothing to name
        # here too.
        self._db_skip_dir: typing.Optional[str] = None
        self._db_skip_names: typing.Optional[typing.Set[str]] = None
        if not cmd._no_file_db():
            db_path = Path(cmd.db)
            self._db_skip_dir = os.path.realpath(db_path.parent)
            self._db_skip_names = {
                db_path.name + suffix for suffix in ("", "-journal", "-wal", "-shm")
            }
        self._warned_db_skip = False

    def record(self, path: Path) -> bool:
        """Record ``path``'s entry (unless already known, or excluded).

        Returns ``False`` only when ``matcher`` excluded ``path`` -- the
        signal :meth:`walk` uses to prune an excluded directory's subtree:
        never descended into, and its contents never recorded either,
        matching a directory copy's/archive prune's own ``-X`` behavior.
        """
        try:
            if self._db_skip_names is not None and path.name in self._db_skip_names:
                if os.path.realpath(path.parent) == self._db_skip_dir:
                    if self._warned_db_skip:
                        self.cmd._logger_.debug("Skipping the file DB %s", path)
                    else:
                        self.cmd._logger_.warning(
                            "Skipping the file DB %s found inside the "
                            "recorded tree; keep --db (and dbdump output) "
                            "outside --buildroot",
                            path,
                        )
                        self._warned_db_skip = True
                return True
            if self.matcher is not None and self.matcher.match(path, meta=self.meta):
                self.cmd._logger_.debug("Excluding %s", path)
                return False
            fspath = os.fspath(self.cmd.buildpath(path))
            if self.known.get(fspath) is None:
                st = path.lstat()
                ftype = _file_type(path, st)
                entry = _resolve_stat(self.bases[ftype], path, st, lookupval=AUTO)
                self.cmd._logger_.debug("Updating file entry for: %s", fspath)
                self.cmd.add_entry(fspath, entry=entry)
                self.recorded += 1
            return True
        except TypeError as exc:
            # _file_type raises TypeError for a fifo/socket. A glob-only -X
            # (e.g. '*.fifo') already skipped such a path above without ever
            # reaching here; this is the path that cannot be recorded at all.
            raise PkgForgeError(
                f"{path}: unsupported file type (not a file, directory "
                "or symlink); exclude it with -X"
            ) from exc

    def walk(self, top: Path) -> None:
        """Record every path under ``top`` -- never ``top`` itself."""
        for base, dirs, files in os.walk(top):
            # Prune in place: os.walk only descends into names still left
            # in `dirs` after this line runs.
            dirs[:] = [d for d in dirs if self.record(Path(base, d))]
            for name in files:
                self.record(Path(base, name))
