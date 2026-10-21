"""Build packages directly from the file DB and the build root (:func:`build`).

A format is a :class:`PackageFormat` subclass: it receives one
:class:`PackagePayload` per declared package, plus a :class:`BuildContext`, and
writes one file per package into the output directory. Every format shares one
payload writer, so owners, modes and times come out the same in each of them:
user and group names from the DB (``-`` is ``root``), numeric ids always 0, the
staged permission bits where the DB leaves the mode unset, and times clamped to
``SOURCE_DATE_EPOCH`` when it is set.

A third party adds a format by subclassing :class:`PackageFormat` with its own
``NAME`` and listing it in the ``pkgforge.package_formats`` entry-point group.
"""

from __future__ import annotations

import abc
import dataclasses
import logging
import os
import platform
import re
import typing
from pathlib import Path

from .._registry import Registered
from ..config import PackageInfo, ProjectConfig, SubPackageInfo
from ..dbdump import (
    _packages,
    _require_keys,
    _source_date_epoch,
    _surviving_entries,
    _validate_entries,
)
from ..entry import FileEntries, FileEntry, StrPath
from ..exceptions import UsageError

_LOGGER = logging.getLogger("pkgforge.package")
_ARCH_RE = re.compile(r"[a-z0-9_]+")


@dataclasses.dataclass(frozen=True)
class BuildContext:
    """What a format builds against besides the payloads.

    ``config`` is the project configuration, ``buildroot`` the directory that
    maps to ``/`` in the DB, ``output`` the directory the packages are written
    into, ``compression`` the chosen payload compression (``None`` for a
    format that offers none), ``arch`` the ``--arch`` value, ``epoch`` the
    ``SOURCE_DATE_EPOCH`` seconds (``None`` when unset) and ``config_path`` the
    file ``config`` was read from when known.
    """

    config: ProjectConfig
    buildroot: Path
    output: Path
    compression: typing.Optional[str]
    arch: typing.Optional[str]
    epoch: typing.Optional[int]
    config_path: typing.Optional[Path] = None


class PackagePayload(typing.NamedTuple):
    """One package to build."""

    #: The package name.
    name: str
    #: The ``[package]`` table, which also holds the version and release.
    main: PackageInfo
    #: This package's own table: ``main`` itself, or its ``[[packages]]`` element.
    info: typing.Union[PackageInfo, SubPackageInfo]
    #: ``"all"`` or ``"any"``: the package's own, else the main's, else ``"any"``.
    arch: str
    #: The package's live entries, sorted by DB path.
    entries: FileEntries


class PackageFormat(Registered, abc.ABC):
    """A package format, selected by ``package -f NAME``.

    A subclass registers by declaring its own ``NAME`` (plus, optionally,
    ``ALIASES``); see :class:`pkgforge._registry.Registered`.
    """

    NAME: typing.ClassVar[str] = ""
    ALIASES: typing.ClassVar[typing.Tuple[str, ...]] = ()
    #: The payload compressions the format offers, the default first. Empty:
    #: the format takes no ``--compression``.
    COMPRESSIONS: typing.ClassVar[typing.Tuple[str, ...]] = ()
    #: The ``[package]`` keys the format needs besides ``name``.
    REQUIRED: typing.ClassVar[typing.Tuple[str, ...]] = ()
    #: The keys each ``[[packages]]`` element needs.
    SUB_REQUIRED: typing.ClassVar[typing.Tuple[str, ...]] = ()
    #: The format's name for an architecture-independent package.
    ALL_ARCH: typing.ClassVar[str] = "all"
    #: ``platform.machine()`` in lowercase to the format's architecture name.
    MACHINES: typing.ClassVar[typing.Mapping[str, str]] = {}
    _registry: typing.ClassVar[typing.Dict[str, type]] = {}
    _KIND = "package format"
    _GROUP = "pkgforge.package_formats"

    @abc.abstractmethod
    def build(
        self, payloads: typing.Sequence[PackagePayload], context: BuildContext
    ) -> typing.List[Path]:
        """Write one package per payload into ``context.output`` and return
        their paths in payload order."""

    def arch_name(self, payload: PackagePayload, context: BuildContext) -> str:
        """The architecture name ``payload`` carries in this format:
        :attr:`ALL_ARCH` for an ``all`` package, else ``context.arch``
        verbatim, else this machine's through :attr:`MACHINES`.

        Raises :class:`~pkgforge.exceptions.UsageError` for a machine the
        table lacks.
        """
        if payload.arch == "all":
            return self.ALL_ARCH
        if context.arch:
            return context.arch
        machine = platform.machine().lower()
        try:
            return self.MACHINES[machine]
        except KeyError:
            raise UsageError(
                f"{self.NAME}: no architecture name for machine {machine!r}: "
                "pass --arch"
            ) from None


