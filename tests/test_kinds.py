"""Entry kinds and file capabilities: the helpers, the record path, the file
DBs and both dump formats."""

from __future__ import annotations

import json
import sqlite3
import typing
from pathlib import Path

import pytest

from pkgforge import DbError, DumpError, Project, UsageError, open_db
from pkgforge.dbdump import DebianFiles, RPMSpecFiles, dump
from pkgforge.entry import (
    FileKind,
    normalize_caps,
    normalize_kind,
    _new_entry,
)

KINDS = ("file", "config", "config-noreplace", "doc", "license", "ghost")
OLD_DB = "this DB was written by an older pkgforge: run initdb and record again"


def entry(ftype: str = "file", kind: str = "file", caps: str = "-", **over):
    return {
        "mode": "644",
        "owner": "root",
        "group": "root",
        "type": ftype,
        "kind": kind,
        "caps": caps,
        "package": "-",
        "meta": {},
        **over,
    }


# -- the helpers --------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_normalize_kind_accepts_every_kind(kind):
    assert normalize_kind(kind) == kind
    assert normalize_kind(FileKind(kind)) == kind


@pytest.mark.parametrize("value", ["-", ""])
def test_normalize_kind_unset(value):
    assert normalize_kind(value) == "-"


@pytest.mark.parametrize("value", ["Config", "conf", "--", "state", "file "])
def test_normalize_kind_refuses(value):
    with pytest.raises(UsageError, match="choose from file, config"):
        normalize_kind(value)


@pytest.mark.parametrize(
    "value",
    [
        "cap_net_bind_service=ep",
        "cap_net_raw,cap_net_admin+ep",
        "all=ep",
        "cap_chown-i",
        "cap_net_raw=ep+i",
        "cap_net_raw=",
    ],
)
def test_normalize_caps_accepts(value):
    assert normalize_caps(value) == value


@pytest.mark.parametrize("value", ["-", ""])
def test_normalize_caps_unset(value):
    assert normalize_caps(value) == "-"


@pytest.mark.parametrize(
    "value",
    [
        "CAP_NET_RAW=ep",
        "cap_net_raw = ep",
        "cap_net_raw=ep%",
        "cap_x=ep cap_y=p",
        "cap_net_raw",
        "net_raw=ep",
        "cap_net_raw=ep\n",
    ],
)
def test_normalize_caps_refuses(value):
    with pytest.raises(UsageError, match="invalid caps"):
        normalize_caps(value)


def test_new_entry_carries_kind_and_caps():
    built = _new_entry(
        mode="755",
        owner="-",
        group="-",
        type="file",
        kind="config",
        caps="cap_net_raw=ep",
        meta={},
    )
    assert (built["kind"], built["caps"]) == ("config", "cap_net_raw=ep")
    unset = _new_entry(mode="-", owner="-", group="-", type="file", meta={})
    assert (unset["kind"], unset["caps"]) == ("-", "-")


# -- the record path ----------------------------------------------------------


def run(cli, tmp_path: Path, *args: str, cfg: typing.Optional[Path] = None):
    base = ["--db", str(tmp_path / "f.jsonl"), "--buildroot", str(tmp_path / "root")]
    if cfg is not None:
        base = ["--config", str(cfg), *base]
    return cli(*base, *args)


