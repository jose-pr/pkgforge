"""Cross-platform, no-rpm-needed tests for ``tests/rpm_probe.py``'s own logic
(escaping, dedup grouping, verdicts, and the ``--analyze`` policy). Loaded via
``importlib`` since ``rpm_probe.py`` is not a ``test_*.py`` file (see its own
module docstring) and ``tests/`` is not an importable package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_rpm_probe():
    path = Path(__file__).with_name("rpm_probe.py")
    spec = importlib.util.spec_from_file_location("rpm_probe", path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses (Case, below) resolves its own module via
    # sys.modules[cls.__module__], so the module must be registered there
    # before exec_module runs its body.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rpm_probe = _load_rpm_probe()


@pytest.mark.parametrize(
    "spelling, arg, expected",
    [
        # G escapes each glob character with a backslash; rpm's own
        # quoted-string globbing already matches such a character literally
        # (see src/pkgforge/dbdump/rpm.py), so this spelling exists only to
        # measure whether escaping it anyway changes rpm's behavior.
        ("quoted_glob", "star*", '"star\\*"'),
        # GS additionally escapes a space; unquoted, so no surrounding
        # quotes are added.
        ("bare_glob", "with space", "with\\ space"),
        # P4 doubles an already-doubled percent.
        ("quoted_p4", "%x", '"%%%%x"'),
        # pkgforge's own escaping (E): backslash, then double-quote.
        ("quoted", 'quo"te', '"quo\\"te"'),
        # bare_p2: GS escapes the backslash (doubling it) first, then P2
        # doubles the percent. The literal input here is back\slash%
        # (one backslash, one percent).
        ("bare_p2", "back\\slash%", "back\\\\slash%%"),
    ],
)
def test_probe_spellings(spelling, arg, expected):
    assert rpm_probe.SPELLINGS[spelling](arg) == expected


@pytest.mark.parametrize(
    "rc, packaged, witness_hit, expected",
    [
        (1, None, True, "expanded"),
        (0, ["/opt/p/plain", "/opt/p/other"], True, "expanded"),
        (1, None, False, "loud"),
        (1, ["/opt/p/plain"], False, "loud"),
        (0, ["/opt/p/plain"], False, "exact"),
        (0, ["/opt/p/plain", "/opt/p/other"], False, "overmatch"),
        (0, ["/opt/p/other"], False, "wrong"),
        (0, [], False, "wrong"),
    ],
)
def test_probe_verdict(rc, packaged, witness_hit, expected):
    assert rpm_probe._verdict("/opt/p/plain", rc, packaged, witness_hit) == expected


def test_probe_dedupes_identical_lines():
    case = next(c for c in rpm_probe.CASES if c.cls == "plain")
    path = f"{rpm_probe.ROOT_PATH}/{case.name}"
    lines = rpm_probe._case_lines(path)
    groups = rpm_probe._group_lines(lines)

    # "plain" has no character any spelling needs to escape, so every
    # unquoted spelling collapses to one line and every quoted spelling
    # (including pkgforge's own, which is unescaped `quoted`) collapses to
    # another -- exactly one build for each of the two groups.
    assert len(groups) == 2
    grouped_spellings = [frozenset(v) for v in groups.values()]
    assert frozenset({"bare", "bare_glob", "bare_p2", "bare_p4"}) in grouped_spellings
    assert (
        frozenset(
            {
                "quoted_raw",
                "quoted",
                "quoted_glob",
                "quoted_p2",
                "quoted_p4",
                "pkgforge",
            }
        )
        in grouped_spellings
    )


def _synthetic_records(overrides):
    """Build one record per (leg, class, spelling), verdict "exact" by
    default, rpm_version fixed per leg (fedora is the NEW leg; el9/el8 are
    the two required OLD legs), overridden per ``(label, cls, spelling)``."""
    legs = {"fedora": (4, 19), "el9": (4, 16), "el8": (4, 14)}
    classes = rpm_probe.NON_PCT + rpm_probe.PCT
    records = []
    for label, version in legs.items():
        for cls in classes:
            for spelling in rpm_probe.SPELLING_ORDER:
                verdict = overrides.get((label, cls, spelling), "exact")
                records.append(
                    {
                        "label": label,
                        "rpm_version": list(version),
                        "class": cls,
                        "name_hex": "",
                        "spelling": spelling,
                        "line_hex": "",
                        "verdict": verdict,
                        "rc": 0,
                        "packaged": None,
                        "stderr_tail": "",
                    }
                )
    return records


def _write_and_analyze(tmp_path, records, capsys):
    (tmp_path / "rpm-probe-synthetic.json").write_text(
        json.dumps(records), encoding="utf-8"
    )
    rc = rpm_probe.run_analyze(tmp_path)
    return rc, capsys.readouterr().out


def test_probe_analyze_synthetic(tmp_path, capsys):
    # A uniformly "exact" grid: nothing is unsafe anywhere, so there is
    # nothing for a pre419 format to add over the base quoted family.
    rc, out = _write_and_analyze(tmp_path, _synthetic_records({}), capsys)
    assert rc == 0
    assert "base: quoted supported=" in out
    assert "old_unsafe=-" in out
    assert "base_switch: none" in out
    assert "pre419: none" in out

    # Make `quoted` unsafe for `star` on both OLD legs (and disqualify
    # `quoted_glob` there and on NEW too), while leaving `bare`/`bare_glob`
    # untouched (still exact everywhere) -- `bare` should both replace the
    # base family and win the pre419 slot, since it recovers `star`.
    overrides = {
        ("el9", "star", "quoted"): "overmatch",
        ("el8", "star", "quoted"): "overmatch",
        ("fedora", "star", "quoted_glob"): "loud",
        ("el9", "star", "quoted_glob"): "wrong",
        ("el8", "star", "quoted_glob"): "wrong",
    }
    rc, out = _write_and_analyze(tmp_path, _synthetic_records(overrides), capsys)
    assert rc == 0
    assert "old_unsafe=star" in out
    assert "base_switch: bare" in out
    assert "pre419: bare/p2" in out
    assert "star" in out.split("pre419: bare/p2", 1)[1].split("\n", 1)[0]


def test_probe_analyze_too_few_legs(tmp_path, capsys):
    records = [r for r in _synthetic_records({}) if r["label"] != "el8"]
    (tmp_path / "rpm-probe-synthetic.json").write_text(
        json.dumps(records), encoding="utf-8"
    )
    rc = rpm_probe.run_analyze(tmp_path)
    assert rc == 1
    assert "too few legs" in capsys.readouterr().err