def _payloads(
    entries: FileEntries, config: ProjectConfig
) -> typing.List[PackagePayload]:
    main = config.package
    assert main is not None  # _require_keys: name is always required
    subs = {sub.name: sub for sub in config.packages}
    payloads = []
    for name, group in _packages(entries, config):
        info = main if name == main.name else subs[name]
        payloads.append(
            PackagePayload(name, main, info, info.arch or main.arch or "any", group)
        )
    return payloads


def build(
    db: typing.Mapping[str, typing.Optional[FileEntry]],
    fmt: str,
    output: StrPath,
    *,
    buildroot: StrPath,
    config: typing.Optional[ProjectConfig] = None,
    config_path: typing.Optional[StrPath] = None,
    arch: typing.Optional[str] = None,
    compression: typing.Optional[str] = None,
) -> typing.List[Path]:
    """Build one package per declared package from the live entries of ``db``
    and the files under ``buildroot``, in the format ``fmt``.

    ``output`` is the directory the packages are written into (created if
    missing). ``config`` names the packages (default: an empty configuration,
    which no format accepts) and ``config_path`` is the file it was read from.
    ``arch`` names the architecture of an ``any`` package in the format's own
    spelling (default: this machine's); ``compression`` is one the format
    offers (default: its first). Build-time stamps and file times are
    ``SOURCE_DATE_EPOCH`` when it is set, so two builds of one build root are
    byte-identical.

    Returns the paths written, the main package first, then each
    ``[[packages]]`` element in order.

    Raises :class:`~pkgforge.exceptions.UsageError` for an unknown format, a
    compression or architecture the format cannot take, a ``[package]`` key the
    format needs, a ``SOURCE_DATE_EPOCH`` that is not a number, an entry of a
    package ``config`` does not declare, a recorded path missing from (or of
    another type in) ``buildroot`` or below a symlink that leads outside it,
    or a missing optional dependency;
    :class:`~pkgforge.exceptions.InvalidEntryError` for an entry no format can
    represent and :class:`~pkgforge.exceptions.DumpError` for one this format
    cannot.
    """
    cls = PackageFormat.get(fmt)
    if compression is None:
        chosen = cls.COMPRESSIONS[0] if cls.COMPRESSIONS else None
    elif not cls.COMPRESSIONS:
        raise UsageError(f"{cls.NAME} takes no --compression")
    elif compression not in cls.COMPRESSIONS:
        raise UsageError(
            f"{cls.NAME} takes --compression {', '.join(cls.COMPRESSIONS)}"
        )
    else:
        chosen = compression
    if arch is not None and not _ARCH_RE.fullmatch(arch):
        raise UsageError(f"--arch {arch!r} is not an architecture name ([a-z0-9_]+)")
    config = config if config is not None else ProjectConfig()
    _require_keys(cls.NAME, config, cls.REQUIRED, cls.SUB_REQUIRED)
    epoch = _source_date_epoch()
    entries = _surviving_entries(db, ())
    _validate_entries(entries)
    payloads = _payloads(entries, config)
    target = Path(os.fspath(output))
    target.mkdir(parents=True, exist_ok=True)
    context = BuildContext(
        config=config,
        buildroot=Path(os.fspath(buildroot)),
        output=target,
        compression=chosen,
        arch=arch,
        epoch=epoch,
        config_path=None if config_path is None else Path(os.fspath(config_path)),
    )
    written = cls().build(payloads, context)
    for path in written:
        _LOGGER.info("Wrote %s", path)
    _LOGGER.info("built %d package(s) with %s", len(written), cls.NAME)
    return written


# --------------------------------------------------------------------------
# Built-in formats: importing each module registers its format class.
# --------------------------------------------------------------------------

from ._apk import ApkFormat  # noqa: E402
from ._arch import ArchFormat  # noqa: E402
from ._deb import DebFormat  # noqa: E402
from ._rpm import RpmFormat  # noqa: E402
from ._tar import TarFormat  # noqa: E402

__all__ = [
    "ApkFormat",
    "ArchFormat",
    "BuildContext",
    "DebFormat",
    "PackageFormat",
    "PackagePayload",
    "RpmFormat",
    "TarFormat",
    "build",
]
