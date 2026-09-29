#!/usr/bin/env python3
"""Measure how rpm's ``%files -f`` parser handles a quoted/escaped path (not
collected by pytest; only ``test_*.py`` is). Build (``--label L --out-dir D``,
needs rpmbuild/rpm): stage each case, render one line per spelling, build one
RPM per distinct line, record a verdict. Analyze (``--analyze D``,
stdlib-only): read ``rpm-probe-*.json`` files, apply :func:`run_analyze`'s
policy. Exit: 0 ok; 1 self-check/analysis error; 2 rpmbuild/rpm missing."""

from __future__ import annotations

import argparse, dataclasses, errno, json, re, shutil, subprocess, sys, tempfile, textwrap
from pathlib import Path

ROOT_PATH = "/opt/p"  # where every case's file is staged; root of every %files line
SPEC_NAME = "pfprobe"  # fixed spec Name, so %{name} always expands to this string


def _escape_core(s: str, extra: str = "") -> str:
    """Backslash before each of ``\\ * ? [ ] { }`` plus any char in ``extra``."""
    special = "\\*?[]{}" + extra
    return "".join(("\\" + ch if ch in special else ch) for ch in s)


def _E(s: str) -> str:  # pkgforge's own escaping: backslash, then double-quote.
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _G(s: str) -> str:  # backslash before each glob char, then quote.
    return _escape_core(s).replace('"', '\\"')


def _GS(s: str) -> str:  # _G plus a backslash before each space.
    return _escape_core(s, extra=" ").replace('"', '\\"')


def _P2(s: str) -> str:
    return s.replace("%", "%%")


def _P4(s: str) -> str:
    return s.replace("%", "%%%%")


#: The nine spellings computed from a bare path string ("pkgforge" is
#: separate, :func:`_pkgforge_spelling`: it can refuse instead of a string).
SPELLINGS = {
    "bare": lambda s: s,
    "quoted_raw": lambda s: f'"{s}"',
    "quoted": lambda s: f'"{_E(s)}"',
    "quoted_glob": lambda s: f'"{_G(s)}"',
    "bare_glob": _GS,
    "quoted_p2": lambda s: f'"{_P2(_E(s))}"',
    "quoted_p4": lambda s: f'"{_P4(_E(s))}"',
    "bare_p2": lambda s: _P2(_GS(s)),
    "bare_p4": lambda s: _P4(_GS(s)),
}

# fmt: off
#: pkgforge first, then the nine computed spellings, in a fixed order.
SPELLING_ORDER = (
    "pkgforge", "bare", "quoted_raw", "quoted", "quoted_glob",
    "bare_glob", "quoted_p2", "quoted_p4", "bare_p2", "bare_p4",
)
# fmt: on

_ENTRY = {"mode": "644", "owner": "root", "group": "root", "type": "file", "meta": {}}


def _pkgforge_spelling(path: str) -> str | None:
    """pkgforge's real quoting (strips render_entry's line wrapper back off,
    to match a SPELLINGS entry); ``None`` means refused (DumpError)."""
    from pkgforge.dbdump import DumpError
    from pkgforge.dbdump.rpm import RpmSpecFiles

    try:
        line = RpmSpecFiles().render_entry(path, _ENTRY)
    except DumpError:
        return None
    prefix = "%attr(644,root,root) "
    text = line.decode("utf-8", "surrogateescape")
    return text[len(prefix) : -1]


@dataclasses.dataclass(frozen=True)
class Case:
    cls: str
    name: str
    siblings: tuple = ()
    witness: str | None = (
        None  # seen when ROOT_PATH+"/"+this is in stdout/stderr/`rpm -qlp`
    )
    witness_file: str | None = None  # seen when this file exists in the build's cwd


# fmt: off
CASES = (
    Case("plain", "plain"), Case("space", "with space"), Case("utf8", "café"),
    Case("nonutf8", b"caf\xe9".decode("utf-8", "surrogateescape")), Case("dquote", 'quo"te'),
    Case("backslash", "back\\slash"), Case("star", "star*", siblings=("starfish",)),
    Case("qmark", "q?", siblings=("qx",)), Case("bracket", "br[x]", siblings=("brx",)),
    Case("brace", "brace{a,b}", siblings=("bracea", "braceb")),
    Case("pct", "100%done"), Case("pct_end", "end%"), Case("pct_pct", "%%lit"),
    Case("pct_name", "%{name}", witness=SPEC_NAME),
    Case("pct_shell", "%(touch pfmark)", witness_file="pfmark"),
    Case("pct_expr", "x%[6*7]", witness="x42"),
    Case("pct_lua", "lua%{lua:print(6*7)}", witness="lua42"),
)
# fmt: on

