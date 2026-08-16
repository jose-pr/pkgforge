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
        """Evaluate this statement: ``True``/``False`` decide, ``None`` defers.

        A statement that does not apply returns ``None`` so the caller keeps
        evaluating later statements — never ``False``, which would veto them.
        """
        matched = path.match(self.pattern) if self.pattern else True
        for test in self.tests:
            if not matched:
                break
            matched = matched and test(path, fileentry)

        if matched:
            return not self.negate

        return None

    def rebased(self, root: Path) -> "PathMatchStmt":
        """Copy of this statement with an absolute pattern re-rooted at ``root``.

        Returns ``self`` when the pattern is relative (nothing to rewrite).
        Never mutates: parsed statements are shared across `PathMatch`
        constructions (a multi-source ``install`` reuses them per source), so
        rewriting in place would re-prefix the pattern once per construction.
        """
        pattern = Path(self.pattern)
        if not pattern.is_absolute():
            return self
        rebased = PathMatchStmt()
        rebased.negate = self.negate
        rebased.tests = self.tests
        # relative_to(anchor) rather than "/" so a drive-anchored pattern is
        # handled too (the runtime is POSIX, but the grammar is unit-tested
        # everywhere and on Windows "/" is not a path's anchor).
        rebased.pattern = os.fspath(Path(root, pattern.relative_to(pattern.anchor)))
        return rebased

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
        # Rebase absolute patterns onto `root` as COPIES: the incoming
        # statements come from parsed argv and are shared between
        # constructions (multi-source install builds one PathMatch per
        # source), so an in-place rewrite would prefix them once per source.
        if root:
            stmts = [stmt.rebased(root) for stmt in stmts]
        super().__init__(stmts)

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
