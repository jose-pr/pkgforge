"""``install``'s decompression kind table: ``-x``/``--decompress`` KIND
resolution and the stdin-source suffix-to-kind inference it feeds."""

from __future__ import annotations

import os
import typing

from ..errors import UsageError

#: Kind -> (argv prefix, canonical suffix). ``argv`` always includes ``-d``
#: (or the tool's own always-decompressing form), so a compressor's name
#: passed as a KIND can never compress instead of decompress; the source (or
#: ``-`` for stdin) is appended by :meth:`Install.install`.
_DECOMPRESSORS: typing.Dict[str, typing.Tuple[typing.List[str], str]] = {
    "gz": (["gzip", "-dc"], ".gz"),
    "xz": (["xz", "-dc"], ".xz"),
    "bz2": (["bzip2", "-dc"], ".bz2"),
    "zst": (["zstd", "-dcq"], ".zst"),
    "lzma": (["xz", "--format=lzma", "-dc"], ".lzma"),
}
#: Decompressor tool name -> canonical kind key in :data:`_DECOMPRESSORS`, so
#: ``-x gunzip``/``-x unxz``/etc keep working as aliases for the kind.
_KIND_ALIASES: typing.Dict[str, str] = {
    "gzip": "gz",
    "gunzip": "gz",
    "xz": "xz",
    "unxz": "xz",
    "bzip2": "bz2",
    "bunzip2": "bz2",
    "zstd": "zst",
    "unzstd": "zst",
    "lzma": "lzma",
    "unlzma": "lzma",
}
#: Canonical suffix (lowercased) -> kind key, for inferring KIND from a bare
#: ``-x``'s source suffix.
_SUFFIX_TO_KIND: typing.Dict[str, str] = {
    suffix: kind for kind, (_, suffix) in _DECOMPRESSORS.items()
}


def _resolve_kind(kind: str) -> str:
    """Resolve a ``--decompress`` value (a kind or a decompressor tool name,
    matched case-insensitively) to a canonical key in :data:`_DECOMPRESSORS`.

    Raises :class:`UsageError` for anything else -- never falls back to
    running the value as an arbitrary command.
    """
    key = kind.lower()
    key = _KIND_ALIASES.get(key, key)
    if key not in _DECOMPRESSORS:
        raise UsageError(
            f"unknown compression kind {kind!r}; use one of "
            f"{', '.join(sorted(_DECOMPRESSORS))}"
        )
    return key


def _looks_like_path(kind: str) -> bool:
    """True if a ``--decompress`` value looks like a path instead of a kind.

    ``-x`` takes an *optional* argument, so argparse hands it the next token:
    ``install -x SRC DST`` parses ``SRC`` as the compression kind (and then
    errors out about a missing destination, or, with more sources, silently
    shifts every positional along by one). A kind is a bare word — ``gz`` or a
    decompressor command name — so a separator or a suffix means the misparse.
    """
    return bool(kind) and any(sep in kind for sep in (".", "/", os.sep))
