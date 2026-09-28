"""``yaml``: append-only YAML, a single mapping appended key by key."""

from __future__ import annotations

import functools
import typing
from pathlib import Path

from . import Db, DbError, _fields
from ._appendlog import AppendLogDb, _normalize

if typing.TYPE_CHECKING:
    from ..entry import FileEntry

#: The one YAML 1.1 implicit-resolver tag :func:`_yaml_io`'s loader keeps
#: (every other implicit tag -- int, float, bool, timestamp -- is dropped so
#: an unquoted scalar loads as the text it was written as).
_YAML_NULL_TAG = "tag:yaml.org,2002:null"


@functools.lru_cache(maxsize=None)
def _yaml_io() -> typing.Tuple[type, type]:
    """Lazily import PyYAML and return its ``(Loader, Dumper)`` classes.

    The import is deferred here rather than a module-level ``import yaml``,
    so ``import pkgforge``, ``--help`` and the ``jsonl`` backend all work on
    an interpreter that lacks PyYAML; only actually touching the ``yaml``
    backend pays for the import. A missing module raises one clear
    :class:`~pkgforge.errors.PkgForgeError` instead of a bare ``ImportError``
    surfacing from wherever this was first called.

    Prefers PyYAML's libyaml-backed ``CSafeLoader``/``CSafeDumper`` (several
    times faster than the pure-Python ``SafeLoader``/``SafeDumper``) when the
    installed PyYAML build has them, falling back to the pure-Python classes
    otherwise. Both give identical results: the C loader only swaps the
    scanner/parser (its constructor is still ``SafeConstructor``, so
    duplicate-key last-wins is unaffected), and the C dumper's output is
    byte-identical to the pure-Python one.

    The returned Loader is a subclass with every *implicit* resolver but
    ``null`` removed, so an unquoted scalar loads as the text it was
    written as (``mode: 0755`` is ``"0755"``, not the int ``493``) instead
    of PyYAML's YAML-1.1 int/float/bool/timestamp guessing; ``~``/``null``
    still load as ``None`` so tombstones are unaffected, and an explicitly
    quoted or tagged scalar is untouched either way. This changes nothing
    for anything pkgforge itself writes (its own dumps already quote every
    value pkgforge cares about type-fidelity for).
    """
    try:
        import yaml
    except ImportError as exc:
        from ..errors import PkgForgeError

        raise PkgForgeError(
            "the yaml DB backend needs PyYAML, which is not installed; "
            "use --db-format jsonl or sqlite"
        ) from exc
    base_loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    dumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)

    class _StrLoader(base_loader):
        pass

    _StrLoader.yaml_implicit_resolvers = {
        first_char: [
            (tag, regexp) for tag, regexp in resolvers if tag == _YAML_NULL_TAG
        ]
        for first_char, resolvers in base_loader.yaml_implicit_resolvers.items()
    }
    return _StrLoader, dumper


def _is_flow_style_yaml(path: Path) -> bool:
    """True if an existing, non-empty YAML file's first substantive line
    (skipping blank lines, ``#`` comments and a lone ``---`` document-start
    marker) opens a flow-style mapping (``{``).

    ``YamlDb._record`` only ever writes a block-style top-level mapping;
    appending that onto an existing flow-style document (e.g.
    ``{/usr/bin/x: {...}}``, or the empty mapping ``{}``) produces invalid
    YAML. pkgforge itself never writes flow style, so this only matters for
    a hand-written or third-party file.
    """
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped == "---":
                    continue
                return stripped.startswith("{")
    except (OSError, UnicodeDecodeError):
        return False
    return False


class YamlDb(AppendLogDb):
    """Append-only YAML: a single mapping, appended key by key -- the last
    duplicate key wins.

    Kept for compatibility and as an explicitly-selectable backend. Reading
    relies on the YAML loader letting a later duplicate mapping key win -- a
    property of this backend's format, not a general guarantee.
    """

    NAME = "yaml"
    SUFFIXES = (".yaml", ".yml")

    def load(self) -> Db:
        if not self.path.exists():
            return {}
        loader, _ = _yaml_io()
        import yaml

        try:
            with self.path.open(encoding="utf-8") as fh:
                data = yaml.load(fh, Loader=loader)
        except UnicodeDecodeError as exc:
            raise DbError(f"{self.path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise DbError(f"{self.path}: {exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise DbError(
                f"{self.path}: invalid YAML file DB: top-level document is "
                "not a mapping"
            )
        return {
            path: (None if rec is None else _normalize(self.path, path, rec))
            for path, rec in data.items()
        }

    def _record(self, path: str, entry: typing.Optional["FileEntry"]) -> str:
        if self.path.exists() and _is_flow_style_yaml(self.path):
            raise DbError(
                f"{self.path}: a flow-style YAML DB cannot be appended to; "
                "run `pkgforge --db-format yaml compact` first to rewrite "
                "it in block style"
            )
        _, dumper = _yaml_io()
        import yaml

        value = None if entry is None else _fields(entry)
        return yaml.dump({path: value}, Dumper=dumper)

    def _render_all(self, live: typing.Dict[str, "FileEntry"]) -> str:
        if not live:
            return ""
        _, dumper = _yaml_io()
        import yaml

        return yaml.dump({p: _fields(e) for p, e in live.items()}, Dumper=dumper)
