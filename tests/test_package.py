"""``pkgforge package``: the command, the payload writer and ``-f tar``."""

from __future__ import annotations

import gzip
import io
import logging
import lzma
import os
import sys
import tarfile
import typing
from pathlib import Path

import pytest

from pkgforge import PackageFormat, Project, UsageError, open_db
from pkgforge.package import BuildContext, PackagePayload, PackageWriter

#: A hand-written DB record; a test overrides the fields it cares about.
_RECORD = {
    "mode": "644",
    "owner": "root",
    "group": "root",
    "type": "file",
    "kind": "file",
    "caps": "-",
    "package": "-",
    "meta": {},
}
EPOCH = 1_500_000_000
OLD = 1_000_000_000
LONG_USER = "u" * 33

CFG = f"""\
[package]
name = "tool"
version = "1.2"
release = "3"
summary = "A tool"
license = "MIT"
maintainer = "Example Maintainer <maintainer@example.org>"

[[packages]]
name = "tool-doc"

[[users]]
name = "{LONG_USER}"
create = false

[[rules]]
match = ["/usr/share/doc/tool-doc", "/usr/share/doc/tool-doc/**"]
package = "tool-doc"

[[stage]]
dir = "/opt/tool"
mode = "750"

[[stage]]
src = "src/tool"
dest = "/usr/bin"
mode = "755"

[[stage]]
src = "src/data"
dest = "/usr/share/tool"

[[stage]]
symlink = "/usr/bin/t"
target = "tool"

[[stage]]
src = "src/helper"
dest = "/usr/libexec/tool"
mode = "755"
caps = "cap_net_bind_service=ep"

[[stage]]
src = "src/tool.conf"
dest = "/etc/tool"
owner = "daemon"
group = "adm"
mode = "640"
kind = "config"

[[stage]]
dir = "/run/tool"
kind = "ghost"

[[stage]]
file = "/var/log/tool.log"
kind = "ghost"

[[stage]]
src = "src/README"
dest = "/usr/share/doc/tool-doc"
mode = "644"
kind = "doc"
"""

PAYLOAD_NAMES = [
    "etc",
    "etc/tool",
    "etc/tool/tool.conf",
    "opt",
    "opt/tool",
    "usr",
    "usr/bin",
    "usr/bin/t",
    "usr/bin/tool",
    "usr/lib",
    "usr/lib/tmpfiles.d",
    "usr/lib/tmpfiles.d/tool.conf",
    "usr/libexec",
    "usr/libexec/tool",
    "usr/libexec/tool/helper",
    "usr/share",
    "usr/share/tool",
    "usr/share/tool/data",
]


