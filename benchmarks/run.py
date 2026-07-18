#!/usr/bin/env python3
"""Structured benchmark runner for buildutils.

Produces a comparable JSON result plus a human summary. Save a run to the
history with --save; results land in benchmarks/results/<name>.json where
<name> defaults to buildutils-<version>-py<major><minor>.

    python benchmarks/run.py            # print summary only
    python benchmarks/run.py --save     # also write benchmarks/results/<name>.json
    python benchmarks/run.py --name foo # custom result name

Each metric is sampled `repeat` times (each sample is `inner` iterations) and
reported as min/median/max ms-per-call, so run-to-run timing noise is visible
rather than averaged away. Counts are fixed so numbers stay comparable across
runs and commits. Requires buildutils importable (PYTHONPATH=src, or installed).
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import timeit
from datetime import datetime, timezone
from pathlib import Path

import yaml

import buildutils
from buildutils.dbdump import _debian_artifacts, rpmspecfile

# Per-metric inner iteration counts, sized so each metric runs in ~1s regardless
# of how expensive one call is (YAML load of a 1000-entry DB is ~100x a render).
LOAD_INNER = 10
RENDER_INNER = 500
SCAN_INNER = 20
REPEAT = 5

#: Number of entries in the synthetic in-memory file DB (load/render metrics).
DB_SIZE = 1000
#: Number of files in the on-disk scan tree (filesystem-bound; kept smaller and
#: machine-dependent -- CI, not a laptop, is the source of truth for this one).
SCAN_TREE_SIZE = 250


def _make_db(n: int) -> "dict":
    return {
        f"/usr/share/app/file{i:04d}.dat": {
            "mode": "644",
            "owner": "root",
            "group": "root",
            "type": "file",
            "meta": {},
        }
        for i in range(n)
    }


def _entries_list(db: "dict"):
    return [(p, e) for p, e in db.items() if e is not None]


def sample(fn, inner, repeat=REPEAT):
    """Return ms-per-call as min/median/max over `repeat` samples."""
    fn()  # warmup
    per_call = [timeit.timeit(fn, number=inner) / inner * 1000 for _ in range(repeat)]
    return {
        "median_ms": round(statistics.median(per_call), 4),
        "min_ms": round(min(per_call), 4),
        "max_ms": round(max(per_call), 4),
    }


def measure():
    db = _make_db(DB_SIZE)
    entries = _entries_list(db)

    # The two DB encodings, parsed the way loaddb() parses each.
    jsonl_text = "".join(
        json.dumps({"path": p, **e}, sort_keys=True) + "\n" for p, e in entries
    )
    yaml_text = yaml.safe_dump(db)

    def _load_jsonl():
        out = {}
        for line in jsonl_text.splitlines():
            if line.strip():
                rec = json.loads(line)
                out[rec.pop("path")] = rec
        return out

    def _render_rpm():
        for path, entry in entries:
            rpmspecfile(path, entry)

    def _render_debian():
        _debian_artifacts(entries)

    metrics = {
        # Current format vs the legacy YAML load, for comparison.
        "db.load_jsonl": sample(_load_jsonl, LOAD_INNER),
        "db.load_yaml_legacy": sample(lambda: yaml.safe_load(yaml_text), LOAD_INNER),
        "dump.rpmspecfiles": sample(_render_rpm, RENDER_INNER),
        "dump.debian": sample(_render_debian, RENDER_INNER),
    }

    # scan walk over a real tmp tree (filesystem-bound; fewer iterations).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for i in range(SCAN_TREE_SIZE):
            (root / f"file{i:04d}.dat").write_bytes(b"")
        import os

        def _walk():
            count = 0
            for _top, _dirs, files in os.walk(root):
                count += len(files)
            return count

        metrics["scan.walk"] = sample(_walk, SCAN_INNER)

    return metrics


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run buildutils benchmarks")
    ap.add_argument("--save", action="store_true", help="write result to benchmarks/results/")
    ap.add_argument("--name", default=None, help="result name (default buildutils-<ver>-py<ver>)")
    args = ap.parse_args(argv)

    version = getattr(buildutils, "__version__", "0")
    pyver = f"py{sys.version_info.major}{sys.version_info.minor}"
    name = args.name or f"buildutils-{version}-{pyver}"
    metrics = measure()
    result = {
        "name": name,
        "buildutils_version": version,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db_size": DB_SIZE,
        "scan_tree_size": SCAN_TREE_SIZE,
        "iterations": {
            "load_inner": LOAD_INNER,
            "render_inner": RENDER_INNER,
            "scan_inner": SCAN_INNER,
            "repeat": REPEAT,
        },
        "metrics": metrics,
    }

    print("=== buildutils Benchmark ===")
    print(f"{name}  ({result['python']} on {result['processor']})")
    print(f"{'metric':20s} {'median':>10s} {'min':>10s} {'max':>10s}   (ms/call)")
    for key, m in metrics.items():
        print(f"{key:20s} {m['median_ms']:10.4f} {m['min_ms']:10.4f} {m['max_ms']:10.4f}")

    if args.save:
        dest = Path(__file__).resolve().parent / "results"
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{name}.json"
        out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
