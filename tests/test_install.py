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
import tarfile
import tempfile
import threading
from pathlib import Path

import pytest

from pkgforge.common import UsageError
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


# --------------------------------------------------------------------------
# Atomicity: a failed install leaves the destination as it was; re-runs work
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_install_failed_decompress_leaves_nothing(tmp_path):
    if shutil.which("gzip") is None:
        pytest.skip("gzip not available")
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "bad.gz"
    src.write_bytes(b"not actually gzip data")

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-x",
            "gz",
            str(src),
            "/e",
        ]
    )
    with pytest.raises(subprocess.CalledProcessError):
        inst()

    # -p created the parent dir; the destination FILE itself must not exist.
    assert not (root / "e" / "bad").exists()
    assert not list(root.glob("**/*.pkgforge-tmp"))


@pytest.mark.posix
def test_install_failed_reinstall_keeps_previous_file(tmp_path):
    if shutil.which("gzip") is None:
        pytest.skip("gzip not available")
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    good_src = tmp_path / "good.gz"
    _write_compressed(good_src, "gz", b"hello\n")

    common = ["--db", str(db), "--buildroot", str(root), "-p", "-x", "gz"]
    Install._parser_().parse_args(common + [str(good_src), "/e"])()
    staged = root / "e" / "good"
    assert staged.read_bytes() == b"hello\n"

    good_src.write_bytes(b"now junk, not gzip")
    inst = Install._parser_().parse_args(common + [str(good_src), "/e"])
    with pytest.raises(subprocess.CalledProcessError):
        inst()

    assert staged.read_bytes() == b"hello\n"
    assert not list((root / "e").glob("*.pkgforge-tmp"))


@pytest.mark.posix
def test_install_bad_archive_leaves_nothing(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "f1").write_bytes(b"x" * 200_000)
    (payload / "f2").write_bytes(b"y" * 200_000)
    tar_path = tmp_path / "trunc.tar"
    with tarfile.open(tar_path, "w") as tf:
        tf.add(payload / "f1", arcname="f1")
        tf.add(payload / "f2", arcname="f2")
    data = tar_path.read_bytes()
    tar_path.write_bytes(data[: len(data) - 250_000])  # cut it mid-member

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-d",
            str(tar_path),
            "/tr",
        ]
    )
    with pytest.raises(tarfile.ReadError):
        inst()

    # -p created the parent dir; the extracted destination dir itself
    # (the ".tar" suffix stripped from the archive's basename) must not.
    assert not (root / "tr" / "trunc").exists()
    assert not list(root.glob("**/*.pkgforge-tmp"))


@pytest.mark.posix
def test_install_staged_modes_follow_umask(tmp_path):
    if shutil.which("gzip") is None:
        pytest.skip("gzip not available")
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    old_umask = os.umask(0o022)
    try:
        gz_src = tmp_path / "a.gz"
        _write_compressed(gz_src, "gz", b"data\n")
        Install._parser_().parse_args(
            [
                "--db",
                str(db),
                "--buildroot",
                str(root),
                "-p",
                "-x",
                "gz",
                str(gz_src),
                "/f",
            ]
        )()
        staged_file = root / "f" / "a"
        assert (staged_file.stat().st_mode & 0o777) == 0o644

        payload = tmp_path / "d"
        payload.mkdir()
        (payload / "x").write_text("x")
        tar_src = tmp_path / "d.tar"
        with tarfile.open(tar_src, "w") as tf:
            tf.add(payload, arcname=".")
        Install._parser_().parse_args(
            [
                "--db",
                str(db),
                "--buildroot",
                str(root),
                "-p",
                "-d",
                str(tar_src),
                "/g",
            ]
        )()
        staged_dir = root / "g"
        assert (staged_dir.stat().st_mode & 0o777) == 0o755
    finally:
        os.umask(old_umask)


