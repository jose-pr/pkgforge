"""Path/entry matching for ``--exclude`` and dump filters.

A match statement is written as an optional leading ``!`` (negate), zero or
more inline tests ``(?name:arg)`` (or ``(?!name:arg)`` to invert a single
test, whose ``arg`` cannot contain ``(``/``)``), and a trailing glob
pattern, e.g.::

    (?type:file)**/*.pyc            # every .pyc file, at any depth
    !(?meta:keep=1)**/tmp/**        # keep entries tagged keep=1 under tmp/ ...
    **/tmp/**                       # ... paired with a broader exclude

``**`` as a whole segment matches zero or more path segments; a *trailing*
``**`` (or a bare ``**``) matches one or more -- a directory's contents,
never the directory itself. A relative glob matches at any depth; an
absolute one is anchored at the root of the input the command reads -- each
``install`` source (a directory's tree, an archive's member names), the
``scan`` PATH, or the ``dbdump`` DB path. See the exclude-pattern guide for
the full grammar and worked examples.
"""

from __future__ import annotations

import argparse
import functools
import itertools
import os
import re
import typing
from pathlib import Path, PurePath

from .entry import FileEntry, FileType, entry_from_path
from .exceptions import ExcludeSyntaxError

__all__ = ["PathMatch", "PathMatchStmt", "PathTest"]

_FILTER_TEST_RE = re.compile(r"^\(\?([^:()]+):([^()]+)\)")


#: Characters escaped inside a translated ``[...]`` class.
_CLASS_ESCAPE = re.compile(r"[\\\[&~|]")


