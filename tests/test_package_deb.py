"""``pkgforge package -f deb``: the ar archive, control data and payload,
read back by a pure-Python reader."""

from __future__ import annotations

import gzip
import hashlib
import io
import lzma
import shutil
import tarfile
import typing
from pathlib import Path

import pytest

from pkgforge import DumpError, Project
from pkgforge.package._deb import _ar_member

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EPOCH = 1_500_000_000
HELLO = "hello_1.0-1_all.deb"
DOC = "hello-doc_1.0-1_all.deb"
UNITS = "hello.service"


@pytest.fixture
def work(tmp_path, monkeypatch):
    """``examples/pkgforge.cfg`` and its sources as the working directory, with
    the build root and file DB beside it, staged."""
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy(EXAMPLES / "pkgforge.cfg", work / "pkgforge.cfg")
    shutil.copytree(EXAMPLES / "src", work / "src")
    monkeypatch.chdir(work)
    monkeypatch.delenv("PKGFORGE_CONFIG", raising=False)
    monkeypatch.setenv("PKGFORGE_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("PKGFORGE_DB", str(tmp_path / "f.jsonl"))
    (tmp_path / "root").mkdir()
    return work


def _stage(cli) -> None:
    result = cli("stage", "--init")
    assert result.rc == 0, result.err


def _edit_cfg(work: Path, *pairs: typing.Tuple[str, str], append: str = "") -> None:
    path = work / "pkgforge.cfg"
    text = path.read_bytes().decode("utf-8")
    for old, new in pairs:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path.write_bytes((text + append).encode("utf-8"))


@pytest.fixture
def staged(work, cli):
    _stage(cli)
    return work


def _package(cli, out: Path, *args: str):
    return cli("package", "-f", "deb", "-o", str(out), *args)


def _ar(path: Path) -> typing.List[typing.Tuple[str, bytes, bytes]]:
    """The members of an ar archive: ``(name, header, data)``."""
    data = path.read_bytes()
    assert data[:8] == b"!<arch>\n"
    offset, members = 8, []
    while offset < len(data):
        header = data[offset : offset + 60]
        assert len(header) == 60 and header[58:] == b"`\n"
        size = int(header[48:58])
        body = data[offset + 60 : offset + 60 + size]
        assert len(body) == size
        members.append((header[:16].decode().strip(), header, body))
        offset += 60 + size
        if size % 2:
            assert data[offset : offset + 1] == b"\n"
            offset += 1
        assert offset % 2 == 0
    return members


def _plain(name: str, data: bytes) -> bytes:
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".zst"):
        try:
            from compression import zstd

            return zstd.decompress(data)
        except ImportError:
            import zstandard

            return zstandard.ZstdDecompressor().decompress(data)
    return data


def _tar(
    path: Path, which: str
) -> typing.Tuple[bytes, typing.Dict[str, tarfile.TarInfo]]:
    """``control.tar`` or ``data.tar`` of ``path``: its bytes and members."""
    for name, _header, body in _ar(path):
        if name.startswith(which):
            raw = _plain(name, body)
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                return raw, {member.name: member for member in archive.getmembers()}
    raise AssertionError(which)


def _text(path: Path, name: str) -> str:
    raw, members = _tar(path, "control.tar")
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        extracted = archive.extractfile(members[name])
        assert extracted is not None
        return extracted.read().decode("utf-8")


def _fields(text: str) -> typing.List[typing.Tuple[str, str]]:
    result = []
    for line in text.splitlines():
        if not line.startswith(" "):
            key, _, value = line.partition(": ")
            result.append((key, value))
    return result