#: The acceptance policy treats these differently: only ``%``-classes need a
#: macro-refusal/doubling choice.
NON_PCT = tuple(c.cls for c in CASES if not c.cls.startswith("pct"))
PCT = tuple(c.cls for c in CASES if c.cls.startswith("pct"))


def _spelled_line(spelled: str | None) -> bytes | None:
    return (
        None
        if spelled is None
        else f"%attr(644,root,root) {spelled}\n".encode("utf-8", "surrogateescape")
    )


def _case_lines(path: str) -> dict:
    """Every spelling's full line for ``path``; ``None`` for a refused (pkgforge-only) spelling."""
    return {
        s: _spelled_line(
            _pkgforge_spelling(path) if s == "pkgforge" else SPELLINGS[s](path)
        )
        for s in SPELLING_ORDER
    }


def _group_lines(lines: dict) -> dict:
    """Group spellings that produced byte-identical lines (a refused/``None`` one never builds)."""
    groups: dict = {}
    for spelling in SPELLING_ORDER:
        line = lines.get(spelling)
        if line is not None:
            groups.setdefault(line, []).append(spelling)
    return groups


def _verdict(expected: str, rc: int | None, packaged: list | None, hit: bool) -> str:
    """Verdict precedence: expanded > loud > exact/overmatch/wrong."""
    if hit:
        return "expanded"
    if rc != 0:
        return "loud"
    pset = set(packaged or [])
    if pset == {expected}:
        return "exact"
    return "overmatch" if expected in pset else "wrong"


# fmt: off
def _run(args, **kw):  # capture text output as UTF-8, tolerating a non-UTF-8 name
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", **kw
    )
# fmt: on


