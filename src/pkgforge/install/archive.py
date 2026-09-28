"""``install``'s archive extraction policy: tar-family via stdlib
:mod:`tarfile`, everything else (and stdin) via ``bsdtar`` -- one shared
containment/ownership policy for both paths, and :func:`extract`, the glue
that picks between them."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import typing
from pathlib import Path

from ..entry import DEFAULT
from ..errors import PkgForgeError

#: Archive suffixes handled by stdlib :mod:`tarfile` (tar family + compression).
#: Anything else (e.g. ``.iso``) falls back to ``bsdtar``.
TAR_SUFFIXES = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tbz",
    ".tar.xz",
    ".txz",
)

#: True when this interpreter's ``tarfile`` supports the ``filter=`` extraction
#: argument (PEP 706, added in 3.12; backported to 3.9.17+). Passing ``filter=``
#: on an interpreter without it raises ``TypeError``, so we only opt in when safe.
_TARFILE_HAS_FILTER = hasattr(tarfile, "data_filter")

#: Suffixes stripped from an extracted archive's destination directory name
#: (``app-1.0.tgz`` -> ``app-1.0``): every :data:`TAR_SUFFIXES` entry plus the
#: two bsdtar-only formats named on the ``-x``/``--decompress`` KIND list
#: elsewhere in this module. Matched case-insensitively by
#: :func:`_archive_dir_name`, longest match first.
ARCHIVE_SUFFIXES: typing.Tuple[str, ...] = (*TAR_SUFFIXES, ".iso", ".zip")


def _archive_dir_name(name: str) -> str:
    """Strip an archive source's suffix from a directory-install
    destination name (``app-1.0.tgz`` -> ``app-1.0``). The caller applies
    this only when the source is an archive being extracted, never a real
    directory source, which keeps its own name (``conf.tar.d`` stays
    ``conf.tar.d``).

    Matches the longest of :data:`ARCHIVE_SUFFIXES` against ``name.lower()``
    first. Failing that, falls back to the old ``tar``/``iso`` dot-segment
    rule (now case-insensitive) for a bsdtar-only tar variant with no fixed
    suffix list entry, such as ``.tar.zst`` or ``.tar.lz4``: it cuts at the
    last ``tar`` or ``iso`` segment, preferring ``tar`` when both are
    present. Never returns an empty name -- a source literally named e.g.
    ``.tgz``, with nothing before the suffix, keeps it as-is.
    """
    low = name.lower()
    matched = max(
        (suffix for suffix in ARCHIVE_SUFFIXES if low.endswith(suffix)),
        key=len,
        default=None,
    )
    if matched is not None and len(name) > len(matched):
        return name[: -len(matched)]

    parts = name.split(".")
    if len(parts) > 1:
        suffixes = list(reversed(parts[1:]))
        lowered = [s.lower() for s in suffixes]
        for ty in ("tar", "iso"):
            if ty in lowered:
                idx = lowered.index(ty)
                stripped = ".".join([parts[0], *reversed(suffixes[idx + 1 :])])
                if stripped:
                    return stripped
                break
    return name


def _is_tar_source(src: Path | str) -> bool:
    """True if ``src`` is a tar-family archive stdlib :mod:`tarfile` can extract."""
    name = os.fspath(src).lower()
    return name.endswith(TAR_SUFFIXES)


#: Flags always passed to ``bsdtar -x``, so an extraction never restores an
#: archive's ownership, setuid/setgid bit, group/other write bit, xattrs,
#: ACLs or file flags -- even when this process runs as root, where bsdtar's
#: own defaults (``--same-owner``, ``-p``) would otherwise apply them. Needs
#: libarchive 3.3+ for the xattr/ACL/fflags flags; every extraction is still
#: followed by :func:`_reject_special_files`, since these flags alone do not
#: stop bsdtar from creating a device node or FIFO as root.
BSDTAR_EXTRACT_FLAGS: typing.Tuple[str, ...] = (
    "--no-same-owner",
    "--no-same-permissions",
    "--no-xattrs",
    "--no-acls",
    "--no-fflags",
)


def _special_file_kind(mode: int) -> typing.Optional[str]:
    """Name the special-file kind of a raw ``st_mode``, or ``None`` for a
    regular file, directory or symlink (nothing to refuse)."""
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISCHR(mode):
        return "character device"
    if stat.S_ISBLK(mode):
        return "block device"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        return None
    return "special file"


def _reject_special_files(root: Path) -> None:
    """Walk an already-extracted ``root`` and raise :class:`PkgForgeError`
    naming the first device node, FIFO or socket found -- the same kind of
    entry the tarfile path already refuses via ``tarfile.SpecialFileError``,
    but that bsdtar happily creates (as root, even with
    :data:`BSDTAR_EXTRACT_FLAGS`). The caller removes the whole extraction
    directory on this (or any) failure, so nothing further is unlinked here.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            path = Path(dirpath, name)
            kind = _special_file_kind(os.lstat(path).st_mode)
            if kind is not None:
                raise PkgForgeError(f"{path}: refusing to extract a {kind}")


def _extract_bsdtar(src: typing.Union[Path, str], dst: Path) -> None:
    """Extract ``src`` (a real path, or :data:`DEFAULT` for stdin) into
    ``dst`` via the external ``bsdtar`` binary -- the fallback for stdin and
    any format stdlib :mod:`tarfile` cannot open (``.zip``, ``.iso``,
    ``.cpio``, or a tar variant it has no codec for).

    Always passes :data:`BSDTAR_EXTRACT_FLAGS` and then runs
    :func:`_reject_special_files` on the result, so this path applies the
    same extraction policy as :func:`_extract_tar` regardless of format or
    whether the source came from stdin. Raises :class:`PkgForgeError` naming
    ``bsdtar`` if it is not on ``PATH``, before running anything.
    """
    if shutil.which("bsdtar") is None:
        raise PkgForgeError(f"cannot extract {src}: bsdtar not found on PATH")
    from_stdin = src == DEFAULT
    subprocess.run(
        [
            "bsdtar",
            "-x",
            *BSDTAR_EXTRACT_FLAGS,
            "-C",
            os.fspath(dst),
            "-f",
            "-" if from_stdin else os.fspath(src),
        ],
        stdin=sys.stdin if from_stdin else None,
        check=True,
    )
    _reject_special_files(dst)


def _inside(path: str, root: str) -> bool:
    """True if realpath ``path`` is ``root`` itself or strictly below it."""
    return path == root or path.startswith(root + os.sep)


def _parent_has_symlink(dest_path: str, name: str) -> bool:
    """True if any path component between ``dest_path`` and ``name``'s own
    parent directory is itself a symlink -- even one that resolves back
    inside ``dest_path``. Matches ``bsdtar``'s own policy: it refuses to
    write through any such component, not only one that escapes."""
    current = dest_path
    for part in os.path.dirname(name).split("/"):
        if not part or part == ".":
            continue
        current = os.path.join(current, part)
        if os.path.islink(current):
            return True
    return False


def _staging_filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo:
    """Extraction filter for :func:`_extract_tar`, replacing stdlib's own
    ``'data'``/``'tar'`` filters with a policy that matches ``bsdtar``'s
    default instead.

    A **symlink** member's target is kept exactly as stored -- absolute or
    climbing above the destination included, ordinary content for a build
    root -- but the member's own placement is refused (before anything is
    touched) if any path component between the destination and its parent
    is itself a symlink, even one that resolves back inside the
    destination: writing through it is refused the same way ``bsdtar``
    refuses it. A **hardlink** member's target, unlike a symlink's, names
    another *archive member's* already-extracted path, so a leading ``/``
    is stripped (making it relative, like a member's own name) before the
    resolved target is required to stay inside the destination, else
    :class:`tarfile.LinkOutsideDestinationError`; the stripped value is
    carried forward on the returned member, never the original -- an
    unstripped absolute linkname reaches ``os.path.join(dest_path,
    linkname)`` during the actual link creation unchanged (``os.path.join``
    *discards* ``dest_path`` for an absolute second argument), which would
    hardlink straight to that real host path if one happens to exist there,
    entirely bypassing containment. ``tarfile.tar_filter`` itself never
    checks or rewrites a hardlink's target at all. A device, FIFO or socket
    member raises :class:`tarfile.SpecialFileError`, the same as the
    ``'data'`` filter this replaces.

    Once a member's placement is confirmed safe, any non-directory already
    at its own path is removed before ``tar_filter`` runs: re-extracting
    the same archive over an earlier run (or a hardlink replacing another
    member's stale output) must not fail with ``FileExistsError``, and must
    not let ``tar_filter``'s own realpath check chase a stale symlink an
    earlier run left at that exact path.

    ``tarfile.tar_filter`` clears setuid/setgid/sticky and group/other
    write, but -- unlike ``'data'`` -- leaves the archive's recorded
    owner/group in place (restored as root); this filter always nulls them
    on the way out.
    """
    name = member.name.replace(os.sep, "/").lstrip("/")
    dest_real = os.path.realpath(dest_path)
    parent_real = os.path.realpath(os.path.join(dest_path, os.path.dirname(name)))

    if _parent_has_symlink(dest_path, name) or not _inside(parent_real, dest_real):
        raise tarfile.OutsideDestinationError(
            member, os.path.join(parent_real, os.path.basename(name))
        )

    if member.isdev():
        raise tarfile.SpecialFileError(member)

    if member.islnk():
        linkname = member.linkname.replace(os.sep, "/").lstrip("/")
        link_real = os.path.realpath(os.path.join(dest_path, linkname))
        if not _inside(link_real, dest_real):
            raise tarfile.LinkOutsideDestinationError(member, link_real)
        member = member.replace(linkname=linkname, deep=False)

    if not member.isdir():
        target = os.path.join(parent_real, os.path.basename(name))
        if os.path.lexists(target) and not os.path.isdir(target):
            os.remove(target)

    filtered = tarfile.tar_filter(member, dest_path)
    return filtered.replace(uid=None, gid=None, uname=None, gname=None, deep=False)


def _extract_tar(path: typing.Union[str, os.PathLike], dst: Path) -> None:
    """Extract the tar-family archive at ``path`` into ``dst`` using stdlib
    :mod:`tarfile` and :func:`_staging_filter`.

    Path-only: a stdin (``-``) source is never tar-family-typed
    (:func:`_is_tar_source` only runs on a real path) and stages through
    ``bsdtar`` instead. Refuses outright, with :class:`PkgForgeError`
    naming ``path``, when this interpreter's :mod:`tarfile` has no
    extraction filter at all (:data:`_TARFILE_HAS_FILTER`; PEP 706, needs
    3.9.17+/3.10.12+/3.11.4+) -- the caller routes such an interpreter to
    ``bsdtar`` instead, but a direct caller of this function must not
    silently fall through to an unfiltered ``extractall``. A
    :class:`tarfile.FilterError` (raised by :func:`_staging_filter` itself,
    or by ``tarfile.tar_filter``) or the bare ``KeyError`` tarfile's own
    hardlink resolution raises for an unknown/refused earlier member is
    re-raised as :class:`PkgForgeError` naming ``path``.
    """
    if not _TARFILE_HAS_FILTER:
        raise PkgForgeError(
            f"refusing to extract {path}: this Python's tarfile has no "
            "extraction filter (PEP 706; needs 3.9.17+/3.10.12+/3.11.4+); "
            "install bsdtar, or use a newer Python"
        )
    try:
        with tarfile.open(name=os.fspath(path), mode="r:*") as tar:
            tar.extractall(os.fspath(dst), filter=_staging_filter)
    except (tarfile.FilterError, KeyError) as exc:
        raise PkgForgeError(f"refusing to extract {path}: {exc}") from exc


def extract(src: typing.Union[Path, str], dst: Path, logger: logging.Logger) -> None:
    """Extract ``src`` into ``dst``, preferring stdlib ``tarfile`` (no
    external binary, cross-platform, staging filter) when this interpreter
    has an extraction filter at all; falls back to ``bsdtar`` for stdin,
    formats ``tarfile`` can't open (e.g. iso), and an interpreter with no
    filter (:func:`_extract_tar` itself would refuse rather than extract
    unfiltered).
    """
    if src != DEFAULT and _is_tar_source(src) and _TARFILE_HAS_FILTER:
        logger.debug("Extracting %s via tarfile", src)
        _extract_tar(src, dst)
    else:
        logger.debug("Extracting %s via bsdtar", src)
        _extract_bsdtar(src, dst)
