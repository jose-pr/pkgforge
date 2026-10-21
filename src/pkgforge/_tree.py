"""Shared tree walker for ``scan`` and ``install`` with ``record_tree``: records
every path under a root into the DB, skipping the file DB itself and an
optional ``-X`` matcher, and filling gaps only -- never overwriting a key
``known`` already holds a (non-``None``) entry for.
"""

from __future__ import annotations

import logging
import os
import stat
import typing
from pathlib import Path

from .entry import (
    AUTO,
    DEFAULT,
    FileEntry,
    FileType,
    _file_type,
    _resolve_stat,
    apply_entry,
)
from .exceptions import PkgForgeError
from .exclude import PathMatch

if typing.TYPE_CHECKING:
    from .project import Project


class TreeRecorder:
    """Walk a real, already-on-disk tree and record each path's
    :class:`~pkgforge.entry.FileEntry` through ``write``.

    Shared by ``scan`` (``known`` is the loaded DB only under ``missing``,
    else ``{}``, so a plain scan replaces existing entries) and ``install``
    with ``record_tree`` (``known`` is always a fresh
    :meth:`~pkgforge.project.Project.load`, so it only ever fills gaps): a key
    already present (a non-``None`` value) in ``known`` is never overwritten.

    ``bases`` supplies the un-resolved (``AUTO``/``DEFAULT``-valued) entry
    template per :class:`~pkgforge.entry.FileType`; :meth:`record` fills each
    ``DEFAULT`` field from the project's rules and defaults, then each one
    still ``DEFAULT`` that ``fallback`` names (a symlink never takes a mode),
    then resolves each ``AUTO``-valued field from the path's own ``lstat``.
    ``matcher`` is
    ``None`` when there is nothing to filter (no ``-X`` given); callers build
    their own, rooted at the tree they walk: ``scan``'s at PATH,
    ``install``'s record-tree at DESTINATION. ``write(dbpath, entry)`` is the
    caller's recording callback; ``logger`` receives the skip and exclusion
    messages. With ``apply_mode`` a non-symlink path whose resolved mode
    differs from its on-disk permission bits is chmod'ed to it (mode only,
    never a chown) before it is written.
    """

    def __init__(
        self,
        project: "Project",
        bases: typing.Mapping[FileType, FileEntry],
        matcher: typing.Optional[PathMatch],
        meta: typing.Dict[str, str],
        known: typing.Mapping[str, typing.Optional[FileEntry]],
        *,
        logger: logging.Logger,
        write: typing.Callable[[str, FileEntry], typing.Any],
        fallback: typing.Mapping[str, str] = {},
        apply_mode: bool = False,
    ) -> None:
        self.project = project
        self.bases = bases
        self.matcher = matcher
        self.meta = meta
        self.known = known
        self.logger = logger
        self.write = write
        self.fallback = fallback
        self.apply_mode = apply_mode
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
        if project.db is not None:
            db_path = Path(project.db.path)
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
                        self.logger.debug("Skipping the file DB %s", path)
                    else:
                        self.logger.warning(
                            "Skipping the file DB %s found inside the "
                            "recorded tree; keep --db (and dbdump output) "
                            "outside --buildroot",
                            path,
                        )
                        self._warned_db_skip = True
                return True
            if self.matcher is not None and self.matcher.match(path, meta=self.meta):
                self.logger.debug("Excluding %s", path)
                return False
            fspath = self.project.dbpath(path)
            if self.known.get(fspath) is None:
                st = path.lstat()
                ftype = _file_type(path, st)
                entry = self.project._configured(
                    fspath, {**self.bases[ftype], "type": ftype}, local=path
                )
                for name, value in self.fallback.items():
                    if entry[name] == DEFAULT and not (
                        name == "mode" and ftype == FileType.Symlink
                    ):
                        entry = {**entry, name: value}
                entry = _resolve_stat(entry, path, st, lookupval=AUTO)
                if (
                    self.apply_mode
                    and ftype != FileType.Symlink
                    and entry["mode"] != DEFAULT
                    and int(entry["mode"], 8) != stat.S_IMODE(st.st_mode)
                ):
                    apply_entry(
                        {**entry, "owner": DEFAULT, "group": DEFAULT},
                        path,
                        logger=self.logger,
                    )
                self.logger.debug("Updating file entry for: %s", fspath)
                self.write(fspath, entry)
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
