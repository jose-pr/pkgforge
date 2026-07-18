"""Path/entry matching for ``--exclude`` and dump filters.

A match statement is written as an optional leading ``!`` (negate), zero or
more inline tests ``(?name:arg)`` (or ``(?!name:arg)`` to invert a single
test), and a trailing glob pattern, e.g.::

    (?type:file)**/*.pyc        # every .pyc file
    !(?meta:keep=1)**/tmp/**    # keep entries tagged keep=1 under tmp/
"""

from __future__ import annotations

import os
import re
import typing
from pathlib import Path

from duho import NS

from .common import FileEntry, FileType

FilterTestRe = re.compile(r"^\(\?([^:())]+):([^()]+)\)")


def filetypetest(type):
    type = FileType(type)
    return lambda x, e: e["type"] == type


def metatest(meta: str):
    k, v = meta.split("=", maxsplit=1)
    return lambda x, e: e["meta"].get(k) == v


class PathTest(typing.Protocol):
    GENERATORS = {"type": filetypetest, "meta": metatest}

    def __call__(self, path: Path, entry: FileEntry) -> bool:
        raise NotImplementedError(self)

    @classmethod
    def factory(cls, name: str, arg: str, inverse: bool):
        test = cls.GENERATORS[name](arg)
        if inverse:

            def _test(path: Path, entry: FileEntry):
                return not test(path, entry)

            return _test
        return test


class PathMatchStmt(NS):
    negate: bool
    tests: "typing.List[PathTest]"
    pattern: str

    def match(self, path: Path, fileentry: FileEntry):
        matched = path.match(self.pattern) if self.pattern else True
        for test in self.tests:
            if not matched:
                break
            matched = matched and test(path, fileentry)

        if matched:
            return not self.negate

        recursive = "**" in self.pattern

        if fileentry["type"] == FileType.Directory and recursive:
            return False

        return None

    @classmethod
    def parse(cls, pattern: str) -> "PathMatchStmt":
        filter = PathMatchStmt()
        filter.tests = []

        if pattern.startswith("!"):
            filter.negate = True
            pattern = pattern[1:]
        else:
            filter.negate = False

        while True:
            test = FilterTestRe.match(pattern)
            if not test:
                break
            name = test[1]
            arg = test[2]
            if name.startswith("!"):
                name = name[1:]
                inversed = True
            else:
                inversed = False
            filter.tests.append(PathTest.factory(name, arg, inversed))
            pattern = pattern[test.span()[1] :]

        filter.pattern = pattern

        return filter


class PathMatch(typing.List[PathMatchStmt]):
    def __init__(self, stmts: "typing.Iterable[PathMatchStmt]", root: Path = None):
        super().__init__(stmts)
        if root:
            for stmt in self:
                pattern = Path(stmt.pattern)
                if pattern.is_absolute():
                    stmt.pattern = os.fspath(Path(root, pattern.relative_to("/")))

    def match(
        self,
        path: Path,
        entry: FileEntry = None,
        _default: bool = None,
        **overrides,
    ):
        if not self:
            return True
        fileentry = FileEntry.from_path(path) if not entry else entry
        fileentry.update(overrides)

        for stmt in self:
            result = stmt.match(path, fileentry)
            if result is not None:
                return result
        return _default