def records(tmp_path: Path) -> typing.Dict[str, typing.Dict[str, typing.Any]]:
    out = {}
    for line in (tmp_path / "f.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        out[rec.pop("path")] = rec
    return out


def source(tmp_path: Path, name: str = "x") -> Path:
    path = tmp_path / name
    path.write_text("x", encoding="utf-8")
    return path


@pytest.mark.posix
def test_install_records_kind_and_caps(cli, tmp_path):
    result = run(
        cli,
        tmp_path,
        "install",
        "-p",
        "-k",
        "config-noreplace",
        "--caps",
        "cap_net_raw=ep",
        str(source(tmp_path)),
        "/etc/t",
    )
    assert result.rc == 0, result.err
    rec = records(tmp_path)["/etc/t/x"]
    assert (rec["kind"], rec["caps"]) == ("config-noreplace", "cap_net_raw=ep")


@pytest.mark.posix
def test_an_unset_kind_is_file(cli, tmp_path):
    assert run(cli, tmp_path, "install", "-p", str(source(tmp_path)), "/opt").rc == 0
    rec = records(tmp_path)["/opt/x"]
    assert (rec["kind"], rec["caps"]) == ("file", "-")


@pytest.mark.posix
def test_a_kind_that_needs_a_file_is_refused_for_a_directory(cli, tmp_path):
    src = tmp_path / "d"
    src.mkdir()
    result = run(cli, tmp_path, "install", "-p", "-k", "doc", str(src), "/opt")
    assert result.rc == 2
    assert "/opt/d: kind doc needs a regular file" in result.err.decode()
    assert not (tmp_path / "root" / "opt" / "d").exists()
    assert not (tmp_path / "f.jsonl").exists()


@pytest.mark.posix
def test_caps_on_a_ghost_are_refused(cli, tmp_path):
    result = run(
        cli,
        tmp_path,
        "install",
        "-p",
        "-k",
        "ghost",
        "--caps",
        "cap_net_raw=ep",
        str(source(tmp_path)),
        "/opt",
    )
    assert result.rc == 2
    assert "caps need a regular file that is not a ghost" in result.err.decode()


@pytest.mark.posix
def test_a_directory_takes_file_or_ghost(cli, tmp_path):
    result = run(cli, tmp_path, "mkdir", "-p", "-k", "ghost", "/run/t")
    assert result.rc == 0, result.err
    assert records(tmp_path)["/run/t"]["kind"] == "ghost"
    project = Project(tmp_path / "root", tmp_path / "f.jsonl")
    with pytest.raises(UsageError, match="kind config needs a regular file"):
        project.mkdir(["/etc/d"], kind="config", parents=True)


@pytest.mark.posix
def test_a_symlink_takes_file_or_ghost(cli, tmp_path):
    assert run(cli, tmp_path, "symlink", "-p", "-k", "ghost", "t", "/opt/l").rc == 0
    assert records(tmp_path)["/opt/l"]["kind"] == "ghost"
    bad = run(cli, tmp_path, "symlink", "-p", "-k", "doc", "t", "/opt/m")
    assert bad.rc == 2


RULE_CFG = """\
[[rules]]
match = ["(?type:file)/etc/x/**"]
kind = "config-noreplace"
caps = "cap_net_raw=ep"
"""


@pytest.mark.posix
def test_a_rule_sets_kind_and_a_flag_beats_it(cli, tmp_path):
    cfg = tmp_path / "pkgforge.cfg"
    cfg.write_text(RULE_CFG, encoding="utf-8")
    src = source(tmp_path)
    assert run(cli, tmp_path, "install", "-p", str(src), "/etc/x", cfg=cfg).rc == 0
    ruled = records(tmp_path)["/etc/x/x"]
    assert (ruled["kind"], ruled["caps"]) == ("config-noreplace", "cap_net_raw=ep")

    flagged = run(
        cli, tmp_path, "install", "-p", "-k", "file", str(src), "/etc/x/y", cfg=cfg
    )
    assert flagged.rc == 0, flagged.err
    rec = records(tmp_path)["/etc/x/y/x"]
    assert rec["kind"] == "file"
    # Only the flag was given: the rule still supplies caps.
    assert rec["caps"] == "cap_net_raw=ep"


@pytest.mark.posix
def test_a_rule_does_not_reach_a_directory_it_does_not_match(cli, tmp_path):
    cfg = tmp_path / "pkgforge.cfg"
    cfg.write_text(RULE_CFG, encoding="utf-8")
    assert run(cli, tmp_path, "mkdir", "-p", "/etc/x/d", cfg=cfg).rc == 0
    assert records(tmp_path)["/etc/x/d"]["kind"] == "file"


@pytest.mark.posix
def test_record_tree_children_take_a_rules_kind_never_the_installs(cli, tmp_path):
    cfg = tmp_path / "pkgforge.cfg"
    cfg.write_text(
        '[[rules]]\nmatch = ["(?type:file)/opt/**/a.conf"]\nkind = "config"\n',
        encoding="utf-8",
    )
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "a.conf").write_text("x", encoding="utf-8")
    (tree / "b.txt").write_text("x", encoding="utf-8")
    result = run(
        cli,
        tmp_path,
        "install",
        "-p",
        "-k",
        "ghost",
        "--record-tree",
        str(tree),
        "/opt",
        cfg=cfg,
    )
    assert result.rc == 0, result.err
    recs = records(tmp_path)
    assert recs["/opt/t"]["kind"] == "ghost"
    assert recs["/opt/t/a.conf"]["kind"] == "config"
    assert recs["/opt/t/b.txt"]["kind"] == "file"


