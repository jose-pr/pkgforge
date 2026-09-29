"""Tests for archive extraction (tarfile) and the dbdump packaging formats."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tarfile

import pytest
import yaml

from conftest import skip_if_fs_rejects_non_utf8
from pkgforge.errors import UsageError
from pkgforge.dbdump import (
    Debian,
    DbDump,
    DumpError,
    DumpFormat,
    RpmSpecFiles,
    UnsupportedOutputError,
)
from pkgforge.install.archive import _extract_tar, _is_tar_source

try:  # Unix-only; only the @posix fixperms-execution tests need these.
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-Unix
    grp = pwd = None

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


def test_dump_format_names_lists_rpm_and_debian():
    names = DumpFormat.names()
    assert "rpmspecfiles" in names
    assert "debian" in names
    assert DumpFormat.lookup("rpmspecfiles") is RpmSpecFiles
    assert DumpFormat.lookup("debian") is Debian


@pytest.mark.parametrize(
    "alias,canonical",
    [("rpm", "rpmspecfiles"), ("rpmspec", "rpmspecfiles"), ("deb", "debian")],
)
def test_dump_format_alias_resolves_to_canonical(alias, canonical):
    assert DumpFormat.lookup(alias) is DumpFormat.lookup(canonical)
    assert DumpFormat.lookup(alias).NAME == canonical


def test_unknown_dump_format_lists_aliases():
    with pytest.raises(UsageError) as excinfo:
        DumpFormat.lookup("nope")
    err = str(excinfo.value)
    assert "rpm" in err
    assert "rpmspec" in err
    assert "deb" in err


@pytest.mark.parametrize(
    "case",
    ["per_entry_dir", "multi_artifact_file"],
    ids=["per_entry_dir", "multi_artifact_file"],
)
def test_unsupported_output_error(tmp_path, case):
    if case == "per_entry_dir":
        target = tmp_path / "outdir"
        target.mkdir()
        fmt = RpmSpecFiles()
    else:
        target = tmp_path / "out"
        target.write_text("existing", encoding="utf-8")
        fmt = Debian()

    before = list(target.iterdir()) if target.is_dir() else target.read_text()
    with pytest.raises(UnsupportedOutputError) as excinfo:
        fmt.check_output(target)
    assert isinstance(excinfo.value, NotImplementedError)
    assert isinstance(excinfo.value, DumpError)
    assert isinstance(excinfo.value, UsageError)
    after = list(target.iterdir()) if target.is_dir() else target.read_text()
    assert after == before


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
    arts = Debian().render(_entries())
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
    arts = Debian().render(_one_entry(path))
    install = arts["install"].decode().strip()
    assert install == expected


def test_debian_rejects_control_char():
    with pytest.raises(DumpError):
        Debian().render(_one_entry("/usr/share/x\ny"))


def test_debian_rejects_space_in_owner():
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
        Debian().render(entries)


def test_debian_permissions_rsplit_keeps_path():
    # Guard: the permissions format is unescaped, so a path containing
    # spaces must still be parseable right-to-left.
    entries = _one_entry(
        "/usr/share/t/with space", mode="640", owner="root", group="adm"
    )
    arts = Debian().render(entries)
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
    arts = Debian().render(entries)
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
    from pkgforge.dbdump.rpm import _rpm_quote

    assert _rpm_quote(path).encode("utf-8", "surrogateescape") == expected


@pytest.mark.parametrize(
    "path",
    ["/a\nb", "/a\tb", "/a\x7fb", "/100%done", "/%{name}", "/%(id)"],
    ids=["nl", "tab", "del", "pct", "pct_brace", "pct_paren"],
)
def test_rpmspecfile_rejects(path):
    from pkgforge.dbdump.rpm import _rpm_quote

    with pytest.raises(DumpError):
        _rpm_quote(path)


def test_rpmspecfile_non_utf8_keeps_bytes():
    entry = {"mode": "-", "owner": "-", "group": "-", "type": "file", "meta": {}}
    line = RpmSpecFiles().render_entry("/opt/caf\udce9", entry)
    assert line == b'%attr(-,-,-) "/opt/caf\xe9"\n'


def test_rpmspecfile_empty_fields_render_default():
    # An empty mode/owner/group renders as "-" (DEFAULT), not verbatim
    # (rpmbuild rejects "%attr(,-,-)" with "Bad syntax").
    entry = {"mode": "", "owner": "", "group": "", "type": "file", "meta": {}}
    assert RpmSpecFiles().render_entry("/x", entry) == b'%attr(-,-,-) "/x"\n'


def test_debian_permissions_skip_empty_fields():
    entries = [
        (
            "/etc/tool",
            {"mode": "", "owner": "", "group": "", "type": "file", "meta": {}},
        ),
    ]
    arts = Debian().render(entries)
    assert arts["permissions"] == b""


def test_debian_permissions_artifact():
    arts = Debian().render(_entries())
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
    arts = Debian().render(entries)
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
    arts = Debian().render(entries)
    dirs = arts["dirs"].decode().splitlines()
    assert dirs == ["var/lib/tool", "var/lib/sp${Space}ace", "./#state"]
    # A directory entry is never an install target.
    assert "var/lib/tool" not in arts["install"].decode()


# --------------------------------------------------------------------------
# debian: fixperms
# --------------------------------------------------------------------------


def test_debian_fixperms_header_only():
    # No pinned field anywhere -> the fixperms script is just its own
    # four-line header.
    arts = Debian().render([_dir_entry("/etc/tool")])
    assert arts["fixperms"] == (
        b"#!/bin/sh\n"
        b"# Generated by pkgforge dbdump -f debian. Usage: sh fixperms PACKAGE-DIR\n"
        b"set -e\n"
        b"d=${1:?usage: sh fixperms PACKAGE-DIR}\n"
    )


def test_debian_fixperms_bytes():
    entries = [
        (
            "/usr/bin/tool",
            {
                "mode": "4755",
                "owner": "root",
                "group": "root",
                "type": "file",
                "meta": {},
            },
        ),
        _dir_entry("/var/lib/state", mode="750"),
        (
            "/opt/app/owner-only",
            {"mode": "-", "owner": "svc", "group": "-", "type": "file", "meta": {}},
        ),
        (
            "/opt/app/group-only",
            {"mode": "-", "owner": "-", "group": "adm", "type": "file", "meta": {}},
        ),
        (
            "/opt/app/link",
            {
                "mode": "644",
                "owner": "svc",
                "group": "-",
                "type": "symlink",
                "meta": {},
            },
        ),
    ]
    arts = Debian().render(entries)
    assert arts["fixperms"] == (
        b"#!/bin/sh\n"
        b"# Generated by pkgforge dbdump -f debian. Usage: sh fixperms PACKAGE-DIR\n"
        b"set -e\n"
        b"d=${1:?usage: sh fixperms PACKAGE-DIR}\n"
        b'chown -- root "$d"/usr/bin/tool\n'
        b'chgrp -- root "$d"/usr/bin/tool\n'
        b'chmod -- 4755 "$d"/usr/bin/tool\n'
        b'chmod -- 750 "$d"/var/lib/state\n'
        b'chown -- svc "$d"/opt/app/owner-only\n'
        b'chgrp -- adm "$d"/opt/app/group-only\n'
        # A symlink gets chown -h (never chown/chgrp without -h, and never
        # chmod at all -- POSIX chmod has no -h and would follow the link).
        b'chown -h -- svc "$d"/opt/app/link\n'
    )


@pytest.mark.parametrize(
    "path,mode,owner,expected",
    [
        ("/a b", "644", "-", "chmod -- 644 \"$d\"'/a b'"),
        ("/it's", "644", "-", "chmod -- 644 \"$d\"'/it'\"'\"'s'"),
        ("/x/d$HOME", "644", "-", "chmod -- 644 \"$d\"'/x/d$HOME'"),
        ("/back\\slash", "644", "-", "chmod -- 644 \"$d\"'/back\\slash'"),
        ("/star*", "644", "-", "chmod -- 644 \"$d\"'/star*'"),
        ("/café", "644", "-", "chmod -- 644 \"$d\"'/café'"),
        ("/f", "-", "-x", 'chown -- -x "$d"/f'),
    ],
    ids=["space", "squote", "dollar", "backslash", "glob", "utf8", "dash_owner"],
)
def test_debian_fixperms_quotes(path, mode, owner, expected):
    entries = [
        (path, {"mode": mode, "owner": owner, "group": "-", "type": "file", "meta": {}})
    ]
    arts = Debian().render(entries)
    lines = arts["fixperms"].decode("utf-8", "surrogateescape").splitlines()
    assert expected in lines


def test_debian_fixperms_non_utf8_keeps_bytes():
    entries = [
        (
            "/opt/caf\udce9",
            {"mode": "644", "owner": "-", "group": "-", "type": "file", "meta": {}},
        )
    ]
    arts = Debian().render(entries)
    assert b"'/opt/caf\xe9'" in arts["fixperms"]


@pytest.mark.posix
def test_debian_fixperms_requires_package_dir(tmp_path):
    arts = Debian().render([_dir_entry("/etc/tool")])
    script = tmp_path / "fixperms"
    script.write_bytes(arts["fixperms"])
    for args in ([], [""]):
        result = subprocess.run(
            ["sh", str(script), *args], capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "PACKAGE-DIR" in result.stderr


@pytest.mark.posix
def test_debian_fixperms_script_applies_pins(tmp_path):
    skip_if_fs_rejects_non_utf8(tmp_path)

    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "x").mkdir()

    paths_modes = {
        "/a b": "751",
        "/it's": "600",
        "/x/d$HOME": "640",
        "/back\\slash": "751",
        "/star*": "600",
    }
    entries = []
    for path, mode in paths_modes.items():
        rel = path.lstrip("/")
        (tree / rel).write_text("x", encoding="utf-8")
        entries.append(
            (
                path,
                {
                    "mode": mode,
                    "owner": user,
                    "group": group,
                    "type": "file",
                    "meta": {},
                },
            )
        )

    nonutf8_name = os.fsdecode(b"caf\xe9")
    (tree / nonutf8_name).write_bytes(b"x")
    entries.append(
        (
            "/" + nonutf8_name,
            {"mode": "640", "owner": user, "group": group, "type": "file", "meta": {}},
        )
    )

    # A symlink whose target lies outside the tree, with a known initial
    # mode: fixperms must never chmod it (chmod is skipped for a symlink
    # entry entirely) and must chown/chgrp -h (the link itself, not the
    # target) -- either slip would leave this target's mode changed.
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    os.chmod(outside, 0o644)
    link = tree / "link"
    os.symlink(outside, link)
    entries.append(
        (
            "/link",
            {
                "mode": "600",
                "owner": user,
                "group": group,
                "type": "symlink",
                "meta": {},
            },
        )
    )

    arts = Debian().render(entries)
    script = tmp_path / "fixperms"
    script.write_bytes(arts["fixperms"])

    result = subprocess.run(
        ["sh", str(script), str(tree)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

    for path, mode in paths_modes.items():
        got = stat.S_IMODE(os.stat(tree / path.lstrip("/")).st_mode)
        assert got == int(mode, 8), path

    got = stat.S_IMODE(os.stat(tree / nonutf8_name).st_mode)
    assert got == 0o640

    # The symlink's own target is untouched, both in mode and identity.
    assert stat.S_IMODE(os.stat(outside).st_mode) == 0o644
    assert os.path.realpath(link) == os.path.realpath(outside)


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
    assert (outdir / "fixperms").read_text().startswith("#!/bin/sh\n")
