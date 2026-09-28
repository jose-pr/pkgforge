"""Tests for the ``install`` subcommand.

Every new ``install`` regression test from the staging-semantics rework lands
here (``tests/test_pkgforge.py`` keeps the tests that predate it). Tests that
need POSIX facilities (chmod, chown, symlinks, subprocess decompressors) are
marked ``@pytest.mark.posix``; a handful that only exercise argument parsing
or in-memory state run on any platform and say so in a comment.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from pkgforge.install import Install


@pytest.mark.posix
def test_install_file_copies_and_records(tmp_path):
    # install copies file sources; it no longer hardlinks them.
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src = tmp_path / "app.conf"
    src.write_text("hello")
    os.chmod(src, 0o644)

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "-m",
            "640",
            str(src),
            "/etc",
        ]
    )
    inst()

    staged = root / "etc" / "app.conf"
    assert staged.exists()
    # Not a hardlink: a distinct inode from the source.
    assert staged.stat().st_ino != src.stat().st_ino
    # -m never touches the source.
    assert (src.stat().st_mode & 0o777) == 0o644
    assert (staged.stat().st_mode & 0o777) == 0o640
    recorded = inst.loaddb()
    assert recorded["/etc/app.conf"]["mode"] == "640"


@pytest.mark.posix
def test_install_file_source_rewrite_keeps_staged(tmp_path):
    # Rewriting the source in place after install must not change the
    # already-staged copy (a hardlink would share the source's inode).
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src = tmp_path / "tool.conf"
    src.write_text("v1")

    parser = Install._parser_()
    parser.parse_args(
        ["--db", str(db), "--buildroot", str(root), "-p", str(src), "/etc"]
    )()

    src.write_text("v2-rebuilt")
    staged = root / "etc" / "tool.conf"
    assert staged.read_text() == "v1"


@pytest.mark.posix
def test_install_file_cross_filesystem_root(tmp_path):
    # A hardlink fails with EXDEV when the build root is on another
    # filesystem (e.g. the documented PKGFORGE_ROOT=/tmp/stage on tmpfs);
    # shutil.copy2 works across filesystems.
    shm = Path("/dev/shm")
    if not shm.is_dir():
        pytest.skip("/dev/shm not available")
    if os.stat(shm).st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("/dev/shm is on the same filesystem as tmp_path")

    root = Path(tempfile.mkdtemp(dir=shm))
    try:
        db = tmp_path / "files.jsonl"
        src = tmp_path / "app.bin"
        src.write_text("payload")

        parser = Install._parser_()
        parser.parse_args(
            ["--db", str(db), "--buildroot", str(root), "-p", str(src), "/opt"]
        )()

        staged = root / "opt" / "app.bin"
        assert staged.read_text() == "payload"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.posix
def test_install_non_owned_source(tmp_path):
    # A hardlink raised EPERM under fs.protected_hardlinks for a source the
    # user does not own; a copy never touches the source's ownership at all.
    candidate = None
    for name in ("true", "false", "env", "sh"):
        path = shutil.which(name)
        if path is None:
            continue
        st = os.stat(path)
        if st.st_uid != os.getuid():
            candidate = Path(path)
            break
    if candidate is None:
        pytest.skip("no non-owned, readable source found on PATH")

    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"

    parser = Install._parser_()
    parser.parse_args(
        ["--db", str(db), "--buildroot", str(root), "-p", str(candidate), "/opt"]
    )()

    staged = root / "opt" / candidate.name
    assert staged.exists()
    assert staged.read_bytes() == candidate.read_bytes()


# --------------------------------------------------------------------------
# Direct Python-API construction must not silently enable decompress
# --------------------------------------------------------------------------


def test_install_direct_construction_does_not_decompress_path():
    # Cross-platform: constructing Install() directly (not through the
    # parser) with no `decompress=` must keep the documented default
    # (False), not silently infer decompression the way a bare CLI -x does.
    inst = Install(source=Path("a.txt"), destination=Path("opt"))
    assert inst.decompress is False


def test_install_direct_construction_does_not_decompress_list():
    inst = Install(source=[Path("a.txt")], destination=Path("opt"))
    assert inst.decompress is False


@pytest.mark.posix
def test_install_direct_construction_never_runs_source(tmp_path):
    # Before the fix, leaving `decompress` out made __call__ infer a
    # decompressor from the source's suffix and run it as a subprocess --
    # for a `.sh` source that means executing it. Guard both the bare-Path
    # and the list form.
    root = tmp_path / "root"
    root.mkdir()
    marker = tmp_path / "marker"
    src = tmp_path / "hello.sh"
    src.write_text(f"#!/bin/sh\ntouch {marker}\necho ran\n")
    src.chmod(src.stat().st_mode | stat.S_IEXEC)

    for source in (src, [src]):
        if marker.exists():
            marker.unlink()
        Install(
            source=source,
            destination=Path("/usr/bin"),
            buildroot=root,
            parents=True,
            db=None,
        )()
        assert not marker.exists()
        staged = root / "usr" / "bin" / "hello.sh"
        assert staged.read_bytes() == src.read_bytes()


# --------------------------------------------------------------------------
# -x runs only a known decompressor
# --------------------------------------------------------------------------


def _write_compressed(path: Path, kind: str, data: bytes) -> None:
    if kind == "gz":
        with gzip.open(path, "wb") as fh:
            fh.write(data)
    elif kind == "xz":
        with lzma.open(path, "wb", format=lzma.FORMAT_XZ) as fh:
            fh.write(data)
    elif kind == "bz2":
        with bz2.open(path, "wb") as fh:
            fh.write(data)
    elif kind == "lzma":
        with lzma.open(path, "wb", format=lzma.FORMAT_ALONE) as fh:
            fh.write(data)
    elif kind == "zst":
        zstd = shutil.which("zstd")
        if zstd is None:
            pytest.skip("zstd not available")
        subprocess.run(
            [zstd, "-q", "-f", "-o", os.fspath(path)], input=data, check=True
        )
    else:
        raise ValueError(kind)


@pytest.mark.posix
@pytest.mark.parametrize("alias", ["gzip", "gunzip"])
def test_install_decompress_alias(tmp_path, alias):
    # -x gzip is a COMPRESSOR name; it must still decompress, not compress
    # the already-compressed source.
    if shutil.which("gzip") is None:
        pytest.skip("gzip not available")
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "plain.gz"
    _write_compressed(src, "gz", b"hello world\n")

    parser = Install._parser_()
    parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-x",
            alias,
            str(src),
            "/a",
        ]
    )()

    staged = root / "a" / "plain"
    assert staged.read_bytes() == b"hello world\n"


@pytest.mark.posix
@pytest.mark.parametrize("kind", ["xz", "bz2", "zst", "lzma"])
def test_install_decompress_kinds(tmp_path, kind):
    tool = {"xz": "xz", "bz2": "bzip2", "zst": "zstd", "lzma": "xz"}[kind]
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not available")

    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / f"plain.{kind}"
    _write_compressed(src, kind, b"kind payload\n")

    parser = Install._parser_()
    parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-x",
            kind,
            str(src),
            "/a",
        ]
    )()

    staged = root / "a" / "plain"
    assert staged.read_bytes() == b"kind payload\n"


@pytest.mark.posix
@pytest.mark.parametrize("name", ["nosuffix", "notes.txt", "x.evil"])
def test_install_bare_x_unknown_suffix_refused(tmp_path, monkeypatch, name):
    # Bare -x on a suffix that names no known kind must raise, and -- unlike
    # the old suffix-as-command fallback -- never run anything on PATH.
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / name
    src.write_text("plain")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "evil-ran"
    evil = bindir / "evil"
    evil.write_text(f"#!/bin/sh\ntouch {marker}\n")
    evil.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            str(src),
            "/opt",
            "-x",
        ]
    )
    with pytest.raises(ValueError, match="cannot infer compression"):
        inst()

    assert not marker.exists()
    assert not (root / "opt").exists()


@pytest.mark.posix
def test_install_unknown_kind_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "a.bin"
    src.write_text("plain")

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-x",
            "lz4",
            str(src),
            "/opt",
        ]
    )
    with pytest.raises(ValueError, match="unknown compression kind"):
        inst()
    assert not (root / "opt").exists()


@pytest.mark.posix
def test_install_leading_dash_source(tmp_path, monkeypatch):
    if shutil.which("gzip") is None:
        pytest.skip("gzip not available")
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.chdir(tmp_path)
    src = Path("-v.gz")
    _write_compressed(src, "gz", b"dashed\n")

    parser = Install._parser_()
    parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-x",
            "gz",
            "./-v.gz",
            "/opt",
        ]
    )()

    staged = root / "opt" / "-v"
    assert staged.read_bytes() == b"dashed\n"


def test_install_decompress_from_stdin_pipe(tmp_path, monkeypatch):
    # x-plat (relative DESTINATION): a real gzip'd file opened and handed to
    # install as sys.stdin, decompressed via an explicit kind.
    if shutil.which("gzip") is None:
        pytest.skip("gzip not available")
    root = tmp_path / "root"
    root.mkdir()
    gz_path = tmp_path / "data.gz"
    _write_compressed(gz_path, "gz", b"piped content\n")

    fh = open(gz_path, "rb")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(fh))
    try:
        parser = Install._parser_()
        parser.parse_args(
            [
                "--db",
                str(tmp_path / "files.jsonl"),
                "--buildroot",
                str(root),
                "-p",
                "-x",
                "gz",
                "-T",
                "-",
                "out",
            ]
        )()
    finally:
        fh.close()

    staged = root / "out"
    assert staged.read_bytes() == b"piped content\n"


def test_install_decompressor_missing_is_one_line(tmp_path, monkeypatch, cli):
    # x-plat (relative DESTINATION): a missing decompressor is one
    # `pkgforge: error:` line naming the tool, and nothing is staged.
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "a.gz"
    src.write_bytes(b"not really gzip, but the tool lookup fails first anyway")
    db = tmp_path / "files.jsonl"

    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-p",
        "-x",
        "gz",
        str(src),
        "out",
    )
    assert result.rc == 1
    lines = [line for line in result.err.decode().splitlines() if line]
    assert len(lines) == 1
    assert "pkgforge: error:" in lines[0]
    assert "gzip" in lines[0]
    assert "not found on PATH" in lines[0]
    assert not (root / "out").exists()


# --------------------------------------------------------------------------
# stdin ('-') and stream sources
# --------------------------------------------------------------------------


class _FakeTTY:
    """A minimal stand-in for a terminal `sys.stdin`: only `isatty()` matters
    to `_require_stdin`, so nothing else needs to be implemented."""

    def isatty(self):
        return True


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_install_stdin_tty_refused(tmp_path, monkeypatch, kind):
    # x-plat (relative DESTINATION), patched sys.stdin: a terminal stdin is
    # a usage error for both a file and a directory ('-d') '-' source,
    # raised before anything is staged.
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(sys, "stdin", _FakeTTY())

    argv = ["--db", str(tmp_path / "files.jsonl"), "--buildroot", str(root), "-p"]
    if kind == "directory":
        argv += ["-d"]
    argv += ["-T", "-", "out"]

    parser = Install._parser_()
    inst = parser.parse_args(argv)
    with pytest.raises(ValueError, match="terminal"):
        inst()
    assert not (root / "out").exists()


def test_install_stdin_closed_refused(tmp_path, monkeypatch):
    # x-plat: sys.stdin is None (a process started with stdin closed).
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(sys, "stdin", None)

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-T",
            "-",
            "out",
        ]
    )
    with pytest.raises(ValueError, match="closed"):
        inst()
    assert not (root / "out").exists()


def test_install_stdin_keeps_fd0_open(tmp_path, monkeypatch):
    # x-plat: reading '-' must not close the underlying stream (the old
    # os.fdopen(sys.stdin.fileno()) wrapping did, taking fd 0 with it).
    root = tmp_path / "root"
    root.mkdir()
    wrapped = io.TextIOWrapper(io.BytesIO(b"stdin data"))
    monkeypatch.setattr(sys, "stdin", wrapped)

    parser = Install._parser_()
    parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-T",
            "-",
            "out",
        ]
    )()

    assert not wrapped.closed
    assert (root / "out").read_bytes() == b"stdin data"


def test_install_stdin_empty_stages_empty_file(tmp_path, monkeypatch):
    # x-plat: empty stdin (e.g. /dev/null or an empty pipe) is legitimate
    # input, not an error -- it stages a real, empty file.
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))

    parser = Install._parser_()
    parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-T",
            "-",
            "out",
        ]
    )()

    staged = root / "out"
    assert staged.exists()
    assert staged.read_bytes() == b""


def test_install_stdin_twice_refused(tmp_path):
    # x-plat: the stream can only be consumed once, so two '-' sources in
    # one invocation are rejected before any cloning or staging.
    root = tmp_path / "root"
    root.mkdir()

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-D",
            "-",
            "-",
            "out",
        ]
    )
    with pytest.raises(ValueError, match="only one source"):
        inst()


def test_install_stdin_needs_T(tmp_path):
    # x-plat: 06's error for a bare '-' without -T/-D must still fire.
    root = tmp_path / "root"
    root.mkdir()

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-",
            "out",
        ]
    )
    with pytest.raises(ValueError, match="needs -T"):
        inst()


@pytest.mark.posix
def test_install_fifo_source_staged_as_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    data = b"fifo payload\n"

    def _writer():
        with open(fifo, "wb") as w:
            w.write(data)

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()
    try:
        parser = Install._parser_()
        inst = parser.parse_args(
            [
                "--db",
                str(tmp_path / "files.jsonl"),
                "--buildroot",
                str(root),
                "-p",
                str(fifo),
                "/opt",
            ]
        )
        inst()
    finally:
        writer.join(timeout=5)

    staged = root / "opt" / "pipe"
    assert staged.read_bytes() == data
    assert inst.loaddb()["/opt/pipe"]["type"] == "file"


@pytest.mark.posix
def test_install_pipe_fd_source_staged_as_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    read_fd, write_fd = os.pipe()
    data = b"pipe payload\n"

    def _writer():
        os.write(write_fd, data)
        os.close(write_fd)

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()
    try:
        src = Path(f"/dev/fd/{read_fd}")
        parser = Install._parser_()
        parser.parse_args(
            [
                "--db",
                str(tmp_path / "files.jsonl"),
                "--buildroot",
                str(root),
                "-p",
                str(src),
                "/opt",
            ]
        )()
    finally:
        writer.join(timeout=5)
        os.close(read_fd)

    staged = root / "opt" / str(read_fd)
    assert staged.read_bytes() == data


@pytest.mark.posix
def test_install_dev_tty_source_refused(tmp_path):
    import pty

    master, slave = pty.openpty()
    try:
        # Preloaded so a reader that skips the isatty() check gets EOF
        # instead of hanging on an empty terminal.
        os.write(master, b"x\n\x04")
        slave_path = Path(os.ttyname(slave))

        root = tmp_path / "root"
        root.mkdir()
        parser = Install._parser_()
        inst = parser.parse_args(
            [
                "--db",
                str(tmp_path / "files.jsonl"),
                "--buildroot",
                str(root),
                "-p",
                str(slave_path),
                "/opt",
            ]
        )
        with pytest.raises(ValueError, match="terminal"):
            inst()
        assert not (root / "opt").exists()
    finally:
        os.close(master)
        os.close(slave)


# --------------------------------------------------------------------------
# Multi-source install: entries stay apart, collisions are refused
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_install_symlink_meta_does_not_leak(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    link = tmp_path / "app.link"
    link.symlink_to("/usr/bin/app")
    regular = tmp_path / "regular.conf"
    regular.write_text("hi")

    # Through argv: a real symlink source followed by a regular file must
    # not carry the link's meta.target into the file's own entry.
    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            str(link),
            str(regular),
            "/opt",
        ]
    )
    inst()
    recorded = inst.loaddb()
    assert recorded["/opt/app.link"]["meta"]["target"] == "/usr/bin/app"
    assert "target" not in recorded["/opt/regular.conf"]["meta"]

    # The Python API caller's own meta dict must not be poisoned either.
    caller_meta = {"k": "v"}
    Install(
        source=[link, regular],
        destination=Path("/opt2"),
        buildroot=root,
        parents=True,
        db=None,
        meta=caller_meta,
    )()
    assert caller_meta == {"k": "v"}

    # Nor the shared class-level default a bare construction with no meta=
    # would otherwise poison for every later Install/parser built in the
    # same process.
    from pkgforge.common import FileEntryArgs

    Install(
        source=link, destination=Path("/opt3"), buildroot=root, parents=True, db=None
    )()
    assert FileEntryArgs.meta == {}


@pytest.mark.parametrize("mode", ["-D", "basename"])
def test_install_colliding_sources_refused(tmp_path, mode):
    # x-plat (relative DESTINATION): two distinct FILE sources resolving to
    # one destination exit 2 before anything is staged, whether that is
    # forced by -D or happens because both share a basename.
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"

    if mode == "-D":
        a = tmp_path / "a.txt"
        a.write_text("A")
        b = tmp_path / "b.txt"
        b.write_text("B")
        argv = [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "--remove-source",
            "-D",
            str(a),
            str(b),
            "out",
        ]
        sources = [a, b]
    else:
        suba = tmp_path / "suba"
        suba.mkdir()
        srca = suba / "x"
        srca.write_text("A")
        subb = tmp_path / "subb"
        subb.mkdir()
        srcb = subb / "x"
        srcb.write_text("B")
        argv = [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "--remove-source",
            str(srca),
            str(srcb),
            "out",
        ]
        sources = [srca, srcb]

    parser = Install._parser_()
    inst = parser.parse_args(argv)
    with pytest.raises(ValueError, match="resolve to"):
        inst()

    assert not (root / "out").exists()
    for src in sources:
        assert src.exists()  # --remove-source never ran: nothing was staged


@pytest.mark.posix
def test_install_directory_sources_still_merge(tmp_path):
    # Regression guard: several directory (or archive) sources sharing a
    # destination must keep merging -- only non-directory collisions
    # are refused.
    root = tmp_path / "root"
    root.mkdir()
    d1 = tmp_path / "d1"
    d1.mkdir()
    (d1 / "one").write_text("1")
    d2 = tmp_path / "d2"
    d2.mkdir()
    (d2 / "two").write_text("2")

    parser = Install._parser_()
    parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            "-D",
            str(d1),
            str(d2),
            "/opt/app",
        ]
    )()

    assert (root / "opt" / "app" / "one").read_text() == "1"
    assert (root / "opt" / "app" / "two").read_text() == "2"
