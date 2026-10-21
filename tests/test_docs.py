"""Guards that pin the docs site and the shipped API header to the CLI's
real behavior, so they cannot drift from it: the quick-start blocks stay
identical and private, every documented ``pkgforge`` invocation actually
parses, the shipped sequences actually run, README/header links stay
absolute (PyPI has no repo to resolve them against), the API reference
covers the full public surface, and the header documents every exported
name plus one exit-status section.
"""

from __future__ import annotations

import importlib
import inspect
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import pkgforge

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
INDEX = ROOT / "docs" / "index.md"
UNATTENDED = ROOT / "docs" / "guide" / "unattended.md"
COMMANDS = ROOT / "docs" / "guide" / "commands.md"
CONFIG_GUIDE = ROOT / "docs" / "guide" / "config.md"
PACKAGING = ROOT / "docs" / "guide" / "packaging.md"
HEADER = ROOT / "src" / "pkgforge" / "AGENTS.md"

# Pages whose `pkgforge ...` examples must parse.
DOC_PAGES = [
    README,
    INDEX,
    ROOT / "docs" / "guide" / "install.md",
    ROOT / "docs" / "guide" / "commands.md",
    CONFIG_GUIDE,
    UNATTENDED,
    ROOT / "docs" / "guide" / "file-db.md",
    ROOT / "docs" / "guide" / "formats.md",
    ROOT / "docs" / "guide" / "exclude.md",
    PACKAGING,
]

_CUT_TOKENS = {"|", ">", "<", ";", "&&"}

API_DIR = ROOT / "docs" / "api"

# mkdocstrings targets (`module.qualname`) the API reference must cover:
# ``pkgforge.__all__`` (bar ``__version__`` and re-exports), plus the backends
# and commands the shipped header names but ``__all__`` exposes only as modules.
REQUIRED_API_TARGETS = (
    "pkgforge.project.Project",
    "pkgforge.project.VerifyIssue",
    "pkgforge.config.ProjectConfig",
    "pkgforge.config.PackageInfo",
    "pkgforge.config.SubPackageInfo",
    "pkgforge.config.RpmInfo",
    "pkgforge.config.DebianInfo",
    "pkgforge.config.PacmanInfo",
    "pkgforge.config.ApkInfo",
    "pkgforge.config.EntryDefaults",
    "pkgforge.config.EntryRule",
    "pkgforge.config.StageStep",
    "pkgforge.config.UserAccount",
    "pkgforge.config.GroupAccount",
    "pkgforge.config.load",
    "pkgforge.exceptions.PkgForgeError",
    "pkgforge.exceptions.UsageError",
    "pkgforge.entry.FileType",
    "pkgforge.entry.FileKind",
    "pkgforge.entry.FileEntry",
    "pkgforge.entry.entry_from_path",
    "pkgforge.entry.resolve_entry",
    "pkgforge.entry.apply_entry",
    "pkgforge.entry.normalize_mode",
    "pkgforge.entry.normalize_kind",
    "pkgforge.entry.normalize_caps",
    "pkgforge.entry.normalize_package",
    "pkgforge.entry.mode_to_octal",
    "pkgforge.exceptions.DbError",
    "pkgforge.db.DbProvider",
    "pkgforge.db.open_db",
    "pkgforge.db.format_for_suffix",
    "pkgforge.db.jsonl_line",
    "pkgforge.db.parse_jsonl",
    "pkgforge.db.JSONLinesDb",
    "pkgforge.db.YAMLDb",
    "pkgforge.db.SQLiteDb",
    "pkgforge.exclude.PathMatchStmt",
    "pkgforge.exclude.PathMatch",
    "pkgforge.exclude.PathTest",
    "pkgforge.exceptions.ExcludeSyntaxError",
    "pkgforge.exceptions.DumpError",
    "pkgforge.exceptions.InvalidEntryError",
    "pkgforge.exceptions.UnsupportedOutputError",
    "pkgforge.dbdump.dump",
    "pkgforge.dbdump.DumpContext",
    "pkgforge.dbdump.DumpFormat",
    "pkgforge.dbdump.PerEntryFormat",
    "pkgforge.dbdump.MultiArtifactFormat",
    "pkgforge.dbdump.RPMSpecFiles",
    "pkgforge.dbdump.RPMSpecFilesPre419",
    "pkgforge.dbdump.RPMSpec",
    "pkgforge.dbdump.RPMSpecPre419",
    "pkgforge.dbdump.DebianFiles",
    "pkgforge.package.PackageFormat",
    "pkgforge.package.BuildContext",
    "pkgforge.package.PackagePayload",
    "pkgforge.package.PackageWriter",
    "pkgforge.package.build",
    "pkgforge.package.ArchFormat",
    "pkgforge.package.ApkFormat",
    "pkgforge.package.DebFormat",
    "pkgforge.package.RpmFormat",
    "pkgforge.package.TarFormat",
    "pkgforge.testing.check_db_provider",
    "pkgforge.testing.check_dump_format",
)


