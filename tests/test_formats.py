"""Tests for archive extraction (tarfile) and the dbdump packaging formats."""

from __future__ import annotations

import json
import tarfile

import pytest
import yaml

from pkgforge.dbdump import (
    MULTI_ARTIFACT_FORMATS,
    PER_ENTRY_FORMATS,
    DbDump,
    dump_formats,
    rpmspecfile,
)
from pkgforge.install import _extract_tar, _is_tar_source

#: The tarfile route needs PEP 706's extraction filter (3.9.17+, 3.10.12+,
#: 3.11.4+, 3.12+); without it pkgforge routes to bsdtar or refuses, so tests of
#: the tarfile route itself cannot run there (e.g. the 3.9.13 binary builds).
requires_tar_filter = pytest.mark.skipif(
    not hasattr(tarfile, "data_filter"),
    reason="this Python's tarfile has no extraction filter (PEP 706)",
)

# --------------------------------------------------------------------------
# tar detection + extraction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("x.tar", True),
        ("x.tar.gz", True),
        ("x.tgz", True),
        ("x.tar.bz2", True),
        ("x.tar.xz", True),
        ("x.txz", True),
        ("x.iso", False),
        ("x.zip", False),
        ("plain", False),
    ],
)
def test_is_tar_source(name, expected):
    assert _is_tar_source(name) is expected


@requires_tar_filter
def test_extract_tar_gz_roundtrip(tmp_path):
    # Build a .tar.gz, extract via stdlib tarfile (no bsdtar involved).
    srcdir = tmp_path / "content"
    (srcdir / "sub").mkdir(parents=True)
    (srcdir / "sub" / "a.txt").write_text("hello")
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(srcdir / "sub" / "a.txt", arcname="sub/a.txt")

    dst = tmp_path / "out"
    dst.mkdir()
    _extract_tar(archive, dst)
    assert (dst / "sub" / "a.txt").read_text() == "hello"


# --------------------------------------------------------------------------
# format registry
# --------------------------------------------------------------------------


def test_dump_formats_lists_rpm_and_debian():
    fmts = dump_formats()
    assert "rpmspecfiles" in fmts
    assert "debian" in fmts
    assert "rpmspecfiles" in PER_ENTRY_FORMATS
    assert "debian" in MULTI_ARTIFACT_FORMATS


# --------------------------------------------------------------------------
# debian artifacts
# --------------------------------------------------------------------------


def _entries():
    return [
        (
            "/usr/bin/tool",
            {
                "mode": "755",
                "owner": "root",
                "group": "root",
                "type": "file",
                "meta": {},
            },
        ),
        (
            "/etc/tool",
            {"mode": "-", "owner": "-", "group": "-", "type": "directory", "meta": {}},
        ),
        (
            "/etc/tool/conf",
            {
                "mode": "640",
                "owner": "root",
                "group": "adm",
                "type": "file",
                "meta": {},
            },
        ),
    ]


def test_debian_install_artifact():
    arts = MULTI_ARTIFACT_FORMATS["debian"](_entries())
    install = arts["install"].decode()
    # Non-directory entries -> "<rel-src> <dest-dir>"
    assert "usr/bin/tool usr/bin" in install
    assert "etc/tool/conf etc/tool" in install
    # Directories are not install targets.
    assert "etc/tool " not in install.replace("etc/tool/conf", "")


def _one_entry(path, mode="644", owner="root", group="root"):
    return [
        (
            path,
            {"mode": mode, "owner": owner, "group": group, "type": "file", "meta": {}},
        )
    ]


