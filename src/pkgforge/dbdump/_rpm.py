"""``rpmspecfiles``: one RPM ``%files``/``%attr`` line per DB entry."""

from __future__ import annotations

import re

from ..entry import DEFAULT, FileEntries, FileEntry, FileType
from ..exceptions import DumpError
from . import PerEntryFormat, _has_control, _reject_control

#: Plain strings, compared with a DB's own (str) type values: cheaper per
#: entry than a ``FileType`` member comparison, and equal to the members.
_DIRECTORY = FileType.Directory.value
_SYMLINK = FileType.Symlink.value
#: The ``%files`` directive each entry kind starts its line with.
_RPM_KIND = {
    "file": "",
    "config": "%config ",
    "config-noreplace": "%config(noreplace) ",
    "doc": "%doc ",
    "license": "%license ",
    "ghost": "%ghost ",
}


def _rpm_reject(path: str) -> None:
    """Raise :class:`DumpError` if ``path`` cannot be represented in an rpm
    ``%files``/``%attr`` line (rpm 4.19+): a control character, or a ``%``
    (macro-expanded by rpm's ``%files -f`` parser on every version -- see
    :func:`_rpm_escape`). Validation only; :func:`_rpm_escape` does the
    quoting once a path is known good.
    """
    _reject_control(path, "rpmspecfiles")
    if "%" in path:
        raise DumpError(
            "rpmspecfiles: a DB entry's path contains '%', which rpm's "
            "%files parser expands as a macro on every rpm version; write "
            "this entry's %files line by hand"
        )


def _rpm_escape(path: str) -> str:
    """Quote an already-validated ``path`` for rpm 4.19+ (no rejection):
    backslash and double quote are escaped, glob characters
    (``* ? [ ]``) are left alone -- rpm's own shell-globbing quoting rules
    make a quoted glob character match only the literal path anyway.

    Two chained ``.replace`` calls, not ``str.translate``: ``translate``
    only has a fast single-character path when every replacement is a
    single character (or deletion) -- a table whose values are
    *multi*-character strings (both of these are: ``\\`` -> ``\\\\``, ``"``
    -> ``\\"``) falls onto a much slower generic path. Measured on
    3.9/3.14: swapping in ``translate`` here made a 1000-entry render
    slower, not faster, on both.
    """
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


#: Characters :func:`_rpm_reject_pre419` refuses outright: a space (ends rpm's
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
    (below rpm 4.19 a quoted name is macro-expanded twice, and an unquoted
    glob character is globbed by rpm, so no escaping is safe). Validation
    only; a validated path is written back completely unquoted.
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


class RPMSpecFiles(PerEntryFormat):
    """RPM ``%files`` lines: ``%attr(mode,owner,group) "path"``, a ``%dir``
    prefix for directories, ``%caps(CAPS)`` for an entry with file
    capabilities, and the entry kind's directive first (``%config``,
    ``%config(noreplace)``, ``%doc``, ``%license`` or ``%ghost``; none for
    ``file``).

    The path is quoted for rpm's ``%files -f`` parser (targets rpm 4.19+):
    a backslash and a double quote are escaped, and the whole line is
    written as UTF-8 with ``surrogateescape`` (a non-UTF-8 name round-trips
    its original bytes). Glob characters are never escaped -- rpm's own
    quoted-string globbing already matches only the literal name. A ``%``
    anywhere in the path, or a control character (including a tab), raises
    :class:`~pkgforge.exceptions.DumpError`. Below rpm 4.19, use
    :class:`RPMSpecFilesPre419` instead -- this quoting is not safe there.
    """

    NAME = "rpmspecfiles"
    ALIASES = ("rpm", "rpmspec")
    #: Quote an already-validated path for the ``%files`` line.
    _escape = staticmethod(_rpm_escape)
    #: Raise :class:`DumpError` for a path the format cannot represent; run
    #: per entry, in entry order, once :meth:`_batch_reject_needed` fires.
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

    def render(self, entries: FileEntries) -> bytes:
        """Render every entry, validating the whole batch's paths once: a
        single check over every path joined together decides whether
        anything needs the slower per-entry rejection at all, so a clean
        batch never runs a rejection check per entry. Byte-identical to
        rendering each entry through :meth:`render_entry` alone.

        One pass builds every line AND collects the paths for the batch
        check; the check itself (and, on a hit, the per-entry fallback) runs
        only after, on the collected list -- rejecting a bad entry only
        after also having escaped it wastes a little work on the rare bad
        batch, but a second full pass over ``entries`` up front cost more
        than that on every batch, clean ones included (measured on 3.9).
        The per-line body is inlined here rather than split into a helper:
        this is the hot loop, and ``x or DEFAULT`` is exactly what
        :func:`~pkgforge.entry._or_default` does for every string value
        (``apply_entry`` still calls that function directly; this loop just
        skips the extra call).
        """
        escape = self._escape
        kinds = _RPM_KIND
        lines = []
        paths = []
        for path, entry in entries:
            paths.append(path)
            prefix = kinds[entry["kind"]]
            kind = entry["type"]
            if kind == _DIRECTORY:
                prefix += "%dir "
            caps = entry["caps"]
            if caps != DEFAULT:
                prefix += f"%caps({caps}) "
            # rpm warns on an explicit mode for a symlink, which it ignores.
            if kind == _SYMLINK:
                mode = DEFAULT
            else:
                mode = entry["mode"] or DEFAULT
            owner = entry["owner"] or DEFAULT
            group = entry["group"] or DEFAULT
            lines.append(
                f"{prefix}%attr({mode},{owner},{group}) {escape(path)}\n".encode(
                    "utf-8", "surrogateescape"
                )
            )
        if self._batch_reject_needed("".join(paths)):
            # Something in the batch is bad -- run today's exact per-entry
            # check in entry order so the first offending entry raises
            # exactly the error it would raise rendered alone (nothing was
            # returned yet, so no partial output can have escaped this
            # method).
            for path in paths:
                self._reject(path)
        return b"".join(lines)

    def render_entry(self, path: str, entry: FileEntry) -> bytes:
        return self.render([(path, entry)])


class RPMSpecFilesPre419(RPMSpecFiles):
    """RPM ``%files`` lines for rpm older than 4.19 (measured against real
    rpmbuild runs on 4.14.3, 4.16.1 and 4.18.2).

    Inherits :class:`RPMSpecFiles`'s ``%attr``/``%dir``/kind/caps
    rendering and batch-then-per-entry validation, overriding only the
    escaping/rejection hooks: the path is written completely unquoted, and a
    path containing a space, a glob character (``* ? [ ] { }``), ``%`` (in
    any form), or a byte that is not valid UTF-8 raises
    :class:`~pkgforge.exceptions.DumpError` instead of risking a silently wrong
    or overmatched package. Use :class:`RPMSpecFiles` instead when the rpm
    that builds the package is 4.19 or newer.
    """

    NAME = "rpmspecfiles-pre419"
    ALIASES = ("rpm-pre419",)
    #: No escaping at all here -- an already-validated path is written back
    #: unquoted (see :func:`_rpm_reject_pre419`).
    _escape = staticmethod(lambda path: path)
    _reject = staticmethod(_rpm_reject_pre419)

    @staticmethod
    def _batch_reject_needed(joined: str) -> bool:
        return bool(
            _has_control([joined])
            or _REFUSED_PRE419_RE.search(joined)
            or _SURROGATE_RE.search(joined)
        )
