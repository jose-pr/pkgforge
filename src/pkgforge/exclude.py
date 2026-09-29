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
absolute one is anchored at the *install path* -- the ``/``-rooted path the
entry has (``dbdump``) or will have (``install``/``scan``) in the file DB --
the same coordinate on every command. See the exclude-pattern guide for the
full grammar and worked examples.
"""

from __future__ import annotations

import argparse
import functools
import posixpath
import re
import typing
from pathlib import Path, PurePath

import duho
from duho import NS

from .entry import FileEntry, FileType, entry_from_path
from .errors import UsageError

FilterTestRe = re.compile(r"^\(\?([^:()]+):([^()]+)\)")


class ExcludeSyntaxError(UsageError, argparse.ArgumentTypeError):
    """A malformed ``--exclude`` statement: an unknown inline test name, a
    bad ``(?type:...)``/``(?meta:...)`` argument, or an unterminated
    ``(?...`` that never closed.

    Subclasses both :class:`~pkgforge.errors.UsageError` (so
    ``pkgforge.main()``'s error boundary maps it to a clean one-line message
    and exit 2, and a Python-API caller catching ``ValueError`` still works)
    and :class:`argparse.ArgumentTypeError` (the only exception type argparse
    itself reports as a usage error rather than letting escape as a
    traceback, since ``--exclude`` is parsed through a ``type=`` converter).
    """


#: What a statement is actually matched against: either the real ``Path`` a
#: caller passed in (no root -- ``dbdump``), or the install-path string
#: :meth:`PathMatch._candidate` built for this call.
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


def _literal_prefix(pattern: str) -> str:
    """The literal (glob-metacharacter-free) leading path text of an
    ANCHORED ``pattern``: its anchor, plus whole segments up to (not
    including) the first one containing ``*``, ``?`` or ``[``. Used only by
    :meth:`PathMatch.unreachable` to judge whether a pattern can ever match
    anything under an install root -- never for actual matching, which stays
    on :func:`_glob_regex`.
    """
    parsed = PurePath(pattern)
    anchor_posix = PurePath(parsed.anchor).as_posix() if parsed.anchor else ""
    rest = parsed.as_posix()[len(anchor_posix) :]
    literal: typing.List[str] = []
    for seg in rest.split("/") if rest else []:
        if not seg:
            continue
        if any(c in seg for c in "*?["):
            break
        literal.append(seg)
    if not literal:
        return anchor_posix or "/"
    joined = "/".join(literal)
    return joined if not anchor_posix else anchor_posix.rstrip("/") + "/" + joined


def _contains(parent: str, child: str) -> bool:
    """True when the POSIX path text ``parent`` is an ancestor of, or equal
    to, ``child`` -- e.g. ``_contains("/opt", "/opt/app")`` is true, as is
    ``_contains("/opt/app", "/opt/app")``. ``parent == "/"`` (or a bare
    drive root) contains everything.
    """
    trimmed = parent.rstrip("/")
    if not trimmed:
        return True
    return child == trimmed or child.startswith(trimmed + "/")


def filetypetest(name: str) -> PathTest:
    try:
        ftype = FileType(name)
    except ValueError:
        raise ExcludeSyntaxError(
            f"invalid type {name!r}; choose from file, directory, symlink"
        ) from None
    return lambda _path, e: e["type"] == ftype


def metatest(arg: str) -> PathTest:
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
#: from the test's argument text. Kept module-level (not a Protocol class
#: attribute -- a Protocol's members must all be declared types) and
#: exposed on :class:`PathTest` as back-compat aliases below.
_TESTS: typing.Dict[str, typing.Callable[[str], PathTest]] = {
    "type": filetypetest,
    "meta": metatest,
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


# Back-compat aliases: some callers reach the registry/factory through
# PathTest itself. Assigned after the class body, not inside it, since a
# Protocol's own members must all be explicitly-typed callables.
PathTest.GENERATORS = _TESTS  # type: ignore[attr-defined]
PathTest.factory = staticmethod(_make_test)  # type: ignore[attr-defined]


class PathMatchStmt(NS):
    """One parsed ``--exclude`` statement: ``negate``, ``tests`` (a list of
    :class:`PathTest`) and ``pattern``. :meth:`parse` builds one from
    ``[!](?name:arg)*<glob>``; :meth:`match` evaluates it against a path."""

    negate: bool
    tests: typing.List[PathTest]
    pattern: str

    @property
    def anchored(self) -> bool:
        """True when ``pattern`` is rooted (a leading ``/``, or a Windows
        drive): it is matched against the install path's own leading
        segments rather than at any depth. Also drives
        :meth:`PathMatch.unreachable`."""
        return bool(PurePath(self.pattern).anchor)

    def _pattern_matches(self, candidate: _Candidate) -> bool:
        return (
            _glob_regex(self.pattern).fullmatch(PurePath(candidate).as_posix())
            is not None
        )

    def _tests_pass(self, path: _Candidate, fileentry: FileEntry) -> bool:
        return all(test(path, fileentry) for test in self.tests)

    def match(self, path: _Candidate, fileentry: FileEntry) -> typing.Optional[bool]:
        """Evaluate this statement: ``True``/``False`` decide, ``None`` defers.

        A statement that does not apply returns ``None`` so the caller keeps
        evaluating later statements — never ``False``, which would veto them.
        Requires an already-built ``fileentry`` -- :class:`PathMatch` is the
        one that builds it lazily, only for a statement whose glob already
        matched and that actually has inline tests.
        """
        if (not self.pattern or self._pattern_matches(path)) and self._tests_pass(
            path, fileentry
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

        if pattern.startswith("(?"):
            # FilterTestRe stopped matching partway through a "(?...": the
            # old behavior silently turned the rest into a literal glob
            # that excluded nothing. A leading "(" meant as a literal
            # character is written as the glob class "[(]" instead.
            raise ExcludeSyntaxError(
                f"unterminated or malformed inline test in {original!r}"
            )

        return cls(negate=negate, tests=tests, pattern=pattern)


class PathMatch(typing.List[PathMatchStmt]):
    """An ordered set of :class:`PathMatchStmt`, matched against the
    **install path** -- the ``/``-rooted path an entry has (``dbdump``) or
    will have (``install``/``scan``) in the file DB -- the one coordinate
    all three commands share. :meth:`match` evaluates each statement in
    order and returns the first non-``None`` result. No pattern rewriting:
    statements are stored exactly as parsed; :meth:`_candidate` builds one
    fresh install-path string per :meth:`match` call instead.
    """

    def __init__(
        self,
        stmts: typing.Iterable[PathMatchStmt],
        root: typing.Optional[Path] = None,
        installroot: str = "/",
    ):
        # Absolutized, never resolved (no symlink following). `root` is the
        # real filesystem location a call's `path` is under (a source
        # directory for `install`, the scanned tree for `scan`); `None` for
        # `dbdump`, which has no filesystem root at all -- its `path` IS the
        # DB key already.
        self.root = Path(root).absolute() if root is not None else None
        if not installroot.startswith("/"):
            raise ValueError(
                f"installroot must be an absolute POSIX path, got {installroot!r}"
            )
        # normpath, not just the raw text: collapses "//"/"/./" the same way
        # buildpath()'s own PurePosixPath text is already free of, so a
        # caller passing one through unnormalized still lines up.
        self.installroot = posixpath.normpath(installroot)
        super().__init__(stmts)

    def _candidate(self, path: typing.Union[str, Path]) -> _Candidate:
        """The single install-path string every statement in one
        :meth:`match` call is matched against -- or ``path`` itself with no
        ``root`` (``dbdump``, where ``path`` already IS the DB key).

        ``path == root`` (the single-file/symlink ``scan`` case) is
        :attr:`installroot` itself; below ``root`` it is ``installroot``
        joined with the path's own root-relative POSIX segments. A path
        outside ``root`` entirely (reachable only via the Python API, never
        through the CLI) keeps its own POSIX text rather than raising.
        """
        if self.root is None:
            return path
        abspath = Path(path).absolute()
        if abspath == self.root:
            return self.installroot
        try:
            rel = abspath.relative_to(self.root).as_posix()
        except ValueError:
            return PurePath(abspath).as_posix()
        return posixpath.join(self.installroot, rel)

    def match(
        self,
        path: Path,
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
                    base = entry if entry is not None else entry_from_path(path)
                    fileentry = typing.cast(
                        FileEntry, {**base, **overrides} if overrides else base
                    )
                if not stmt._tests_pass(candidate, fileentry):
                    continue

            return not stmt.negate
        return _default

    def unreachable(self) -> typing.List[PathMatchStmt]:
        """Anchored statements (negated ones too) that can never match
        anything under :attr:`installroot`: their literal prefix -- the
        segments before the first one containing ``*``, ``?`` or ``[`` -- is
        neither an ancestor-or-self nor a descendant-or-self of it. A
        drive-anchored pattern is always included (a POSIX installroot can
        never sit under a drive, or vice versa); a bare ``/`` prefix never
        is (it is an ancestor of everything). A relative statement is never
        included -- it matches at any depth, so it is never structurally
        impossible. Used by ``install``/``scan`` to warn about a pattern
        written for the OLD source-/scan-root anchor that no install path
        can reach any more; ``dbdump`` has no root and never calls this.
        """
        unreachable = []
        for stmt in self:
            if not stmt.anchored:
                continue
            prefix = _literal_prefix(stmt.pattern)
            if not (
                _contains(prefix, self.installroot)
                or _contains(self.installroot, prefix)
            ):
                unreachable.append(stmt)
        return unreachable


def log_unreachable(matcher: PathMatch, logger: typing.Any) -> None:
    """Log one WARNING per :meth:`PathMatch.unreachable` statement -- shared
    by ``install`` (once per source) and ``scan`` (once); ``dbdump`` never
    calls this (no root, so nothing is ever structurally unreachable there).
    """
    for stmt in matcher.unreachable():
        logger.warning(
            "-X %r matches install paths; nothing below %s can match it",
            stmt.pattern,
            matcher.installroot,
        )


class ExcludeArgs(duho.Cmd):
    """Mixin supplying ``--exclude``/``-X`` -- shared by every command that
    filters paths against a :class:`PathMatch` (``install``, ``scan``,
    ``dbdump``), so the load-bearing ``duho.Append`` shape below is
    declared exactly once.
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
            help="exclude paths matching STMT (repeatable); "
            "STMT is [!][(?[!]test:arg)...]GLOB; an absolute GLOB matches "
            "the /-rooted install path; see the exclude-pattern guide for "
            "the grammar"
        ),
    ] = []
    ("--exclude", "-X")
