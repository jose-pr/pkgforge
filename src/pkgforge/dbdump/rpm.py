"""``rpmspecfiles``: one RPM ``%files``/``%attr`` line per DB entry."""

from __future__ import annotations

import re

from ..entry import FileEntry, FileType, _or_default
from . import PerEntryFormat, DumpError, _reject_control


def _rpm_quote(path: str) -> str:
    """Quote ``path`` for an rpm ``%files``/``%attr`` line (rpm 4.19+).

    rpm's ``%files -f`` parser macro-expands every line BEFORE it sees the
    quoting: on rpm 4.19+ no spelling of ``%`` is literal inside or outside
    double quotes, so a path containing one is refused outright rather than
    escaped (below 4.19, ``%%`` ran the doubled-percent expansion and
    ``%(cmd)`` ran ``cmd`` as a shell command -- there is currently no
    escaping that is safe on that range, so it is refused there too). Glob
    characters
    (``* ? [ ]``) are never escaped: rpm's own shell-globbing quoting rules
    make a quoted glob character match only the literal path anyway.
    """
    _reject_control(path, "rpmspecfiles")
    if "%" in path:
        raise DumpError(
            "rpmspecfiles: a DB entry's path contains '%', which rpm's "
            "%files parser expands as a macro on every rpm version; write "
            "this entry's %files line by hand"
        )
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


#: Characters :func:`_rpm_quote_pre419` refuses outright: a space (ends rpm's
#: unquoted token early), every glob character (unquoted, so rpm's own
#: globbing may match a sibling instead of the literal name), and ``%`` (rpm
#: macro-expands every ``%files -f`` line, on every version). Measured
#: 2026-09-29 against real rpmbuild runs on rpm 4.14.3, 4.16.1 and 4.18.2: a
#: bare ``"`` or ``\`` survives an unquoted token unescaped and literal, so
#: neither is refused here.
_REFUSED_PRE419 = frozenset(" *?[]{}%")
#: A lone surrogate (``surrogateescape``'s stand-in for a non-UTF-8 byte).
_SURROGATE_RE = re.compile("[\udc80-\udcff]")


def _rpm_quote_pre419(path: str) -> str:
    """Quote ``path`` for rpm older than 4.19's ``%files -f`` parser
    (measured against real rpmbuild runs on 4.14.3, 4.16.1 and 4.18.2).

    Below rpm 4.19, a quoted ``%files -f`` name is macro-expanded TWICE
    (``specExpand``, then ``rpmExpand`` again while resolving the file), and
    an unquoted name's glob characters are matched by rpm's own globbing
    before pkgforge's own escaping is ever consulted -- there is no
    escaping on that range that is safe for a space or a glob character.
    This writes the path completely unquoted instead (rpm's bare-token
    reader passes a literal ``"`` or ``\\`` straight through, unescaped),
    and refuses outright a path containing a space, a glob character
    (``* ? [ ] { }``), ``%`` (in any form), or a byte that is not valid
    UTF-8, rather than risk packaging the wrong file.
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
    #: The quoting hook: a plain function, held as a class attribute so a
    #: subclass can override it without also overriding `render_entry`.
    _quote = staticmethod(_rpm_quote)

    def render_entry(self, path: str, entry: FileEntry) -> bytes:
        prefix = entry["meta"].get("rpmprefix") or ""
        if prefix:
            prefix += " "
        if entry["type"] == FileType.Directory:
            prefix += "%dir "

        mode = _or_default(entry["mode"])
        owner = _or_default(entry["owner"])
        group = _or_default(entry["group"])

        quoted = self._quote(path)
        return f"{prefix}%attr({mode},{owner},{group}) {quoted}\n".encode(
            "utf-8", "surrogateescape"
        )


class RpmSpecFilesPre419(RpmSpecFiles):
    """RPM ``%files`` lines for rpm older than 4.19 (measured against real
    rpmbuild runs on 4.14.3, 4.16.1 and 4.18.2).

    Inherits :class:`RpmSpecFiles`'s ``%attr``/``%dir``/``rpmprefix``
    rendering and overrides only the quoting: the path is written completely
    unquoted, and a path containing a space, a glob character
    (``* ? [ ] { }``), ``%`` (in any form), or a byte that is not valid
    UTF-8 raises :class:`~pkgforge.dbdump.DumpError` instead of risking a
    silently wrong or overmatched package. Use :class:`RpmSpecFiles` instead
    when the rpm that builds the package is 4.19 or newer.
    """

    NAME = "rpmspecfiles-pre419"
    ALIASES = ("rpm-pre419",)
    _quote = staticmethod(_rpm_quote_pre419)