def _kib(members: typing.Iterable[tarfile.TarInfo]) -> int:
    total = 0
    for member in members:
        if member.isreg():
            total += -(-member.size // 1024)
        elif member.issym():
            total += -(-len(member.linkname) // 1024)
        else:
            total += 1
    return total


@pytest.mark.posix
def test_deb_ar_layout(staged, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
    result = _package(cli, tmp_path / "out")
    assert result.rc == 0, result.err
    assert result.out.decode().splitlines() == [
        str(tmp_path / "out" / HELLO),
        str(tmp_path / "out" / DOC),
    ]
    for name in (HELLO, DOC):
        members = _ar(tmp_path / "out" / name)
        assert [member[0] for member in members] == [
            "debian-binary",
            "control.tar.xz",
            "data.tar.xz",
        ]
        assert members[0][2] == b"2.0\n"
        for member_name, header, body in members:
            assert header[:16] == f"{member_name:<16}".encode()
            assert header[16:28] == f"{EPOCH:<12}".encode()
            assert header[28:34] == b"0     " and header[34:40] == b"0     "
            assert header[40:48] == b"100644  "
            assert int(header[48:58]) == len(body)


@pytest.mark.posix
def test_deb_ar_pads_odd_members(staged, cli, tmp_path):
    assert _package(cli, tmp_path / "out", "--compression", "none").rc == 0
    path = tmp_path / "out" / HELLO
    raw = path.read_bytes()
    members = _ar(path)
    assert [member[0] for member in members] == [
        "debian-binary",
        "control.tar",
        "data.tar",
    ]
    assert len(members[0][2]) == 4
    assert raw.count(b"\n") > 0 and len(raw) % 2 == 0


@pytest.mark.posix
def test_deb_control_fields(staged, cli, tmp_path):
    assert _package(cli, tmp_path / "out").rc == 0
    hello = tmp_path / "out" / HELLO
    fields = _fields(_text(hello, "./control"))
    assert [key for key, _value in fields] == [
        "Package",
        "Version",
        "Architecture",
        "Maintainer",
        "Installed-Size",
        "Depends",
        "Section",
        "Priority",
        "Description",
    ]
    values = dict(fields)
    assert values["Package"] == "hello" and values["Version"] == "1.0-1"
    assert values["Architecture"] == "all"
    assert values["Maintainer"] == "Example Maintainer <maintainer@example.org>"
    assert values["Section"] == "misc" and values["Priority"] == "optional"
    assert values["Description"] == "pkgforge example package"
    assert values["Depends"] == (
        "systemd | systemd-standalone-sysusers | systemd-sysusers, libcap2-bin"
    )
    _raw, payload = _tar(hello, "data.tar")
    assert int(values["Installed-Size"]) == _kib(payload.values())
    text = _text(hello, "./control")
    assert text.endswith(
        " A sample package whose file list, modes and owners come from a pkgforge file\n"
        " DB. The hello user comes from the sysusers.d file in the package.\n"
    )

    doc = tmp_path / "out" / DOC
    doc_fields = _fields(_text(doc, "./control"))
    assert [key for key, _value in doc_fields] == [
        "Package",
        "Source",
        "Version",
        "Architecture",
        "Maintainer",
        "Installed-Size",
        "Section",
        "Priority",
        "Description",
    ]
    doc_values = dict(doc_fields)
    assert doc_values["Source"] == "hello" and doc_values["Section"] == "misc"
    _raw, doc_payload = _tar(doc, "data.tar")
    assert int(doc_values["Installed-Size"]) == _kib(doc_payload.values())


@pytest.mark.posix
def test_deb_homepage_and_section_inheritance(work, cli, tmp_path):
    _edit_cfg(
        work,
        (
            'license = "MIT"\n',
            'license = "MIT"\nhomepage = "https://example.org/hello"\n',
        ),
        ('section = "misc"\n', 'section = "utils"\npriority = "extra"\n'),
    )
    _stage(cli)
    assert _package(cli, tmp_path / "out").rc == 0
    for name in (HELLO, DOC):
        values = dict(_fields(_text(tmp_path / "out" / name, "./control")))
        assert values["Homepage"] == "https://example.org/hello"
        assert values["Section"] == "utils" and values["Priority"] == "extra"
        assert list(values).index("Homepage") == list(values).index("Priority") + 1


@pytest.mark.posix
def test_deb_conffiles_md5sums_scripts(staged, cli, tmp_path):
    assert _package(cli, tmp_path / "out").rc == 0
    hello = tmp_path / "out" / HELLO
    raw, members = _tar(hello, "control.tar")
    assert list(members) == [
        ".",
        "./conffiles",
        "./control",
        "./md5sums",
        "./postinst",
        "./postrm",
        "./prerm",
    ]
    assert raw[257:265] == b"ustar  \x00"
    for name, member in members.items():
        assert (member.uname, member.gname, member.uid, member.gid) == (
            "root",
            "root",
            0,
            0,
        )
        want = 0o755 if name in (".", "./postinst", "./postrm", "./prerm") else 0o644
        assert member.mode == want, name

    assert _text(hello, "./conffiles") == "/etc/hello/hello.conf\n"
    sums = _text(hello, "./md5sums").splitlines()
    _raw, payload = _tar(hello, "data.tar")
    with tarfile.open(fileobj=io.BytesIO(_raw)) as archive:
        expected = [
            f"{hashlib.md5(archive.extractfile(m).read()).hexdigest()}  {m.name[2:]}"
            for m in archive.getmembers()
            if m.isreg() and m.name != "./etc/hello/hello.conf"
        ]
    assert sums == expected
    assert not any("etc/hello" in line for line in sums)

    postinst = _text(hello, "./postinst")
    assert postinst.startswith('#!/bin/sh\nset -e\nif [ "$1" = configure ]; then\n')
    order = [
        postinst.index(text)
        for text in (
            'systemd-sysusers ${DPKG_ROOT:+--root="$DPKG_ROOT"} hello.conf',
            "command -v systemd-tmpfiles >/dev/null; then systemd-tmpfiles "
            '${DPKG_ROOT:+--root="$DPKG_ROOT"} --create hello.conf || true; fi',
            f"deb-systemd-helper unmask {UNITS} >/dev/null || true",
            f"deb-systemd-helper --quiet was-enabled {UNITS}",
            f"deb-systemd-helper enable {UNITS}",
            f"deb-systemd-helper update-state {UNITS}",
            "systemctl --system daemon-reload",
            f"deb-systemd-invoke $_action {UNITS}",
            "dpkg-statoverride --update --add hello hello 750 /var/lib/hello",
            'setcap cap_net_bind_service=ep "$DPKG_ROOT"/usr/libexec/hello/helper',
        )
    ]
    assert order == sorted(order), postinst
    assert "_action=restart; else _action=start" in postinst
    assert postinst.endswith("fi\n")

    prerm = _text(hello, "./prerm")
    assert prerm == (
        "#!/bin/sh\nset -e\n"
        'if [ -z "$DPKG_ROOT" ] && [ "$1" = remove ] && [ -d /run/systemd/system ]; then\n'
        f"  deb-systemd-invoke stop {UNITS} >/dev/null || true\n"
        "fi\n"
    )
    postrm = _text(hello, "./postrm")
    assert "systemctl --system daemon-reload >/dev/null || true" in postrm
    assert f"deb-systemd-helper purge {UNITS} >/dev/null || true" in postrm
    assert "[ -x /usr/bin/deb-systemd-helper ]" in postrm
    assert "dpkg-statoverride --remove /var/lib/hello" in postrm and postrm.index(
        "deb-systemd-helper purge"
    ) < postrm.index("--remove")

    _raw, doc_members = _tar(tmp_path / "out" / DOC, "control.tar")
    assert list(doc_members) == [".", "./control", "./md5sums"]


@pytest.mark.posix
def test_deb_payload_owners(staged, cli, tmp_path):
    assert _package(cli, tmp_path / "out").rc == 0
    hello = tmp_path / "out" / HELLO
    raw, members = _tar(hello, "data.tar")
    assert raw[257:265] == b"ustar  \x00"
    names = list(members)
    assert names[0] == "." and all(name.startswith("./") for name in names[1:])
    root = members["."]
    assert (root.mode, root.uname, root.gname) == (0o755, "root", "root")
    state = members["./var/lib/hello"]
    assert (state.mode, state.uname, state.gname) == (0o750, "root", "root")
    conf = members["./etc/hello/hello.conf"]
    assert (conf.mode, conf.uname, conf.gname) == (0o640, "root", "adm")
    assert members["./usr/bin/hi"].issym()
    assert members["./usr/bin/hi"].linkname == "hello"
    assert not any(name.startswith("./run") for name in names)
    assert not any(name.endswith("hello.log") for name in names)
    assert all(m.uid == m.gid == 0 for m in members.values())
    assert names[1:] == sorted(names[1:])


@pytest.mark.posix
def test_deb_depends_substvars(work, cli, tmp_path):
    _edit_cfg(
        work,
        (
            'section = "misc"\n',
            'section = "misc"\n'
            'depends = ["libhello (= $${binary:Version})", "other (>= $${source:Version})"]\n',
        ),
    )
    _stage(cli)
    assert _package(cli, tmp_path / "out").rc == 0
    values = dict(_fields(_text(tmp_path / "out" / HELLO, "./control")))
    assert values["Depends"].endswith(
        "libcap2-bin, libhello (= 1.0-1), other (>= 1.0-1)"
    )


@pytest.mark.posix
def test_deb_depends_refuses_other_substvars(work, cli, tmp_path):
    _edit_cfg(
        work, ('section = "misc"\n', 'section = "misc"\ndepends = ["x $${foo:Bar}"]\n')
    )
    _stage(cli)
    result = _package(cli, tmp_path / "out")
    assert result.rc == 2
    assert (
        b"deb: [package.debian] depends item 'x ${foo:Bar}' holds ${foo:Bar}, "
        b"which only dpkg-gencontrol defines"
    ) in result.err
    assert not list((tmp_path / "out").glob("*.deb"))


@pytest.mark.posix
def test_deb_arch_names(work, cli, tmp_path, monkeypatch):
    _stage(cli)
    assert _package(cli, tmp_path / "all", "--arch", "armhf").rc == 0
    assert sorted(path.name for path in (tmp_path / "all").iterdir()) == [DOC, HELLO]

    _edit_cfg(work, ('arch = "all"\n', 'arch = "any"\n'))
    result = cli("package", "-f", "deb", "-o", str(tmp_path / "any"), "--arch", "armhf")
    assert result.rc == 0, result.err
    assert sorted(path.name for path in (tmp_path / "any").iterdir()) == [
        "hello-doc_1.0-1_armhf.deb",
        "hello_1.0-1_armhf.deb",
    ]
    values = dict(_fields(_text(tmp_path / "any/hello_1.0-1_armhf.deb", "./control")))
    assert values["Architecture"] == "armhf"

    monkeypatch.setattr("platform.machine", lambda: "AARCH64")
    result = cli("package", "-f", "deb", "-o", str(tmp_path / "host"))
    assert result.rc == 0, result.err
    assert (tmp_path / "host/hello_1.0-1_arm64.deb").exists()

    monkeypatch.setattr("platform.machine", lambda: "m68k")
    result = cli("package", "-f", "deb", "-o", str(tmp_path / "bad"))
    assert result.rc == 2
    assert b"no architecture name for machine 'm68k': pass --arch" in result.err


@pytest.mark.posix
@pytest.mark.parametrize("kind", ["gz", "xz", "zst", "none"])
def test_deb_compressions(staged, cli, tmp_path, kind):
    result = _package(cli, tmp_path / "out", "--compression", kind)
    assert result.rc == 0, result.err
    suffix = "" if kind == "none" else f".{kind}"
    path = tmp_path / "out" / HELLO
    assert [member[0] for member in _ar(path)] == [
        "debian-binary",
        f"control.tar{suffix}",
        f"data.tar{suffix}",
    ]
    _raw, members = _tar(path, "data.tar")
    assert "./usr/bin/hello" in members
    assert "./control" in _tar(path, "control.tar")[1]


@pytest.mark.posix
def test_deb_reproducible(staged, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
    assert _package(cli, tmp_path / "a").rc == 0
    assert _package(cli, tmp_path / "b").rc == 0
    for name in (HELLO, DOC):
        assert (tmp_path / "a" / name).read_bytes() == (
            tmp_path / "b" / name
        ).read_bytes()
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH + 1))
    assert _package(cli, tmp_path / "c").rc == 0
    assert (tmp_path / "c" / HELLO).read_bytes() != (
        tmp_path / "a" / HELLO
    ).read_bytes()


@pytest.mark.posix
def test_deb_needs_keys(work, cli, tmp_path):
    _edit_cfg(
        work, ('maintainer = "Example Maintainer <maintainer@example.org>"\n', "")
    )
    _stage(cli)
    result = _package(cli, tmp_path / "out")
    assert result.rc == 2
    assert b"deb needs [package] maintainer" in result.err


@pytest.mark.posix
def test_deb_library_call(staged, tmp_path):
    project = Project.from_config(
        staged / "pkgforge.cfg",
        buildroot=tmp_path / "root",
        db=tmp_path / "f.jsonl",
    )
    paths = project.package("deb", tmp_path / "lib")
    assert [path.name for path in paths] == [HELLO, DOC]


_HARDLINKS = "".join(
    f'\n[[stage]]\nsrc = "src/README"\ndest = "/opt/{name}"\nmethod = "link"\n'
    'mode = "644"\n'
    for name in ("h1", "h2")
)


@pytest.mark.posix
def test_deb_hardlinks(work, cli, tmp_path):
    _edit_cfg(work, append=_HARDLINKS)
    _stage(cli)
    assert _package(cli, tmp_path / "out").rc == 0
    hello = tmp_path / "out" / HELLO
    _raw, payload = _tar(hello, "data.tar")
    first, second = payload["./opt/h1/README"], payload["./opt/h2/README"]
    assert first.isreg() and first.size > 0
    assert second.islnk() and second.size == 0
    assert second.linkname == "./opt/h1/README"
    sums = _text(hello, "./md5sums")
    assert sums.count("  opt/h1/README\n") == sums.count("  opt/h2/README\n") == 1
    values = dict(_fields(_text(hello, "./control")))
    assert int(values["Installed-Size"]) == _kib(
        member for member in payload.values() if not member.islnk()
    )


def test_deb_ar_member_longer_than_the_header_holds_is_refused():
    out = io.BytesIO()
    with pytest.raises(DumpError, match=r"data\.tar\.xz is 10000000000 bytes"):
        with _ar_member(out, "data.tar.xz", 0) as member:
            # Seeking past the end stands in for writing ten billion bytes.
            member.seek(member.tell() + 10_000_000_000)
    assert out.getvalue()[48:58] == b" " * 10
