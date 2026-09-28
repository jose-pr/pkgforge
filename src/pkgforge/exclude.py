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

from .common import FileEntry, FileType, entry_from_path

FilterTestRe = re.compile(r"^\(\?([^:())]+):([^()]+)\)")


def filetypetest(name: str) -> PathTest:
    ftype = FileType(name)
    return lambda _path, e: e["type"] == ftype


def metatest(arg: str) -> PathTest:
    k, v = arg.split("=", maxsplit=1)
    return lambda _path, e: e["meta"].get(k) == v


class PathTest(typing.Protocol):
    """A single inline ``(?name:arg)`` test: ``__call__(path, entry) -> bool``."""

    def __call__(self, path: Path, entry: FileEntry) -> bool: ...


#: Registered inline-test names -> a factory building a :class:`PathTest`
#: from the test's argument text. Kept module-level (not a Protocol class
#: attribute -- a Protocol's members must all be declared types) and
#: exposed on :class:`PathTest` as back-compat aliases below.
_TESTS: typing.Dict[str, typing.Callable[[str], PathTest]] = {
    "type": filetypetest,
    "meta": metatest,
}


def _make_test(name: str, arg: str, inverse: bool) -> PathTest:
    test = _TESTS[name](arg)
    if inverse:

        def _test(path: Path, entry: FileEntry) -> bool:
            return not test(path, entry)

        return _test
    return test


# Back-compat aliases: some callers reach the registry/factory through
# PathTest itself. Assigned after the class body, not inside it, since a
# Protocol's own members must all be explicitly-typed callables.
PathTest.GENERATORS = _TESTS  # type: ignore[attr-defined]
PathTest.factory = staticmethod(_make_test)  # type: ignore[attr-defined]


class PathMatchStmt(NS):
    negate: bool
    tests: typing.List[PathTest]
    pattern: str

    def match(self, path: Path, fileentry: FileEntry) -> typing.Optional[bool]:
        """Evaluate this statement: ``True``/``False`` decide, ``None`` defers.

        A statement that does not apply returns ``None`` so the caller keeps
        evaluating later statements — never ``False``, which would veto them.
        """
        if (not self.pattern or path.match(self.pattern)) and all(
            test(path, fileentry) for test in self.tests
        ):
            return not self.negate
        return None

    def rebased(self, root: Path) -> PathMatchStmt:
        """Copy of this statement with an absolute pattern re-rooted at ``root``.

        Returns ``self`` when the pattern is relative (nothing to rewrite).
        Never mutates: parsed statements are shared across `PathMatch`
        constructions (a multi-source ``install`` reuses them per source), so
        rewriting in place would re-prefix the pattern once per construction.
        """
        pattern = Path(self.pattern)
        if not pattern.is_absolute():
            return self
        # relative_to(anchor) rather than "/" so a drive-anchored pattern is
        # handled too (the runtime is POSIX, but the grammar is unit-tested
        # everywhere and on Windows "/" is not a path's anchor).
        return PathMatchStmt(
            negate=self.negate,
            tests=self.tests,
            pattern=os.fspath(Path(root, pattern.relative_to(pattern.anchor))),
        )

    @classmethod
    def parse(cls, pattern: str) -> PathMatchStmt:
        tests: typing.List[PathTest] = []

        if pattern.startswith("!"):
            negate = True
            pattern = pattern[1:]
        else:
            negate = False

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
            tests.append(_make_test(name, arg, inversed))
            pattern = pattern[test.span()[1] :]

        return cls(negate=negate, tests=tests, pattern=pattern)


class PathMatch(typing.List[PathMatchStmt]):
    def __init__(
        self,
        stmts: typing.Iterable[PathMatchStmt],
        root: typing.Optional[Path] = None,
    ):
        # Rebase absolute patterns onto `root` as COPIES: see rebased()'s own
        # docstring -- the incoming statements come from parsed argv and are
        # shared between constructions (multi-source install builds one
        # PathMatch per source), so an in-place rewrite would prefix them
        # once per source.
        if root:
            stmts = [stmt.rebased(root) for stmt in stmts]
        super().__init__(stmts)

    def match(
        self,
        path: Path,
        entry: typing.Optional[FileEntry] = None,
        _default: typing.Optional[bool] = None,
        **overrides,
    ) -> typing.Optional[bool]:
        if not self:
            return True
        fileentry = entry_from_path(path) if not entry else entry
        fileentry.update(typing.cast(FileEntry, overrides))

        for stmt in self:
            result = stmt.match(path, fileentry)
            if result is not None:
                return result
        return _default
