"""Path/entry matching for ``--exclude`` and dump filters.

A match statement is written as an optional leading ``!`` (negate), zero or
more inline tests ``(?name:arg)`` (or ``(?!name:arg)`` to invert a single
test), and a trailing glob pattern, e.g.::

    (?type:file)**/*.pyc        # every .pyc file
    !(?meta:keep=1)**/tmp/**    # keep entries tagged keep=1 under tmp/
"""

from __future__ import annotations

import functools
import glob
import os
import re
import typing
from pathlib import Path, PurePath

import duho
from duho import NS

from .common import FileEntry, FileType, entry_from_path

FilterTestRe = re.compile(r"^\(\?([^:())]+):([^()]+)\)")

#: What a statement is actually matched against: either the real ``Path`` a
#: caller passed in, or -- once :class:`PathMatch` has rebased it relative to
#: a root -- a plain root-relative POSIX string.
_Candidate = typing.Union[str, Path]


def _translate_class(seg: str, i: int) -> typing.Optional[typing.Tuple[str, int]]:
    """``seg[i] == "["``: translate a ``[...]``/``[!...]`` character class.

    Returns ``(regex fragment, index just past the closing "]")``, or
    ``None`` for an unterminated class (the caller then treats the ``[`` as
    a literal character, as every glob implementation does). A ``]``
    immediately after ``[``/``[!`` is a literal member, not the terminator
    (classic glob bracket syntax). Only ``[`` and ``\\`` are re-escaped
    inside the class -- ``-`` (ranges) and ``^`` pass through unchanged.
    """
    n = len(seg)
    j = i + 1
    negate = j < n and seg[j] == "!"
    if negate:
        j += 1
    start = j
    if j < n and seg[j] == "]":
        j += 1  # a leading "]" right after "[" / "[!" is a literal member
    while j < n and seg[j] != "]":
        j += 1
    if j >= n:
        return None
    content = seg[start:j].replace("\\", "\\\\").replace("[", "\\[")
    prefix = "^" if negate else ""
    return f"[{prefix}{content}]", j + 1


def _translate_segment(seg: str) -> str:
    """Translate one non-``**`` path segment (no ``/``) to a regex fragment.

    ``*``/``?`` never cross ``/`` (``[^/]*``/``[^/]``, not ``.*``/``.``);
    everything else not part of a ``[...]`` class is ``re.escape``d.
    """
    out: typing.List[str] = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            parsed = _translate_class(seg, i)
            if parsed is None:
                out.append(re.escape(c))
                i += 1
            else:
                frag, i = parsed
                out.append(frag)
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _translate_segments(segments: typing.List[str]) -> str:
    """Translate a pattern's already-split (on ``/``) segments to a regex
    body (no leading anchor, no ``\\Z``): a whole ``**`` segment matches
    zero or more segments, except a *trailing* (or bare) ``**``, which
    means "the contents of this directory" and so requires one or more.
    """
    parts: typing.List[str] = []
    last = len(segments) - 1
    prev_was_doublestar = False
    for i, seg in enumerate(segments):
        if i > 0 and not prev_was_doublestar:
            parts.append("/")
        if seg == "**":
            parts.append("[^/]+(?:/[^/]+)*" if i == last else "(?:[^/]+/)*")
        else:
            parts.append(_translate_segment(seg))
        prev_was_doublestar = seg == "**"
    return "".join(parts)


@functools.lru_cache(maxsize=None)
def _glob_regex(pattern: str) -> "re.Pattern[str]":
    """Compile ``pattern`` (a ``/``-separated glob) to a regex, fullmatched
    against a POSIX candidate string.

    An anchored pattern (``PurePath(pattern).anchor``, e.g. a leading ``/``
    or a Windows drive) matches from that anchor exactly. A relative
    pattern gets an implicit ``(?:.*/)?`` prefix, so it keeps matching at
    any depth -- today's behavior for a plain ``*.pyc``/``a/b`` pattern.
    """
    anchor = PurePath(pattern).anchor
    posix = PurePath(pattern).as_posix()
    if anchor:
        anchor_posix = PurePath(anchor).as_posix()
        body = _translate_segments(posix[len(anchor_posix) :].split("/"))
        prefix = re.escape(anchor_posix)
    else:
        body = _translate_segments(posix.split("/") if posix else [""])
        prefix = "(?:.*/)?"
    return re.compile(prefix + body + r"\Z")


def _relative_candidate(path: Path, root: Path) -> str:
    """Root-relative POSIX text for a RELATIVE statement's candidate.

    ``path == root`` -- the single-file ``scan`` case, where there is no
    deeper relative path to compute -- matches by the root's own name. A
    path genuinely outside ``root`` (reachable only from the Python API)
    keeps its own text unchanged rather than raising.
    """
    if path == root:
        return root.name
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return PurePath(path).as_posix()


