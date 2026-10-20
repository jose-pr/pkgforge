"""Shared registration mechanism for pluggable backend/format classes.

Both :mod:`pkgforge.db` (storage backends) and :mod:`pkgforge.dbdump` (dump
formats) need the same thing: a family of subclasses, each identifying
itself by a canonical ``NAME`` plus optional ``ALIASES``, selectable by that
name at runtime. :class:`Registered` is the one small, auditable mechanism
both use, so there is exactly one place that decides how registration and
lookup work.

A class registers by being defined, or by being advertised in its family's
entry-point group (``_GROUP``) and loaded on first use.
"""

from __future__ import annotations

import importlib.metadata
import logging
import typing

from .exceptions import PkgForgeError, UsageError

T = typing.TypeVar("T", bound="Registered")

#: ``(namespace, key)``: a name or alias, or a file suffix, that one class holds.
Claim = typing.Tuple[str, str]


class Registered:
    """Mixin: register a subclass under its own ``NAME``/``ALIASES``.

    A family base class (:class:`pkgforge.db.DbProvider`,
    :class:`pkgforge.dbdump.DumpFormat`) declares its own
    ``_registry: ClassVar[Dict[str, type]] = {}``, ``_KIND`` (the noun used
    in error text, e.g. ``"db format"``) and ``_GROUP`` (its entry-point
    group). ``__init_subclass__`` reads only the class's OWN body
    (``cls.__dict__``, never an inherited value) for ``NAME``: a subclass
    that does not itself set ``NAME`` registers nothing -- it is an abstract
    intermediate class (e.g. ``AppendLogDb``), and a further subclass of an
    already-registered class inherits neither its ``ALIASES`` nor (for
    :class:`pkgforge.db.DbProvider`) its ``SUFFIXES`` unless it redeclares
    them itself.

    A ``NAME``, alias or suffix that another class already holds raises
    :class:`ValueError` and leaves the new class unregistered. Lookups are
    case-sensitive.
    """

    #: The canonical name this class registers under. Only a subclass whose
    #: OWN body sets this is registered at all.
    NAME: typing.ClassVar[str] = ""
    #: Extra names that also resolve to this class.
    ALIASES: typing.ClassVar[typing.Tuple[str, ...]] = ()
    #: ``{name-or-alias: class}``. Every family base class overrides this
    #: with its own dict -- never shared between families.
    _registry: typing.ClassVar[typing.Dict[str, type]]
    #: The noun used in an "unknown ..." error message (e.g. "db format").
    _KIND: typing.ClassVar[str] = "entry"
    #: The entry-point group whose entries advertise third-party classes.
    _GROUP: typing.ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: typing.Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("NAME"):
            cls._add()

    @classmethod
    def _claims(cls) -> typing.List[Claim]:
        """What this class's own body claims: its name and aliases."""
        return [("name", key) for key in (cls.NAME, *cls.__dict__.get("ALIASES", ()))]

    @classmethod
    def _add(cls) -> None:
        """Register this class, or raise :class:`ValueError` naming the class
        that already holds one of its names, aliases or suffixes. Nothing is
        registered unless every claim is free."""
        claims = cls._claims()
        for other in set(cls._registry.values()):
            if other is cls:
                continue
            clash = [claim for claim in claims if claim in other._claims()]
            if clash:
                namespace, key = clash[0]
                raise ValueError(
                    f"{cls.__qualname__}: {namespace} {key!r} is already "
                    f"registered by {other.__qualname__}"
                )
        for namespace, key in claims:
            if namespace == "name":
                cls._registry[key] = cls

    @classmethod
    def _base(cls) -> type:
        """The family base class: the one that declares ``_registry``."""
        return next(klass for klass in cls.__mro__ if "_registry" in vars(klass))

    @classmethod
    def _advertised(cls) -> typing.List["importlib.metadata.EntryPoint"]:
        """The entry points of this family's group, nothing loaded."""
        found = importlib.metadata.entry_points()
        if hasattr(found, "select"):
            return list(found.select(group=cls._GROUP))
        return list(found.get(cls._GROUP, ()))

    @classmethod
    def _load(
        cls: typing.Type[T], entry_point: "importlib.metadata.EntryPoint"
    ) -> typing.Type[T]:
        """Import ``entry_point`` and register what it names.

        Raises :class:`~pkgforge.exceptions.PkgForgeError` naming the entry
        point when the import fails, the object is not a subclass of the
        family base whose own ``NAME`` is the entry point's name, or it
        clashes with a registered class.
        """

        def failure(reason: object) -> PkgForgeError:
            return PkgForgeError(
                f"{cls._KIND} {entry_point.name!r} "
                f"(entry point {entry_point.value}): {reason}"
            )

        try:
            loaded = entry_point.load()
            base = cls._base()
            if not (
                isinstance(loaded, type)
                and issubclass(loaded, base)
                and loaded.__dict__.get("NAME") == entry_point.name
            ):
                raise failure(
                    f"not a {base.__name__} subclass with NAME {entry_point.name!r}"
                )
            if cls._registry.get(entry_point.name) is not loaded:
                loaded._add()
        except PkgForgeError:
            raise
        except Exception as error:
            raise failure(f"{type(error).__name__}: {error}") from error
        return typing.cast("typing.Type[T]", loaded)

    @classmethod
    def _load_pending(cls, logger: logging.Logger) -> None:
        """Load every advertised class not yet registered; a failure is
        logged as a warning on ``logger`` and skipped."""
        for entry_point in cls._advertised():
            if entry_point.name not in cls._registry:
                try:
                    cls._load(entry_point)
                except PkgForgeError as error:
                    logger.warning("%s", error)

    @classmethod
    def names(cls) -> typing.List[str]:
        """Every canonical ``NAME``, sorted: registered classes plus the
        entry points advertised for this family, none of which is loaded."""
        names = {registered.NAME for registered in cls._registry.values()}
        names.update(entry_point.name for entry_point in cls._advertised())
        return sorted(names)

    @classmethod
    def _describe(cls) -> typing.List[str]:
        """:meth:`names`, each followed by its registered aliases in
        parens (e.g. ``"debian (deb)"``) -- used only to build the "choose
        from" text below.
        """
        aliases_by_name: typing.Dict[str, typing.List[str]] = {
            name: [] for name in cls.names()
        }
        for key, registered in cls._registry.items():
            if key != registered.NAME:
                aliases_by_name[registered.NAME].append(key)
        out = []
        for name in sorted(aliases_by_name):
            aliases = sorted(aliases_by_name[name])
            out.append(f"{name} ({', '.join(aliases)})" if aliases else name)
        return out

    @classmethod
    def get(cls: typing.Type[T], name: str) -> typing.Type[T]:
        """The class for ``name`` (its ``NAME`` or an alias).

        A name that is only advertised in the entry-point group is imported
        and registered here.

        Raises :class:`~pkgforge.exceptions.UsageError` naming every
        registered class (with its aliases) for an unrecognized ``name``, and
        :class:`~pkgforge.exceptions.PkgForgeError` for an advertised class
        that cannot be loaded.
        """
        try:
            return cls._registry[name]
        except KeyError:
            pass
        for entry_point in cls._advertised():
            if entry_point.name == name:
                return cls._load(entry_point)
        raise UsageError(
            f"unknown {cls._KIND} {name!r}; choose from {', '.join(cls._describe())}"
        ) from None