@pytest.mark.parametrize(
    "path,expected",
    [
        (
            "/usr/share/t/with space",
            "usr/share/t/with${Space}space usr/share/t",
        ),
        ("/usr/share/t/star*", "usr/share/t/star\\* usr/share/t"),
        ("/usr/share/t/q?", "usr/share/t/q\\? usr/share/t"),
        ("/usr/share/t/br[x]", "usr/share/t/br\\[x\\] usr/share/t"),
        (
            "/usr/share/t/brace{a,b}",
            "usr/share/t/brace\\{a,b\\} usr/share/t",
        ),
        (
            "/usr/share/t/back\\slash",
            "usr/share/t/back\\\\slash usr/share/t",
        ),
        (
            "/usr/share/t/dollar${x}",
            "usr/share/t/dollar$\\{x\\} usr/share/t",
        ),
        ("/#top/f", "\\#top/f #top"),
        (
            "/usr/share/sp dir/f",
            "usr/share/sp${Space}dir/f usr/share/sp${Space}dir",
        ),
        (
            "/usr/share/e[1]/f",
            "usr/share/e\\[1\\]/f usr/share/e[1]",
        ),
        (
            "/usr/share/dol${y}dir/f",
            "usr/share/dol$\\{y\\}dir/f usr/share/dol${Dollar}{y}dir",
        ),
        ("/d$x", "d$x"),  # guard: a bare $ is left alone
        ("/usr/bin/tool", "usr/bin/tool usr/bin"),  # guard: plain path unchanged
    ],
    ids=[
        "space",
        "star",
        "question",
        "bracket",
        "brace",
        "backslash",
        "dollar_brace",
        "leading_hash",
        "space_parent",
        "bracket_parent_unescaped",
        "dollar_dest",
        "bare_dollar_guard",
        "plain_guard",
    ],
)
def test_debian_install_escapes(path, expected):
    arts = MULTI_ARTIFACT_FORMATS["debian"](_one_entry(path))
    install = arts["install"].decode().strip()
    assert install == expected


def test_debian_rejects_control_char():
    from pkgforge.dbdump import DumpError

    with pytest.raises(DumpError):
        MULTI_ARTIFACT_FORMATS["debian"](_one_entry("/usr/share/x\ny"))


def test_debian_rejects_space_in_owner():
    from pkgforge.dbdump import DumpError

    entries = [
        (
            "/a",
            {
                "mode": "644",
                "owner": "ro ot",
                "group": "root",
                "type": "file",
                "meta": {},
            },
        )
    ]
    with pytest.raises(DumpError):
        MULTI_ARTIFACT_FORMATS["debian"](entries)


def test_debian_permissions_rsplit_keeps_path():
    # Guard: the permissions format is unescaped, so a path containing
    # spaces must still be parseable right-to-left.
    entries = _one_entry(
        "/usr/share/t/with space", mode="640", owner="root", group="adm"
    )
    arts = MULTI_ARTIFACT_FORMATS["debian"](entries)
    line = arts["permissions"].decode().strip()
    path, mode, owner, group = line.rsplit(" ", 3)
    assert path == "/usr/share/t/with space"
    assert (mode, owner, group) == ("640", "root", "adm")


def test_debian_non_utf8_keeps_bytes():
    entries = [
        (
            "/opt/app/caf\udce9",
            {"mode": "644", "owner": "-", "group": "-", "type": "file", "meta": {}},
        )
    ]
    arts = MULTI_ARTIFACT_FORMATS["debian"](entries)
    assert b"opt/app/caf\xe9 opt/app\n" in arts["install"]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/opt/café", b'"/opt/caf\xc3\xa9"'),
        ("/usr/bin/x", b'"/usr/bin/x"'),  # plain, guard: unchanged from json.dumps
        ("/with space", b'"/with space"'),  # space, guard
        ("/a*?[x]", b'"/a*?[x]"'),  # glob, guard: never escaped
        ('/quo"te', b'"/quo\\"te"'),  # quote
        ("/back\\slash", b'"/back\\\\slash"'),  # backslash
    ],
    ids=["utf8", "plain", "space", "glob", "quote", "backslash"],
)
def test_rpmspecfile_quotes(path, expected):
    from pkgforge.dbdump import _rpm_quote

    assert _rpm_quote(path).encode("utf-8", "surrogateescape") == expected


@pytest.mark.parametrize(
    "path",
    ["/a\nb", "/a\tb", "/a\x7fb", "/100%done", "/%{name}", "/%(id)"],
    ids=["nl", "tab", "del", "pct", "pct_brace", "pct_paren"],
)
def test_rpmspecfile_rejects(path):
    from pkgforge.dbdump import DumpError, _rpm_quote

    with pytest.raises(DumpError):
        _rpm_quote(path)


def test_rpmspecfile_non_utf8_keeps_bytes():
    entry = {"mode": "-", "owner": "-", "group": "-", "type": "file", "meta": {}}
    line = rpmspecfile("/opt/caf\udce9", entry)
    assert line == b'%attr(-,-,-) "/opt/caf\xe9"\n'


def test_rpmspecfile_empty_fields_render_default():
    # An empty mode/owner/group renders as "-" (DEFAULT), not verbatim
    # (rpmbuild rejects "%attr(,-,-)" with "Bad syntax").
    entry = {"mode": "", "owner": "", "group": "", "type": "file", "meta": {}}
    assert rpmspecfile("/x", entry) == b'%attr(-,-,-) "/x"\n'


