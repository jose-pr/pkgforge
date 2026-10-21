"""``tar``: one archive of each package's payload."""

from __future__ import annotations

import logging
import tarfile
import typing
from pathlib import Path

from ..dbdump import _write_files
from ..entry import DEFAULT
from . import BuildContext, PackageFormat, PackagePayload
from ._compress import SUFFIXES, compress
from ._payload import members, tar_bytes

_LOGGER = logging.getLogger("pkgforge.package")


class TarFormat(PackageFormat):
    """``NAME-VERSION[-RELEASE].tar[.gz|.xz|.zst]`` per package.

    GNU tar, names without ``./`` and no entry for the root, so extracting over
    ``/`` touches nothing it does not ship. Ghost entries are left out. A file
    capability cannot be stored in the archive; it is logged as a warning.
    """

    NAME = "tar"
    COMPRESSIONS = ("gz", "xz", "zst", "none")
    REQUIRED = ("version",)

    def build(
        self, payloads: typing.Sequence[PackagePayload], context: BuildContext
    ) -> typing.List[Path]:
        assert context.compression is not None
        files: typing.Dict[Path, bytes] = {}
        for payload in payloads:
            items = members(payload.entries, context.buildroot, epoch=context.epoch)
            for item in items:
                if item.entry is not None and item.entry["caps"] != DEFAULT:
                    _LOGGER.warning(
                        "%s: a tar carries no file capability; run setcap %s %s "
                        "after extracting",
                        item.path,
                        item.entry["caps"],
                        item.path,
                    )
            data = compress(
                tar_bytes(items, format=tarfile.GNU_FORMAT), context.compression
            )
            version = payload.main.version
            release = f"-{payload.main.release}" if payload.main.release else ""
            name = f"{payload.name}-{version}{release}.tar"
            files[context.output / (name + SUFFIXES[context.compression])] = data
        _write_files(files)
        return list(files)