@pytest.mark.posix
@pytest.mark.parametrize(
    "kind",
    [
        "file",
        "dir",
        "symlink_rel",
        "symlink_pkg_only",
        "type_symlink",
        "archive",
        "decompress",
    ],
)
def test_install_rerun_is_idempotent(tmp_path, kind):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    common = ["--db", str(db), "--buildroot", str(root), "-p"]

    if kind == "file":
        src = tmp_path / "f.conf"
        src.write_text("hello")
        argv = common + [str(src), "/e"]
        check = lambda: (root / "e" / "f.conf").read_text() == "hello"
    elif kind == "dir":
        d = tmp_path / "d"
        d.mkdir()
        (d / "a").write_text("A")
        argv = common + ["-d", "-D", str(d), "/opt/app"]
        check = lambda: (root / "opt" / "app" / "a").read_text() == "A"
    elif kind == "symlink_rel":
        link = tmp_path / "rel.link"
        link.symlink_to("reltarget")
        argv = common + [str(link), "/usr/bin"]
        check = lambda: os.readlink(root / "usr" / "bin" / "rel.link") == "reltarget"
    elif kind == "symlink_pkg_only":
        link = tmp_path / "pkg.link"
        link.symlink_to("/usr/bin/not-on-host-xyz")
        argv = common + [str(link), "/usr/bin"]
        check = (
            lambda: os.readlink(root / "usr" / "bin" / "pkg.link")
            == "/usr/bin/not-on-host-xyz"
        )
    elif kind == "type_symlink":
        argv = common + [
            "-T",
            "--type",
            "symlink",
            "-O",
            "target=/usr/bin/app",
            "-",
            "/usr/bin/app.link",
        ]
        check = lambda: os.readlink(root / "usr" / "bin" / "app.link") == "/usr/bin/app"
    elif kind == "archive":
        d = tmp_path / "archsrc"
        d.mkdir()
        (d / "a").write_text("A")
        tar_src = tmp_path / "t.tar"
        with tarfile.open(tar_src, "w") as tf:
            tf.add(d, arcname=".")
        argv = common + ["-d", "-D", str(tar_src), "/opt/from_tar"]
        check = lambda: (root / "opt" / "from_tar" / "a").read_text() == "A"
    else:  # decompress
        if shutil.which("gzip") is None:
            pytest.skip("gzip not available")
        gz_src = tmp_path / "z.gz"
        _write_compressed(gz_src, "gz", b"zdata\n")
        argv = common + ["-x", "gz", str(gz_src), "/e2"]
        check = lambda: (root / "e2" / "z").read_bytes() == b"zdata\n"

    for _ in range(2):
        Install._parser_().parse_args(argv)()
        assert check()


@pytest.mark.posix
def test_install_symlink_rerun_applies_new_target(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    link = tmp_path / "ch.link"
    link.symlink_to("/usr/bin/env")

    def _argv():
        return ["--db", str(db), "--buildroot", str(root), "-p", str(link), "/usr/bin"]

    Install._parser_().parse_args(_argv())()
    staged = root / "usr" / "bin" / "ch.link"
    assert os.readlink(staged) == "/usr/bin/env"

    link.unlink()
    link.symlink_to("/usr/bin/bash")
    Install._parser_().parse_args(_argv())()
    assert os.readlink(staged) == "/usr/bin/bash"

    common = [
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "-p",
        "-T",
        "--type",
        "symlink",
    ]
    Install._parser_().parse_args(common + ["-O", "target=/a", "-", "/usr/bin/s"])()
    assert os.readlink(root / "usr" / "bin" / "s") == "/a"
    Install._parser_().parse_args(common + ["-O", "target=/b", "-", "/usr/bin/s"])()
    assert os.readlink(root / "usr" / "bin" / "s") == "/b"


@pytest.mark.posix
def test_install_symlink_inplace_records_target(tmp_path):
    # The source IS the already-staged path (an in-place build, where the
    # source tree lives under the build root itself).
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    link_dir = root / "usr" / "lib"
    link_dir.mkdir(parents=True)
    link = link_dir / "libfoo.so"
    link.symlink_to("libfoo.so.1")

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-T",
            str(link),
            "/usr/lib/libfoo.so",
        ]
    )
    inst()

    assert os.readlink(link) == "libfoo.so.1"
    recorded = inst.loaddb()["/usr/lib/libfoo.so"]
    assert recorded["meta"]["target"] == "libfoo.so.1"


