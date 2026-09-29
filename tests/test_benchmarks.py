"""Tests for benchmarks/run.py.

``benchmarks/`` is excluded from the sdist (it is dev tooling, not a shipped
artifact), so this module skips at collection time when ``run.py`` is absent
rather than failing a sdist-only test run. The module is not a package (no
``benchmarks/__init__.py``), so it is loaded by file path rather than
imported normally.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

from pkgforge.db import open_db

_RUN_PY = Path(__file__).resolve().parent.parent / "benchmarks" / "run.py"

if not _RUN_PY.exists():
    pytest.skip(
        "benchmarks/run.py not present (excluded from the sdist)",
        allow_module_level=True,
    )


def _load_run_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_bench_run", _RUN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_EXPECTED_METRICS = {
    "db.load_jsonl",
    "db.load_yaml",
    "db.load_sqlite",
    "dump.rpmspecfiles",
    "dump.debian",
    "fs.walk_baseline",
    "scan.cmd_jsonl",
    "scan.cmd_yaml",
    "scan.cmd_sqlite",
    "scan.cmd_auto_owner",
    "install.tree_copy",
    "install.tree_link",
    "install.tree_move",
}

_EXPECTED_ITERATIONS = {
    "load_inner",
    "render_inner",
    "scan_inner",
    "scan_cmd_inner",
    "repeat",
}

_EXPECTED_RESULT_KEYS = {
    "name",
    "pkgforge_version",
    "python",
    "platform",
    "processor",
    "timestamp",
    "db_size",
    "scan_tree_size",
    "iterations",
    "metrics",
}


@pytest.fixture
def run_module(monkeypatch: pytest.MonkeyPatch):
    """A fresh, shrunk load of run.py: real work, small counts, one repeat."""
    module = _load_run_module()
    monkeypatch.setattr(module, "DB_SIZE", 3)
    monkeypatch.setattr(module, "SCAN_TREE_SIZE", 3)
    monkeypatch.setattr(module, "LOAD_INNER", 1)
    monkeypatch.setattr(module, "RENDER_INNER", 1)
    monkeypatch.setattr(module, "SCAN_INNER", 1)
    monkeypatch.setattr(module, "SCAN_CMD_INNER", 1, raising=False)
    monkeypatch.setattr(module, "INSTALL_TREE_SIZE", 3, raising=False)
    monkeypatch.setattr(module, "INSTALL_TREE_INNER", 1, raising=False)
    monkeypatch.setattr(module, "REPEAT", 1)
    return module


def test_measure_reports_documented_metrics(run_module):
    """Every documented metric id is reported, each as a min/median/max triple."""
    metrics = run_module.measure()
    assert set(metrics) == _EXPECTED_METRICS
    for metric_id, sampled in metrics.items():
        assert set(sampled) == {"median_ms", "min_ms", "max_ms"}, metric_id
        for value in sampled.values():
            assert isinstance(value, float), metric_id


def test_build_result_schema(run_module):
    """build_result() is pure and produces the documented top-level schema."""
    result = run_module.build_result("t", {})
    assert set(result) == _EXPECTED_RESULT_KEYS
    assert set(result["iterations"]) == _EXPECTED_ITERATIONS
    assert result["name"] == "t"
    assert result["metrics"] == {}


def test_scan_guard_rejects_short_db(run_module, tmp_path):
    """_assert_scanned raises when a scan.cmd_* metric wrote too few entries."""
    db_path = tmp_path / "e.jsonl"
    open_db(db_path, "jsonl").init()
    with pytest.raises(RuntimeError):
        run_module._assert_scanned(db_path, "jsonl", 1)
