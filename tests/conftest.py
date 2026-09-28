"""Shared pytest fixtures and markers for the pkgforge test suite.

Hermeticity: ``pkgforge.common`` binds ``PKGFORGE_ROOT``/``PKGFORGE_DB``/
``PKGFORGE_DB_FORMAT`` into class defaults AT IMPORT TIME, so a developer
shell that exports the README's env vars -- or a stray ``PKGFORGE_DB_FORMAT``
-- would otherwise change what the suite does, and duho's MCP/agent-help
triggers would make ``main()`` serve MCP or print agent help instead of running
a command. This module pops all of them BEFORE the first ``import pkgforge``
anywhere in the process: pytest always imports a directory's ``conftest.py``
before collecting its test modules, so this runs first. Keep the pop here, at MODULE level, not
inside a fixture -- a fixture runs per-test, long after the class defaults are
already bound.
"""

from __future__ import annotations

import os

#: Env vars that change what an in-process ``main()`` does: pkgforge's own
#: configuration, plus duho's triggers that would serve MCP or print agent help
#: instead of running the command.
SCRUBBED_ENV = (
    "PKGFORGE_ROOT",
    "PKGFORGE_DB",
    "PKGFORGE_DB_FORMAT",
    "PKGFORGE_MCP",
    "PKG_FORGE_MCP",
    "AGENT_HELP",
    "AGENTS_HELP",
)

for _name in SCRUBBED_ENV:
    os.environ.pop(_name, None)

import typing
from pathlib import Path

import pytest

import pkgforge
from pkgforge.common import FileEntry, PkgForgeCmd


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "posix: requires POSIX facilities (skipped off POSIX)"
    )


def pytest_collection_modifyitems(config: pytest.Config, items) -> None:
    if os.name == "posix":
        return
    skip_posix = pytest.mark.skip(reason="requires POSIX facilities")
    for item in items:
        if "posix" in item.keywords:
            item.add_marker(skip_posix)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch):
    """Scrub the PKGFORGE_* env vars around every test.

    The module-level pop above only guards the one-time import-time binding
    (F51/F22); this additionally protects a test that reads the environment
    directly (e.g. a subprocess test that forgets to override one of the
    three), and stays correct once env resolution moves to parse time.
    """
    for name in SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def make_entry():
    """Factory for a :class:`~pkgforge.common.FileEntry` dict with sane defaults.

    Replaces the two diverging ``_entry`` copies (F103).
    """

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
def cmd(tmp_path: Path):
    """Factory for a :class:`~pkgforge.common.PkgForgeCmd` bound to ``tmp_path``.

    Replaces the old ``PkgForge.__new__(PkgForge)`` + setattr seeding (F103):
    the constructor accepts ``db``/``db_format``/``buildroot`` directly
    (measured against duho 0.5.0 and 0.5.4), so no bypass is needed.
    """

    def _make(name: str = "files.yaml", **over) -> PkgForgeCmd:
        kwargs = {"db": tmp_path / name, "db_format": None, "buildroot": tmp_path}
        kwargs.update(over)
        return PkgForgeCmd(**kwargs)

    return _make


class CliResult(typing.NamedTuple):
    rc: int
    out: bytes
    err: bytes


@pytest.fixture
def cli(capfdbinary):
    """Factory driving ``pkgforge.main(argv)`` like a real invocation (F52).

    ``rc``: ``None`` -> 0, an int stays, any other ``SystemExit`` code -> 1
    with ``str(code)`` appended to ``err`` (as CPython's own top-level
    ``SystemExit`` handling does for a non-int code).
    """

    def _run(*argv: str) -> CliResult:
        capfdbinary.readouterr()  # drain any earlier capture
        code = None
        try:
            rc = pkgforge.main(list(argv))
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