def _fence_after(heading: str, text: str, index: int = 0) -> str:
    """Body of the ``index``-th fenced code block appearing after ``heading``."""
    pos = text.index(heading)
    fences = re.findall(r"```[^\n]*\n(.*?)```", text[pos:], re.DOTALL)
    return fences[index]


def _fences(text: str, langs=("sh", "bash")):
    for lang, body in re.findall(r"```(\w*)\n(.*?)```", text, re.DOTALL):
        if lang in langs:
            yield body


def _pkgforge_lines(text: str):
    """Every logical (backslash-joined) ``pkgforge ...`` line in a sh/bash fence."""
    for fence in _fences(text):
        raw_lines = fence.splitlines()
        joined = []
        buf = ""
        for line in raw_lines:
            buf += line
            if buf.rstrip().endswith("\\"):
                buf = buf.rstrip()[:-1] + " "
                continue
            joined.append(buf)
            buf = ""
        if buf:
            joined.append(buf)
        for line in joined:
            stripped = line.strip()
            if stripped == "pkgforge" or stripped.startswith("pkgforge "):
                yield stripped


def _doc_argv(line: str):
    """``line``'s argv (dropping ``pkgforge``), or ``None`` if it should be skipped."""
    if "[" in line or "..." in line or "…" in line:
        return None
    tokens = shlex.split(line, comments=True)
    for i, tok in enumerate(tokens):
        if tok in _CUT_TOKENS:
            tokens = tokens[:i]
            break
    argv = tokens[1:]  # drop "pkgforge"
    for tok in argv:
        if tok.isalpha() and tok.isupper() and len(tok) > 1:
            return None
    return argv


_SNIPPET = re.compile(r'^--8<-- "([^"]+)"$', re.MULTILINE)


def test_cfg_examples_load(tmp_path):
    """Every ``toml`` fence of the project-file guide and the README is a whole
    ``pkgforge.cfg`` that loads."""
    from pkgforge.config import load

    checked = 0
    for page in (CONFIG_GUIDE, README):
        text = page.read_text(encoding="utf-8")
        for body in _fences(text, langs=("toml",)):
            # The docs build expands a snippet directive with the file it names.
            body = _SNIPPET.sub(
                lambda m: (ROOT / m.group(1)).read_text(encoding="utf-8"), body
            )
            cfg = tmp_path / "pkgforge.cfg"
            cfg.write_bytes(body.encode("utf-8"))
            try:
                load(cfg)
            except Exception as exc:
                raise AssertionError(f"{page.name}: {body!r} -> {exc}") from exc
            checked += 1
    assert checked >= 2


def test_quickstart_blocks_identical_and_private():
    readme_fence = _fence_after("## Quick start", README.read_text(encoding="utf-8"))
    index_fence = _fence_after("## At a glance", INDEX.read_text(encoding="utf-8"))
    assert readme_fence == index_fence
    for fence in (readme_fence, index_fence):
        assert 'work="$(mktemp -d)"' in fence
        assert 'PKGFORGE_ROOT="$work/' in fence
        assert "/tmp/" not in fence


def test_doc_examples_parse(tmp_path, monkeypatch):
    from pkgforge.cli import PkgForge

    # --config checks that its file exists; the examples name pkgforge.cfg.
    (tmp_path / "pkgforge.cfg").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    parser = PkgForge._parser_()
    checked = 0
    for page in DOC_PAGES:
        text = page.read_text(encoding="utf-8")
        for line in _pkgforge_lines(text):
            argv = _doc_argv(line)
            if argv is None:
                continue
            checked += 1
            try:
                parser.parse_args(argv)
            except SystemExit as exc:
                assert exc.code in (
                    0,
                    None,
                ), f"{page.name}: {line!r} -> exit {exc.code}"
    assert checked  # the pages above must actually contain checkable lines