@pytest.mark.posix
def test_install_file_replaces_stale_host_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "files.jsonl"
    src = tmp_path / "f.conf"
    src.write_text("payload")

    dest_dir = root / "etc"
    dest_dir.mkdir()
    stale = dest_dir / "f.conf"
    stale.symlink_to(src)  # points AT the source itself

    parser = Install._parser_()
    inst = parser.parse_args(
        ["--db", str(db), "--buildroot", str(root), "-T", str(src), "/etc/f.conf"]
    )
    inst()

    assert not stale.is_symlink()
    assert stale.read_text() == "payload"
    recorded = inst.loaddb()["/etc/f.conf"]
    assert recorded["type"] == "file"


# --------------------------------------------------------------------------
# Validate arguments before staging; remove the source last
# --------------------------------------------------------------------------


@pytest.mark.posix
@pytest.mark.parametrize("bad", ["mode", "owner", "group", "db_dir", "db_format"])
def test_install_bad_args_touch_nothing(tmp_path, bad):
    # Constructed directly (bypassing the CLI's own converters) so this
    # exercises _preflight's own validation, not argparse's.
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "f.conf"
    src.write_text("data")

    kwargs = dict(
        source=src,
        destination=Path("/etc"),
        buildroot=root,
        parents=True,
        db=tmp_path / "files.jsonl",
        remove_source=True,
        decompress=False,
    )

    if bad == "mode":
        kwargs["mode"] = "zzz"
    elif bad == "owner":
        kwargs["chown"] = True
        kwargs["owner"] = "nosuchuser_pkgforge_xyz"
    elif bad == "group":
        kwargs["chown"] = True
        kwargs["group"] = "nosuchgroup_pkgforge_xyz"
    elif bad == "db_dir":
        kwargs["db"] = tmp_path / "missing" / "f.jsonl"
    else:  # db_format
        kwargs["db_format"] = "jsonlines"

    with pytest.raises(ValueError):
        Install(**kwargs)()

    assert src.exists()
    assert not (root / "etc").exists()


@pytest.mark.posix
def test_install_owner_without_chown_recorded(tmp_path):
    # Without --chown, an owner/group name is only recorded, never resolved
    # -- packaging commonly names an account created later by %pre.
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "f.conf"
    src.write_text("data")

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "-o",
            "nosuchuser_pkgforge_xyz",
            str(src),
            "/etc",
        ]
    )
    inst()
    assert inst.loaddb()["/etc/f.conf"]["owner"] == "nosuchuser_pkgforge_xyz"


@pytest.mark.posix
@pytest.mark.parametrize("attached", ["--owner=--", "--group=--"])
def test_install_chown_attached_auto_owner(tmp_path, attached):
    import grp
    import pwd

    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "f.conf"
    src.write_text("data")

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(tmp_path / "files.jsonl"),
            "--buildroot",
            str(root),
            "-p",
            "--chown",
            attached,
            str(src),
            "/etc",
        ]
    )
    inst()  # must not raise: py3.9 strips the attached "--" to []

    staged = root / "etc" / "f.conf"
    recorded = inst.loaddb()["/etc/f.conf"]
    st = staged.stat()
    if attached == "--owner=--":
        assert recorded["owner"] == pwd.getpwuid(st.st_uid).pw_name
    else:
        assert recorded["group"] == grp.getgrgid(st.st_gid).gr_name


@pytest.mark.posix
def test_install_remove_source_same_as_dest_keeps_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    etc = root / "etc"
    etc.mkdir()
    f = etc / "a.conf"
    f.write_text("keep")
    db = tmp_path / "files.jsonl"

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-T",
            "--remove-source",
            "-m",
            "640",
            str(f),
            "/etc/a.conf",
        ]
    )
    inst()

    assert f.exists()
    assert (f.stat().st_mode & 0o777) == 0o640
    assert inst.loaddb()["/etc/a.conf"]["mode"] == "640"


