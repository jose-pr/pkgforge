"""``record_tree``: after staging a directory or archive DESTINATION, also
record every path below it that the DB does not already hold, so no separate
``scan --missing`` step is needed for a tree ``install`` itself just staged.
"""

from __future__ import annotations

from pathlib import Path

from .. import _tree
from ..entry import AUTO, DEFAULT, FileEntry, FileType, _normalize_field
from ..exclude import PathMatch


class _RecordTree:
    """Mixin supplying the ``record_tree`` walk.

    Uses the host :class:`~pkgforge._install._Installer`'s ``type``,
    ``exclude``, ``meta``, ``owner``, ``group``, ``record_tree``, ``project``,
    ``loaddb``/``add_entry`` and ``_logger_``.
    """

    def _record_tree(self, dest: Path) -> None:
        """Record ``dest``'s children.

        A no-op unless :attr:`record_tree` is set and this clone's resolved
        ``type`` is :attr:`~pkgforge.entry.FileType.Directory` (a directory
        copy, an archive, or an empty ``-t directory``) -- silently ignored
        for a file or symlink install; the caller never calls this at all
        when the install records nothing.
        """
        if not self.record_tree or self.type != FileType.Directory:
            return

        # A child's mode comes from a matching rule or `[defaults]` and the
        # child is chmod'ed to it (apply_mode), else the mode is read from
        # disk (the fallback) -- DESTINATION's own `-m` never inherits down,
        # and there is no per-child `--dir-mode` equivalent.
        # owner/group/meta are this install's own -o/-g/-O, `--` resolved per
        # child from disk the same as -m/-o/-g always resolve AUTO. kind and
        # caps come from rules only, never from this install's -k/--caps.
        base: FileEntry = {
            "mode": DEFAULT,
            "owner": _normalize_field(self.owner),
            "group": _normalize_field(self.group),
            "type": AUTO,
            "kind": DEFAULT,
            "caps": DEFAULT,
            "meta": dict(self.meta),
        }
        bases = {
            FileType.File: base,
            FileType.Directory: base,
            FileType.Symlink: base,
        }

        # DEST holds the same tree as the source, so a pattern relative to
        # the source is relative to DEST here.
        matcher = PathMatch(self.exclude, dest) if self.exclude else None

        recorder = _tree.TreeRecorder(
            self.project,
            bases,
            matcher,
            dict(self.meta),
            self.loaddb(),
            logger=self._logger_,
            write=self.add_entry,
            fallback={"mode": AUTO},
            apply_mode=True,
        )
        recorder.walk(dest)