def filetypetest(name: str) -> PathTest:
    ftype = FileType(name)
    return lambda _path, e: e["type"] == ftype


def metatest(arg: str) -> PathTest:
    k, v = arg.split("=", maxsplit=1)
    return lambda _path, e: e["meta"].get(k) == v


class PathTest(typing.Protocol):
    """A single inline ``(?name:arg)`` test: ``__call__(path, entry) -> bool``.

    ``path`` is typed loosely (no test actually reads it, only ``entry``):
    once a statement is matched against a root, ``path`` may be a plain
    root-relative string rather than the real filesystem ``Path``.
    """

    def __call__(self, path: typing.Any, entry: FileEntry) -> bool: ...


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

    @property
    def anchored(self) -> bool:
        """True when ``pattern`` is rooted (a leading ``/``, or a Windows
        drive) -- true both for an originally-absolute pattern and, once
        rebased, for its root-prefixed copy."""
        return bool(PurePath(self.pattern).anchor)

    def _pattern_matches(self, candidate: _Candidate) -> bool:
        return (
            _glob_regex(self.pattern).fullmatch(PurePath(candidate).as_posix())
            is not None
        )

    def match(self, path: _Candidate, fileentry: FileEntry) -> typing.Optional[bool]:
        """Evaluate this statement: ``True``/``False`` decide, ``None`` defers.

        A statement that does not apply returns ``None`` so the caller keeps
        evaluating later statements — never ``False``, which would veto them.
        """
        if (not self.pattern or self._pattern_matches(path)) and all(
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
        if not pattern.anchor:
            return self
        # `root` is absolutized (never `resolve()`, which would also follow
        # symlinks) and glob-escaped, so a relative --buildroot no longer
        # makes the rebased pattern float, and glob characters in the root
        # text (e.g. a source directory named "pkg[1]") stay literal.
        # `.anchor`, not `.is_absolute()`: the runtime is POSIX, but the
        # grammar is unit-tested everywhere, and on Windows a bare leading
        # "/" pattern (no drive) has a truthy `.anchor` ("\\") while
        # `.is_absolute()` is False -- `relative_to(anchor)` rather than
        # `relative_to("/")` handles a drive-anchored pattern too.
        root_text = glob.escape(os.fspath(Path(root).absolute()))
        return PathMatchStmt(
            negate=self.negate,
            tests=self.tests,
            pattern=os.fspath(Path(root_text, pattern.relative_to(pattern.anchor))),
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
        # Absolutized, never resolved (no symlink following) -- see
        # rebased()'s docstring. Kept on self so .match() can build each
        # candidate the same way rebased() built the stored patterns.
        self.root = Path(root).absolute() if root is not None else None
        # Rebase absolute patterns onto `root` as COPIES: see rebased()'s own
        # docstring -- the incoming statements come from parsed argv and are
        # shared between constructions (multi-source install builds one
        # PathMatch per source), so an in-place rewrite would prefix them
        # once per source.
        if self.root is not None:
            stmts = [stmt.rebased(self.root) for stmt in stmts]
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

        # With a root, an ANCHORED statement is matched against the
        # candidate's own absolute path (the rebased pattern already embeds
        # the root's absolute, escaped text as its prefix); a RELATIVE
        # statement is matched against the path taken relative to the root,
        # so it can no longer float above the root or drift with a relative
        # --buildroot. With no root (dbdump has none), every statement sees
        # the path exactly as given -- "the key".
        abspath = Path(path).absolute() if self.root is not None else None
        for stmt in self:
            if self.root is None:
                candidate: _Candidate = path
            elif stmt.anchored:
                candidate = typing.cast(Path, abspath)
            else:
                candidate = _relative_candidate(typing.cast(Path, abspath), self.root)
            result = stmt.match(candidate, fileentry)
            if result is not None:
                return result
        return _default


class ExcludeArgs(duho.Cmd):
    """Mixin supplying ``--exclude``/``-X`` -- shared by every command that
    filters paths against a :class:`PathMatch` (``install``, ``scan``,
    ``dbdump``), so the load-bearing ``duho.Append`` shape (see
    ``.agents/AGENTS.md``) is declared exactly once.
    """

    # A collection field must use `duho.Append`, not a bare `List[...]`
    # collection: duho would otherwise gather one *occurrence* worth of
    # tokens per `-X`, rather than one statement per occurrence. `metavar`
    # goes through `duho.Append`'s own `**kw` (its raw add_argument
    # escape-hatch), and `duho.Meta(help=...)` is a separate metadata entry
    # because passing `help=` to `duho.Append` itself fails at parser build.
    exclude: duho.Arg[
        typing.List[PathMatchStmt],
        duho.Append(PathMatchStmt.parse, metavar="STMT"),
        duho.Meta(
            help="exclude paths matching STMT (repeatable); see the "
            "exclude-pattern guide for the grammar"
        ),
    ] = []
    ("--exclude", "-X")