@pytest.mark.posix
def test_install_remove_source_kept_on_chown_eperm(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("meaningless as root")
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "f.conf"
    src.write_text("data")
    db = tmp_path / "files.jsonl"

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-p",
            "--chown",
            "-o",
            "root",
            "--remove-source",
            str(src),
            "/etc",
        ]
    )
    with pytest.raises(PermissionError):
        inst()

    assert src.exists()


@pytest.mark.posix
def test_install_remove_source_dest_inside_source_refused(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "sub").mkdir()
    (stage / "sub" / "f").write_text("x")
    db = tmp_path / "files.jsonl"

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(stage),
            "-d",
            "-T",
            "--remove-source",
            str(stage),
            "/inner",
        ]
    )
    with pytest.raises(ValueError, match="inside the source"):
        inst()

    assert (stage / "sub" / "f").exists()


@pytest.mark.posix
def test_install_remove_source_db_inside_source_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    srcdir = tmp_path / "srcdir"
    srcdir.mkdir()
    (srcdir / "a").write_text("A")
    db = srcdir / "files.jsonl"  # the DB lives INSIDE the source directory

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-d",
            "-D",
            "--remove-source",
            str(srcdir),
            "/opt/app",
        ]
    )
    with pytest.raises(ValueError, match="inside the source"):
        inst()

    assert (srcdir / "a").exists()
    assert not db.exists()


def test_install_missing_parent_without_p_exits_2(tmp_path, cli):
    # x-plat (relative DESTINATION).
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "f.conf"
    src.write_text("data")
    db = tmp_path / "files.jsonl"

    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-T",
        str(src),
        "missingdir/f.conf",
    )
    assert result.rc == 2
    lines = [line for line in result.err.decode().splitlines() if line]
    assert len(lines) == 1
    assert "missingdir" in lines[0]
    assert "-p" in lines[0]
    assert not (root / "missingdir").exists()
    assert not list(root.glob("**/*.pkgforge-tmp"))


# --------------------------------------------------------------------------
# -d conflicts with -t
# --------------------------------------------------------------------------


def test_install_d_conflicts_with_type(tmp_path):
    # x-plat: a declared duho conflicts= group, enforced by argparse itself
    # before Install is ever constructed.
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "a.txt"
    src.write_text("plain")

    parser = Install._parser_()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(
            [
                "--db",
                str(tmp_path / "files.jsonl"),
                "--buildroot",
                str(root),
                "-d",
                "-t",
                "file",
                str(src),
                "out",
            ]
        )
    assert excinfo.value.code == 2
    assert not (root / "out").exists()


# --------------------------------------------------------------------------
# Build-root containment: DESTINATION must resolve inside --buildroot
# --------------------------------------------------------------------------


@pytest.mark.posix
@pytest.mark.parametrize(
    "dest",
    ["/../esc", "../esc", "" + "/../esc"],
    ids=["abs_dotdot", "rel_dotdot", "empty_prefix"],
)
def test_install_dotdot_escape_refused(tmp_path, cli, dest):
    # A '..' that climbs above --buildroot -- spelled directly, as a
    # relative destination, or produced by an empty shell variable
    # ("$EMPTY/../esc" with EMPTY="") -- must be refused before anything is
    # written, not silently normalized to a path beside the build root.
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "f"
    src.write_text("x")
    db = tmp_path / "files.jsonl"

    result = cli(
        "--db", str(db), "--buildroot", str(root), "install", "-D", str(src), dest
    )
    assert result.rc == 2
    assert not (tmp_path / "esc").exists()
    assert not db.exists()


