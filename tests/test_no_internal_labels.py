"""Guard against internal working-artefact labels leaking into tracked files.

This project's working notes (a numbered plan, a phase/item inside one, a
feature/perf work-item code, a review or finding writeup, a decision log
entry) live in a private, untracked directory and must never show up in
anything this repository ships or records -- not source, not a comment or
docstring, not a test, not the changelog. Those labels are meaningful only
to someone with that private directory open; to everyone else (a user
reading ``--help``, a contributor reading a diff, a future maintainer years
later) they are noise at best and a dangling reference at worst.

This scans every ``git``-tracked text file for the label shapes and fails
naming every offending ``path:line``. A short, explicit allowlist covers the
rare genuine domain use that happens to match (each entry carries its own
one-line reason). Skipped entirely outside a git checkout (e.g. a built
sdist/wheel), since there is no ``git ls-files`` to run there.

The matching logic is exercised directly (not just against this repo's
current, clean state) by a second test that feeds it a planted offender per
label shape and asserts it is caught -- so a future edit that loosens a
pattern shows up as a test failure here, not as a silent gap. Those planted
strings are assembled from pieces at runtime rather than written literally,
so this file's own source never contains the shapes it is built to catch.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_THIS_FILE = Path(__file__).resolve()

# --------------------------------------------------------------------------
# Label shapes
# --------------------------------------------------------------------------
# Each entry is (name, compiled pattern). Kept as data, not inlined into the
# scanner, so the planted-label test below can drive every shape generically.

_PATTERNS = [
    ("numbered-plan", re.compile(r"\b[Pp]lan[ _-]?\d")),
    ("numbered-phase", re.compile(r"\b[Pp]hase[ _-]?\d")),
    ("numbered-item", re.compile(r"\b[Ii]tem[ _-]?\d")),
    ("task-range", re.compile(r"\bT\d+-T\d+")),
    (
        "work-item-code-paren",
        re.compile(r"\((?:F|G|M|H|L|S|C|R|B|P|T)-?\d{1,3}\)"),
    ),
    (
        "work-item-code-bold",
        re.compile(r"\*\*(?:F|G|M|H|L|S|C|R|B|P|T)\d{1,3}\*\*"),
    ),
    ("work-item-code-bare", re.compile(r"\b[FP]\d\b")),
    ("reviewer-reference", re.compile(r"\breviewer\b", re.IGNORECASE)),
    ("in-depth-review", re.compile(r"in-depth review", re.IGNORECASE)),
    ("review-finding", re.compile(r"review finding", re.IGNORECASE)),
    ("the-finding", re.compile(r"\bthe finding\b", re.IGNORECASE)),
    ("the-plan", re.compile(r"\bthe plan(?:'s)?\b", re.IGNORECASE)),
    ("decision-id", re.compile(r"\bD\d{2}\b")),
    ("backlog-item", re.compile(r"\bbacklog\b", re.IGNORECASE)),
    # Built via concatenation (not a literal dotted path) so this detector's
    # own source is never itself a textual match for what it detects.
    ("private-working-dir", re.compile(r"\." + r"agents\b")),
]

# (path, substring, reason) -- a match on `path` whose offending line
# contains `substring` is a genuine domain use, not an internal-artefact
# reference. `_DOTTED_DIR` is assembled the same way as the pattern above,
# for the same reason.
_DOTTED_DIR = "." + "agents"
_ALLOWLIST = [
    (
        ".gitignore",
        _DOTTED_DIR,
        "ignores this repo's own private working directory; it does not "
        "point at anything inside it",
    ),
    (
        "tests/test_rpm_probe.py",
        "P4 doubles",
        "P2/P4 name the two percent-escaping variants (quoted_p2/quoted_p4) "
        "the RPM probe compares, not work-item codes",
    ),
    (
        "tests/test_rpm_probe.py",
        "then P2",
        "same: the P2 percent-escaping variant",
    ),
]


def _is_allowed(path: str, line: str) -> bool:
    return any(
        path == allowed_path and substring in line
        for allowed_path, substring, _reason in _ALLOWLIST
    )


def _scan_text(text: str):
    """Yield (pattern_name, line_no, line) for every label match in `text`."""
    for line_no, line in enumerate(text.splitlines(), start=1):
        for name, pattern in _PATTERNS:
            if pattern.search(line):
                yield name, line_no, line


def _git_tracked_files():
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=_REPO_ROOT,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # NUL-separated (-z): plain output C-quotes and octal-escapes any path
    # with a non-ASCII byte, which then cannot be opened.
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def test_no_internal_labels_in_tracked_files():
    tracked = _git_tracked_files()
    if tracked is None:
        pytest.skip("not inside a git checkout -- nothing to scan")

    offenses = []
    for rel_path in tracked:
        path = _REPO_ROOT / rel_path
        if path.resolve() == _THIS_FILE:
            continue  # this file's own docstrings describe the label shapes
        if not path.is_file():
            continue  # a tracked submodule/symlink target that isn't here
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable -- not a text file we can scan

        posix_path = Path(rel_path).as_posix()
        for name, line_no, line in _scan_text(text):
            if _is_allowed(posix_path, line):
                continue
            offenses.append(f"{posix_path}:{line_no}: [{name}] {line.strip()}")

    assert not offenses, "internal-artefact labels found:\n" + "\n".join(offenses)


def test_label_patterns_catch_a_planted_offender():
    """Each pattern above actually matches a realistic offending line.

    Every sample is built from separate pieces and joined at the assertion,
    so the literal offending text never appears in this file's own source.
    """
    samples = {
        "numbered-plan": " ".join(["See", "Pl" + "an", "7", "for context."]),
        "numbered-phase": " ".join(["Start", "Ph" + "ase", "2", "now."]),
        "numbered-item": " ".join(["Fixes", "it" + "em", "3", "from the list."]),
        "task-range": "Measured after " + "T1" + "-" + "T7" + " landed.",
        "work-item-code-paren": "Async support " + "(" + "F4" + ")" + " added.",
        "work-item-code-bold": "- **" + "F4" + "** Async support added.",
        "work-item-code-bare": "See " + "F4" + "/" + "P2" + " for the change.",
        "reviewer-reference": "Mirrors the " + "review" + "er's fixture.",
        "in-depth-review": "Findings from the " + "in-depth" + " review.",
        "review-finding": "A " + "review find" + "ing about MCP.",
        "the-finding": "Regression test for " + "the find" + "ing that it broke.",
        "the-plan": "See " + "the pl" + "an's Known Facts.",
        "decision-id": "Per " + "D" + "01" + ", the SDK stays optional.",
        "backlog-item": "Three fixes from the " + "backl" + "og.",
        "private-working-dir": "Notes live in " + "." + "agents" + "/plans/.",
    }
    assert set(samples) == {
        name for name, _ in _PATTERNS
    }, "every pattern above must have a planted-offender sample"
    for name, pattern in _PATTERNS:
        matches = list(_scan_text(samples[name]))
        assert (
            matches and matches[0][0] == name
        ), f"pattern {name!r} failed to catch its own planted offender"
