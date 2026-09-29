"""Link/move staging machinery shared by :class:`~pkgforge.install._Staging`."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path


def _env_method(value: str) -> str:
    """Env-var/CLI value -> an install method (one of :data:`_INSTALL_METHODS`).

    Empty means unset, the same as :meth:`PkgForgeCmd.db`'s own env fields:
    giving this a non-``str`` ``type=`` converter (even though it's just an
    ``str -> str`` identity otherwise) is what makes duho's "empty env value
    means unset" rule apply to it -- a bare ``str``-typed field would keep an
    explicitly-empty ``PKGFORGE_INSTALL_METHOD=`` as a real, choices-checked
    value instead of falling through to the default.
    """
    return value if value else "copy"


#: errnos `os.link` can raise for a source it just can't hardlink (a
#: different filesystem, ``fs.protected_hardlinks``, the per-inode link
#: limit, or a permission error) -- ``--method link`` falls back to copying
#: that one file/entry instead of failing the whole install. Anything else
#: propagates.
_LINK_FALLBACK_ERRNOS = frozenset(
    (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.EACCES)
)


def _try_link(src: Path, tmp: Path) -> bool:
    """Attempt ``os.link(src, tmp)`` for ``--method link``.

    Returns ``True`` on success, ``False`` when it failed for one of
    :data:`_LINK_FALLBACK_ERRNOS` (the caller then falls back to a copy);
    any other ``OSError`` propagates.
    """
    try:
        os.link(src, tmp)
        return True
    except OSError as exc:
        if exc.errno in _LINK_FALLBACK_ERRNOS:
            return False
        raise


class _Transfer:
    """``--method link``/``--method move`` staging helpers for
    :class:`~pkgforge.install._Staging`; uses the host's ``_logger_`` and
    ``source``."""

    #: Set once :meth:`_warn_link_fallback` has logged, so a tree with many
    #: fallback files (or a multi-source install with several such clones)
    #: gets one WARNING each, not one per file.
    _link_fallback_warned: bool = False
    #: Set by :meth:`_stage_file`/:meth:`_stage_directory` only for the
    #: staging actions that consume ``self.source`` as a single reversible
    #: unit (a file move, or a directory's whole-tree rename fast path) --
    #: never for a merge move, which is not atomic and, like a partial
    #: copy merge, is not rolled back. ``Install._stage`` reads this to
    #: decide whether a later apply/replace/record failure can still move
    #: the data back to ``self.source``.
    _move_atomic: bool = False

    def _warn_link_fallback(self) -> None:
        """Log one WARNING (per :class:`Install` clone -- i.e. per source,
        which is per command for the common single-source case) the first
        time ``--method link`` has to fall back to copying instead of
        hardlinking a file, rather than once per file."""
        if self._link_fallback_warned:
            return
        self._link_fallback_warned = True
        self._logger_.warning(
            "--method link: one or more sources could not be hardlinked "
            "(different filesystem, protected_hardlinks, the per-inode "
            "link limit, or a permission error); copying instead"
        )

    def _move_file(self, src: Path, tmp: Path) -> None:
        """Move ``src``'s content into ``tmp`` for ``--method move``:
        ``os.replace`` when both paths are on the same filesystem, else
        ``shutil.move`` (a copy, then unlinking ``src``).
        ``tmp`` must not already exist (mirrors :func:`_try_link` and the
        plain-copy branch, which also stage into a freshly reserved name).
        """
        try:
            os.replace(src, tmp)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.move(os.fspath(src), os.fspath(tmp))

    def _rollback_move(self, current: Path) -> None:
        """Move ``current`` -- wherever ``--method move`` just left
        ``self.source``'s data, a sibling temp or ``dst`` itself once
        ``os.replace`` already ran -- back onto ``self.source`` after a
        later apply/replace/record failure: a move must never lose the
        only copy. Only called when :attr:`_move_atomic` is set,
        i.e. never for a merge move (not a single reversible unit; a
        partial merge is not rolled back, same as for a copy).

        Best effort: logs and swallows its own failure rather than raising,
        so it never shadows the real exception the caller is already
        re-raising.
        """
        if not (current.exists() or current.is_symlink()):
            return  # nothing left to move back
        try:
            if current.is_dir() and not current.is_symlink():
                os.rename(current, self.source)
            else:
                try:
                    os.replace(current, self.source)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    shutil.move(os.fspath(current), os.fspath(self.source))
        except OSError as exc:
            self._logger_.error(
                "--method move: could not restore %s to %s after a staging "
                "failure (%s); the source is gone",
                current,
                self.source,
                exc,
            )

    def _link_copy_function(self, src_path: str, dst_path: str) -> None:
        """``shutil.copytree``'s per-file ``copy_function`` for a directory
        source staged with ``--method link``: hardlink each file, falling
        back to a copy (the same errnos, and the same once-per-clone
        warning, as the single-file path) for one ``os.link`` can't span.
        """
        src = Path(src_path)
        tmp = Path(dst_path)
        if _try_link(src, tmp):
            return
        self._warn_link_fallback()
        shutil.copyfile(src_path, dst_path)
        shutil.copymode(src_path, dst_path)

    def _move_copy_function(self, src_path: str, dst_path: str) -> None:
        """``shutil.copytree``'s per-file ``copy_function`` for a directory
        source staged with ``--method move``'s merge path: move (not copy)
        each file, via :meth:`_move_file`. A symlink in the tree is
        recreated fresh at the destination by ``copytree`` itself (never
        routed through ``copy_function``), so it is left as-is in the
        source; :meth:`_stage_directory` only removes directories this
        leaves *empty* afterwards, never a symlink's own parent.
        """
        self._move_file(Path(src_path), Path(dst_path))

    def _remove_empty_dirs(self, root: Path) -> None:
        """Bottom-up: remove every directory under (and including) ``root``
        that is empty right now. Used after a ``--method move`` merge,
        where an ``--exclude`` (or a symlink ``copytree`` recreated at the
        destination without consuming the source's own copy) can leave some
        files behind in ``root`` -- unlike ``shutil.rmtree``, this never
        touches a directory that still holds something.

        Freshly re-``listdir``s each directory rather than trusting
        ``os.walk``'s own (per-directory, snapshotted-once) ``dirnames``/
        ``filenames`` lists, which -- since this walk is ``topdown=False``,
        bottom-up -- can otherwise still list a child this same loop already
        removed a moment earlier.
        """
        for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass
