"""Payload compression: gzip, xz and zstd at the levels packages ship with."""

from __future__ import annotations

import gzip
import typing

from ..exceptions import UsageError

#: The file suffix each compression adds to a payload.
SUFFIXES: typing.Dict[str, str] = {"gz": ".gz", "xz": ".xz", "zst": ".zst", "none": ""}

_ZSTD_LEVEL = 19


def _zstd(data: bytes) -> bytes:
    """Python 3.14's ``compression.zstd``, else the ``zstandard`` package
    (the ``zstd`` extra)."""
    try:
        from compression import zstd
    except ImportError:
        pass
    else:
        return zstd.compress(data, level=_ZSTD_LEVEL)
    try:
        import zstandard
    except ImportError:
        raise UsageError(
            "zstd compression needs Python 3.14 or the 'zstd' extra: pip install "
            '"pkgforge[zstd]" (or pass --compression xz)'
        ) from None
    return zstandard.ZstdCompressor(level=_ZSTD_LEVEL).compress(data)


def compress(data: bytes, kind: str) -> bytes:
    """``data`` compressed as ``kind`` (``gz``, ``xz``, ``zst`` or ``none``).

    gzip runs at level 9 with a zero timestamp, xz at preset 6 with a CRC64
    check and zstd at level 19, so equal input gives equal output for one
    Python and one zstd library.

    Raises :class:`~pkgforge.exceptions.UsageError` for ``zst`` when neither
    ``compression.zstd`` nor ``zstandard`` imports.
    """
    if kind == "gz":
        return gzip.compress(data, 9, mtime=0)
    if kind == "xz":
        import lzma  # absent from an interpreter built without liblzma

        return lzma.compress(data, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64)
    if kind == "zst":
        return _zstd(data)
    if kind == "none":
        return data
    raise ValueError(f"unknown compression {kind!r}")