@pytest.mark.posix
def test_install_inner_dotdot_normalized(tmp_path):
    # An in-root '..' is legitimate and stays inside --buildroot; it must be
    # normalized, both on disk and in the recorded key, not refused.
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "f"
    src.write_text("x")
    db = tmp_path / "files.jsonl"

    parser = Install._parser_()
    inst = parser.parse_args(
        [
            "--db",
            str(db),
            "--buildroot",
            str(root),
            "-D",
            str(src),
            "/usr/share/../lib/x",
        ]
    )
    inst()

    staged = root / "usr" / "lib" / "x"
    assert staged.read_text() == "x"
    assert "/usr/lib/x" in inst.loaddb()


@pytest.mark.posix
def test_install_symlinked_parent_escape_refused(tmp_path, cli):
    # pkgforge's own directory staging can leave an absolute symlink inside
    # the build root (copytree(symlinks=True)); a later install must not
    # follow it onto a sibling directory outside --buildroot.
    root = tmp_path / "root"
    root.mkdir()
    (root / "usr" / "share").mkdir(parents=True)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (root / "usr" / "share" / "alt").symlink_to(sibling)
    src = tmp_path / "f"
    src.write_text("x")
    db = tmp_path / "files.jsonl"

    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-D",
        str(src),
        "/usr/share/alt/f2",
    )
    assert result.rc == 2
    assert list(sibling.iterdir()) == []


@pytest.mark.posix
def test_install_directory_onto_escape_symlink_refused(tmp_path, cli):
    # A Directory-typed install onto an in-root symlink is refused too: the
    # leaf is followed here because it names an existing directory the
    # merge would write into.
    root = tmp_path / "root"
    root.mkdir()
    (root / "usr" / "share").mkdir(parents=True)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (root / "usr" / "share" / "alt").symlink_to(sibling)
    srcdir = tmp_path / "srcdir"
    srcdir.mkdir()
    (srcdir / "payload").write_text("x")
    db = tmp_path / "files.jsonl"

    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-d",
        "-T",
        str(srcdir),
        "/usr/share/alt",
    )
    assert result.rc == 2
    assert list(sibling.iterdir()) == []


@pytest.mark.posix
def test_install_T_victim_outside_root_survives(tmp_path, cli):
    # The containment check runs in _resolve(), before any staging -- an
    # escaping -T destination must be refused BEFORE the pre-existing target
    # is ever unlinked.
    root = tmp_path / "root"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("original")
    src = tmp_path / "f"
    src.write_text("new")
    db = tmp_path / "files.jsonl"

    result = cli(
        "--db",
        str(db),
        "--buildroot",
        str(root),
        "install",
        "-T",
        str(src),
        "/../victim",
    )
    assert result.rc == 2
    assert victim.read_text() == "original"


@pytest.mark.posix
def test_install_relative_inroot_symlink_ok(tmp_path):
    # Regression: a relative in-root link (e.g. lib64 -> usr/lib64) must
    # keep working.
    root = tmp_path / "root"
    root.mkdir()
    (root / "usr" / "lib64").mkdir(parents=True)
    (root / "lib64").symlink_to("usr/lib64")
    src = tmp_path / "f"
    src.write_text("x")
    db = tmp_path / "files.jsonl"

    Install._parser_().parse_args(
        ["--db", str(db), "--buildroot", str(root), "-D", str(src), "/lib64/libx.so"]
    )()

    assert (root / "usr" / "lib64" / "libx.so").read_text() == "x"


@pytest.mark.posix
def test_install_symlinked_buildroot_ok(tmp_path):
    # Regression: a --buildroot that is itself a symlink must keep working.
    real_root = tmp_path / "real_root"
    real_root.mkdir()
    root = tmp_path / "root_link"
    root.symlink_to(real_root)
    src = tmp_path / "f"
    src.write_text("x")
    db = tmp_path / "files.jsonl"

    Install._parser_().parse_args(
        ["--db", str(db), "--buildroot", str(root), "-D", str(src), "/etc/f"]
    )()

    assert (real_root / "etc" / "f").read_text() == "x"