def _rpmbuild_version() -> tuple | None:
    try:
        out = _run(["rpmbuild", "--version"], check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _build_one(case: Case, line: bytes, root: Path, case_dir: Path):
    """One rpmbuild for one distinct line. Returns ``(rc, packaged, stderr_tail, witness_hit)``."""
    build_dir = Path(tempfile.mkdtemp(dir=str(case_dir)))
    topdir = build_dir / "top"
    for sub in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS", "BUILDROOT"):
        (topdir / sub).mkdir(parents=True, exist_ok=True)
    manifest = build_dir / "files.txt"
    manifest.write_bytes(line)
    spec = build_dir / f"{SPEC_NAME}.spec"
    spec.write_text(
        textwrap.dedent(f"""\
            Name: {SPEC_NAME}
            Version: 1
            Release: 1
            Summary: pkgforge rpm quoting probe
            License: MIT
            BuildArch: noarch
            AutoReqProv: no
            %description
            pkgforge rpm quoting probe package.
            %install
            rm -rf %{{buildroot}}
            mkdir -p %{{buildroot}}
            cp -a {root}/. %{{buildroot}}/
            %files -f {manifest}
            """),
        encoding="utf-8",
    )
    # fmt: off
    # cwd is a fresh dir per build: pct_shell's "%(touch pfmark)" witness
    # lands here if the shell macro actually runs.
    result = _run(
        ["rpmbuild", "-bb", "--nodeps",
         "--define", f"_topdir {topdir}",
         "--define", "_unpackaged_files_terminate_build 0",
         "--define", "__os_install_post %{nil}",
         str(spec)],
        cwd=str(build_dir),
    )
    # fmt: on
    combined = result.stdout + result.stderr
    packaged = None
    if result.returncode == 0:
        rpms = list((topdir / "RPMS" / "noarch").glob("*.rpm"))
        if len(rpms) == 1:
            qres = _run(["rpm", "-qlp", str(rpms[0])])
            if qres.returncode == 0:
                packaged = qres.stdout.splitlines()
                combined += qres.stdout + qres.stderr
    witness_hit = (
        case.witness is not None and f"{ROOT_PATH}/{case.witness}" in combined
    ) or (case.witness_file is not None and (build_dir / case.witness_file).exists())
    stderr_tail = "\n".join(result.stderr.splitlines()[-5:])
    return result.returncode, packaged, stderr_tail, witness_hit


def _record(label, rpm_version, case: Case, spelling, **fields) -> dict:
    """A JSON record; ``fields`` overrides the "nothing built" defaults."""
    rec = {
        "label": label,
        "rpm_version": list(rpm_version),
        "class": case.cls,
        "name_hex": case.name.encode("utf-8", "surrogateescape").hex(),
        "spelling": spelling,
        "line_hex": None,
        "verdict": None,
        "rc": None,
        "packaged": None,
        "stderr_tail": "",
    }
    rec.update(fields)
    return rec


def _process_case(case: Case, tmp_root: Path, label: str, rpm_version: tuple) -> list:
    case_dir = tmp_root / case.cls
    stage_dir = case_dir / "root" / ROOT_PATH.lstrip("/")
    stage_dir.mkdir(parents=True)

    try:
        (stage_dir / case.name).write_bytes(b"x")
    except OSError as exc:
        if exc.errno != errno.EILSEQ:
            raise
        return [
            _record(label, rpm_version, case, s, verdict="n/a") for s in SPELLING_ORDER
        ]
    for sibling in case.siblings:
        (stage_dir / sibling).write_bytes(b"x")

    path = f"{ROOT_PATH}/{case.name}"
    lines = _case_lines(path)
    groups = _group_lines(lines)
    root = case_dir / "root"
    result_by_line = {line: _build_one(case, line, root, case_dir) for line in groups}

    records = []
    for spelling in SPELLING_ORDER:
        line = lines[spelling]
        if line is None:
            records.append(
                _record(label, rpm_version, case, spelling, verdict="refused")
            )
            continue
        rc, packaged, stderr_tail, hit = result_by_line[line]
        verdict = _verdict(path, rc, packaged, hit)
        records.append(
            _record(
                label,
                rpm_version,
                case,
                spelling,
                line_hex=line.hex(),
                verdict=verdict,
                rc=rc,
                packaged=packaged,
                stderr_tail=stderr_tail,
            )
        )
    return records


def _write_markdown(label: str, rpm_version: tuple | None, records: list) -> str:
    version_text = f"{rpm_version[0]}.{rpm_version[1]}" if rpm_version else "unknown"
    lines = [f"# rpm-probe: {label} (rpm {version_text})", ""]
    lines.append("| class | " + " | ".join(SPELLING_ORDER) + " |")
    lines.append("|---" * (len(SPELLING_ORDER) + 1) + "|")
    by_class: dict = {}
    for r in records:
        by_class.setdefault(r["class"], {})[r["spelling"]] = r["verdict"]
    for case in CASES:
        row = [case.cls] + [by_class[case.cls][s] for s in SPELLING_ORDER]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def _fail(msg: str, code: int = 1) -> int:
    print(msg, file=sys.stderr)
    return code


def run_probe(label: str, out_dir: Path) -> int:
    if shutil.which("rpmbuild") is None or shutil.which("rpm") is None:
        return _fail("rpm-probe: rpmbuild/rpm not found on PATH", 2)
    rpm_version = _rpmbuild_version()
    if rpm_version is None:
        return _fail("rpm-probe: could not parse `rpmbuild --version`", 2)

    out_dir.mkdir(parents=True, exist_ok=True)
    records: list = []
    with tempfile.TemporaryDirectory(prefix="rpmprobe-") as tmp:
        tmp_root = Path(tmp)
        for case in CASES:
            case_records = _process_case(case, tmp_root, label, rpm_version)
            records.extend(case_records)
            if case.cls == "plain" and any(
                r["verdict"] != "exact" for r in case_records
            ):
                return _fail(
                    "rpm-probe: self-check failed: a `plain` line was not `exact`"
                )

    (out_dir / f"rpm-probe-{label}.json").write_text(
        json.dumps(records, indent=2, sort_keys=True), encoding="utf-8"
    )
    markdown = _write_markdown(label, rpm_version, records)
    (out_dir / f"rpm-probe-{label}.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


def run_analyze(directory: Path) -> int:
    """Apply the acceptance policy over every ``rpm-probe-*.json`` under
    ``directory``. Legs: NEW if ``rpm_version >= (4, 19)``, else OLD; error
    (exit 1) unless there is >=1 NEW leg and >=2 OLD legs.

    Base family ``quoted``: a non-% class is *supported* when exact on NEW,
    *unsafe* when not exact on some OLD leg (tolerated, reported).
    %-handling: first of p2/p4 exact on NEW, exact-or-loud on every OLD leg,
    for every %-class, else refuse. Base switch: first of
    quoted_glob/bare/bare_glob exact on every leg for every supported
    class, when unsafe is non-empty, else none. pre419: the family
    (tie order quoted/quoted_glob/bare/bare_glob) covering the most non-%
    classes under the OLD-exact/NEW-exact-or-loud rule, plus its own best
    p2/p4 choice; ``none`` when it adds nothing base refuses/flags unsafe.
    Detail lines use the switch family (or ``quoted``) per non-% class,
    and the chosen %-handling (or literal ``refused``) per %-class.
    """
    pfx = "rpm-probe --analyze: "
    records: list = []
    for path in sorted(directory.rglob("rpm-probe-*.json")):
        try:
            records.extend(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            return _fail(f"{pfx}could not read {path}: {exc}")
    if not records:
        return _fail(f"{pfx}no rpm-probe-*.json under {directory}")

    by_label: dict = {}
    for r in records:
        by_label.setdefault(r["label"], []).append(r)

    leg_version = {}
    for label, recs in by_label.items():
        versions = {tuple(r["rpm_version"]) for r in recs if r["rpm_version"]}
        if len(versions) != 1:
            return _fail(f"{pfx}leg {label!r} has inconsistent rpm_version")
        leg_version[label] = next(iter(versions))

    new_legs = sorted(l for l, v in leg_version.items() if v >= (4, 19))
    old_legs = sorted(l for l, v in leg_version.items() if v < (4, 19))
    if not new_legs or len(old_legs) < 2:
        return _fail(
            f"{pfx}too few legs (need NEW>=1, OLD>=2; got {len(new_legs)} NEW, {len(old_legs)} OLD)"
        )
    new_label = new_legs[0]

    lookup = {(r["label"], r["class"], r["spelling"]): r["verdict"] for r in records}

    def verdict(label, cls, spelling):
        return lookup[(label, cls, spelling)]

    def all_ok(classes, spelling, new_ok, old_ok):
        return all(
            verdict(new_label, c, spelling) in new_ok
            and all(verdict(l, c, spelling) in old_ok for l in old_legs)
            for c in classes
        )

    supported = [c for c in NON_PCT if verdict(new_label, c, "quoted") == "exact"]
    unsafe = [c for c in supported if not all_ok([c], "quoted", {"exact"}, {"exact"})]

    pmode = "refuse"
    for p, spelling in (("p2", "quoted_p2"), ("p4", "quoted_p4")):
        if all_ok(PCT, spelling, {"exact"}, {"exact", "loud"}):
            pmode = p
            break

    switch = "none"
    if unsafe:
        for family in ("quoted_glob", "bare", "bare_glob"):
            if all_ok(supported, family, {"exact"}, {"exact"}):
                switch = family
                break

    families = ("quoted", "quoted_glob", "bare", "bare_glob")

    def pre419_ok(cls, family):
        return verdict(new_label, cls, family) in ("exact", "loud") and all(
            verdict(l, cls, family) == "exact" for l in old_legs
        )

    counts = {f: sum(pre419_ok(c, f) for c in NON_PCT) for f in families}
    best = families[0]
    for f in families[1:]:
        if counts[f] > counts[best]:
            best = f

    p419 = "refuse"
    for p, suffix in (("p2", "_p2"), ("p4", "_p4")):
        if all_ok(PCT, best + suffix, {"exact", "loud"}, {"exact"}):
            p419 = p
            break

    p419_sup = [c for c in NON_PCT if pre419_ok(c, best)]
    p419_ref = [c for c in NON_PCT if c not in p419_sup]
    pctsup, pctref = ([], list(PCT)) if p419 == "refuse" else (list(PCT), [])
    added_value = any(c not in supported or c in unsafe for c in p419_sup)

    out = [
        "base: quoted supported=%s old_unsafe=%s pct=%s"
        % (",".join(supported) or "-", ",".join(unsafe) or "-", pmode),
        f"base_switch: {switch}",
    ]
    if added_value:
        out.append(
            "pre419: %s/%s supported=%s refused=%s"
            % (
                best,
                p419,
                ",".join(p419_sup + pctsup) or "-",
                ",".join(p419_ref + pctref) or "-",
            )
        )
    else:
        out.append("pre419: none")

    effective_non_pct = switch if switch != "none" else "quoted"
    for c in NON_PCT:
        cells = [verdict(new_label, c, effective_non_pct)] + [
            verdict(l, c, effective_non_pct) for l in old_legs
        ]
        out.append(f"{c} " + " ".join(cells))
    for c in PCT:
        if pmode == "refuse":
            cells = ["refused"] * (1 + len(old_legs))
        else:
            spelling = "quoted_" + pmode
            cells = [verdict(new_label, c, spelling)] + [
                verdict(l, c, spelling) for l in old_legs
            ]
        out.append(f"{c} " + " ".join(cells))

    print("\n".join(out))
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="rpm %files quoting probe")
    parser.add_argument("--label", help="this leg's label, e.g. fedora")
    parser.add_argument("--out-dir", help="dir for rpm-probe-LABEL.{json,md}")
    parser.add_argument(
        "--analyze", metavar="DIR", help="analyze rpm-probe-*.json under DIR"
    )
    args = parser.parse_args(argv)

    if args.analyze is not None:
        return run_analyze(Path(args.analyze))
    if args.label and args.out_dir:
        return run_probe(args.label, Path(args.out_dir))
    parser.error(
        "either --analyze DIR, or both --label LABEL and --out-dir DIR, are required"
    )
    return 2  # pragma: no cover - parser.error exits before this


if __name__ == "__main__":
    sys.exit(main())