@pytest.mark.posix
def test_owned_parents_are_files(cli, tmp_path):
    result = run(
        cli, tmp_path, "--own", "/opt/p", "install", "-p", "-k", "doc",
        str(source(tmp_path)), "/opt/p/sub",
    )  # fmt: skip
    assert result.rc == 0, result.err
    recs = records(tmp_path)
    assert recs["/opt/p"]["kind"] == "file"
    assert recs["/opt/p/sub/x"]["kind"] == "doc"


# -- meta.rpmprefix is gone ---------------------------------------------------


@pytest.mark.posix
def test_the_rpmprefix_meta_key_is_refused(cli, tmp_path):
    result = run(
        cli,
        tmp_path,
        "install",
        "-p",
        "-O",
        "rpmprefix=%doc",
        str(source(tmp_path)),
        "/opt",
    )
    assert result.rc == 2
    assert (
        "meta key 'rpmprefix' is no longer read: use kind or caps"
        in result.err.decode()
    )
    assert not (tmp_path / "f.jsonl").exists()


def test_the_rpmprefix_meta_key_is_refused_in_the_cfg(cli, tmp_path):
    cfg = tmp_path / "pkgforge.cfg"
    cfg.write_text(
        '[[stage]]\nsrc = "x"\ndest = "/opt"\nmeta = { rpmprefix = "x" }\n',
        encoding="utf-8",
    )
    source(tmp_path)
    result = cli("--config", str(cfg), "--db", "-", "stage")
    assert result.rc == 2
    assert "rpmprefix" in result.err.decode()


def test_the_rpmprefix_meta_key_is_refused_by_the_library(tmp_path):
    with pytest.raises(UsageError, match="rpmprefix"):
        Project(tmp_path / "root").install(
            [""], "/opt/f", into=False, meta={"rpmprefix": "x"}
        )


# -- the cfg ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[[rules]]\nmatch = ["/a"]\nkind = "state"\n', "kind: invalid kind"),
        ('[[rules]]\nmatch = ["/a"]\nkind = "-"\n', "kind: invalid kind"),
        ('[[rules]]\nmatch = ["/a"]\ncaps = "CAP_X=ep"\n', "caps: invalid caps"),
        ('[[rules]]\nmatch = ["/a"]\ncaps = "-"\n', "caps: invalid caps"),
        (
            '[[stage]]\ndir = "/d"\ncaps = "cap_net_raw=ep"\n',
            "caps: only valid with src or file",
        ),
        (
            '[[stage]]\nsymlink = "/l"\ntarget = "t"\ncaps = "cap_net_raw=ep"\n',
            "caps: only valid with src or file",
        ),
        (
            '[[stage]]\nsymlink = "/l"\ntarget = "t"\nkind = "config"\n',
            "kind: config needs a regular file, not a symlink",
        ),
        (
            '[[stage]]\ndir = "/d"\nkind = "doc"\n',
            "kind: doc needs a regular file, not a directory",
        ),
    ],
)
def test_cfg_refuses(tmp_path, text, message):
    from pkgforge.config import load

    cfg = tmp_path / "pkgforge.cfg"
    cfg.write_text(text, encoding="utf-8")
    with pytest.raises(UsageError, match=message):
        load(cfg)


