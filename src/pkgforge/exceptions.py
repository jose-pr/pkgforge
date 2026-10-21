"""pkgforge's own exception types.

:class:`PkgForgeError` and :class:`UsageError` are caught by
:func:`pkgforge.cli.main`'s error boundary; see its docstring for the exit-code
mapping. Python-API callers that invoke a command directly still see them
raised normally.
"""

from __future__ import annotations

__all__ = [
    "PkgForgeError",
    "UsageError",
    "DbError",
    "DumpError",
    "InvalidEntryError",
    "UnsupportedOutputError",
    "ExcludeSyntaxError",
]


class PkgForgeError(Exception):
    """Base class for pkgforge's own runtime failures.

    Caught by :func:`pkgforge.cli.main`'s error boundary: prints one
    ``pkgforge: error: ...`` line to stderr and exits 1. Python-API callers
    still see it raised normally.
    """


class UsageError(PkgForgeError, ValueError):
    """An argument-shaped mistake (a bad or missing value the caller gave).

    Caught by :func:`pkgforge.cli.main`'s error boundary and mapped to exit 2,
    like an argparse usage error. Subclasses :class:`ValueError` because it
    reports malformed input.
    """


class DbError(PkgForgeError, ValueError):
    """A file DB's on-disk content could not be used as one.

    Raised for a parse error, non-UTF-8 bytes, content that is not in the
    chosen format, a JSON Lines line or YAML top-level document that is not a
    mapping, or a record that breaks the schema (see :func:`_check_record`).
    Caught by :func:`pkgforge.cli.main`'s error boundary like any
    :class:`~pkgforge.exceptions.PkgForgeError` (one stderr line, exit 1). A
    :class:`ValueError` because it reports malformed input.
    """


class DumpError(PkgForgeError, ValueError):
    """A DB entry cannot be rendered in the chosen format.

    Raised for a path the target format's own tooling cannot represent -- a
    control character, or (rpm only) a ``%``. Caught by
    :func:`pkgforge.cli.main`'s error boundary like any
    :class:`~pkgforge.exceptions.PkgForgeError`: one stderr line, exit 1. Nothing
    is written to OUTPUT first: every entry is rendered before OUTPUT is
    opened, so this leaves no partial file.
    """


class InvalidEntryError(UsageError, DumpError):
    """A DB entry is malformed for every format: a key that is not an absolute
    normalized path, an unknown ``type``, a ``mode``/``owner``/``group`` outside
    the recorded grammar, an unknown ``kind`` or one that does not fit the
    ``type``, or ``caps`` that are malformed or on anything but a regular
    file that is not a ghost.

    Raised by :func:`_validate_entries` before any format renders, so it
    leaves no output behind. A :class:`~pkgforge.exceptions.UsageError` (exit 2):
    the DB, not the format, holds the bad value.
    """


class UnsupportedOutputError(UsageError, DumpError, NotImplementedError):
    """OUTPUT is the wrong shape for the chosen format: a file for a
    multi-artifact format, or an existing directory for a per-entry one.

    Subclasses :class:`~pkgforge.exceptions.UsageError` (exit 2, same as this
    replaces two separate raises with) and :class:`DumpError` (the format
    itself is refusing this OUTPUT, not a bad argument in general) and, on
    top of both, :class:`NotImplementedError` (this format cannot write
    this output shape) -- a valid MRO (measured, py3.14).
    """


class ExcludeSyntaxError(UsageError):
    """A malformed ``--exclude`` statement: an unknown inline test name, a
    bad ``(?type:...)``/``(?meta:...)`` argument, or an unterminated
    ``(?...`` that never closed.

    Subclasses :class:`~pkgforge.exceptions.UsageError`, so
    ``pkgforge.cli.main()``'s error boundary maps it to a clean one-line message
    and exit 2; it is also a ``ValueError``.
    """
