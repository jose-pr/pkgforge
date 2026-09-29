"""Tests for archive extraction (tarfile) and the dbdump packaging formats."""

from __future__ import annotations

import json
import os
import shlex
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
    RpmSpecFilesPre419,
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
    assert "rpmspecfiles-pre419" in names
    assert "debian" in names
    assert DumpFormat.lookup("rpmspecfiles") is RpmSpecFiles
    assert DumpFormat.lookup("rpmspecfiles-pre419") is RpmSpecFilesPre419
    assert DumpFormat.lookup("debian") is Debian


def test_dump_format_alias_pre419():
    assert DumpFormat.lookup("rpm-pre419") is RpmSpecFilesPre419
    assert DumpFormat.lookup("rpm-pre419").NAME == "rpmspecfiles-pre419"


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


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/usr/bin/x", b"/usr/bin/x"),  # plain
        ("/opt/café", b"/opt/caf\xc3\xa9"),  # utf8
        ('/quo"te', b'/quo"te'),  # dquote: passed through unescaped
        ("/back\\slash", b"/back\\slash"),  # backslash: passed through unescaped
    ],
    ids=["plain", "utf8", "dquote", "backslash"],
)
def test_rpmspecfiles_pre419_quotes(path, expected):
    from pkgforge.dbdump.rpm import _rpm_quote_pre419

    assert _rpm_quote_pre419(path).encode("utf-8", "surrogateescape") == expected


@pytest.mark.parametrize(
    "path",
    [
        "/with space",
        "/star*",
        "/q?",
        "/br[x]",
        "/brace{a,b}",
        "/100%done",
        "/%{name}",
        "/caf\udce9",
    ],
    ids=["space", "star", "qmark", "bracket", "brace", "pct", "pct_brace", "nonutf8"],
)
def test_rpmspecfiles_pre419_rejects(path):
    from pkgforge.dbdump.rpm import _rpm_quote_pre419

    with pytest.raises(DumpError):
        _rpm_quote_pre419(path)


def test_rpmspecfile_non_utf8_keeps_bytes():
    entry = {"mode": "-", "owner": "-", "group": "-", "type": "file", "meta": {}}
    line = RpmSpecFiles().render_entry("/opt/caf\udce9", entry)
    assert line == b'%attr(-,-,-) "/opt/caf\xe9"\n'


def test_rpmspecfile_empty_fields_render_default():
    # An empty mode/owner/group renders as "-" (DEFAULT), not verbatim
    # (rpmbuild rejects "%attr(,-,-)" with "Bad syntax").
    entry = {"mode": "", "owner": "", "group": "", "type": "file", "meta": {}}
    assert RpmSpecFiles().render_entry("/x", entry) == b'%attr(-,-,-) "/x"\n'


# --------------------------------------------------------------------------
# rpm: a whole batch rendered together (validate-once-per-batch, not per
# entry) -- pins the aggregate byte output, one entry per feature, dirs
# interleaved with files.
# --------------------------------------------------------------------------


def _bs(n):
    """``n`` literal backslash characters, spelled without a dense run of
    backslashes in the source."""
    return "\\" * n


def _rpm_corpus_entries():
    return [
        (
            "/etc/tool",
            {
                "mode": "-",
                "owner": "-",
                "group": "-",
                "type": "directory",
                "meta": {"rpmprefix": "%{buildroot}"},
            },
        ),
        (
            "/usr/bin/back" + _bs(1) + "slash",
            {
                "mode": "755",
                "owner": "root",
                "group": "root",
                "type": "file",
                "meta": {},
            },
        ),
        (
            '/usr/share/t/quo"te',
            {"mode": "", "owner": "", "group": "", "type": "file", "meta": {}},
        ),
        (
            "/usr/share/t/star*br[a]?",
            {"mode": "644", "owner": "-", "group": "-", "type": "file", "meta": {}},
        ),
        (
            "/usr/share/t/with space",
            {
                "mode": "750",
                "owner": "root",
                "group": "root",
                "type": "directory",
                "meta": {},
            },
        ),
        (
            "/opt/app/caf\udce9",
            {"mode": "640", "owner": "svc", "group": "svc", "type": "file", "meta": {}},
        ),
    ]


