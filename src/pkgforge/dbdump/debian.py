"""``debian``: an ``install`` path list + ``permissions``/``dirs`` manifests."""

from __future__ import annotations

import posixpath
import typing

from ..common import DEFAULT, FileType, _or_default
from . import DumpError, Entries, MultiArtifactFormat, _WHITESPACE_RE, _reject_control

#: Characters dh_install/dh_installdirs read as shell-glob syntax in a
#: SOURCE line, plus the backslash that escapes them; ``{``/``}`` are
#: included, so escaping them also makes a literal ``${`` read as literal
#: (``$\{``), with no separate ``$`` handling needed on the source side.
_DH_GLOB_CHARS = "\\*?[]{}"
_DH_SRC_TABLE = str.maketrans(
    {**{c: "\\" + c for c in _DH_GLOB_CHARS}, " ": "${Space}"}
)


def _dh_src(rel: str) -> str:
    """Escape a debian ``install``/``dirs`` SOURCE (debhelper compat 13+).

    Backslash-escapes every glob character so the name matches only
    itself, and ``${Space}``-escapes a literal space; a leading ``#`` (the
    line's own first character) is backslash-escaped too, since dh_install
    treats a line starting with ``#`` as a comment. A bare ``$`` is left
    alone -- see :func:`_dh_dest`.
    """
    text = rel.translate(_DH_SRC_TABLE)
    if text.startswith("#"):
        text = "\\" + text
    return text


def _dh_dest(rel: str) -> str:
    """Escape a debian ``install``/``dirs`` DESTINATION.

    dh_install never globs the destination (a backslash there is kept
    literally), so only a literal ``${`` (an unresolved compat-13 variable)
    and a space need handling; a bare ``$`` not followed by ``{`` stays
    literal, which also works on compat 12.
    """
    return rel.replace("${", "${Dollar}{").replace(" ", "${Space}")


class Debian(MultiArtifactFormat):
    """Debian packaging artifacts built from surviving DB entries:

    * ``install`` -- ``dh_install``-style lines ``<src> <dest-dir>`` (the
      source is the build-root-relative path, debhelper-escaped; the
      destination is the entry's parent directory, escaped for ``$``/space
      only), one per non-directory entry;
    * ``permissions`` -- a pkgforge-specific ``<path> <mode> <owner>
      <group>`` manifest (unescaped: parse it right-to-left, since the path
      itself may contain spaces) for every entry that pins a non-default
      mode/owner/group;
    * ``dirs`` -- ``dh_installdirs``-style lines (dest-escaped, one per
      directory entry, in every case -- an already-populated directory is a
      harmless duplicate `mkdir`), so a directory recorded empty (``install
      -d``) still reaches the package; never routed through ``install``,
      which would re-copy any children ``--exclude`` dropped.

    Every entry is validated (path free of control characters; mode/owner/
    group free of whitespace) before any artifact is built, so a
    :class:`~pkgforge.dbdump.DumpError` leaves no partial output.
    """

    NAME = "debian"
    ALIASES = ("deb",)

    def render(self, entries: Entries) -> typing.Dict[str, bytes]:
        for path, entry in entries:
            _reject_control(path, "debian")
            for field in ("mode", "owner", "group"):
                value = _or_default(entry[field])
                if _WHITESPACE_RE.search(value):
                    raise DumpError(
                        f"debian: a DB entry's {field} contains whitespace, "
                        "which the permissions format cannot represent"
                    )

        install_lines: typing.List[str] = []
        perm_lines: typing.List[str] = []
        dir_lines: typing.List[str] = []
        for path, entry in entries:
            rel = path.lstrip("/")
            mode = _or_default(entry["mode"])
            owner = _or_default(entry["owner"])
            group = _or_default(entry["group"])
            if entry["type"] == FileType.Directory:
                dline = _dh_dest(rel)
                # dh_installdirs, like dh_install, treats a line starting
                # with "#" as a comment; "./" is a directory-neutral prefix
                # that keeps the leaf name literal instead of backslash-
                # escaping it (dh_installdirs never globs, so there is
                # nothing to escape).
                if dline.startswith("#"):
                    dline = "./" + dline
                dir_lines.append(dline)
            else:
                dest_dir = _dh_dest(posixpath.dirname(rel))
                install_lines.append(f"{_dh_src(rel)} {dest_dir}".rstrip())
            if mode != DEFAULT or owner != DEFAULT or group != DEFAULT:
                perm_lines.append(f"{path} {mode} {owner} {group}")

        def _join(lines: typing.List[str]) -> bytes:
            text = "\n".join(lines) + "\n" if lines else ""
            return text.encode("utf-8", "surrogateescape")

        return {
            "install": _join(install_lines),
            "permissions": _join(perm_lines),
            "dirs": _join(dir_lines),
        }
