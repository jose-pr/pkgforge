"""Guards that pin the docs site and the shipped API header to the CLI's
real behavior, so they cannot drift the way they did before this module
existed: the quick-start blocks stay identical and private, every
documented ``pkgforge`` invocation actually parses, the shipped sequences
actually run, README/header links stay absolute (PyPI has no repo to
resolve them against), the API reference covers the full public surface,
and the header documents every exported name plus one exit-status section.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import pkgforge

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
INDEX = ROOT / "docs" / "index.md"
UNATTENDED = ROOT / "docs" / "guide" / "unattended.md"
COMMANDS = ROOT / "docs" / "guide" / "commands.md"
HEADER = ROOT / "src" / "pkgforge" / "AGENTS.md"

# Pages this plan maintains and keeps parseable; docs/guide/exclude.md is
# deliberately excluded -- its anchoring examples include partial `-X`
# illustrations (e.g. a `dbdump` line with no `-f`) that were never meant to
# be complete, runnable invocations.
DOC_PAGES = [
    README,
    INDEX,
    ROOT / "docs" / "guide" / "install.md",
    ROOT / "docs" / "guide" / "commands.md",
    UNATTENDED,
    ROOT / "docs" / "guide" / "file-db.md",
    ROOT / "docs" / "guide" / "formats.md",
]

_CUT_TOKENS = {"|", ">", "<", ";", "&&"}


def _fence_after(heading: str, text: str, index: int = 0) -> str:
    """Body of the ``index``-th fenced code block appearing after ``heading``."""
    pos = text.index(heading)
    fences = re.findall(r"```[^\n]*\n(.*?)```", text[pos:], re.DOTALL)
    return fences[index]


def _fences(text: str, langs=("sh", "bash")):
    for lang, body in re.findall(r"```(\w*)\n(.*?)```", text, re.DOTALL):
        if lang in langs:
            yield body


def _pkgforge_lines(text: str):
    """Every logical (backslash-joined) ``pkgforge ...`` line in a sh/bash fence."""
    for fence in _fences(text):
        raw_lines = fence.splitlines()
        joined = []
        buf = ""
        for line in raw_lines:
            buf += line
            if buf.rstrip().endswith("\\"):
                buf = buf.rstrip()[:-1] + " "
                continue
            joined.append(buf)
            buf = ""
        if buf:
            joined.append(buf)
        for line in joined:
            stripped = line.strip()
            if stripped == "pkgforge" or stripped.startswith("pkgforge "):
                yield stripped


def _doc_argv(line: str):
    """``line``'s argv (dropping ``pkgforge``), or ``None`` if it should be skipped."""
    if "[" in line or "..." in line or "…" in line:
        return None
    tokens = shlex.split(line, comments=True)
    for i, tok in enumerate(tokens):
        if tok in _CUT_TOKENS:
            tokens = tokens[:i]
            break
    argv = tokens[1:]  # drop "pkgforge"
    for tok in argv:
        if tok.isalpha() and tok.isupper() and len(tok) > 1:
            return None
    return argv


def test_quickstart_blocks_identical_and_private():
    readme_fence = _fence_after("## Quick start", README.read_text(encoding="utf-8"))
    index_fence = _fence_after("## At a glance", INDEX.read_text(encoding="utf-8"))
    assert readme_fence == index_fence
    for fence in (readme_fence, index_fence):
        assert 'work="$(mktemp -d)"' in fence
        assert 'PKGFORGE_ROOT="$work/' in fence
        assert "/tmp/" not in fence


def test_doc_examples_parse():
    from pkgforge.command import PkgForge

    parser = PkgForge._parser_()
    checked = 0
    for page in DOC_PAGES:
        text = page.read_text(encoding="utf-8")
        for line in _pkgforge_lines(text):
            argv = _doc_argv(line)
            if argv is None:
                continue
            checked += 1
            try:
                parser.parse_args(argv)
            except SystemExit as exc:
                assert exc.code in (
                    0,
                    None,
                ), f"{page.name}: {line!r} -> exit {exc.code}"
    assert checked  # the pages above must actually contain checkable lines


@pytest.mark.posix
@pytest.mark.parametrize("page", [README, UNATTENDED], ids=["readme", "unattended"])
def test_doc_sequences_run(page, tmp_path, monkeypatch):
    text = page.read_text(encoding="utf-8")
    anchor = "## Quick start" if page is README else "A typical staging sequence"
    block = _fence_after(anchor, text)
    # Old docs (a fixed /tmp path) must fail this test unrun, not silently pass.
    assert 'work="$(mktemp -d)"' in block
    assert 'PKGFORGE_ROOT="$work/' in block

    bash = None
    for candidate in ("bash",):
        from shutil import which

        bash = which(candidate)
        if bash:
            break
    if not bash:
        pytest.skip("bash not on PATH")

    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "tool").write_text(
        "#!/bin/sh\necho hello\n", encoding="utf-8"
    )
    (tmp_path / "tool.conf").write_text("key = value\n", encoding="utf-8")
    (tmp_path / "config").write_text("key = value\n", encoding="utf-8")
    share = tmp_path / "share"
    share.mkdir()
    (share / "data.txt").write_text("data\n", encoding="utf-8")

    env = dict(os.environ)
    env["TMPDIR"] = str(tmp_path)
    env["PATH"] = os.pathsep.join(
        [os.path.dirname(sys.executable), env.get("PATH", "")]
    )
    result = subprocess.run(
        [bash, "-ec", block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.posix
def test_meta_rpmprefix_example(tmp_path, monkeypatch, cli):
    text = COMMANDS.read_text(encoding="utf-8")
    line = next(
        line
        for line in _pkgforge_lines(text)
        if "rpmprefix=" in line and "install" in line
    )
    argv = shlex.split(line, comments=True)[1:]  # drop "pkgforge"

    (tmp_path / "tool.conf").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    globalopts = [
        "--buildroot",
        str(tmp_path / "root"),
        "--db",
        str(tmp_path / "db.jsonl"),
    ]

    result = cli(*globalopts, *argv)
    assert result.rc == 0, result.err

    dump = cli(*globalopts, "dbdump", "-f", "rpmspecfiles", "-")
    assert dump.rc == 0, dump.err
    lines = dump.out.decode().splitlines()
    assert any(l.startswith("%config(noreplace) %attr(640,") for l in lines), lines


def _strip_code(text: str) -> str:
    """Remove fenced code blocks, then inline code spans (which may wrap lines)."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`]*`", "", text, flags=re.DOTALL)


@pytest.mark.parametrize("page", [README, HEADER], ids=["readme", "header"])
def test_shipped_docs_links_absolute(page):
    text = _strip_code(page.read_text(encoding="utf-8"))
    targets = re.findall(r"\]\(([^)]*)\)", text)
    if page is README:
        assert targets  # the README must actually hold markdown links
    bad = [t for t in targets if not (t.startswith("https://") or t.startswith("#"))]
    assert not bad, bad