def test_rpm_render_pinned():
    expected = (
        '%{buildroot} %dir %attr(-,-,-) "/etc/tool"\n'
        '%attr(755,root,root) "/usr/bin/back' + _bs(2) + 'slash"\n'
        '%attr(-,-,-) "/usr/share/t/quo' + _bs(1) + '"te"\n'
        '%attr(644,-,-) "/usr/share/t/star*br[a]?"\n'
        '%dir %attr(750,root,root) "/usr/share/t/with space"\n'
        '%attr(640,svc,svc) "/opt/app/caf\udce9"\n'
    ).encode("utf-8", "surrogateescape")
    assert RpmSpecFiles().render(_rpm_corpus_entries()) == expected


def test_rpm_render_matches_render_entry():
    fmt = RpmSpecFiles()
    entries = _rpm_corpus_entries()
    assert fmt.render(entries) == b"".join(
        fmt.render_entry(path, entry) for path, entry in entries
    )


def test_rpm_render_empty_list():
    assert RpmSpecFiles().render([]) == b""
    assert RpmSpecFilesPre419().render([]) == b""


def _rpm_pre419_corpus_entries():
    return [
        (
            "/etc/tool",
            {
                "mode": "-",
                "owner": "-",
                "group": "-",
                "type": "directory",
                "meta": {"rpmprefix": "%{buildroot}"},
            },
        ),
        (
            "/usr/bin/back" + _bs(1) + "slash",
            {
                "mode": "755",
                "owner": "root",
                "group": "root",
                "type": "file",
                "meta": {},
            },
        ),
        (
            '/usr/share/t/quo"te',
            {"mode": "", "owner": "", "group": "", "type": "file", "meta": {}},
        ),
        (
            "/usr/share/t/plain",
            {"mode": "644", "owner": "-", "group": "-", "type": "file", "meta": {}},
        ),
    ]


def test_rpm_pre419_render_pinned():
    expected = (
        "%{buildroot} %dir %attr(-,-,-) /etc/tool\n"
        "%attr(755,root,root) /usr/bin/back" + _bs(1) + "slash\n"
        '%attr(-,-,-) /usr/share/t/quo"te\n'
        "%attr(644,-,-) /usr/share/t/plain\n"
    ).encode("utf-8", "surrogateescape")
    assert RpmSpecFilesPre419().render(_rpm_pre419_corpus_entries()) == expected


def test_rpm_pre419_render_matches_render_entry():
    fmt = RpmSpecFilesPre419()
    entries = _rpm_pre419_corpus_entries()
    assert fmt.render(entries) == b"".join(
        fmt.render_entry(path, entry) for path, entry in entries
    )


@pytest.mark.parametrize(
    "fmt_cls,bad_paths",
    [
        (RpmSpecFiles, ["/a\nb", "/100%done"]),
        (RpmSpecFilesPre419, ["/a\nb", "/star*", "/caf\udce9"]),
    ],
    ids=["rpmspecfiles", "rpmspecfiles-pre419"],
)
def test_rpm_render_first_error_wins(fmt_cls, bad_paths):
    good = (
        "/ok",
        {"mode": "-", "owner": "-", "group": "-", "type": "file", "meta": {}},
    )

    def entry_for(path):
        return (
            path,
            {"mode": "-", "owner": "-", "group": "-", "type": "file", "meta": {}},
        )

    for order in (bad_paths, list(reversed(bad_paths))):
        entries = [good] + [entry_for(p) for p in order] + [good]
        fmt = fmt_cls()
        with pytest.raises(DumpError) as batch_exc:
            fmt.render(entries)
        with pytest.raises(DumpError) as single_exc:
            fmt.render([entry_for(order[0])])
        assert str(batch_exc.value) == str(single_exc.value)


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
# debian: a whole batch rendered together (validate-once-per-batch, not per
# entry) -- pins every artifact's aggregate byte output, dirs interleaved
# with files.
# --------------------------------------------------------------------------


