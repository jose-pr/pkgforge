"""Pins README.md's samples to the CLI's real output, so they cannot drift.

Not collected on non-POSIX: the Quick start stages real files (chmod) and
records literal owner/group text, which only lines up with the real dump
formats guide on a POSIX host (see .agents/AGENTS.md "Dev env").
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import pytest

import pkgforge

README = Path(__file__).resolve().parents[1] / "README.md"


def _fence_after(heading: str, text: str, index: int = 0) -> str:
    """Body of the ``index``-th fenced code block appearing after ``heading``."""
    pos = text.index(heading)
    fences = re.findall(r"```[^\n]*\n(.*?)```", text[pos:], re.DOTALL)
    return fences[index]


@pytest.mark.posix
def test_quickstart_output_matches_readme(tmp_path, monkeypatch, capfdbinary):
    text = README.read_text(encoding="utf-8")
    quickstart = _fence_after("## Quick start", text)
    # Every `pkgforge ...` line of the Quick start fence except its two
    # dbdump-to-a-file/directory lines (checked separately, against stdout).
    lines = [
        line.strip()
        for line in quickstart.splitlines()
        if line.strip().startswith("pkgforge ") and "dbdump" not in line
    ]
    assert lines  # the fence must actually have been found and parsed

    # Every relative source the Quick start fence names, with real modes so
    # umask never reaches the recorded/staged output.
    (tmp_path / "build").mkdir()
    tool = tmp_path / "build" / "tool"
    tool.write_text("#!/bin/sh\necho hello\n", encoding="utf-8")
    os.chmod(tool, 0o755)
    conf = tmp_path / "tool.conf"
    conf.write_text("key = value\n", encoding="utf-8")
    os.chmod(conf, 0o644)
    share = tmp_path / "share"
    share.mkdir()
    os.chmod(share, 0o755)
    data = share / "data.txt"
    data.write_text("data\n", encoding="utf-8")
    os.chmod(data, 0o644)

    db = tmp_path / "files.jsonl"
    root = tmp_path / "stage"
    globalopts = ["--buildroot", str(root), "--db", str(db)]

    monkeypatch.chdir(tmp_path)
    for line in lines:
        argv = shlex.split(line)[1:]  # drop the leading "pkgforge"
        rc = pkgforge.main([*globalopts, *argv])
        assert rc in (0, None), line

    def _dump(fmt: str) -> bytes:
        capfdbinary.readouterr()
        rc = pkgforge.main([*globalopts, "dbdump", "-f", fmt, "-"])
        assert rc in (0, None)
        return capfdbinary.readouterr().out

    rpm_fence = _fence_after("## Dump formats", text, index=0)
    debian_fence = _fence_after("## Dump formats", text, index=1)
    # Each fence's first line is a "$ pkgforge dbdump ..." prompt, not output.
    rpm_expected = "\n".join(rpm_fence.splitlines()[1:]) + "\n"
    debian_expected = "\n".join(debian_fence.splitlines()[1:]) + "\n"

    assert _dump("rpmspecfiles").decode() == rpm_expected
    assert _dump("debian").decode() == debian_expected