def test_debian_permissions_skip_empty_fields():
    entries = [
        (
            "/etc/tool",
            {"mode": "", "owner": "", "group": "", "type": "file", "meta": {}},
        ),
    ]
    arts = MULTI_ARTIFACT_FORMATS["debian"](entries)
    assert arts["permissions"] == b""


def test_debian_permissions_artifact():
    arts = MULTI_ARTIFACT_FORMATS["debian"](_entries())
    perms = arts["permissions"].decode()
    assert "/usr/bin/tool 755 root root" in perms
    assert "/etc/tool/conf 640 root adm" in perms
    # The all-default directory entry contributes no permission override.
    assert "/etc/tool -" not in perms


def test_debian_permissions_partial_pin_placeholder():
    # Guard: '-' means "unpinned", not a dpkg-statoverride value (that tool
    # rejects '-' outright); a real consumer parses this right-to-left, via
    # override_dh_fixperms, and must skip a '-' field.
    entries = [_dir_entry("/usr/share/tool/share", mode="755")]
    arts = MULTI_ARTIFACT_FORMATS["debian"](entries)
    assert arts["permissions"].decode().strip() == "/usr/share/tool/share 755 - -"


def _dir_entry(path, mode="-", owner="-", group="-"):
    return (
        path,
        {"mode": mode, "owner": owner, "group": group, "type": "directory", "meta": {}},
    )


def test_debian_dirs_artifact():
    entries = [
        _dir_entry("/var/lib/tool"),
        _dir_entry("/var/lib/sp ace"),
        _dir_entry("/#state"),
    ]
    arts = MULTI_ARTIFACT_FORMATS["debian"](entries)
    dirs = arts["dirs"].decode().splitlines()
    assert dirs == ["var/lib/tool", "var/lib/sp${Space}ace", "./#state"]
    # A directory entry is never an install target.
    assert "var/lib/tool" not in arts["install"].decode()


def _jsonl_row(path: str) -> str:
    return json.dumps(
        {
            "path": path,
            "mode": "644",
            "owner": "-",
            "group": "-",
            "type": "file",
            "meta": {},
        }
    )


@pytest.mark.parametrize("fmt", ["rpmspecfiles", "debian"])
def test_dbdump_sorts_by_path(tmp_path, fmt):
    # Insertion order (/z, /a, /m) must never reach the manifest: rendering
    # sorts by DB path so a staged tree gives byte-identical manifests
    # regardless of backend or filesystem readdir order.
    db = tmp_path / "files.jsonl"
    db.write_text(
        "\n".join(_jsonl_row(p) for p in ("/z", "/a", "/m")) + "\n",
        encoding="utf-8",
    )
    parser = DbDump._parser_()
    if fmt == "rpmspecfiles":
        out = tmp_path / "out.txt"
        parser.parse_args(["--db", str(db), "-f", fmt, str(out)])()
        order = [line.split('"')[1] for line in out.read_text().splitlines()]
    else:
        outdir = tmp_path / "debian"
        parser.parse_args(["--db", str(db), "-f", fmt, str(outdir)])()
        lines = [line for line in (outdir / "install").read_text().splitlines() if line]
        order = ["/" + line.split()[0] for line in lines]
    assert order == ["/a", "/m", "/z"]


def test_dbdump_debian_writes_directory(tmp_path):
    db = tmp_path / "files.yaml"
    db.write_text(
        yaml.safe_dump(
            {
                "/usr/bin/tool": {
                    "mode": "755",
                    "owner": "root",
                    "group": "root",
                    "type": "file",
                    "meta": {},
                },
                "/removed": None,
            }
        )
    )
    outdir = tmp_path / "debian"
    parser = DbDump._parser_()
    cmd = parser.parse_args(
        ["--db", str(db), "--buildroot", str(tmp_path), "-f", "debian", str(outdir)]
    )
    cmd()
    assert (outdir / "install").read_text().strip() == "usr/bin/tool usr/bin"
    perms = (outdir / "permissions").read_text()
    assert "/usr/bin/tool 755 root root" in perms
    # None (removed) entries skipped.
    assert "removed" not in (outdir / "install").read_text()
    # No directory entries in this DB -> an empty (but present) dirs file.
    assert (outdir / "dirs").read_text() == ""
