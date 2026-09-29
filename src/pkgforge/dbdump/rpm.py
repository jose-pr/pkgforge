"""``rpmspecfiles``: one RPM ``%files``/``%attr`` line per DB entry."""

from __future__ import annotations

import re

from ..entry import DEFAULT, FileEntry, FileType
from . import Entries, PerEntryFormat, DumpError, _has_control, _reject_control


def _rpm_reject(path: str) -> None:
    """Raise :class:`DumpError` if ``path`` cannot be represented in an rpm
    ``%files``/``%attr`` line (rpm 4.19+): a control character, or a ``%``
    (macro-expanded by rpm's ``%files -f`` parser on every version -- see
    :func:`_rpm_quote`). Validation only; :func:`_rpm_escape` does the
    quoting once a path is known good.
    """
    _reject_control(path, "rpmspecfiles")
    if "%" in path:
        raise DumpError(
            "rpmspecfiles: a DB entry's path contains '%', which rpm's "
            "%files parser expands as a macro on every rpm version; write "
            "this entry's %files line by hand"
        )


#: `path.translate` (one pass) instead of two chained `.replace` calls (two
#: passes): both express the same two independent, context-free
#: substitutions (each original character maps to a fixed output regardless
#: of its neighbors), so the two are always byte-identical.
_RPM_ESCAPE_TABLE = str.maketrans({"\\": "\\\\", '"': '\\"'})


def _rpm_escape(path: str) -> str:
    """Quote an already-validated ``path`` for rpm 4.19+ (no rejection):
    backslash and double quote are escaped, glob characters
    (``* ? [ ]``) are left alone -- rpm's own shell-globbing quoting rules
    make a quoted glob character match only the literal path anyway.
    """
    return f'"{path.translate(_RPM_ESCAPE_TABLE)}"'


def _rpm_quote(path: str) -> str:
    """Validate then quote ``path`` for an rpm ``%files``/``%attr`` line
    (rpm 4.19+): see :func:`_rpm_reject` and :func:`_rpm_escape`.
    """
    _rpm_reject(path)
    return _rpm_escape(path)


#: Characters :func:`_rpm_quote_pre419` refuses outright: a space (ends rpm's
#: unquoted token early), every glob character (unquoted, so rpm's own
#: globbing may match a sibling instead of the literal name), and ``%`` (rpm
#: macro-expands every ``%files -f`` line, on every version). Measured
#: 2026-09-29 against real rpmbuild runs on rpm 4.14.3, 4.16.1 and 4.18.2: a
#: bare ``"`` or ``\`` survives an unquoted token unescaped and literal, so
#: neither is refused here.
_REFUSED_PRE419 = frozenset(" *?[]{}%")
#: A single-character class matching anything in :data:`_REFUSED_PRE419` --
#: the batch pre-check for the pre-4.19 format (one search over every path
#: joined together, instead of the per-character loop below run once per
#: entry).
_REFUSED_PRE419_RE = re.compile("[" + re.escape("".join(_REFUSED_PRE419)) + "]")
#: A lone surrogate (``surrogateescape``'s stand-in for a non-UTF-8 byte).
_SURROGATE_RE = re.compile("[\udc80-\udcff]")


def _rpm_reject_pre419(path: str) -> None:
    """Raise :class:`DumpError` if ``path`` cannot be represented in rpm
    older than 4.19's ``%files -f`` parser (measured against real rpmbuild
    runs on 4.14.3, 4.16.1 and 4.18.2): a control character, a byte that is
    not valid UTF-8, a space, or a glob character (``* ? [ ] { }``) or ``%``
    (see :func:`_rpm_quote_pre419`). Validation only; a validated path is
    written back completely unquoted -- there is no separate escaping step.
    """
    _reject_control(path, "rpmspecfiles-pre419")
    if _SURROGATE_RE.search(path):
        raise DumpError(
            "rpmspecfiles-pre419: a DB entry's path contains a byte that is "
            "not valid UTF-8; rpm older than 4.19 cannot represent it; use "
            "rpmspecfiles with rpm 4.19+ or write this line by hand"
        )
    for ch in path:
        if ch in _REFUSED_PRE419:
            raise DumpError(
                f"rpmspecfiles-pre419: a DB entry's path contains {ch!r}; "
                "rpm older than 4.19 cannot represent it; use rpmspecfiles "
                "with rpm 4.19+ or write this line by hand"
            )


def _rpm_quote_pre419(path: str) -> str:
    """Validate then return ``path`` unquoted for rpm older than 4.19's
    ``%files -f`` parser: see :func:`_rpm_reject_pre419`.

    Below rpm 4.19, a quoted ``%files -f`` name is macro-expanded TWICE
    (``specExpand``, then ``rpmExpand`` again while resolving the file), and
    an unquoted name's glob characters are matched by rpm's own globbing
    before pkgforge's own escaping is ever consulted -- there is no
    escaping on that range that is safe for a space or a glob character.
    This writes the path completely unquoted instead (rpm's bare-token
    reader passes a literal ``"`` or ``\\`` straight through, unescaped).
    """
    _rpm_reject_pre419(path)
    return path


