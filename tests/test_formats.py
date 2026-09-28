"""Tests for archive extraction (tarfile) and the dbdump packaging formats."""

from __future__ import annotations

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
