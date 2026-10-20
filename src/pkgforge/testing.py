"""The contract suite every DB backend and dump format must pass.

A third-party package runs the same checks the built-in classes do, from any
test runner::

    import pytest
    from pkgforge.testing import DB_PROVIDER_CHECKS, check_db_provider

    def test_contract(tmp_path):
        check_db_provider(MyDb, tmp_path)

    @pytest.mark.parametrize("check", DB_PROVIDER_CHECKS)
    def test_one(check, tmp_path):
        check(MyDb, tmp_path)

A failed check raises :class:`AssertionError` naming the class and the check.
Nothing here imports pytest.
"""

from __future__ import annotations

import functools
import os
import typing
from pathlib import Path

from .db import DbProvider
from .dbdump import DumpFormat, MultiArtifactFormat, PerEntryFormat
from .entry import FileEntry, FileType, StrPath
from .exceptions import UnsupportedOutputError

__all__ = [
    "DB_PROVIDER_CHECKS",
    "DUMP_FORMAT_CHECKS",
    "check_db_provider",
    "check_dump_format",
]

_C = typing.TypeVar("_C")
_Check = typing.Callable[[_C, Path], None]


def _require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _check(fn: typing.Callable[[_C, Path], None]) -> _Check:
    """Run ``fn`` in a subdirectory named after it; a failure names the class
    and the check."""

    @functools.wraps(fn)
    def run(cls: _C, directory: StrPath) -> None:
        work = Path(os.fspath(directory)) / fn.__name__
        work.mkdir(parents=True, exist_ok=True)
        try:
            fn(cls, work)
        except AssertionError as error:
            raise AssertionError(
                f"{getattr(cls, '__qualname__', cls)}: {fn.__name__}: {error}"
            ) from error

    return run


# --------------------------------------------------------------------------
# DB backends
# --------------------------------------------------------------------------


def _entry(kind: FileType, **meta: str) -> FileEntry:
    return {
        "mode": "755" if kind is FileType.Directory else "644",
        "owner": "root",
        "group": "root",
        "type": kind.value,
        "meta": dict(meta),
    }


#: One entry per file type, each with ``meta``; the first path is not ASCII.
_DB_ENTRIES: typing.Dict[str, FileEntry] = {
    "/usr/share/café/file": _entry(FileType.File, note="café"),
    "/usr/share/café": _entry(FileType.Directory, note="dir"),
    "/usr/bin/link": _entry(FileType.Symlink, target="../share/café/file"),
}


def _db_file(cls: typing.Type[DbProvider], work: Path) -> Path:
    suffixes = cls.__dict__.get("SUFFIXES") or (".db",)
    return work / f"contract{suffixes[0]}"


@_check
def _db_init(cls: typing.Type[DbProvider], work: Path) -> None:
    provider = cls(_db_file(cls, work))
    provider.init()
    _require(provider.load() == {}, "load() after init() is not empty")


@_check
def _db_roundtrip(cls: typing.Type[DbProvider], work: Path) -> None:
    path = _db_file(cls, work)
    provider = cls(path)
    provider.init()
    for key, entry in _DB_ENTRIES.items():
        provider.add(key, entry)
    loaded = cls(path).load()
    _require(
        loaded == _DB_ENTRIES,
        f"a fresh instance loads {loaded!r}, expected {_DB_ENTRIES!r}",
    )


@_check
def _db_last_add_wins(cls: typing.Type[DbProvider], work: Path) -> None:
    provider = cls(_db_file(cls, work))
    provider.init()
    first, second = _entry(FileType.File), _entry(FileType.File)
    second["mode"] = "600"
    provider.add("/a", first)
    provider.add("/a", second)
    _require(provider.load() == {"/a": second}, "a second add() did not win")


@_check
def _db_remove(cls: typing.Type[DbProvider], work: Path) -> None:
    provider = cls(_db_file(cls, work))
    provider.init()
    provider.add("/a", _entry(FileType.File))
    provider.add("/b", _entry(FileType.File))
    provider.remove("/a")
    loaded = provider.load()
    _require("/a" in loaded and loaded["/a"] is None, "a removed path is not None")
    _require(loaded.get("/b") == _entry(FileType.File), "remove() changed another path")


@_check
def _db_compact(cls: typing.Type[DbProvider], work: Path) -> None:
    provider = cls(_db_file(cls, work))
    provider.init()
    provider.add("/a", _entry(FileType.File))
    provider.add("/b", _entry(FileType.File))
    provider.remove("/b")
    provider.add("/c", _entry(FileType.Directory))
    provider.compact()
    expected = {"/a": _entry(FileType.File), "/c": _entry(FileType.Directory)}
    loaded = cls(provider.path).load()
    _require(loaded == expected, f"compact() left {loaded!r}, expected {expected!r}")


