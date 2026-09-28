#!/usr/bin/env python3
"""Structured benchmark runner for pkgforge.

Produces a comparable JSON result plus a human summary. Save a run to the
history with --save; results land in benchmarks/results/<name>.json where
<name> defaults to pkgforge-<version>-py<major><minor>.

    python benchmarks/run.py            # print summary only
    python benchmarks/run.py --save     # also write benchmarks/results/<name>.json
    python benchmarks/run.py --name foo # custom result name

Each metric is sampled `repeat` times and reported as min/median/max
ms-per-call, so run-to-run timing noise is visible rather than averaged away.
Counts are fixed so numbers stay comparable across runs and commits. Requires
pkgforge importable (PYTHONPATH=src, or installed). See benchmarks/README.md
for the metric list, the JSON schema and how committed baselines are named.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import timeit
from datetime import datetime, timezone
from pathlib import Path

import pkgforge
from pkgforge.db import open_db
from pkgforge.dbdump import MULTI_ARTIFACT_FORMATS, rpmspecfile
from pkgforge.scan import ScanCmd

# Per-metric inner iteration counts, sized so each metric runs in ~1s regardless
# of how expensive one call is (YAML load of a 1000-entry DB is ~100x a render).
LOAD_INNER = 10
RENDER_INNER = 500
SCAN_INNER = 20
#: A scan.cmd_* call walks a real directory tree and commits to a real DB file
#: on every call (the sqlite backend fsyncs per row) -- seconds, not
#: milliseconds, on a real disk -- so each sample times a single call.
SCAN_CMD_INNER = 1
REPEAT = 5

#: Number of entries in the synthetic in-memory file DB (load/render metrics).
DB_SIZE = 1000
#: Number of files in the on-disk scan tree (filesystem-bound; kept smaller and
#: machine-dependent -- CI, not a laptop, is the source of truth for this one).
SCAN_TREE_SIZE = 250


def _make_db(n: int) -> dict:
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


def sample(fn, inner, repeat=None, setup=None):
    """Return ms-per-call as min/median/max over `repeat` samples.

    Without `setup`, `fn` is called with no arguments and a whole burst of
    `inner` calls is timed as one unit (via timeit) -- the shape the
    in-process metrics (DB load, dump rendering, the walk baseline) need.

    With `setup`, each of the `inner` calls in a sample gets its own untimed
    `arg = setup()` (a fresh DB init plus a fresh command instance, for the
    scan metrics) and only `fn(arg)` is timed, so setup cost never pollutes
    the measured time.
    """
    if repeat is None:
        repeat = REPEAT

    if setup is None:
        fn()  # warmup
        per_call = [
            timeit.timeit(fn, number=inner) / inner * 1000 for _ in range(repeat)
        ]
    else:

        def _burst():
            total = 0.0
            for _ in range(inner):
                arg = setup()
                start = time.perf_counter()
                fn(arg)
                total += time.perf_counter() - start
            return total

        _burst()  # warmup (setup + fn), not timed
        per_call = [_burst() / inner * 1000 for _ in range(repeat)]

    return {
        "median_ms": round(statistics.median(per_call), 4),
        "min_ms": round(min(per_call), 4),
        "max_ms": round(max(per_call), 4),
    }


def _assert_scanned(db: Path, fmt: str, expected: int) -> None:
    """Raise unless `db` holds at least `expected` entries.

    Guards every scan.cmd_* metric against silently timing a no-op: without
    this, a broken ScanCmd construction would still report a (meaningless)
    fast time instead of failing loudly.
    """
    count = len(open_db(db, fmt, for_read=True).load())
    if count < expected:
        raise RuntimeError(
            f"scan metric wrote only {count} entries to {db}, expected at least {expected}"
        )


def measure():
    db = _make_db(DB_SIZE)
    entries = list(db.items())

    def _render_rpm():
        for path, entry in entries:
            rpmspecfile(path, entry)

    def _render_debian():
        MULTI_ARTIFACT_FORMATS["debian"](entries)

    metrics = {}

    # Load time per storage backend, through the real provider load() path.
    with tempfile.TemporaryDirectory() as td:
        for fmt, ext in (("jsonl", "jsonl"), ("yaml", "yaml"), ("sqlite", "db")):
            provider = open_db(Path(td) / f"bench.{ext}", fmt)
            provider.init()
            for path, entry in entries:
                provider.add(path, entry)
            reader = open_db(Path(td) / f"bench.{ext}", fmt)
            metrics[f"db.load_{fmt}"] = sample(reader.load, LOAD_INNER)

    metrics["dump.rpmspecfiles"] = sample(_render_rpm, RENDER_INNER)
    metrics["dump.debian"] = sample(_render_debian, RENDER_INNER)

    # A real build-root tree, shared by the filesystem baseline and every
    # scan.cmd_* metric below.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "root"
        tree = root / "usr" / "share" / "app"
        tree.mkdir(parents=True)
        for i in range(SCAN_TREE_SIZE):
            (tree / f"file{i:04d}.dat").write_bytes(b"")

        def _walk():
            count = 0
            for _top, _dirs, files in os.walk(root):
                count += len(files)
            return count

        metrics["fs.walk_baseline"] = sample(_walk, SCAN_INNER)

        def _run_scan(cmd: ScanCmd) -> None:
            cmd()

        def _scan_setup(fmt: str, db_path: Path, **overrides):
            def _setup() -> ScanCmd:
                open_db(db_path, fmt).init()
                kwargs = {
                    "db": db_path,
                    "db_format": fmt,
                    "buildroot": root,
                    "path": "/",
                }
                kwargs.update(overrides)
                return ScanCmd(**kwargs)

            return _setup

        for fmt, ext in (("jsonl", "jsonl"), ("yaml", "yaml"), ("sqlite", "db")):
            db_path = Path(td) / f"scan.{ext}"
            metrics[f"scan.cmd_{fmt}"] = sample(
                _run_scan, SCAN_CMD_INNER, setup=_scan_setup(fmt, db_path)
            )
            _assert_scanned(db_path, fmt, SCAN_TREE_SIZE)

        # The documented AUTO sentinel (--) makes owner/group resolve from
        # disk (pwd/grp lookups) on top of the mode/type resolution a plain
        # scan already does -- this is the cost path C107 changes.
        auto_owner_db = Path(td) / "scan.auto_owner.jsonl"
        metrics["scan.cmd_auto_owner"] = sample(
            _run_scan,
            SCAN_CMD_INNER,
            setup=_scan_setup("jsonl", auto_owner_db, owner="--", group="--"),
        )
        _assert_scanned(auto_owner_db, "jsonl", SCAN_TREE_SIZE)

    return metrics


def _platform_id() -> str:
    """OS, architecture and libc only; the kernel release identifies the host."""
    libc, ver = platform.libc_ver()
    parts = [platform.system(), platform.machine()]
    return "-".join(parts + ([f"{libc}{ver}"] if libc else []))


def build_result(name: str, metrics: dict) -> dict:
    """The comparable JSON result for one run. Pure: no I/O, no measuring."""
    return {
        "name": name,
        "pkgforge_version": pkgforge.__version__,
        "python": platform.python_version(),
        "platform": _platform_id(),
        "processor": platform.processor() or platform.machine(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db_size": DB_SIZE,
        "scan_tree_size": SCAN_TREE_SIZE,
        "iterations": {
            "load_inner": LOAD_INNER,
            "render_inner": RENDER_INNER,
            "scan_inner": SCAN_INNER,
            "scan_cmd_inner": SCAN_CMD_INNER,
            "repeat": REPEAT,
        },
        "metrics": metrics,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run pkgforge benchmarks")
    ap.add_argument(
        "--save", action="store_true", help="write result to benchmarks/results/"
    )
    ap.add_argument(
        "--name", default=None, help="result name (default pkgforge-<ver>-py<ver>)"
    )
    args = ap.parse_args(argv)

    pyver = f"py{sys.version_info.major}{sys.version_info.minor}"
    name = args.name or f"pkgforge-{pkgforge.__version__}-{pyver}"
    metrics = measure()
    result = build_result(name, metrics)

    print("=== pkgforge Benchmark ===")
    print(f"{name}  ({result['python']} on {result['processor']})")
    print(f"{'metric':24s} {'median':>10s} {'min':>10s} {'max':>10s}   (ms/call)")
    for key, m in metrics.items():
        print(
            f"{key:24s} {m['median_ms']:10.4f} {m['min_ms']:10.4f} {m['max_ms']:10.4f}"
        )

    if args.save:
        dest = Path(__file__).resolve().parent / "results"
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{name}.json"
        out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
