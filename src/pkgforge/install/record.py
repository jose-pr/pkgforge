"""``install --record-tree``: after staging a directory or archive
DESTINATION, also record every path below it that the DB does not already
hold -- replacing the ``scan --missing`` step the shipped recipe used to
need for a tree ``install`` itself just staged.
"""

from __future__ import annotations

import os
from pathlib import Path

import duho

from .. import _tree
from ..entry import AUTO, DEFAULT, FileEntry, FileType, _normalize_field
from ..exclude import PathMatch


def _env_flag(value: str) -> bool:
    """Env-var default for :attr:`_RecordTree.record_tree`: truthy tokens
    only (``1 true yes on y t``, case-insensitive; anything else, including
    empty/unset, is ``False``). Used only for the class default -- a direct
    Python-API construction bypasses duho's own strict CLI/env bool parsing
    entirely (the same reason :func:`~pkgforge.install.transfer._env_method`
    exists), so this never raises for a bogus value, unlike duho's own
    resolution through ``main()``/``parse``, which exits 2.
    """
    return value.strip().lower() in ("1", "true", "yes", "on", "y", "t")


class _RecordTree(duho.Cmd):
    """Mixin supplying ``install --record-tree``/``--no-record-tree`` (env
    ``PKGFORGE_INSTALL_RECORD_TREE``) and the walk itself.

    Uses the host :class:`~pkgforge.install.Install`'s ``type``, ``exclude``,
    ``meta``, ``owner``, ``group``, and (via :class:`~pkgforge.command.PkgForgeCmd`)
    ``loaddb``/``add_entry``/``buildpath``.
    """

    record_tree: duho.Arg[bool, duho.NS(env="PKGFORGE_INSTALL_RECORD_TREE")] = (
        _env_flag(os.environ.get("PKGFORGE_INSTALL_RECORD_TREE", ""))
    )
    (
        "after installing a directory or archive DESTINATION, also record "
        "every path below it not already in the DB: owner/group/meta from "
        "this install, mode and type from disk (symlinks '-'), honouring "
        "-X (env PKGFORGE_INSTALL_RECORD_TREE); ignored for a file/symlink "
        "source, and a no-op under --noentry"
    )
    ("--record-tree",)

    def _record_tree(self, dest: Path) -> None:
        """Record ``dest``'s children.

        A no-op unless :attr:`record_tree` is set and this clone's resolved
        ``type`` is :attr:`~pkgforge.entry.FileType.Directory` (a directory
        copy, an archive, or an empty ``-t directory``) -- silently ignored
        for a file or symlink install; the caller never calls this at all
        under ``--noentry``.
        """
        if not self.record_tree or self.type != FileType.Directory:
            return

        # Children's mode is always read from disk (AUTO), for both files
        # and directories -- DESTINATION's own `-m` never inherits down, and
        # there is no per-child `--dir-mode` equivalent. owner/group/meta
        # are this install's own -o/-g/-O, `--` resolved per child from disk
        # the same as -m/-o/-g always resolve AUTO.
        base: FileEntry = {
            "mode": AUTO,
            "owner": _normalize_field(self.owner),
            "group": _normalize_field(self.group),
            "type": AUTO,
            "meta": dict(self.meta),
        }
        bases = {
            FileType.File: base,
            FileType.Directory: base,
            FileType.Symlink: {**base, "mode": DEFAULT},
        }

        matcher = None
        if self.exclude:
            # Built directly (not via _Staging._exclude_matcher), so this
            # never repeats the PathMatch.unreachable() warning staging's
            # own matcher already logged once for the same statements.
            matcher = PathMatch(
                self.exclude, dest, installroot=self.buildpath(dest).as_posix()
            )

        recorder = _tree.TreeRecorder(
            self, bases, matcher, dict(self.meta), self.loaddb()
        )
        recorder.walk(dest)
