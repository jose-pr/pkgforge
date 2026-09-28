"""``rpmspecfiles``: one RPM ``%files``/``%attr`` line per DB entry."""

from __future__ import annotations

from ..common import FileEntry, FileType, _or_default
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


class RpmSpecFiles(PerEntryFormat):
    """RPM ``%files`` lines: ``%attr(mode,owner,group) "path"``, a ``%dir``
    prefix for directories, ``meta["rpmprefix"]`` prepended if set.

    The path is quoted for rpm's ``%files -f`` parser (targets rpm 4.19+):
    a backslash and a double quote are escaped, and the whole line is
    written as UTF-8 with ``surrogateescape`` (a non-UTF-8 name round-trips
    its original bytes). Glob characters are never escaped -- rpm's own
    quoted-string globbing already matches only the literal name. A ``%``
    anywhere in the path, or a control character (including a tab), raises
    :class:`~pkgforge.dbdump.DumpError`.
    """

    NAME = "rpmspecfiles"
    ALIASES = ("rpm", "rpmspec")

    def render_entry(self, path: str, entry: FileEntry) -> bytes:
        prefix = entry["meta"].get("rpmprefix") or ""
        if prefix:
            prefix += " "
        if entry["type"] == FileType.Directory:
            prefix += "%dir "

        mode = _or_default(entry["mode"])
        owner = _or_default(entry["owner"])
        group = _or_default(entry["group"])

        quoted = _rpm_quote(path)
        return f"{prefix}%attr({mode},{owner},{group}) {quoted}\n".encode(
            "utf-8", "surrogateescape"
        )