def _debian_corpus_entries():
    return [
        (
            "/usr/share/t/star*br[a]{x,y}",
            {"mode": "-", "owner": "-", "group": "-", "type": "file", "meta": {}},
        ),
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
        _dir_entry("/etc/tool"),
        (
            "/#comment/file",
            {
                "mode": "640",
                "owner": "root",
                "group": "adm",
                "type": "file",
                "meta": {},
            },
        ),
        _dir_entry("/#dirstate"),
        (
            "/usr/share/t/with space",
            {"mode": "-", "owner": "svc", "group": "-", "type": "file", "meta": {}},
        ),
        (
            "/opt/app/caf\udce9",
            {
                "mode": "644",
                "owner": "svc",
                "group": "-",
                "type": "symlink",
                "meta": {},
            },
        ),
        (
            "/usr/share/dol${y}dir/f",
            {"mode": "-", "owner": "-", "group": "-", "type": "file", "meta": {}},
        ),
        (
            "/x",
            {"mode": "", "owner": "", "group": "", "type": "file", "meta": {}},
        ),
    ]


def test_debian_render_pinned():
    arts = Debian().render(_debian_corpus_entries())

    assert arts["install"] == (
        "usr/share/t/star"
        + _bs(1)
        + "*br"
        + _bs(1)
        + "[a"
        + _bs(1)
        + "]"
        + _bs(1)
        + "{x,y"
        + _bs(1)
        + "} usr/share/t\n"
        "usr/bin/tool usr/bin\n" + _bs(1) + "#comment/file #comment\n"
        "usr/share/t/with${Space}space usr/share/t\n"
        "opt/app/caf\udce9 opt/app\n"
        "usr/share/dol$"
        + _bs(1)
        + "{y"
        + _bs(1)
        + "}dir/f usr/share/dol${Dollar}{y}dir\n"
        "x\n"
    ).encode("utf-8", "surrogateescape")

    assert arts["dirs"] == b"etc/tool\n./#dirstate\n"

    assert arts["permissions"] == (
        "/usr/bin/tool 755 root root\n"
        "/#comment/file 640 root adm\n"
        "/usr/share/t/with space - svc -\n"
        "/opt/app/caf\udce9 644 svc -\n"
    ).encode("utf-8", "surrogateescape")

    target_tool = '"$d"' + shlex.quote("/usr/bin/tool")
    target_hash = '"$d"' + shlex.quote("/#comment/file")
    target_space = '"$d"' + shlex.quote("/usr/share/t/with space")
    target_link = '"$d"' + shlex.quote("/opt/app/caf\udce9")
    assert arts["fixperms"] == (
        b"#!/bin/sh\n"
        b"# Generated by pkgforge dbdump -f debian. Usage: sh fixperms PACKAGE-DIR\n"
        b"set -e\n"
        b"d=${1:?usage: sh fixperms PACKAGE-DIR}\n"
        + f"chown -- root:root {target_tool}\n".encode()
        + f"chmod -- 755 {target_tool}\n".encode()
        + f"chown -- root:adm {target_hash}\n".encode()
        + f"chmod -- 640 {target_hash}\n".encode()
        + f"chown -- svc {target_space}\n".encode()
        # A symlink: chown -h only (group unpinned), never chmod.
        + f"chown -h -- svc {target_link}\n".encode("utf-8", "surrogateescape")
    )


