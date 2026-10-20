"""Shared pytest fixtures and markers for the pkgforge test suite.

Hermeticity: a developer shell that exports the README's env vars would change
what the suite does, and duho's MCP/agent-help triggers would make ``main()``
serve MCP or print agent help instead of running a command. The autouse
``_isolated_env`` fixture removes them around every test.
"""

from __future__ import annotations

import errno
import functools
import importlib
import logging
import os
import shutil
import stat
import tempfile
import typing
from pathlib import Path

import pytest

from pkgforge.cli import main
from pkgforge.entry import FileEntry
from pkgforge import Project

#: Env vars that change what an in-process ``main()`` does: pkgforge's own
#: configuration, plus duho's triggers that would serve MCP or print agent help
#: instead of running the command.
SCRUBBED_ENV = (
    "PKGFORGE_ROOT",
    "PKGFORGE_DB",
    "PKGFORGE_DB_FORMAT",
    "PKGFORGE_INSTALL_METHOD",
    "PKGFORGE_INSTALL_RECORD_TREE",
    "PKGFORGE_MCP",
    "PKG_FORGE_MCP",
    "PKGFORGE_TRACEBACK",
    "AGENT_HELP",
    "AGENTS_HELP",
)


def skip_if_fs_rejects_non_utf8(dirpath: Path) -> None:
    """Skip the current test if ``dirpath``'s filesystem can't hold a
    non-UTF-8 name (e.g. macOS/APFS raises ``OSError(EILSEQ)`` on the raw
    byte sequence). Shared by any test that stages a non-UTF-8 name for a
    real consumer tool to read."""
    probe = dirpath / os.fsdecode(b"probe-\xe9")
    try:
        probe.write_bytes(b"x")
    except OSError as exc:
        if exc.errno == errno.EILSEQ:
            pytest.skip("filesystem rejects non-UTF-8 names")
        raise
    else:
        probe.unlink()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "posix: needs pwd/grp, fcntl, chmod bits and symlinks; skipped when a "
        "probe finds one missing",
    )


@functools.lru_cache(maxsize=None)
def _missing_posix_capability() -> typing.Optional[str]:
    """The first POSIX facility the ``posix`` tests need that this platform
    lacks, or ``None`` when all are present."""
    for module in ("pwd", "grp", "fcntl"):
        try:
            importlib.import_module(module)
        except ImportError:
            return module
    scratch = tempfile.mkdtemp()
    try:
        probe = os.path.join(scratch, "probe")
        with open(probe, "wb"):
            pass
        os.chmod(probe, 0o750)
        if stat.S_IMODE(os.stat(probe).st_mode) != 0o750:
            return "chmod"
        try:
            os.symlink(probe, os.path.join(scratch, "link"))
        except (OSError, NotImplementedError, AttributeError):
            return "symlink"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return None


def pytest_collection_modifyitems(config: pytest.Config, items) -> None:
    missing = _missing_posix_capability()
    if missing is None:
        return
    skip_posix = pytest.mark.skip(reason=f"requires POSIX facilities: {missing}")
    for item in items:
        if "posix" in item.keywords:
            item.add_marker(skip_posix)


@pytest.fixture
def yaml_module():
    """PyYAML, or a skip: it is the optional ``yaml`` extra."""
    return pytest.importorskip("yaml")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch):
    """Scrub ``SCRUBBED_ENV`` around every test."""
    for name in SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)
    # An empty value names no configuration file, so a pkgforge.cfg in the
    # developer's working directory never reaches a test.
    monkeypatch.setenv("PKGFORGE_CONFIG", "")
    yield


@pytest.fixture(autouse=True)
def _clear_name_caches():
    """Clear the per-process uid/gid -> name memoization around every test.

    ``pkgforge.entry._user_name``/``_group_name`` cache real lookups for the
    whole process; without this, a test that fakes ``pkgforge.entry.pwd``/
    ``grp`` after an earlier test already cached the real name for the same
    uid/gid would see the stale cached value instead of its fake.
    """
    from pkgforge.entry import _group_name, _user_name

    _user_name.cache_clear()
    _group_name.cache_clear()
    yield
    _user_name.cache_clear()
    _group_name.cache_clear()