@pytest.fixture
def work(tmp_path, monkeypatch):
    """A project directory holding ``pkgforge.cfg`` and its sources as the
    working directory, with the build root and file DB beside it."""
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    for name, text in (
        ("tool", "#!/bin/sh\n"),
        ("data", "data\n"),
        ("helper", "helper\n"),
        ("tool.conf", "x=1\n"),
        ("README", "readme\n"),
    ):
        source = work / "src" / name
        source.write_bytes(text.encode("utf-8"))
        os.utime(source, (OLD, OLD))
    if hasattr(os, "chmod"):
        (work / "src/data").chmod(0o640)
    (work / "pkgforge.cfg").write_bytes(CFG.encode("utf-8"))
    monkeypatch.chdir(work)
    monkeypatch.delenv("PKGFORGE_CONFIG", raising=False)
    monkeypatch.setenv("PKGFORGE_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("PKGFORGE_DB", str(tmp_path / "f.jsonl"))
    (tmp_path / "root").mkdir()
    return work


@pytest.fixture
def staged(work, cli):
    result = cli("stage", "--init")
    assert result.rc == 0, result.err
    return work


def _zstd_module():
    try:
        from compression import zstd
    except ImportError:
        import zstandard

        class _Zstandard:
            @staticmethod
            def decompress(data: bytes) -> bytes:
                return zstandard.ZstdDecompressor().decompress(data)

        return _Zstandard
    return zstd


def _plain(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix == ".gz":
        return gzip.decompress(data)
    if path.suffix == ".xz":
        return lzma.decompress(data)
    if path.suffix == ".zst":
        return _zstd_module().decompress(data)
    return data


def _members(path: Path) -> typing.Dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(_plain(path))) as archive:
        return {member.name: member for member in archive.getmembers()}


def _package(cli, out: Path, *args: str):
    return cli("package", "-f", "tar", "-o", str(out), *args)


# -- the payload ----------------------------------------------------------


@pytest.mark.posix
def test_tar_payload_matches_db(staged, cli, tmp_path):
    result = _package(cli, tmp_path / "out")
    assert result.rc == 0, result.err
    path = tmp_path / "out/tool-1.2-3.tar.gz"
    members = _members(path)
    assert list(members) == PAYLOAD_NAMES
    assert all(member.uid == member.gid == 0 for member in members.values())
    for implicit in ("etc", "usr", "usr/bin", "usr/share", "opt"):
        member = members[implicit]
        assert (member.mode, member.uname, member.gname) == (0o755, "root", "root")
    conf = members["etc/tool/tool.conf"]
    assert (conf.mode, conf.uname, conf.gname) == (0o640, "daemon", "adm")
    assert (members["opt/tool"].mode, members["opt/tool"].uname) == (0o750, "root")
    assert members["usr/share/tool/data"].mode == 0o640
    assert members["usr/bin/tool"].mode == 0o755
    link = members["usr/bin/t"]
    assert link.issym() and link.linkname == "tool"
    assert not any(name.startswith(("run", "var")) for name in members)
    assert _plain(path)[257:265] == b"ustar  \x00"


@pytest.mark.posix
def test_tar_one_archive_per_package(staged, cli, tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="pkgforge.package"):
        result = _package(cli, tmp_path / "out")
    assert result.rc == 0, result.err
    paths = [tmp_path / "out/tool-1.2-3.tar.gz", tmp_path / "out/tool-doc-1.2-3.tar.gz"]
    assert result.out.decode().splitlines() == [str(path) for path in paths]
    assert list(_members(paths[1])) == [
        "usr",
        "usr/share",
        "usr/share/doc",
        "usr/share/doc/tool-doc",
        "usr/share/doc/tool-doc/README",
    ]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.INFO] == [
        f"Wrote {paths[0]}",
        f"Wrote {paths[1]}",
        "built 2 package(s) with tar",
    ]