@pytest.mark.posix
@pytest.mark.parametrize("page", [README, UNATTENDED], ids=["readme", "unattended"])
def test_doc_sequences_run(page, tmp_path, monkeypatch):
    text = page.read_text(encoding="utf-8")
    anchor = "## Quick start" if page is README else "A typical staging sequence"
    block = _fence_after(anchor, text)
    # Docs with a fixed /tmp path must fail this test unrun, not silently pass.
    assert 'work="$(mktemp -d)"' in block
    assert 'PKGFORGE_ROOT="$work/' in block

    bash = None
    for candidate in ("bash",):
        from shutil import which

        bash = which(candidate)
        if bash:
            break
    if not bash:
        pytest.skip("bash not on PATH")

    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "tool").write_text(
        "#!/bin/sh\necho hello\n", encoding="utf-8"
    )
    (tmp_path / "tool.conf").write_text("key = value\n", encoding="utf-8")
    (tmp_path / "config").write_text("key = value\n", encoding="utf-8")
    share = tmp_path / "share"
    share.mkdir()
    (share / "data.txt").write_text("data\n", encoding="utf-8")

    env = dict(os.environ)
    env["TMPDIR"] = str(tmp_path)
    env["PATH"] = os.pathsep.join(
        [os.path.dirname(sys.executable), env.get("PATH", "")]
    )
    result = subprocess.run(
        [bash, "-ec", block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr


def _can_build_deb() -> bool:
    import grp
    import pwd
    from shutil import which

    for tool in ("dpkg-buildpackage", "fakeroot", "dh_installdirs", "dh_fixperms"):
        if which(tool) is None:
            return False
    try:
        pwd.getpwnam("daemon")
        grp.getgrnam("adm")
    except KeyError:
        return False
    return True


@pytest.mark.posix
def test_packaging_walkthrough(tmp_path):
    """The packaging guide's commands run top to bottom and produce its packages.

    The staging, ``verify``, dump and ``package -f deb|arch|apk|tar`` steps
    always run; the ``rpmbuild``, ``package -f rpm`` and ``dpkg-buildpackage``
    steps run when the host has the tools.
    """
    from shutil import which

    text = PACKAGING.read_text(encoding="utf-8")
    have_rpm = which("rpmbuild") is not None
    have_deb = _can_build_deb()
    steps = []
    for fence in _fences(text, langs=("sh",)):
        if "rpmbuild" in fence:
            if not have_rpm:
                # The spec is still written; only the build needs rpm.
                fence = "\n".join(
                    line for line in fence.splitlines() if "rpmbuild" not in line
                )
            else:
                version = subprocess.run(
                    ["rpmbuild", "--version"], capture_output=True, text=True
                ).stdout
                m = re.search(r"(\d+)\.(\d+)", version)
                if m and (int(m.group(1)), int(m.group(2))) < (4, 19):
                    fence = fence.replace("-f rpmspec ", "-f rpmspec-pre419 ")
        elif "package -f rpm" in fence and not have_rpm:
            fence = "\n".join(
                line for line in fence.splitlines() if "package -f rpm" not in line
            )
        elif "dpkg-buildpackage" in fence:
            if not have_deb:
                continue
            # dh_strip_nondeterminism is a separate package some hosts lack.
            fence = fence.replace(
                "dpkg-buildpackage",
                "(command -v dh_strip_nondeterminism >/dev/null"
                " || printf '\\noverride_dh_strip_nondeterminism:\\n' >> debian/rules)\n"
                "dpkg-buildpackage",
            )
        steps.append(fence.replace("~/src/pkgforge", str(ROOT)))
    assert len(steps) >= 3
    assert "cp -r" in steps[0]  # the first step copies the example in

    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home))
    env["PATH"] = os.pathsep.join(
        [os.path.dirname(sys.executable), env.get("PATH", "")]
    )
    env.pop("PKGFORGE_CONFIG", None)
    env.pop("PKGFORGE_DB", None)
    env.pop("PKGFORGE_ROOT", None)
    result = subprocess.run(
        ["bash", "-ec", "\n".join(steps)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    project = tmp_path / "work" / "hello"
    assert (project / "lists" / "files-hello-doc.txt").is_file()
    assert (project / "hello.spec").is_file()
    assert (project / "debian" / "control").is_file() == have_deb
    dist = project / "dist"
    for name in (
        "hello_1.0-1_all.deb",
        "hello-doc_1.0-1_all.deb",
        "hello-1.0-1-any.pkg.tar.zst",
        "hello-doc-1.0-1-any.pkg.tar.zst",
        "hello-1.0-r1.apk",
        "hello-doc-1.0-r1.apk",
        "hello-1.0-1.tar.gz",
        "hello-doc-1.0-1.tar.gz",
    ):
        assert (dist / name).is_file(), name
    assert (dist / "hello-1.0-1.noarch.rpm").is_file() == have_rpm
    if have_rpm:
        rpms = home / "rpmbuild" / "RPMS" / "noarch"
        assert (rpms / "hello-1.0-1.noarch.rpm").is_file()
        assert (rpms / "hello-doc-1.0-1.noarch.rpm").is_file()
    if have_deb:
        assert (tmp_path / "work" / "hello_1.0-1_all.deb").is_file()
        assert (tmp_path / "work" / "hello-doc_1.0-1_all.deb").is_file()


@pytest.mark.posix
def test_kind_example(tmp_path, monkeypatch, cli):
    text = README.read_text(encoding="utf-8")
    line = next(
        line
        for line in _pkgforge_lines(text)
        if "-k config-noreplace" in line and "install" in line
    )
    argv = shlex.split(line, comments=True)[1:]  # drop "pkgforge"

    (tmp_path / "tool.conf").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    globalopts = [
        "--buildroot",
        str(tmp_path / "root"),
        "--db",
        str(tmp_path / "db.jsonl"),
    ]

    result = cli(*globalopts, *argv)
    assert result.rc == 0, result.err

    dump = cli(*globalopts, "dbdump", "-f", "rpmspecfiles", "-")
    assert dump.rc == 0, dump.err
    lines = dump.out.decode().splitlines()
    assert any(l.startswith("%config(noreplace) %attr(640,") for l in lines), lines


def _strip_code(text: str) -> str:
    """Remove fenced code blocks, then inline code spans (which may wrap lines)."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`]*`", "", text, flags=re.DOTALL)


@pytest.mark.parametrize("page", [README, HEADER], ids=["readme", "header"])
def test_shipped_docs_links_absolute(page):
    text = _strip_code(page.read_text(encoding="utf-8"))
    targets = re.findall(r"\]\(([^)]*)\)", text)
    if page is README:
        assert targets  # the README must actually hold markdown links
    bad = [t for t in targets if not (t.startswith("https://") or t.startswith("#"))]
    assert not bad, bad


def _api_targets() -> list:
    text = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(API_DIR.glob("*.md"))
    )
    return re.findall(r"^::: (\S+)", text, re.MULTILINE)


def test_api_reference_covers_all():
    targets = _api_targets()
    assert targets  # the API pages must hold at least the ones checked below

    missing = [t for t in REQUIRED_API_TARGETS if t not in targets]
    assert not missing, missing

    private = [t for t in targets if any(s.startswith("_") for s in t.split("."))]
    assert not private, private

    # Every class/function pkgforge re-exports at the top level must be
    # covered too, so a future addition to __all__ can't silently skip it.
    for name in pkgforge.__all__:
        obj = getattr(pkgforge, name)
        if inspect.isclass(obj) or inspect.isfunction(obj):
            qualified = f"{obj.__module__}.{obj.__qualname__}"
            assert qualified in targets, qualified


def test_api_reference_docstrings():
    for target in REQUIRED_API_TARGETS:
        module_name, qualname = target.rsplit(".", 1)
        obj = getattr(importlib.import_module(module_name), qualname)
        assert obj.__doc__, target


def test_header_documents_all():
    text = HEADER.read_text(encoding="utf-8")
    for name in pkgforge.__all__:
        obj = getattr(pkgforge, name, None)
        if inspect.ismodule(obj):
            continue  # the leaf-command submodules, documented as a group
        pattern = re.compile(r"\*\*`[\w.]*" + re.escape(name) + r"(?!\w)")
        assert pattern.search(text), name
    assert text.count("## Exit status") == 1
