"""The files of one build, created all or nothing."""

from __future__ import annotations

import contextlib
import os
import shutil
import typing
from pathlib import Path


class PackageWriter:
    """Creates the package files of one :func:`~pkgforge.package.build`.

    Every file is written to a sibling ``.NAME.pkgforge-tmp`` and renamed over
    its final path by :meth:`commit`, so a build that fails leaves every target
    as it was. The output directory, and any parent it lacks, is created with
    the first file and removed again when the build fails.
    """

    def __init__(self) -> None:
        self._temps: typing.Dict[Path, Path] = {}
        self._open: typing.List[typing.BinaryIO] = []
        self._created: typing.List[Path] = []

    def _temp(self, path: Path) -> Path:
        missing = []
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir()
            self._created.append(directory)
        temp = path.with_name(f".{path.name}.pkgforge-tmp")
        self._temps[path] = temp
        return temp

    def create(self, path: Path) -> typing.BinaryIO:
        """Open the file that :meth:`commit` renames to ``path``, for writing.

        The caller closes it before :meth:`commit`.
        """
        handle = open(self._temp(path), "wb")
        self._open.append(handle)
        return handle

    def move(self, source: Path, path: Path) -> None:
        """Take the finished file ``source`` as the content of ``path``."""
        shutil.move(os.fspath(source), os.fspath(self._temp(path)))

    def commit(self) -> None:
        """Rename every file to its path, in creation order."""
        for handle in self._open:
            handle.close()
        for path, temp in self._temps.items():
            os.replace(temp, path)
        self._temps.clear()
        self._created.clear()

    def abort(self) -> None:
        """Remove every file not yet renamed and every directory created."""
        for handle in self._open:
            with contextlib.suppress(OSError):
                handle.close()
        for temp in self._temps.values():
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()
        for directory in reversed(self._created):
            with contextlib.suppress(OSError):
                directory.rmdir()
        self._temps.clear()
        self._created.clear()