@pytest.mark.posix
def test_tar_reproducible_with_source_date_epoch(staged, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
    assert _package(cli, tmp_path / "a").rc == 0
    assert _package(cli, tmp_path / "b").rc == 0
    for name in ("tool-1.2-3.tar.gz", "tool-doc-1.2-3.tar.gz"):
        assert (tmp_path / "a" / name).read_bytes() == (
            tmp_path / "b" / name
        ).read_bytes()
    members = _members(tmp_path / "a/tool-1.2-3.tar.gz")
    assert all(member.mtime <= EPOCH for member in members.values())
    assert members["usr/bin/tool"].mtime == OLD


@pytest.mark.posix
def test_tar_without_source_date_epoch_keeps_staged_times(staged, cli, tmp_path):
    assert _package(cli, tmp_path / "out").rc == 0
    members = _members(tmp_path / "out/tool-1.2-3.tar.gz")
    assert members["usr/bin/tool"].mtime == OLD
    assert members["opt/tool"].mtime > EPOCH


@pytest.mark.posix
@pytest.mark.parametrize("kind", ["gz", "xz", "zst", "none"])
def test_tar_compressions(staged, cli, tmp_path, kind):
    result = _package(cli, tmp_path / "out", "--compression", kind)
    assert result.rc == 0, result.err
    suffix = "" if kind == "none" else f".{kind}"
    path = tmp_path / f"out/tool-1.2-3.tar{suffix}"
    assert path.exists()
    assert list(_members(path)) == PAYLOAD_NAMES


@pytest.mark.posix
def test_zstd_missing_names_the_extra(staged, cli, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "zstandard", None)
    monkeypatch.setitem(sys.modules, "compression.zstd", None)
    try:
        import compression

        monkeypatch.delattr(compression, "zstd", raising=False)
    except ImportError:
        pass
    result = _package(cli, tmp_path / "out", "--compression", "zst")
    assert result.rc == 2
    assert b"pkgforge[zstd]" in result.err and b"--compression xz" in result.err
    assert not list((tmp_path / "out").glob("*.zst"))


@pytest.mark.posix
def test_tar_caps_warning(staged, cli, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="pkgforge.package"):
        result = _package(cli, tmp_path / "out")
    assert result.rc == 0, result.err
    assert [r.getMessage() for r in caplog.records] == [
        "/usr/libexec/tool/helper: a tar carries no file capability; run setcap "
        "cap_net_bind_service=ep /usr/libexec/tool/helper after extracting"
    ]


def _link(cli, dest: str, *args: str):
    result = cli("install", "-p", "--method", "link", *args, "src/data", dest)
    assert result.rc == 0, result.err


@pytest.mark.posix
def test_tar_hardlinks_are_one_file_and_links(staged, cli, tmp_path):
    _link(cli, "/opt/h1")
    _link(cli, "/opt/h2")
    _link(cli, "/opt/h3")
    assert _package(cli, tmp_path / "out").rc == 0
    members = _members(tmp_path / "out/tool-1.2-3.tar.gz")
    first = members["opt/h1/data"]
    assert first.isreg() and first.size == 5
    for name in ("opt/h2/data", "opt/h3/data"):
        assert members[name].islnk() and members[name].size == 0
        assert members[name].linkname == "opt/h1/data"
    # The copy `stage` made has its own inode.
    assert members["usr/share/tool/data"].isreg()


@pytest.mark.posix
def test_tar_hardlinks_with_other_metadata_stay_copies(staged, cli, tmp_path):
    _link(cli, "/opt/h1", "-m", "640")
    _link(cli, "/opt/h2", "-m", "600")
    assert _package(cli, tmp_path / "out").rc == 0
    members = _members(tmp_path / "out/tool-1.2-3.tar.gz")
    assert members["opt/h1/data"].isreg() and members["opt/h2/data"].isreg()
    assert (members["opt/h1/data"].mode, members["opt/h2/data"].mode) == (
        0o640,
        0o600,
    )


@pytest.mark.posix
def test_tar_hardlink_target_in_another_package_is_a_copy(staged, cli, tmp_path):
    _link(cli, "/opt/h1")
    _link(cli, "/opt/h2", "-P", "tool-doc")
    assert _package(cli, tmp_path / "out").rc == 0
    assert _members(tmp_path / "out/tool-1.2-3.tar.gz")["opt/h1/data"].isreg()
    assert _members(tmp_path / "out/tool-doc-1.2-3.tar.gz")["opt/h2/data"].isreg()


@pytest.mark.posix
def test_tar_hardlinks_extract_as_hardlinks(staged, cli, tmp_path):
    _link(cli, "/opt/h1")
    _link(cli, "/opt/h2")
    assert _package(cli, tmp_path / "out").rc == 0
    # Before the extraction filters (3.9.17, 3.10.12, 3.11.4), extract() had none.
    options = {"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}
    with tarfile.open(tmp_path / "out/tool-1.2-3.tar.gz") as archive:
        for name in ("opt/h1/data", "opt/h2/data"):
            archive.extract(archive.getmember(name), tmp_path / "x", **options)
    assert os.stat(tmp_path / "x/opt/h2/data").st_nlink == 2
    assert os.path.samefile(tmp_path / "x/opt/h1/data", tmp_path / "x/opt/h2/data")


# -- a failed build leaves nothing ----------------------------------------


@pytest.mark.posix
def test_package_failure_after_the_first_package_leaves_no_directory(
    staged, cli, tmp_path
):
    result = cli(
        "install", "-p", "-P", "tool-doc", "--owner", LONG_USER, "src/data", "/opt/d"
    )
    assert result.rc == 0, result.err
    out = tmp_path / "deep" / "out"
    result = _package(cli, out)
    assert result.rc == 1
    assert b"is longer than the 32 bytes" in result.err
    assert not (tmp_path / "deep").exists()


@pytest.mark.posix
def test_package_failure_keeps_an_existing_output_as_it_was(staged, cli, tmp_path):
    result = cli(
        "install", "-p", "-P", "tool-doc", "--owner", LONG_USER, "src/data", "/opt/d"
    )
    assert result.rc == 0, result.err
    out = tmp_path / "out"
    out.mkdir()
    (out / "tool-1.2-3.tar.gz").write_bytes(b"old")
    assert _package(cli, out).rc == 1
    assert [path.name for path in out.iterdir()] == ["tool-1.2-3.tar.gz"]
    assert (out / "tool-1.2-3.tar.gz").read_bytes() == b"old"


def test_writer_creates_directories_at_the_first_file_and_removes_them(tmp_path):
    (tmp_path / "kept").mkdir()
    writer = PackageWriter()
    assert not (tmp_path / "kept" / "new").exists()
    with writer.create(tmp_path / "kept" / "new" / "deeper" / "a.pkg") as handle:
        handle.write(b"a")
    assert (tmp_path / "kept/new/deeper/.a.pkg.pkgforge-tmp").is_file()
    writer.abort()
    assert not (tmp_path / "kept" / "new").exists()
    assert list((tmp_path / "kept").iterdir()) == []


def test_writer_commit_replaces_the_targets_together(tmp_path):
    (tmp_path / "a").write_bytes(b"old")
    writer = PackageWriter()
    with writer.create(tmp_path / "a") as handle:
        handle.write(b"new a")
    with writer.create(tmp_path / "b") as handle:
        handle.write(b"new b")
    assert (tmp_path / "a").read_bytes() == b"old" and not (tmp_path / "b").exists()
    writer.commit()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a", "b"]
    assert (tmp_path / "a").read_bytes() == b"new a"


# -- refusals -------------------------------------------------------------


@pytest.mark.posix
def test_package_missing_entry_exit_2(staged, cli, tmp_path):
    (tmp_path / "root/usr/bin/tool").unlink()
    result = _package(cli, tmp_path / "out")
    assert result.rc == 2
    assert b"/usr/bin/tool: recorded but missing from the build root" in result.err
    assert b"pkgforge verify" in result.err
    assert not list((tmp_path / "out").glob("*.tar*"))


@pytest.mark.posix
def test_package_type_changed_exit_2(staged, cli, tmp_path):
    target = tmp_path / "root/usr/share/tool/data"
    target.unlink()
    target.mkdir()
    result = _package(cli, tmp_path / "out")
    assert result.rc == 2
    assert (
        b"/usr/share/tool/data: recorded as file, but the build root holds a "
        b"directory"
    ) in result.err
    assert b"pkgforge verify" in result.err


@pytest.mark.posix
def test_package_member_under_a_symlink_leaving_the_root_exit_2(staged, cli, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"host file\n")
    os.symlink(outside, tmp_path / "root/opt/link")
    link = dict(_RECORD, type="symlink", mode="-")
    secret = dict(_RECORD, type="file", mode="644")
    provider = open_db(tmp_path / "f.jsonl")
    provider.add("/opt/link", link)
    provider.add("/opt/link/secret", secret)
    result = _package(cli, tmp_path / "out")
    assert result.rc == 2
    assert b"/opt/link/secret: a symlink leads outside --buildroot" in result.err
    assert not (tmp_path / "out").exists()


@pytest.mark.posix
def test_package_long_owner_exit_1(staged, cli, tmp_path):
    result = cli("install", "-p", "--owner", LONG_USER, "src/data", "/usr/share/other")
    assert result.rc == 0, result.err
    result = _package(cli, tmp_path / "out")
    assert result.rc == 1
    assert f"owner '{LONG_USER}' is longer than the 32 bytes".encode() in result.err


def test_package_unknown_format_exit_2(work, cli, tmp_path):
    assert cli("initdb").rc == 0
    result = cli("package", "-f", "nosuch", "-o", str(tmp_path / "out"))
    assert result.rc == 2
    assert b"unknown package format 'nosuch'" in result.err
    assert b"tar" in result.err


def test_package_bad_compression_exit_2(work, cli, tmp_path):
    assert cli("initdb").rc == 0
    result = _package(cli, tmp_path / "out", "--compression", "bz2")
    assert result.rc == 2
    assert b"tar takes --compression gz, xz, zst, none" in result.err


def test_package_bad_arch_exit_2(work, cli, tmp_path):
    assert cli("initdb").rc == 0
    result = _package(cli, tmp_path / "out", "--arch", "x86 64")
    assert result.rc == 2
    assert b"is not an architecture name" in result.err


def test_package_missing_keys_exit_2(tmp_path, cli, monkeypatch):
    cfg = tmp_path / "pkgforge.cfg"
    cfg.write_bytes(b'[package]\nname = "tool"\n')
    (tmp_path / "root").mkdir()
    base = ["--config", str(cfg), "--db", str(tmp_path / "f.jsonl")]
    assert cli(*base, "--buildroot", str(tmp_path / "root"), "initdb").rc == 0
    result = cli(*base, "--buildroot", str(tmp_path / "root"), "package", "-f", "tar")
    assert result.rc == 2
    assert b"tar needs [package] version" in result.err


def test_package_bad_source_date_epoch_exit_2(work, cli, tmp_path, monkeypatch):
    assert cli("initdb").rc == 0
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "yesterday")
    result = _package(cli, tmp_path / "out")
    assert result.rc == 2
    assert b"SOURCE_DATE_EPOCH='yesterday' is not a number of seconds" in result.err


def test_package_db_stdin_exit_2(work, cli):
    result = cli("--db", "-", "package", "-f", "tar")
    assert result.rc == 2
    assert b"--db - has no build root" in result.err


def test_package_missing_db_exit_2(work, cli):
    result = cli("package", "-f", "tar")
    assert result.rc == 2
    assert b"no file DB at" in result.err


def test_package_missing_build_root_exit_2(work, cli, tmp_path):
    assert cli("initdb").rc == 0
    (tmp_path / "root").rmdir()
    result = cli("package", "-f", "tar")
    assert result.rc == 2
    assert b"does not exist" in result.err


# -- the library ----------------------------------------------------------


@pytest.mark.posix
def test_package_library_matches_cli(staged, cli, tmp_path):
    assert _package(cli, tmp_path / "cli").rc == 0
    project = Project.from_config(
        staged / "pkgforge.cfg", buildroot=tmp_path / "root", db=tmp_path / "f.jsonl"
    )
    written = project.package("tar", tmp_path / "lib")
    assert [path.name for path in written] == [
        "tool-1.2-3.tar.gz",
        "tool-doc-1.2-3.tar.gz",
    ]
    for path in written:
        assert path.read_bytes() == (tmp_path / "cli" / path.name).read_bytes()


@pytest.mark.posix
def test_arch_name_table(staged, cli, tmp_path, monkeypatch, restore_registries):
    seen: typing.List[str] = []

    class Probe(PackageFormat):
        NAME = "probe"
        MACHINES = {"x86_64": "amd64", "amd64": "amd64"}
        ALL_ARCH = "noarch"

        def build(
            self,
            payloads: typing.Sequence[PackagePayload],
            context: BuildContext,
        ) -> typing.List[Path]:
            seen.extend(self.arch_name(payload, context) for payload in payloads)
            return []

    import pkgforge.package as package

    out = str(tmp_path / "out")
    for machine in ("x86_64", "AMD64"):
        monkeypatch.setattr(package.platform, "machine", lambda m=machine: m)
        assert cli("package", "-f", "probe", "-o", out).rc == 0
    assert seen == ["amd64", "amd64"] * 2
    monkeypatch.setattr(package.platform, "machine", lambda: "sparc64")
    missing = cli("package", "-f", "probe", "-o", out)
    assert missing.rc == 2
    assert b"no architecture name for machine 'sparc64': pass --arch" in missing.err
    seen.clear()
    assert cli("package", "-f", "probe", "-o", out, "--arch", "riscv64").rc == 0
    assert seen == ["riscv64", "riscv64"]


def test_a_format_that_offers_no_compression_refuses_the_option(
    work, cli, tmp_path, restore_registries
):
    class Plain(PackageFormat):
        NAME = "plain"

        def build(self, payloads, context):
            return []

    assert cli("initdb").rc == 0
    result = cli("package", "-f", "plain", "--compression", "gz")
    assert result.rc == 2
    assert b"plain takes no --compression" in result.err


def test_package_requires_a_file_db_from_the_library(tmp_path):
    with pytest.raises(UsageError, match="no file DB configured"):
        Project(tmp_path).package("tar", tmp_path / "out")
