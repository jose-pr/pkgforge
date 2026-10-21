"""``pkgforge package -f rpm``: the spec, the payload and the rpmbuild run,
with ``rpmbuild`` replaced by a recorder so no rpm tooling is needed. The
packages rpmbuild really builds are checked in ``test_consumers.py``."""

from __future__ import annotations

import logging
import shutil
import tarfile
import typing
from pathlib import Path

import pytest

from pkgforge.config import ProjectConfig
from pkgforge.dbdump._rpmspec import _spec
from pkgforge.exceptions import PkgForgeError
from pkgforge.package import _rpm

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
HELLO = "hello-1.0-1.noarch.rpm"
DOC = "hello-doc-1.0-1.noarch.rpm"


@pytest.fixture
def staged(tmp_path, monkeypatch, cli):
    """``examples/pkgforge.cfg`` staged into a build root beside it."""
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy(EXAMPLES / "pkgforge.cfg", work / "pkgforge.cfg")
    shutil.copytree(EXAMPLES / "src", work / "src")
    monkeypatch.chdir(work)
    monkeypatch.delenv("PKGFORGE_CONFIG", raising=False)
    monkeypatch.setenv("PKGFORGE_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("PKGFORGE_DB", str(tmp_path / "f.jsonl"))
    (tmp_path / "root").mkdir()
    result = cli("stage", "--init")
    assert result.rc == 0, result.err
    return work


class Recorder:
    """A stand-in for ``rpmbuild``: records what it was given and writes one
    rpm per package into ``RPMS/noarch``."""

    def __init__(self) -> None:
        self.argv: typing.List[str] = []
        self.top: typing.Optional[Path] = None
        self.spec = ""
        self.payload: typing.List[str] = []
        self.lists: typing.Dict[str, str] = {}
        self.fail = False

    def __call__(self, argv: typing.Sequence[str], *, what: str) -> None:
        assert what == "package -f rpm"
        self.argv = list(argv)
        defines = {
            word.partition(" ")[0]: word.partition(" ")[2]
            for flag, word in zip(argv, argv[1:])
            if flag == "--define"
        }
        self.top = Path(defines["_topdir"])
        self.spec = Path(argv[-1]).read_bytes().decode("utf-8")
        with tarfile.open(defines["pkgforge_payload"]) as archive:
            self.payload = archive.getnames()
        lists = Path(defines["pkgforge_lists"])
        self.lists = {
            path.name: path.read_bytes().decode("utf-8")
            for path in sorted(lists.iterdir())
        }
        if self.fail:
            raise PkgForgeError("package -f rpm: rpmbuild failed (exit 1): boom")
        built = self.top / "RPMS" / "noarch"
        built.mkdir()
        for name in (HELLO, DOC):
            (built / name).write_bytes(b"rpm " + name.encode("ascii"))


@pytest.fixture
def recorder(monkeypatch):
    """``_rpm`` with a recorder for rpmbuild, rpm 4.18 and systemd macros."""
    fake = Recorder()
    monkeypatch.setattr(_rpm.shutil, "which", lambda name, *a, **k: "/bin/rpmbuild")
    monkeypatch.setattr(_rpm, "_rpm_version", lambda rpmbuild: (4, 18))
    monkeypatch.setattr(_rpm, "_eval", lambda rpmbuild, expression: "1")
    monkeypatch.setattr(_rpm, "_run_tool", fake)
    return fake


def _warnings(caplog) -> typing.List[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING"
    ]


def _package(cli, out: Path, *args: str):
    return cli("package", "-f", "rpm", "-o", str(out), *args)


@pytest.mark.posix
def test_rpm_moves_the_packages_into_the_output(staged, recorder, cli, tmp_path):
    result = _package(cli, tmp_path / "out")
    assert result.rc == 0, result.err
    assert result.out.decode().splitlines() == [
        str(tmp_path / "out" / HELLO),
        str(tmp_path / "out" / DOC),
    ]
    assert (tmp_path / "out" / DOC).read_bytes() == b"rpm " + DOC.encode("ascii")
    assert recorder.argv[:2] == ["/bin/rpmbuild", "-bb"]
    assert "--target" not in recorder.argv


@pytest.mark.posix
def test_rpm_replaces_a_package_of_the_same_name(staged, recorder, cli, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / HELLO).write_bytes(b"old")
    assert _package(cli, out).rc == 0
    assert (out / HELLO).read_bytes() == b"rpm " + HELLO.encode("ascii")


@pytest.mark.posix
def test_rpm_arch_is_the_build_target(staged, recorder, cli, tmp_path):
    assert _package(cli, tmp_path / "out", "--arch", "aarch64").rc == 0
    index = recorder.argv.index("--target")
    assert recorder.argv[index + 1] == "aarch64"


@pytest.mark.posix
def test_rpm_spec_unpacks_the_payload_instead_of_staging(
    staged, recorder, cli, tmp_path
):
    assert _package(cli, tmp_path / "out").rc == 0
    spec = recorder.spec
    assert "pkgforge_config" not in spec and "pkgforge stage" not in spec
    assert 'tar -C "%{buildroot}" -xpf "%{pkgforge_payload}"' in spec
    assert 'cp "%{pkgforge_lists}"/files-*.txt' in spec
    assert "%{?systemd_post:%systemd_post hello.service}" in spec
    assert "\n%files -f %{_builddir}/pkgforge-files/files-hello.txt\n" in spec
    assert "\n%files -n hello-doc -f " in spec


@pytest.mark.posix
def test_rpm_payload_holds_the_recorded_files_without_ghosts(
    staged, recorder, cli, tmp_path
):
    assert _package(cli, tmp_path / "out").rc == 0
    names = recorder.payload
    assert names == sorted(names)
    assert "usr/bin/hello" in names and "usr/share/doc/hello-doc/README" in names
    assert "run/hello" not in names and "var/log/hello.log" not in names
    # The lists decide which directories rpm owns, so none is added.
    assert "usr" not in names and "usr/share" not in names
    assert set(recorder.lists) == {"files-hello.txt", "files-hello-doc.txt"}
    assert "/usr/share/doc/hello-doc/README" in recorder.lists["files-hello-doc.txt"]


@pytest.mark.posix
@pytest.mark.parametrize(
    "version, creates_accounts",
    [((4, 18), True), ((4, 19), False), ((6, 0), False)],
)
def test_rpm_variant_follows_the_rpm_version(
    staged, recorder, cli, tmp_path, monkeypatch, version, creates_accounts
):
    monkeypatch.setattr(_rpm, "_rpm_version", lambda rpmbuild: version)
    assert _package(cli, tmp_path / "out").rc == 0
    assert ("systemd-sysusers - <<" in recorder.spec) is creates_accounts
    # The pre-4.19 lists quote nothing: a plain path has no quotes.
    assert (
        '"/usr/bin/hello"' in recorder.lists["files-hello.txt"]
    ) is not creates_accounts


@pytest.mark.posix
def test_rpm_warns_where_rpm_has_no_systemd_macros(
    staged, recorder, cli, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(_rpm, "_eval", lambda rpmbuild, expression: "")
    with caplog.at_level(logging.WARNING, logger="pkgforge.package"):
        assert _package(cli, tmp_path / "out").rc == 0
    assert _warnings(caplog) == [
        "rpm has no systemd macros: the unit scriptlets are left out "
        "(install systemd-rpm-macros)"
    ]


@pytest.mark.posix
def test_rpm_is_quiet_with_systemd_macros(staged, recorder, cli, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="pkgforge.package"):
        assert _package(cli, tmp_path / "out").rc == 0
    assert not _warnings(caplog)


@pytest.mark.posix
def test_rpm_does_not_warn_without_units(
    staged, recorder, cli, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(_rpm, "_eval", lambda rpmbuild, expression: "")
    cfg = staged / "pkgforge.cfg"
    text = cfg.read_bytes().decode("utf-8")
    block = '[[stage]]\nsrc = "src/hello.service"\ndest = "/usr/lib/systemd/system"\nmode = "644"\n'
    assert text.count(block) == 1
    cfg.write_bytes(text.replace(block, "").encode("utf-8"))
    assert cli("stage", "--init").rc == 0
    with caplog.at_level(logging.WARNING, logger="pkgforge.package"):
        assert _package(cli, tmp_path / "out").rc == 0
    assert not _warnings(caplog)
    assert "systemd_" not in recorder.spec


@pytest.mark.posix
def test_rpm_removes_its_topdir_after_a_build(staged, recorder, cli, tmp_path):
    assert _package(cli, tmp_path / "out").rc == 0
    assert recorder.top is not None and not recorder.top.exists()


@pytest.mark.posix
def test_rpm_removes_its_topdir_after_a_failed_build(staged, recorder, cli, tmp_path):
    recorder.fail = True
    result = _package(cli, tmp_path / "out")
    assert result.rc == 1
    assert b"rpmbuild failed (exit 1): boom" in result.err
    assert recorder.top is not None and not recorder.top.exists()
    assert not list((tmp_path / "out").glob("*.rpm"))


@pytest.mark.posix
def test_rpm_without_rpmbuild_is_an_error(staged, cli, tmp_path, monkeypatch):
    monkeypatch.setattr(_rpm.shutil, "which", lambda name, *a, **k: None)
    result = _package(cli, tmp_path / "out")
    assert result.rc == 1
    assert b"package -f rpm needs rpmbuild on PATH" in result.err


@pytest.mark.posix
def test_rpm_takes_no_compression(staged, cli, tmp_path):
    result = _package(cli, tmp_path / "out", "--compression", "gz")
    assert result.rc == 2
    assert b"rpm takes no --compression" in result.err


@pytest.mark.posix
def test_rpm_names_the_missing_keys(staged, recorder, cli, tmp_path):
    cfg = staged / "pkgforge.cfg"
    text = cfg.read_bytes().decode("utf-8")
    assert text.count('license = "MIT"\n') == 1
    cfg.write_bytes(text.replace('license = "MIT"\n', "").encode("utf-8"))
    result = _package(cli, tmp_path / "out")
    assert result.rc == 2
    assert b"rpm needs [package] license" in result.err


def test_rpm_spec_without_the_guard_has_no_config_define():
    config = ProjectConfig.from_dict(
        {
            "package": {
                "name": "tool",
                "version": "1.0",
                "summary": "A tool",
                "license": "MIT",
            }
        }
    )
    spec = _spec(
        config, [], fmt="rpm", sysusers_pre=False, install=["true\n"], guard=False
    )
    assert "pkgforge_config" not in spec
    assert spec.startswith("# Generated by pkgforge rpm.\n")
    assert "\n%install\ntrue\n" in spec
    guarded = _spec(
        config, [], fmt="rpmspec", sysusers_pre=False, install=["true\n"], guard=True
    )
    assert "%{!?pkgforge_config:" in guarded
