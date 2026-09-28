#!/usr/bin/env python3
"""Smoke-test an INSTALLED pkgforge distribution.

Run this with the interpreter of a venv that has ``pkgforge`` installed
NON-editable (e.g. from a built wheel) -- it is never collected by pytest:

    python tests/smoke_installed.py

Prints ``ok <check>`` per passing check. Exits 2 immediately if pkgforge
resolves into this checkout (an editable install) instead of an installed
distribution, since every other check would then be testing the source tree,
not the thing being smoke-tested. Otherwise, the first failing check is
printed as ``FAIL <check>: <reason>`` and the script exits 1.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from shutil import which

CHECKOUT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (CHECKOUT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    if not match:
        raise AssertionError('could not find a version = "..." line in pyproject.toml')
    return match.group(1)


def _console_script() -> Path:
    scripts = Path(sysconfig.get_path("scripts"))
    name = "pkgforge.exe" if os.name == "nt" else "pkgforge"
    return scripts / name


def check_not_editable() -> None:
    import pkgforge

    installed = Path(pkgforge.__file__).resolve()
    if (CHECKOUT / "src") in installed.parents:
        print(f"FAIL not_editable: pkgforge resolves into the checkout at {installed}")
        sys.exit(2)
    print("ok not_editable")


def check_version(cwd: str) -> None:
    version = _pyproject_version()
    script = _console_script()
    result = subprocess.run(
        [str(script), "--version"], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0 or version not in result.stdout:
        raise AssertionError(
            f"{script} --version (rc={result.returncode}) did not contain "
            f"{version!r}: {result.stdout!r} {result.stderr!r}"
        )
    print("ok version")


def check_help(cwd: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pkgforge", "--help"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or "usage:" not in result.stdout:
        raise AssertionError(f"--help failed: rc={result.returncode} {result.stdout!r}")
    print("ok help")


def check_completion(cwd: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pkgforge", "--print-completion", "bash"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or "complete -F" not in result.stdout:
        raise AssertionError(f"completion failed: rc={result.returncode}")
    print("ok completion")


def check_shipped_files(cwd: str) -> None:
    import importlib.resources

    files = importlib.resources.files("pkgforge")
    for name in ("AGENTS.md", "README.md", "py.typed"):
        if not (files / name).is_file():
            raise AssertionError(
                f"shipped file missing from the installed package: {name}"
            )
    print("ok shipped_files")


def check_example(cwd: str) -> None:
    if os.name != "posix" or which("bash") is None:
        print("ok example (skipped: requires POSIX + bash)")
        return
    env = dict(os.environ)
    env["PATH"] = str(_console_script().parent) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        ["bash", str(CHECKOUT / "examples" / "stage_and_package.sh")],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or "/usr/bin/tool" not in result.stdout:
        raise AssertionError(
            f"example failed: rc={result.returncode}\n{result.stdout}\n{result.stderr}"
        )
    print("ok example")


def main() -> int:
    check_not_editable()  # exits 2 itself on failure

    with tempfile.TemporaryDirectory() as tmp:
        for check in (
            check_version,
            check_help,
            check_completion,
            check_shipped_files,
            check_example,
        ):
            try:
                check(tmp)
            except AssertionError as exc:
                print(f"FAIL {check.__name__.removeprefix('check_')}: {exc}")
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