@pytest.fixture(autouse=True)
def restore_pkgforge_log_levels():
    """Snapshot/restore every ``pkgforge``/``pkgforge.*`` logger's level.

    ``pkgforge.cli.main()`` sets the dispatched command's own logger level (e.g.
    from ``-v``/``--loglevel``), and that level persists in-process on the
    module-level ``logging.Logger`` object -- there is no per-``main()``
    reset. Without this, one test's ``-v``/``--loglevel`` leaks into the next
    test that reads the same logger's effective level (e.g. a plain ``main()``
    scan after a ``--loglevel pkgforge.scan:DEBUG`` test would otherwise still
    see DEBUG). Restores each pre-existing logger's level and sets any
    ``pkgforge.*`` logger created during the test to NOTSET.
    """

    def _pkgforge_loggers() -> typing.Dict[str, logging.Logger]:
        loggers = {"pkgforge": logging.getLogger("pkgforge")}
        for name, obj in logging.Logger.manager.loggerDict.items():
            if isinstance(obj, logging.PlaceHolder):
                continue
            if name == "pkgforge" or name.startswith("pkgforge."):
                loggers[name] = obj
        return loggers

    before = {name: logger.level for name, logger in _pkgforge_loggers().items()}
    yield
    for name, logger in _pkgforge_loggers().items():
        logger.setLevel(before.get(name, logging.NOTSET))


@pytest.fixture
def make_entry():
    """Factory for a :class:`~pkgforge.entry.FileEntry` dict with sane defaults."""

    def _make(
        mode: str = "644",
        type: str = "file",
        owner: str = "root",
        group: str = "root",
        meta: dict | None = None,
        **over,
    ) -> FileEntry:
        entry = {
            "mode": mode,
            "owner": owner,
            "group": group,
            "type": type,
            "meta": meta or {},
        }
        entry.update(over)
        return entry

    return _make


@pytest.fixture
def project(tmp_path: Path):
    """Factory for a :class:`~pkgforge.project.Project` bound to ``tmp_path``."""

    def _make(name: str = "files.yaml", **over) -> Project:
        kwargs = {"buildroot": tmp_path, "db": tmp_path / name, "db_format": None}
        kwargs.update(over)
        return Project(kwargs.pop("buildroot"), kwargs.pop("db"), **kwargs)

    return _make


@pytest.fixture
def restore_registries():
    """Snapshot/restore the pluggable-class registries around a test that
    registers a throwaway :class:`~pkgforge.db.DbProvider` or
    :class:`~pkgforge.dbdump.DumpFormat` subclass.

    Both registries are plain ``{name-or-alias: class}`` dicts populated by
    :class:`pkgforge._registry.Registered.__init_subclass__`; a test-only
    subclass registered without this fixture would leak into every test that
    runs afterward in the same process.
    """
    from pkgforge.db import DbProvider
    from pkgforge.dbdump import DumpFormat

    db_before = dict(DbProvider._registry)
    dump_before = dict(DumpFormat._registry)
    try:
        yield
    finally:
        DbProvider._registry.clear()
        DbProvider._registry.update(db_before)
        DumpFormat._registry.clear()
        DumpFormat._registry.update(dump_before)
        assert "tsv" not in DbProvider._registry
        assert "custom" not in DumpFormat._registry


class CliResult(typing.NamedTuple):
    rc: int
    out: bytes
    err: bytes


@pytest.fixture
def cli(capfdbinary):
    """Factory driving ``pkgforge.cli.main(argv)`` like a real invocation.

    ``rc``: ``None`` -> 0, an int stays, any other ``SystemExit`` code -> 1
    with ``str(code)`` appended to ``err`` (as CPython's own top-level
    ``SystemExit`` handling does for a non-int code).
    """

    def _run(*argv: str) -> CliResult:
        capfdbinary.readouterr()  # drain any earlier capture
        code = None
        try:
            rc = main(list(argv))
        except SystemExit as exc:
            code = exc.code
            if code is None:
                rc = 0
            elif isinstance(code, int):
                rc = code
            else:
                rc = 1
        captured = capfdbinary.readouterr()
        out, err = captured.out, captured.err
        if code is not None and not isinstance(code, int):
            err += (str(code) + "\n").encode()
        return CliResult(rc, out, err)

    return _run