def test_cfg_accepts_kinds_and_caps(tmp_path):
    from pkgforge.config import load

    cfg = tmp_path / "pkgforge.cfg"
    cfg.write_text(
        '[[stage]]\ndir = "/run/t"\nkind = "ghost"\n\n'
        '[[stage]]\nfile = "/var/log/t.log"\nkind = "ghost"\n\n'
        '[[stage]]\nfile = "/usr/bin/t"\ncaps = "cap_net_raw=ep"\n\n'
        '[[stage]]\nsymlink = "/l"\ntarget = "t"\nkind = "ghost"\n',
        encoding="utf-8",
    )
    steps = load(cfg).stage
    assert [step.kind for step in steps] == ["ghost", "ghost", None, "ghost"]
    assert steps[2].caps == "cap_net_raw=ep"


@pytest.mark.posix
def test_stage_records_kind_and_caps(tmp_path):
    from pkgforge.config import load

    (tmp_path / "t").write_text("x", encoding="utf-8")
    (tmp_path / "pkgforge.cfg").write_text(
        '[[stage]]\nsrc = "t"\ndest = "/usr/bin"\ncaps = "cap_net_raw=ep"\n\n'
        '[[stage]]\nsrc = "t"\ndest = "/etc/t"\nkind = "config"\n',
        encoding="utf-8",
    )
    project = Project.from_config(
        tmp_path / "pkgforge.cfg", buildroot=tmp_path / "root", db=tmp_path / "f.jsonl"
    )
    project.stage()
    live = project.load()
    assert live["/usr/bin/t"]["caps"] == "cap_net_raw=ep"
    assert live["/usr/bin/t"]["kind"] == "file"
    assert live["/etc/t/t"]["kind"] == "config"


# -- the file DBs -------------------------------------------------------------

ROUND_TRIP = {
    "/etc/t/a.conf": entry(kind="config-noreplace", caps="cap_net_raw=ep"),
    "/usr/share/doc/t": entry(kind="doc"),
    "/var/lib/t": entry("directory", kind="ghost"),
    "/usr/bin/l": entry("symlink", meta={"target": "t"}),
}


@pytest.mark.parametrize("name", ["db.jsonl", "db.yaml", "db.sqlite"])
def test_every_backend_round_trips_kind_and_caps(tmp_path, name):
    if name.endswith("yaml"):
        pytest.importorskip("yaml")
    provider = open_db(tmp_path / name)
    provider.init()
    for path, rec in ROUND_TRIP.items():
        provider.add(path, rec)
    assert open_db(tmp_path / name).load() == ROUND_TRIP


@pytest.mark.parametrize("missing", ["kind", "caps", "package"])
def test_a_jsonl_record_without_kind_or_caps_is_refused(tmp_path, missing):
    rec = {"path": "/a", **entry()}
    del rec[missing]
    db = tmp_path / "f.jsonl"
    db.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    with pytest.raises(DbError, match=f"missing field {missing}; {OLD_DB}"):
        open_db(db).load()


def test_a_jsonl_record_with_a_bad_kind_is_refused(tmp_path):
    db = tmp_path / "f.jsonl"
    db.write_text(json.dumps({"path": "/a", **entry(kind="state")}) + "\n")
    with pytest.raises(DbError, match="kind must be one of file, config"):
        open_db(db).load()


def test_a_yaml_record_without_kind_is_refused(tmp_path):
    pytest.importorskip("yaml")
    db = tmp_path / "f.yaml"
    db.write_text(
        "/a: {mode: '644', owner: root, group: root, type: file, meta: {}}\n",
        encoding="utf-8",
    )
    with pytest.raises(DbError, match=OLD_DB):
        open_db(db).load()