class RpmSpecFiles(PerEntryFormat):
    """RPM ``%files`` lines: ``%attr(mode,owner,group) "path"``, a ``%dir``
    prefix for directories, ``meta["rpmprefix"]`` prepended if set.

    The path is quoted for rpm's ``%files -f`` parser (targets rpm 4.19+):
    a backslash and a double quote are escaped, and the whole line is
    written as UTF-8 with ``surrogateescape`` (a non-UTF-8 name round-trips
    its original bytes). Glob characters are never escaped -- rpm's own
    quoted-string globbing already matches only the literal name. A ``%``
    anywhere in the path, or a control character (including a tab), raises
    :class:`~pkgforge.dbdump.DumpError`. Below rpm 4.19, use
    :class:`RpmSpecFilesPre419` instead -- this quoting is not safe there.
    """

    NAME = "rpmspecfiles"
    ALIASES = ("rpm", "rpmspec")
    #: The quoting hook (validate + escape together): a plain function, held
    #: as a class attribute so a subclass can override it without also
    #: overriding `render`/`render_entry`. Used only by `render_entry` (a
    #: one-entry batch) and, within `render`, only on the slow per-entry
    #: fallback path once a batch check finds something wrong.
    _quote = staticmethod(_rpm_quote)
    #: The escaping half alone, for an already-validated path -- what a
    #: clean batch's per-line loop uses instead of re-validating every path
    #: it already knows is good.
    _escape = staticmethod(_rpm_escape)
    #: The rejection half alone, for the per-entry fallback once a batch
    #: check finds something wrong -- raises with today's exact message on
    #: the first offending entry, in entry order.
    _reject = staticmethod(_rpm_reject)

    @staticmethod
    def _batch_reject_needed(joined: str) -> bool:
        """Whether ``joined`` (every entry's path concatenated) might hide a
        rejected path -- a control character or a ``%`` anywhere. ``False``
        means every path in the batch is already known good, so `render`'s
        per-line loop can call `_escape` directly and skip `_reject`
        entirely.
        """
        return _has_control([joined]) or "%" in joined

    def render(self, entries: Entries) -> bytes:
        """Render every entry, validating the whole batch's paths once
        (parent ``Q1`` design): a single check over every path joined
        together decides whether anything needs the slower per-entry
        rejection at all, so a clean batch never runs a rejection check per
        entry. Byte-identical to rendering each entry through
        :meth:`render_entry` alone.

        The per-line body is inlined here rather than split into a helper:
        this is the hot loop, and ``x or DEFAULT`` is exactly what
        :func:`~pkgforge.entry._or_default` does for every string value
        (``apply_entry`` still calls that function directly; this loop just
        skips the extra call).
        """
        paths = [path for path, _entry in entries]
        if self._batch_reject_needed("".join(paths)):
            # Something in the batch is bad -- run today's exact per-entry
            # check in entry order so the first offending entry raises
            # exactly the error it would raise rendered alone.
            for path in paths:
                self._reject(path)
        escape = self._escape
        lines = []
        for path, entry in entries:
            prefix = entry["meta"].get("rpmprefix") or ""
            if prefix:
                prefix += " "
            if entry["type"] == FileType.Directory:
                prefix += "%dir "
            mode = entry["mode"] or DEFAULT
            owner = entry["owner"] or DEFAULT
            group = entry["group"] or DEFAULT
            lines.append(
                f"{prefix}%attr({mode},{owner},{group}) {escape(path)}\n".encode(
                    "utf-8", "surrogateescape"
                )
            )
        return b"".join(lines)

    def render_entry(self, path: str, entry: FileEntry) -> bytes:
        return self.render([(path, entry)])


class RpmSpecFilesPre419(RpmSpecFiles):
    """RPM ``%files`` lines for rpm older than 4.19 (measured against real
    rpmbuild runs on 4.14.3, 4.16.1 and 4.18.2).

    Inherits :class:`RpmSpecFiles`'s ``%attr``/``%dir``/``rpmprefix``
    rendering and batch-then-per-entry validation, overriding only the
    quoting/rejection hooks: the path is written completely unquoted, and a
    path containing a space, a glob character (``* ? [ ] { }``), ``%`` (in
    any form), or a byte that is not valid UTF-8 raises
    :class:`~pkgforge.dbdump.DumpError` instead of risking a silently wrong
    or overmatched package. Use :class:`RpmSpecFiles` instead when the rpm
    that builds the package is 4.19 or newer.
    """

    NAME = "rpmspecfiles-pre419"
    ALIASES = ("rpm-pre419",)
    _quote = staticmethod(_rpm_quote_pre419)
    #: No escaping at all here -- an already-validated path is written back
    #: unquoted (see :func:`_rpm_quote_pre419`).
    _escape = staticmethod(lambda path: path)
    _reject = staticmethod(_rpm_reject_pre419)

    @staticmethod
    def _batch_reject_needed(joined: str) -> bool:
        return bool(
            _has_control([joined])
            or _REFUSED_PRE419_RE.search(joined)
            or _SURROGATE_RE.search(joined)
        )