@pytest.mark.posix
def test_install_directory_to_root_ok(tmp_path):
    # DESTINATION "/" means the build root itself: a directory source
    # merges straight into it.
    root = tmp_path / "root"
    root.mkdir()
    srcdir = tmp_path / "srcdir"
    srcdir.mkdir()
    (srcdir / "payload").write_text("x")
    db = tmp_path / "files.jsonl"

    Install._parser_().parse_args(
        ["--db", str(db), "--buildroot", str(root), "-d", "-T", str(srcdir), "/"]
    )()

    assert (root / "payload").read_text() == "x"


# --------------------------------------------------------------------------
# A relative build root that resolves to '/' is refused
# --------------------------------------------------------------------------


@pytest.mark.posix
@pytest.mark.parametrize("root_env", ["unset", "empty_env"])
def test_install_relative_root_at_slash_refused(tmp_path, monkeypatch, cli, root_env):
    # With no --buildroot, PKGFORGE_ROOT unset (or explicitly empty, which
    # counts as unset) falls back to the cwd -- if that cwd is '/' (a
    # container's default WORKDIR), an unattended job that lost the
    # variable must not silently map onto the live filesystem.
    if root_env == "empty_env":
        monkeypatch.setenv("PKGFORGE_ROOT", "")
    monkeypatch.chdir("/")
    victim = tmp_path / "victim"
    victim.write_text("original")
    src = tmp_path / "f"
    src.write_text("new")
    db = tmp_path / "files.jsonl"

    result = cli("--db", str(db), "install", "-T", "-m", "600", str(src), str(victim))
    assert result.rc == 2
    assert victim.read_text() == "original"


@pytest.mark.posix
def test_install_explicit_root_slash_allowed(tmp_path, monkeypatch, cli):
    # The deliberate opt-in keeps working: an explicit --buildroot / (or
    # PKGFORGE_ROOT=/) targets the live filesystem on purpose.
    monkeypatch.chdir("/")
    dest = tmp_path / "out" / "g"
    src = tmp_path / "f"
    src.write_text("data")
    db = tmp_path / "files.jsonl"

    result = cli(
        "--db", str(db), "--buildroot", "/", "install", "-D", str(src), str(dest)
    )
    assert result.rc == 0
    assert dest.read_text() == "data"


@pytest.mark.parametrize("buildroot", ["", None])
def test_install_falsy_buildroot_refused(tmp_path, monkeypatch, buildroot):
    # x-plat: a falsy build root is only reachable from the Python API (the
    # CLI always parses --buildroot/PKGFORGE_ROOT to a real Path); it must
    # be refused explicitly, not fall through to a deep TypeError/ValueError
    # inside buildpath().
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "f"
    src.write_text("data")

    with pytest.raises(UsageError):
        Install(
            source=src,
            destination=Path("/etc/f"),
            buildroot=buildroot,
            parents=True,
            db=None,
        )()

    assert list(tmp_path.iterdir()) == [src]


# --------------------------------------------------------------------------
# Directory staging: top mode/mtime and a dangling symlink survive the
# cleanup of install.py's dead/redundant code (no behavior change)
# --------------------------------------------------------------------------


@pytest.mark.posix
def test_install_directory_keeps_top_mode_and_dangling_link(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    srcdir = tmp_path / "srcdir"
    srcdir.mkdir(mode=0o750)
    os.chmod(srcdir, 0o750)  # mkdir's mode is umask-adjusted; pin it exactly
    (srcdir / "f").write_text("x")
    (srcdir / "dangling").symlink_to("no-such-target")
    db = tmp_path / "files.jsonl"

    Install._parser_().parse_args(
        ["--db", str(db), "--buildroot", str(root), "-d", "-D", str(srcdir), "/opt/app"]
    )()

    staged = root / "opt" / "app"
    assert (staged.stat().st_mode & 0o777) == 0o750
    link = staged / "dangling"
    assert link.is_symlink()
    assert os.readlink(link) == "no-such-target"
    assert not link.exists()  # dangling: the target still doesn't exist
