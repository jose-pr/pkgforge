"""The shipped contract suite, run over every built-in backend and format."""

from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest

from pkgforge.db import DbProvider
from pkgforge.dbdump import DumpFormat
from pkgforge.testing import (
    DB_PROVIDER_CHECKS,
    DUMP_FORMAT_CHECKS,
    check_db_provider,
    check_dump_format,
)

HAS_YAML = importlib.util.find_spec("yaml") is not None


def _db_cases():
    skip_yaml = pytest.mark.skipif(not HAS_YAML, reason="PyYAML is not installed")
    return [
        pytest.param(
            name,
            check,
            id=f"{name}-{check.__name__}",
            marks=[skip_yaml] if name == "yaml" else [],
        )
        for name in DbProvider.names()
        for check in DB_PROVIDER_CHECKS
    ]


@pytest.mark.parametrize("name,check", _db_cases())
def test_db_provider_contract(name, check, tmp_path):
    check(DbProvider.get(name), tmp_path)


@pytest.mark.parametrize(
    "name,check",
    [
        pytest.param(name, check, id=f"{name}-{check.__name__}")
        for name in DumpFormat.names()
        for check in DUMP_FORMAT_CHECKS
    ],
)
def test_dump_format_contract(name, check, tmp_path):
    check(DumpFormat.get(name), tmp_path)


def test_check_functions_run_every_check(tmp_path):
    for name in DbProvider.names():
        if name != "yaml" or HAS_YAML:
            check_db_provider(DbProvider.get(name), tmp_path / name)
    for name in DumpFormat.names():
        check_dump_format(DumpFormat.get(name), tmp_path / name)


def test_a_failing_backend_is_named(tmp_path):
    from pkgforge.db import JSONLinesDb

    class Forgetful(JSONLinesDb):
        def add(self, path, entry):
            pass

    with pytest.raises(AssertionError, match=r"Forgetful.*_db_roundtrip"):
        check_db_provider(Forgetful, tmp_path)


def test_testing_imports_no_pytest():
    code = "import sys, pkgforge.testing; assert 'pytest' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