def _old_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE entries (path TEXT PRIMARY KEY, mode TEXT, owner TEXT, "
        '"group" TEXT, type TEXT, meta_json TEXT, removed INTEGER DEFAULT 0)'
    )
    conn.execute("INSERT INTO entries VALUES ('/a','644','root','root','file','{}',0)")
    conn.commit()
    conn.close()


def test_an_old_sqlite_table_is_refused_on_load_and_add(tmp_path):
    db = tmp_path / "old.sqlite"
    _old_sqlite(db)
    provider = open_db(db)
    with pytest.raises(DbError, match=OLD_DB):
        provider.load()
    with pytest.raises(DbError, match=OLD_DB):
        provider.add("/b", entry())


@pytest.mark.parametrize("name", ["old.jsonl", "old.sqlite"])
def test_dbdump_of_an_old_db_exits_1(cli, tmp_path, name):
    db = tmp_path / name
    if name.endswith("jsonl"):
        record = {
            k: v for k, v in entry().items() if k not in ("kind", "caps", "package")
        }
        db.write_text(json.dumps({"path": "/a", **record}) + "\n", encoding="utf-8")
    else:
        _old_sqlite(db)
    result = cli("--db", str(db), "dbdump", "-f", "rpm", "-")
    assert result.rc == 1
    err = result.err.decode()
    assert OLD_DB in err
    assert err.count("\n") == 1


# -- rpm ----------------------------------------------------------------------


def test_rpm_lines():
    entries = [
        ("/etc/a", entry(kind="config", mode="-", owner="-", group="-")),
        ("/etc/b", entry(kind="config-noreplace")),
        ("/usr/share/doc/c", entry(kind="doc")),
        ("/usr/share/licenses/d", entry(kind="license")),
        ("/var/log/e", entry(kind="ghost")),
        ("/run/f", entry("directory", kind="ghost")),
        ("/usr/bin/g", entry(caps="cap_net_raw=ep")),
        ("/etc/h", entry(kind="config", caps="cap_net_raw=ep")),
        ("/opt/i", entry("directory")),
    ]
    lines = RPMSpecFiles().render(entries).decode().splitlines()
    assert lines == [
        '%config %attr(-,-,-) "/etc/a"',
        '%config(noreplace) %attr(644,root,root) "/etc/b"',
        '%doc %attr(644,root,root) "/usr/share/doc/c"',
        '%license %attr(644,root,root) "/usr/share/licenses/d"',
        '%ghost %attr(644,root,root) "/var/log/e"',
        '%ghost %dir %attr(644,root,root) "/run/f"',
        '%caps(cap_net_raw=ep) %attr(644,root,root) "/usr/bin/g"',
        '%config %caps(cap_net_raw=ep) %attr(644,root,root) "/etc/h"',
        '%dir %attr(644,root,root) "/opt/i"',
    ]


# -- debian -------------------------------------------------------------------

DEB_ENTRIES = {
    "/etc/t/a.conf": entry(kind="config-noreplace"),
    "/opt/t/b.conf": entry(kind="config"),
    "/usr/bin/t": entry(caps="cap_net_raw,cap_net_admin+ep", mode="755"),
    "/var/log/t.log": entry(kind="ghost"),
    "/var/log/t log*": entry(kind="ghost"),
    "/run/t": entry("directory", kind="ghost", mode="755"),
    "/usr/share/t": entry("directory", mode="755"),
}