@_check
def _db_batch(cls: typing.Type[DbProvider], work: Path) -> None:
    path = _db_file(cls, work)
    provider = cls(path)
    provider.init()
    expected = {f"/f{number}": _entry(FileType.File) for number in range(5)}
    with provider.batch() as batched:
        for key, entry in expected.items():
            batched.add(key, entry)
    loaded = cls(path).load()
    _require(
        loaded == expected,
        f"a fresh instance after batch() loads {loaded!r}, expected {expected!r}",
    )


DB_PROVIDER_CHECKS: typing.Tuple[_Check, ...] = (
    _db_init,
    _db_roundtrip,
    _db_last_add_wins,
    _db_remove,
    _db_compact,
    _db_batch,
)


def check_db_provider(cls: typing.Type[DbProvider], directory: StrPath) -> None:
    """Run every :data:`DB_PROVIDER_CHECKS` check against ``cls`` in
    ``directory``.

    Each check works in its own subdirectory of ``directory``, in a file named
    ``contract`` plus the class's first ``SUFFIXES`` entry (else ``.db``).
    Raises :class:`AssertionError` naming ``cls`` and the failed check.
    """
    for check in DB_PROVIDER_CHECKS:
        check(cls, directory)


# --------------------------------------------------------------------------
# Dump formats
# --------------------------------------------------------------------------

_FORMAT_ENTRIES: typing.List[typing.Tuple[str, FileEntry]] = [
    ("/usr/bin/tool", _entry(FileType.File)),
    ("/usr/lib/tool", _entry(FileType.Directory)),
    ("/usr/lib/tool/link", _entry(FileType.Symlink, target="../tool")),
]


@_check
def _format_render_type(cls: typing.Type[DumpFormat], work: Path) -> None:
    rendered = cls().render(list(_FORMAT_ENTRIES))
    if issubclass(cls, PerEntryFormat):
        _require(isinstance(rendered, bytes), "render() does not return bytes")
    elif issubclass(cls, MultiArtifactFormat):
        _require(
            isinstance(rendered, dict)
            and rendered
            and all(
                isinstance(name, str) and name and isinstance(data, bytes) and data
                for name, data in rendered.items()
            ),
            "render() does not return a dict of non-empty str to non-empty bytes",
        )
    else:
        raise AssertionError("not a PerEntryFormat or MultiArtifactFormat subclass")


@_check
def _format_deterministic(cls: typing.Type[DumpFormat], work: Path) -> None:
    fmt = cls()
    _require(
        fmt.render(list(_FORMAT_ENTRIES)) == fmt.render(list(_FORMAT_ENTRIES)),
        "the same entries render differently twice",
    )


@_check
def _format_empty(cls: typing.Type[DumpFormat], work: Path) -> None:
    populated = cls().render(list(_FORMAT_ENTRIES))
    empty = cls().render([])
    _require(
        type(empty) is type(populated),
        f"render([]) returns {type(empty).__name__}, not {type(populated).__name__}",
    )


@_check
def _format_check_output(cls: typing.Type[DumpFormat], work: Path) -> None:
    fmt = cls()
    a_file = work / "a_file"
    a_file.write_bytes(b"")
    a_directory = work / "a_directory"
    a_directory.mkdir()
    absent = work / "absent"
    if issubclass(cls, PerEntryFormat):
        wrong, right = a_directory, (a_file, absent)
    else:
        wrong, right = a_file, (a_directory, absent)
    try:
        fmt.check_output(wrong)
    except UnsupportedOutputError:
        pass
    else:
        raise AssertionError(f"check_output({wrong.name}) does not refuse the shape")
    for path in right:
        fmt.check_output(path)


@_check
def _format_write(cls: typing.Type[DumpFormat], work: Path) -> None:
    fmt = cls()
    rendered = fmt.render(list(_FORMAT_ENTRIES))
    output = work / "out"
    written = fmt.write(rendered, output)
    if isinstance(rendered, bytes):
        _require(written == [output], f"write() returned {written!r}")
        _require(output.read_bytes() == rendered, "the file differs from render()")
    else:
        _require(
            sorted(written) == sorted(output / name for name in rendered),
            f"write() returned {written!r}",
        )
        for name, data in rendered.items():
            _require((output / name).read_bytes() == data, f"{name} differs")


DUMP_FORMAT_CHECKS: typing.Tuple[_Check, ...] = (
    _format_render_type,
    _format_deterministic,
    _format_empty,
    _format_check_output,
    _format_write,
)


def check_dump_format(cls: typing.Type[DumpFormat], directory: StrPath) -> None:
    """Run every :data:`DUMP_FORMAT_CHECKS` check against ``cls`` in
    ``directory``.

    The sample is three entries (a file, a directory and a symlink, owned by
    ``root``); each check works in its own subdirectory of ``directory``.
    Raises :class:`AssertionError` naming ``cls`` and the failed check.
    """
    for check in DUMP_FORMAT_CHECKS:
        check(cls, directory)