def test_debian_render_matches_entries_rendered_alone():
    # Guard: batching the validation and the debhelper escaping must not
    # change any entry's own contribution -- rendering the whole corpus
    # together must equal the union of rendering each artifact-relevant
    # subset one entry at a time, entry by entry, for install/dirs (order
    # preserved) built from a single-entry batch each.
    entries = _debian_corpus_entries()
    fmt = Debian()
    whole = fmt.render(entries)
    install_lines = whole["install"].decode("utf-8", "surrogateescape").splitlines()
    dir_lines = whole["dirs"].decode("utf-8", "surrogateescape").splitlines()
    non_dir = [e for e in entries if e[1]["type"] != "directory"]
    dirs = [e for e in entries if e[1]["type"] == "directory"]
    for (path, entry), line in zip(non_dir, install_lines):
        one = fmt.render([(path, entry)])["install"].decode("utf-8", "surrogateescape")
        assert one.strip() == line
    for (path, entry), line in zip(dirs, dir_lines):
        one = fmt.render([(path, entry)])["dirs"].decode("utf-8", "surrogateescape")
        assert one.strip() == line


@pytest.mark.parametrize(
    "make_bad,message_fragment",
    [
        (lambda: _one_entry("/a\nb"), "control character"),
        (
            lambda: [
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
            ],
            "owner",
        ),
    ],
    ids=["control", "owner_whitespace"],
)
def test_debian_render_first_error_wins(make_bad, message_fragment):
    good = _one_entry("/ok")[0]
    bad = make_bad()[0]
    for entries in ([good, bad, good], [bad, good]):
        with pytest.raises(DumpError) as batch_exc:
            Debian().render(entries)
        with pytest.raises(DumpError) as single_exc:
            Debian().render([bad])
        assert message_fragment in str(batch_exc.value)
        assert str(batch_exc.value) == str(single_exc.value)


def test_debian_render_empty_list():
    arts = Debian().render([])
    assert arts["install"] == b""
    assert arts["permissions"] == b""
    assert arts["dirs"] == b""
    assert arts["fixperms"] == (
        b"#!/bin/sh\n"
        b"# Generated by pkgforge dbdump -f debian. Usage: sh fixperms PACKAGE-DIR\n"
        b"set -e\n"
        b"d=${1:?usage: sh fixperms PACKAGE-DIR}\n"
    )


# --------------------------------------------------------------------------
# validate-once-per-batch: a clean batch runs each control/whitespace regex
# exactly once, never once per entry.
# --------------------------------------------------------------------------


class _CountingPattern:
    """Wraps a compiled regex, counting ``search`` calls -- swapped in for
    the module's real pattern via ``monkeypatch.setattr`` so the count
    reflects exactly how many times production code searches it."""

    def __init__(self, real):
        self._real = real
        self.calls = 0

    def search(self, s):
        self.calls += 1
        return self._real.search(s)


def _clean_entries(n):
    return [
        (
            f"/usr/share/app/file{i:04d}.dat",
            {
                "mode": "644",
                "owner": "root",
                "group": "root",
                "type": "file",
                "meta": {},
            },
        )
        for i in range(n)
    ]


def test_rpm_render_checks_control_once(monkeypatch):
    import pkgforge.dbdump as dbdump_mod

    counter = _CountingPattern(dbdump_mod._CONTROL_RE)
    monkeypatch.setattr(dbdump_mod, "_CONTROL_RE", counter)
    RpmSpecFiles().render(_clean_entries(1000))
    assert counter.calls == 1


def test_debian_render_checks_once(monkeypatch):
    import pkgforge.dbdump as dbdump_mod

    control_counter = _CountingPattern(dbdump_mod._CONTROL_RE)
    whitespace_counter = _CountingPattern(dbdump_mod._WHITESPACE_RE)
    monkeypatch.setattr(dbdump_mod, "_CONTROL_RE", control_counter)
    monkeypatch.setattr(dbdump_mod, "_WHITESPACE_RE", whitespace_counter)
    Debian().render(_clean_entries(1000))
    assert control_counter.calls == 1
    assert whitespace_counter.calls == 1


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
        b'chown -- root:root "$d"/usr/bin/tool\n'
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