def test_debian_artifacts_for_kinds_and_caps():
    arts = dump(DEB_ENTRIES, "debian")
    assert list(arts) == [
        "install",
        "dirs",
        "fixperms",
        "conffiles",
        "not-installed",
        "postinst",
        "postrm",
    ]
    install = arts["install"].decode().splitlines()
    assert "var/log/t.log var/log" not in install
    assert all("/run" not in line for line in install)
    assert arts["dirs"].decode().splitlines() == ["usr/share/t"]
    assert b"var/log" not in arts["fixperms"] and b"run/t" not in arts["fixperms"]
    # /etc is added by debhelper itself.
    assert arts["conffiles"] == b"/opt/t/b.conf\n"
    assert arts["not-installed"].decode().splitlines() == [
        "var/log/t${Space}log\\*",
        "var/log/t.log",
    ]
    assert arts["postinst"] == (
        b"#!/bin/sh\n"
        b"# Generated by pkgforge dbdump -f debian\n"
        b"set -e\n\n"
        b"#DEBHELPER#\n"
        b'if [ "$1" = configure ]; then\n'
        b'  setcap cap_net_raw,cap_net_admin+ep "$DPKG_ROOT"/usr/bin/t\n'
        b"fi\n"
    )


def test_debian_without_caps_has_a_plain_postinst():
    arts = dump({"/usr/bin/t": entry()}, "debian")
    assert arts["postinst"].endswith(b"set -e\n\n#DEBHELPER#\n")
    assert arts["conffiles"] == arts["not-installed"] == b""


def test_a_conffile_with_whitespace_is_refused():
    with pytest.raises(DumpError, match="conffile /opt/a b.conf contains whitespace"):
        dump({"/opt/a b.conf": entry(kind="config")}, "debian")
    # Under /etc debhelper adds it, so nothing is written for it.
    assert dump({"/etc/a b.conf": entry(kind="config")}, "debian")["conffiles"] == b""


def test_a_postinst_that_was_not_generated_is_never_overwritten(cli, tmp_path):
    db = tmp_path / "f.jsonl"
    db.write_text(
        json.dumps({"path": "/usr/bin/t", **entry()}) + "\n", encoding="utf-8"
    )
    out = tmp_path / "debian"
    out.mkdir()
    (out / "postinst").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    result = cli("--db", str(db), "dbdump", "-f", "debian", str(out))
    assert result.rc == 2
    err = result.err.decode()
    assert str(out / "postinst") in err and "merge" in err
    assert (out / "postinst").read_text() == "#!/bin/sh\necho mine\n"
    assert sorted(p.name for p in out.iterdir()) == ["postinst"]


def test_a_generated_postinst_is_replaced(cli, tmp_path):
    db = tmp_path / "f.jsonl"
    db.write_text(
        json.dumps({"path": "/usr/bin/t", **entry(caps="cap_net_raw=ep")}) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "debian"
    for _ in range(2):
        result = cli("--db", str(db), "dbdump", "-f", "debian", str(out))
        assert result.rc == 0, result.err
    assert "setcap cap_net_raw=ep" in (out / "postinst").read_text()
    db.write_text(
        json.dumps({"path": "/usr/bin/t", **entry()}) + "\n", encoding="utf-8"
    )
    assert cli("--db", str(db), "dbdump", "-f", "debian", str(out)).rc == 0
    assert "setcap" not in (out / "postinst").read_text()


def test_stdout_is_not_blocked_by_a_postinst_beside_it(cli, tmp_path, monkeypatch):
    db = tmp_path / "f.jsonl"
    db.write_text(
        json.dumps({"path": "/usr/bin/t", **entry()}) + "\n", encoding="utf-8"
    )
    (tmp_path / "postinst").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = cli("--db", str(db), "dbdump", "-f", "debian", "-")
    assert result.rc == 0, result.err
    assert b"# === postinst ===" in result.out


# -- validation at dump time --------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        entry(kind="state"),
        entry("directory", kind="doc"),
        entry("symlink", kind="license"),
        entry(caps="cap_x=ep cap_y=p"),
        entry("directory", caps="cap_net_raw=ep"),
        entry(kind="ghost", caps="cap_net_raw=ep"),
    ],
)
def test_dump_refuses_a_bad_kind_or_caps(bad):
    from pkgforge import InvalidEntryError

    with pytest.raises(InvalidEntryError):
        dump({"/a": bad}, "rpm")
