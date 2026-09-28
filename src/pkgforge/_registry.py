"""Shared registration mechanism for pluggable backend/format classes.

Both :mod:`pkgforge.db` (storage backends) and :mod:`pkgforge.dbdump` (dump
formats) need the same thing: a family of subclasses, each identifying
itself by a canonical ``NAME`` plus optional ``ALIASES``, selectable by that
name at runtime. :class:`Registered` is the one small, auditable mechanism
both use, so there is exactly one place that decides how registration and
lookup work.
"""

from __future__ import annotations

import typing

from .errors import UsageError

T = typing.TypeVar("T", bound="Registered")


class Registered:
    """Mixin: register a subclass under its own ``NAME``/``ALIASES``.

    A family base class (:class:`pkgforge.db.DbProvider`,
    :class:`pkgforge.dbdump.DumpFormat`) declares its own
    ``_registry: ClassVar[Dict[str, type]] = {}`` and ``_KIND`` (the noun
    used in error text, e.g. ``"db format"``). ``__init_subclass__`` reads
    only the class's OWN body (``cls.__dict__``, never an inherited value)
    for ``NAME``: a subclass that does not itself set ``NAME`` registers
    nothing -- it is an abstract intermediate class (e.g.
    ``AppendLogDb``), and a further subclass of an already-registered class
    inherits neither its ``ALIASES`` nor (for :class:`pkgforge.db.DbProvider`)
    its ``SUFFIXES`` unless it redeclares them itself.

    Each of ``NAME`` and the class's own ``ALIASES`` is popped from the
    registry before being reinserted, so a re-registration moves to the end:
    :meth:`_unique` (newest-registration-first) and any "last registered
    wins" lookup both see the latest registration for a name, never a stale
    earlier class left in its original slot.

    Lookups are case-sensitive, as the dict registries they replace always
    were.
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

    def __init_subclass__(cls, **kwargs: typing.Any) -> None:
        super().__init_subclass__(**kwargs)
        name = cls.__dict__.get("NAME")
        if not name:
            return
        for key in (name, *cls.__dict__.get("ALIASES", ())):
            cls._registry.pop(key, None)
            cls._registry[key] = cls

    @classmethod
    def _unique(cls: typing.Type[T]) -> typing.Iterator[typing.Type[T]]:
        """Registered classes, deduplicated, newest registration first.

        A class registered under several keys (its ``NAME`` plus each
        ``ALIASES`` entry) appears once, at the position of its most recent
        key -- so "try the newest registration first" (content sniffing) and
        "the last registration wins" (a suffix claimed by two classes) are
        both this same walk.
        """
        seen: typing.Set[type] = set()
        for registered in reversed(list(cls._registry.values())):
            if registered not in seen:
                seen.add(registered)
                yield registered

    @classmethod
    def names(cls) -> typing.List[str]:
        """Every registered class's own canonical ``NAME``, sorted."""
        return sorted({registered.NAME for registered in cls._registry.values()})

    @classmethod
    def _describe(cls) -> typing.List[str]:
        """:meth:`names`, each followed by its registered aliases in
        parens (e.g. ``"debian (deb)"``) -- used only to build the "choose
        from" text below.
        """
        aliases_by_name: typing.Dict[str, typing.List[str]] = {}
        for key, registered in cls._registry.items():
            aliases_by_name.setdefault(registered.NAME, [])
            if key != registered.NAME:
                aliases_by_name[registered.NAME].append(key)
        out = []
        for name in sorted(aliases_by_name):
            aliases = sorted(aliases_by_name[name])
            out.append(f"{name} ({', '.join(aliases)})" if aliases else name)
        return out

    @classmethod
    def lookup(cls: typing.Type[T], name: str) -> typing.Type[T]:
        """The registered class for ``name`` (its ``NAME`` or an alias).

        Raises :class:`~pkgforge.errors.UsageError` naming every registered
        class (with its aliases) for an unrecognized ``name``.
        """
        try:
            return cls._registry[name]
        except KeyError:
            raise UsageError(
                f"unknown {cls._KIND} {name!r}; choose from "
                f"{', '.join(cls._describe())}"
            ) from None