def _translate_class(seg: str, i: int) -> typing.Optional[typing.Tuple[str, int]]:
    """``seg[i] == "["``: translate a ``[...]``/``[!...]`` character class.

    Returns ``(regex fragment, index just past the closing "]")``, or
    ``None`` for an unterminated class (the caller then treats the ``[`` as
    a literal character, as every glob implementation does). A ``]``
    immediately after ``[``/``[!`` is a literal member, not the terminator
    (classic glob bracket syntax). Only ``[`` and ``\\`` are re-escaped
    inside the class, plus ``&``, ``~`` and ``|`` (which ``re`` reserves
    for future set operations and warns about) -- ``-`` (ranges) and ``^``
    pass through unchanged.
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
    content = _CLASS_ESCAPE.sub(lambda m: "\\" + m[0], seg[start:j])
    prefix = "^" if negate else ""
    return f"[{prefix}{content}]", j + 1


def _translate_segment(seg: str, groups: "itertools.count[int]") -> str:
    """Translate one non-``**`` path segment (no ``/``) to a regex fragment.

    ``*``/``?`` never cross ``/``; everything else not part of a ``[...]``
    class is ``re.escape``d. Each ``*`` that is followed by more pattern is
    translated as ``fnmatch`` does: a lookahead plus a backreference stands
    in for an atomic group, so a failed match never retries the other
    split points of an earlier ``*`` and the cost stays linear in the
    candidate. ``groups`` numbers those backreferences across the pattern.
    """
    pieces: typing.List[typing.List[str]] = [[]]
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "*":
            if i == 0 or seg[i - 1] != "*":
                pieces.append([])
            i += 1
        elif c == "?":
            pieces[-1].append("[^/]")
            i += 1
        elif c == "[":
            parsed = _translate_class(seg, i)
            if parsed is None:
                pieces[-1].append(re.escape(c))
                i += 1
            else:
                frag, i = parsed
                pieces[-1].append(frag)
        else:
            pieces[-1].append(re.escape(c))
            i += 1
    out = ["".join(pieces[0])]
    for piece in pieces[1:-1]:
        name = f"g{next(groups)}"
        out.append(f"(?=(?P<{name}>[^/]*?{''.join(piece)}))(?P={name})")
    if len(pieces) > 1:
        out.append("[^/]*" + "".join(pieces[-1]))
    return "".join(out)


def _translate_segments(segments: typing.List[str]) -> str:
    """Translate a pattern's already-split (on ``/``) segments to a regex
    body (no leading anchor, no ``\\Z``): a whole ``**`` segment matches
    zero or more segments, except a *trailing* (or bare) ``**``, which
    means "the contents of this directory" and so requires one or more.
    A run of ``**`` segments means the same as one, so it is collapsed.
    """
    segments = [
        seg
        for i, seg in enumerate(segments)
        if not (seg == "**" and i + 1 < len(segments) and segments[i + 1] == "**")
    ]
    parts: typing.List[str] = []
    groups = itertools.count()
    last = len(segments) - 1
    prev_was_doublestar = False
    for i, seg in enumerate(segments):
        if i > 0 and not prev_was_doublestar:
            parts.append("/")
        if seg == "**":
            parts.append("[^/]+(?:/[^/]+)*" if i == last else "(?:[^/]+/)*")
        else:
            parts.append(_translate_segment(seg, groups))
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


def _type_test(name: str) -> PathTest:
    try:
        ftype = FileType(name)
    except ValueError:
        raise ExcludeSyntaxError(
            f"invalid type {name!r}; choose from file, directory, symlink"
        ) from None
    return lambda _path, e: e["type"] == ftype


def _meta_test(arg: str) -> PathTest:
    if "=" not in arg:
        raise ExcludeSyntaxError(f"meta test needs KEY=VALUE, got {arg!r}")
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
#: from the test's argument text.
_TESTS: typing.Dict[str, typing.Callable[[str], PathTest]] = {
    "type": _type_test,
    "meta": _meta_test,
}


def _make_test(name: str, arg: str, inverse: bool) -> PathTest:
    factory = _TESTS.get(name)
    if factory is None:
        raise ExcludeSyntaxError(
            f"unknown exclude test {name!r}; choose from "
            f"{', '.join(sorted(_TESTS))}"
        )
    test = factory(arg)
    if inverse:

        def _test(path: Path, entry: FileEntry) -> bool:
            return not test(path, entry)

        return _test
    return test


class PathMatchStmt(argparse.Namespace):
    """One parsed ``--exclude`` statement: ``negate``, ``tests`` (a list of
    :class:`PathTest`) and ``pattern``. :meth:`parse` builds one from
    ``[!](?name:arg)*<glob>``; :meth:`match` evaluates it against a path."""

    negate: bool
    tests: typing.List[PathTest]
    pattern: str

    @property
    def anchored(self) -> bool:
        """True when ``pattern`` is rooted (a leading ``/``, or a Windows
        drive): it is matched from the root of the input rather than at
        any depth."""
        return bool(PurePath(self.pattern).anchor)

    def _pattern_matches(self, candidate: str) -> bool:
        return _glob_regex(self.pattern).fullmatch(candidate) is not None

    def _tests_pass(self, path: typing.Any, fileentry: FileEntry) -> bool:
        return all(test(path, fileentry) for test in self.tests)

    def match(
        self, path: typing.Union[str, Path], fileentry: FileEntry
    ) -> typing.Optional[bool]:
        """Evaluate this statement: ``True``/``False`` decide, ``None`` defers.

        A statement that does not apply returns ``None`` so the caller keeps
        evaluating later statements — never ``False``, which would veto them.
        Requires an already-built ``fileentry`` -- :class:`PathMatch` is the
        one that builds it lazily, only for a statement whose glob already
        matched and that actually has inline tests.
        """
        text = path if isinstance(path, str) else PurePath(path).as_posix()
        if (not self.pattern or self._pattern_matches(text)) and self._tests_pass(
            text, fileentry
        ):
            return not self.negate
        return None

    @classmethod
    def parse(cls, pattern: str) -> PathMatchStmt:
        original = pattern
        tests: typing.List[PathTest] = []

        if pattern.startswith("!"):
            negate = True
            pattern = pattern[1:]
        else:
            negate = False

        while True:
            test = _FILTER_TEST_RE.match(pattern)
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

        if pattern.startswith("(?"):
            # _FILTER_TEST_RE stopped matching partway through a "(?...": the
            # rest would silently become a literal glob that excludes
            # nothing. A leading "(" meant as a literal
            # character is written as the glob class "[(]" instead.
            raise ExcludeSyntaxError(
                f"unterminated or malformed inline test in {original!r}"
            )

        return cls(negate=negate, tests=tests, pattern=pattern)


class PathMatch(typing.List[PathMatchStmt]):
    """An ordered set of :class:`PathMatchStmt`, matched against the names
    of the input a command reads, the way ``tar --exclude`` matches member
    names. :meth:`match` evaluates each statement in order and returns the
    first non-``None`` result.

    With a ``root``, a path is matched as ``/`` plus its root-relative POSIX
    text (``/`` for the root itself), so an absolute pattern is relative to
    the root: ``install`` roots at each source (a directory, or an
    archive's extraction directory) and ``scan`` at PATH. With no ``root``
    (``dbdump``) a path is matched as its own POSIX text, which for a DB key
    is already ``/``-rooted. A relative pattern matches trailing segments at
    any depth either way. Statements are stored exactly as parsed.
    """

    def __init__(
        self,
        stmts: typing.Iterable[PathMatchStmt],
        root: typing.Optional[Path] = None,
    ):
        # Absolutized, never resolved (no symlink following).
        self.root = Path(root).absolute() if root is not None else None
        super().__init__(stmts)

    def _candidate(self, path: typing.Union[str, Path]) -> str:
        """The one string every statement in a :meth:`match` call is matched
        against. Raises :class:`ValueError` for a path outside ``root``: a
        caller bug, not a non-match.
        """
        if self.root is None:
            return path if isinstance(path, str) else PurePath(path).as_posix()
        try:
            rel = Path(path).absolute().relative_to(self.root).as_posix()
        except ValueError:
            raise ValueError(f"{os.fspath(path)!r} is not under {self.root}") from None
        return "/" if rel == "." else "/" + rel

    def match(
        self,
        path: typing.Union[str, Path],
        entry: typing.Optional[FileEntry] = None,
        _default: typing.Optional[bool] = None,
        **overrides,
    ) -> typing.Optional[bool]:
        if not self:
            return True

        candidate = self._candidate(path)

        # The entry is built lazily -- at most once, and only for a
        # statement whose glob ALREADY matched and that actually has inline
        # tests -- so a glob-only "-X '*.fifo'" excludes a FIFO or socket
        # without ever lstat-ing/typing/pwd-grp-looking-up it (and without
        # ever risking FileType.from_path's TypeError for one). `overrides`
        # are layered onto a COPY, so the caller's `entry` dict (e.g. a live
        # DB record in dbdump) is never mutated; `entry is not None` (not a
        # truthiness check) so an explicitly empty `{}` still counts as
        # "the caller supplied one" rather than triggering a real lstat.
        fileentry: typing.Optional[FileEntry] = None

        for stmt in self:
            if stmt.pattern and not stmt._pattern_matches(candidate):
                continue

            if stmt.tests:
                if fileentry is None:
                    base = entry if entry is not None else entry_from_path(Path(path))
                    fileentry = typing.cast(
                        FileEntry, {**base, **overrides} if overrides else base
                    )
                if not stmt._tests_pass(candidate, fileentry):
                    continue

            return not stmt.negate
        return _default


def _statements(
    exclude: typing.Sequence[typing.Union[str, PathMatchStmt]],
) -> typing.List[PathMatchStmt]:
    """``exclude`` as parsed statements: text is parsed (raising
    :class:`~pkgforge.exceptions.ExcludeSyntaxError`), a statement is kept."""
    if isinstance(exclude, (str, bytes)):
        raise TypeError("exclude must be a sequence of statements, not one string")
    return [
        stmt if isinstance(stmt, PathMatchStmt) else PathMatchStmt.parse(stmt)
        for stmt in exclude
    ]
