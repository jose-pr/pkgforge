"""Payload compression: gzip, xz and zstd streams at the levels packages ship with."""

from __future__ import annotations

import contextlib
import gzip
import typing

from ..exceptions import UsageError

#: The file suffix each compression adds to a payload.
SUFFIXES: typing.Dict[str, str] = {"gz": ".gz", "xz": ".xz", "zst": ".zst", "none": ""}

_ZSTD_LEVEL = 19


class Sink(typing.Protocol):
    """What the payload writers need of a file: ``write``."""

    def write(self, data: bytes, /) -> typing.Any: ...


class _ZstdStream:
    """A writable stream of one frame, over Python 3.14's ``compression.zstd``."""

    def __init__(self, zstd: typing.Any, out: Sink, size: typing.Optional[int]) -> None:
        self._zstd = zstd
        self._out = out
        self._compressor = zstd.ZstdCompressor(level=_ZSTD_LEVEL)
        if size is not None:
            self._compressor.set_pledged_input_size(size)

    def write(self, data: bytes) -> int:
        self._out.write(self._compressor.compress(data))
        return len(data)

    def finish(self) -> None:
        self._out.write(self._compressor.flush(self._zstd.ZstdCompressor.FLUSH_FRAME))


@contextlib.contextmanager
def _zstd(out: Sink, size: typing.Optional[int]) -> typing.Iterator[typing.Any]:
    """Python 3.14's ``compression.zstd``, else the ``zstandard`` package
    (the ``zstd`` extra)."""
    try:
        from compression import zstd
    except ImportError:
        pass
    else:
        stream = _ZstdStream(zstd, out, size)
        yield stream
        stream.finish()
        return
    try:
        import zstandard
    except ImportError:
        raise UsageError(
            "zstd compression needs Python 3.14 or the 'zstd' extra: pip install "
            '"pkgforge[zstd]" (or pass --compression xz)'
        ) from None
    compressor = zstandard.ZstdCompressor(level=_ZSTD_LEVEL)
    pledged = -1 if size is None else size
    with compressor.stream_writer(out, size=pledged, closefd=False) as writer:
        yield writer


@contextlib.contextmanager
def compressor(
    out: Sink, kind: str, size: typing.Optional[int] = None
) -> typing.Iterator[typing.Any]:
    """A writable stream whose data lands in ``out`` compressed as ``kind``
    (``gz``, ``xz``, ``zst`` or ``none``), finished when the block ends. ``out``
    is left open.

    gzip runs at level 9 with a zero timestamp and no file name, xz at preset 6
    with a CRC64 check and zstd at level 19, so equal input gives equal output
    for one Python and one zstd library, whatever the sizes of the writes.
    ``size``, when known, is the number of bytes that will be written: zstd
    records it in the frame header and sizes its window to it.

    Raises :class:`~pkgforge.exceptions.UsageError` for ``zst`` when neither
    ``compression.zstd`` nor ``zstandard`` imports.
    """
    if kind == "gz":
        with gzip.GzipFile(
            filename="", mode="wb", compresslevel=9, fileobj=out, mtime=0
        ) as stream:
            yield stream
    elif kind == "xz":
        import lzma  # absent from an interpreter built without liblzma

        with lzma.LZMAFile(
            out, "wb", format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64
        ) as stream:
            yield stream
    elif kind == "zst":
        with _zstd(out, size) as stream:
            yield stream
    elif kind == "none":
        yield out
    else:
        raise ValueError(f"unknown compression {kind!r}")
