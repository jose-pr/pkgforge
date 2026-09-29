"""``install``'s staging helpers and the :class:`_Staging` mixin holding
``Install``'s own staging methods (file/symlink/directory + the top-level
``install()`` dispatcher)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import typing
from pathlib import Path

from . import archive
from .decompress import _DECOMPRESSORS
from .transfer import _Transfer, _try_link
from ..entry import DEFAULT, FileType
from ..errors import PkgForgeError, UsageError
from ..exclude import PathMatch


def _require_stdin() -> typing.BinaryIO:
    """Return stdin's binary buffer for a ``-`` source, after checking it is
    actually readable data (piped, or ``/dev/null``) rather than a terminal
    or a closed fd. Never closes stdin: the caller reads from the returned
    buffer without wrapping fd 0 in a new file object.
    """
    if sys.stdin is None:
        raise UsageError("'-' reads stdin, but stdin is closed")
    if sys.stdin.isatty():
        raise UsageError(
            "'-' reads stdin, but stdin is a terminal; pipe or redirect the data"
        )
    return sys.stdin.buffer


def _detect_source_type(path: Path) -> FileType:
    """Auto-detect a source's :class:`FileType`.

    A FIFO, or a character device that is not itself a terminal, is typed
    :attr:`FileType.File` so it is streamed like a regular file -- this is
    what makes a named pipe, ``/dev/stdin`` and process substitution
    (``<(cmd)``, which is a magic symlink to an anonymous pipe on Linux)
    work as install sources, instead of staging a dangling symlink to the
    pipe's ``pipe:[N]``/``/proc/self/fd/N`` target. This check runs before
    ``is_symlink()`` so it applies even though such a path often *is* one.
    A terminal character device, a socket or a block device is refused. An
    explicit ``-t symlink`` bypasses this entirely (it never calls this
    function) and always copies the link text.
    """
    try:
        followed = os.stat(path)
    except OSError:
        followed = None
    if followed is not None and stat.S_ISFIFO(followed.st_mode):
        return FileType.File
    if followed is not None and stat.S_ISCHR(followed.st_mode):
        fd = os.open(os.fspath(path), os.O_RDONLY | os.O_NOCTTY)
        try:
            is_tty = os.isatty(fd)
        finally:
            os.close(fd)
        if is_tty:
            raise UsageError(f"{path}: source is a terminal; pipe or redirect the data")
        return FileType.File
    if path.is_symlink():
        return FileType.Symlink
    if followed is None:
        raise TypeError(
            f"{path}: not a regular file, directory or symlink "
            "(missing or special file)"
        )
    if stat.S_ISDIR(followed.st_mode):
        return FileType.Directory
    if stat.S_ISREG(followed.st_mode):
        return FileType.File
    raise UsageError(f"{path}: unsupported source type (socket or block device)")


def _umask() -> int:
    """Read the process umask without changing it (``os.umask`` has no
    read-only form: setting it is the only way to read it)."""
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _refuse_real_directory_dest(dst: Path) -> None:
    """Refuse to replace a real (non-symlink) directory with a file or
    symlink -- a clear error instead of a confusing OSError deep in a
    copy/rename call."""
    if dst.is_dir() and not dst.is_symlink():
        raise UsageError(f"{dst}: cannot replace a directory with a file or symlink")


def _copy_ignore(
    src: Path,
    dst: Path,
    exclude: typing.Optional[PathMatch],
    skip: typing.FrozenSet[str] = frozenset(),
    meta: typing.Optional[typing.Dict[str, str]] = None,
) -> typing.Callable[[str, typing.List[str]], typing.List[str]]:
    """Build a ``shutil.copytree`` ``ignore=`` callback shared by a
    directory source's copy and a fresh archive extraction's merge into an
    already-existing destination.

    Beyond the ``-X`` filtering (delegated to ``exclude``'s
    :class:`~pkgforge.exclude.PathMatch`, unchanged from before -- a
    statement whose glob already matched but whose inline test needs a
    file type ``copytree`` cannot give still raises :class:`PkgForgeError`)
    and ``skip`` (paths -- relative to ``src``, as :meth:`Install._self_copy_skips`
    returns them, not bare names -- this call must never copy at all, e.g.
    the build root or the file DB sitting inside the source; an ancestor
    directory of a skipped path is still created, empty, since only the
    exact skipped name is ever added to ``copytree``'s own ignore list),
    every kept name gets a stale destination entry cleared before
    ``copytree`` reaches it:

    * an existing destination *symlink* is unlinked unconditionally --
      ``copytree(symlinks=True)`` recreates a source link with a bare
      ``os.symlink``, which raises ``FileExistsError`` over one, and a
      regular-file source copied with ``copy2`` would otherwise write its
      new content straight through a leftover destination link (possibly
      outside the build root);
    * a destination *regular file* is unlinked only when the source entry
      at that name is itself a symlink, so the link can be created in its
      place.

    A real destination *directory* is never touched here: a source link or
    file colliding with one still raises from ``copytree`` itself, rather
    than being silently replaced by an ``rmtree``.
    """
    resolved_meta = {} if meta is None else meta

    def _ignore(_dir: str, _names: typing.List[str]) -> typing.List[str]:
        ignored: typing.List[str] = []
        rel_dir = os.path.relpath(_dir, os.fspath(src))
        base = dst if rel_dir == os.curdir else dst / rel_dir
        for name in _names:
            entry_rel = name if rel_dir == os.curdir else os.path.join(rel_dir, name)
            if entry_rel in skip:
                ignored.append(name)
                continue
            path = Path(_dir, name)
            if exclude is not None:
                try:
                    if exclude.match(path, meta=resolved_meta):
                        ignored.append(name)
                        continue
                except TypeError as exc:
                    # FileType.from_path raises TypeError for a fifo/socket.
                    # A glob-only -X (e.g. '*.fifo') already excluded such a
                    # path above without ever reaching here; this is a
                    # test-bearing statement whose glob still hit one.
                    raise PkgForgeError(
                        f"{path}: unsupported file type (not a "
                        "file, directory or symlink); exclude it "
                        "with -X"
                    ) from exc
            dst_entry = base / name
            if dst_entry.is_symlink():
                dst_entry.unlink()
            elif path.is_symlink() and dst_entry.is_file():
                dst_entry.unlink()
        return ignored

    return _ignore


class _Staging(_Transfer):
    """Staging steps for Install; uses the host's meta, decompress,
    exclude, buildroot, db, method and _logger_."""

    def _stage_file(self, src: Path, dst: Path) -> Path:
        """Write this clone's file content into a temp file next to ``dst``.

        Never touches ``dst`` itself; the caller applies the entry and
        ``os.replace``s the returned temp onto ``dst``. On any failure the
        temp is removed and the exception re-raised.
        """
        # Computed once and reused below: str(src) == DEFAULT means the "-"
        # positional placeholder, i.e. this clone actually reads stdin.
        from_stdin = str(src) == DEFAULT
        fd, tmp_name = tempfile.mkstemp(
            dir=os.fspath(dst.parent), prefix=f".{dst.name}.", suffix=".pkgforge-tmp"
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            if not src:
                # An empty file (-t file, no source). mkstemp already
                # created it 0600; give it the umask-adjusted default mode
                # a plain touch() would have, since apply_entry() skips
                # chmod entirely when mode is left at DEFAULT.
                os.chmod(tmp, 0o666 & ~_umask())
            elif self.decompress:
                os.chmod(tmp, 0o666 & ~_umask())
                argv, _ = _DECOMPRESSORS[self.decompress]
                with tmp.open("wb") as f:
                    subprocess.run(
                        [
                            *argv,
                            # An absolute path so a source starting with "-"
                            # (e.g. "-v.gz") is never read as an option.
                            "-" if from_stdin else os.fspath(src.absolute()),
                        ],
                        # Only a "-" source reads stdin; for a real file the
                        # child inherits ours. Passing the file object (not
                        # a bare .fileno()) lets subprocess resolve it even
                        # when stdin has been replaced with a wrapped file
                        # object (as the tests do).
                        stdin=sys.stdin if from_stdin else None,
                        stdout=f,
                        check=True,
                    )
            elif not from_stdin:
                if stat.S_ISREG(os.stat(src).st_mode):
                    # mkstemp already created tmp 0600; os.link/copyfile/
                    # os.replace each want to create/replace the name
                    # themselves, so remove the placeholder first (matches
                    # the plain-copy path below, which relied on this
                    # already). A mode set by -m still wins over the
                    # staged one, applied later by apply_entry() -- true
                    # for all three methods, including link, where it also
                    # lands on the shared inode.
                    tmp.unlink()
                    if self.method == "move":
                        # os.replace/shutil.move: consumes src.
                        self._move_file(src, tmp)
                        self._move_atomic = True
                    elif self.method == "link" and _try_link(src, tmp):
                        pass  # hardlinked; nothing further to stage.
                    else:
                        if self.method == "link":
                            self._warn_link_fallback()
                        # Copy src -> tmp: content, permission bits and
                        # modification time only -- never shutil.copy2, which
                        # (via copystat) also copies BSD file flags and
                        # extended attributes (e.g. an SELinux label).
                        # Staging should not carry either: on macOS,
                        # copystat's chflags() call raises PermissionError
                        # for a source with any of the user-immutable/
                        # no-dump flags set, even though this process only
                        # reads the source.
                        shutil.copyfile(os.fspath(src), os.fspath(tmp))
                        shutil.copymode(os.fspath(src), os.fspath(tmp))
                        src_stat = os.stat(src)
                        os.utime(tmp, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
                else:
                    # A FIFO or non-terminal character device (a named pipe,
                    # /dev/stdin, process substitution): stream its bytes.
                    # copyfile/copymode do not apply to a non-regular file.
                    os.chmod(tmp, 0o666 & ~_umask())
                    with open(src, "rb") as stream_in, tmp.open("wb") as stream_out:
                        shutil.copyfileobj(stream_in, stream_out)
            else:
                self._logger_.info("Obtaining data from stdin")
                os.chmod(tmp, 0o666 & ~_umask())
                with tmp.open("wb") as output:
                    shutil.copyfileobj(_require_stdin(), output)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return tmp

    def _stage_symlink(self, src: Path, dst: Path) -> Path:
        """Reserve a temp name next to ``dst`` and symlink it to the
        resolved target. Never touches ``dst`` itself; see :meth:`_stage_file`.
        """
        if str(src) == DEFAULT or not src:
            target = self.meta.get("target")
            if not target:
                # __call__ already checks this right after type resolution
                # (before the destination is even computed); this raise
                # stays for a direct Python-API caller of install() itself.
                raise UsageError("a symlink with no source needs -O target=PATH")
            copystat_from = None
        else:
            target = os.fspath(src.readlink())
            # Rebind, never mutate: self.meta may be the caller's own dict,
            # a shared class-level default, or a parser -O action's stored
            # default. Mutating it in place leaked "target" into every
            # entry recorded after this one, in the same multi-source
            # install and beyond it.
            self.meta = {**self.meta, "target": target}
            copystat_from = src

        fd, tmp_name = tempfile.mkstemp(
            dir=os.fspath(dst.parent), prefix=f".{dst.name}.", suffix=".pkgforge-tmp"
        )
        os.close(fd)
        tmp = Path(tmp_name)
        tmp.unlink()  # mkstemp's placeholder; replaced with the symlink below
        try:
            os.symlink(target, tmp)
            if copystat_from is not None:
                shutil.copystat(copystat_from, tmp, follow_symlinks=False)
        except BaseException:
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink(missing_ok=True)
            raise
        return tmp

    def _self_copy_skips(self, src: Path, dst: Path) -> typing.FrozenSet[str]:
        """Names (each relative to ``src.resolve()``, not a bare filename)
        that a directory copy from ``src`` onto ``dst`` must never descend
        into: the resolved destination, the build root, and the file DB
        (unless there is none, :meth:`PkgForgeCmd._no_file_db`), whichever
        of them sit *strictly inside* the resolved source.

        A directory install's own build root commonly does (Debian's
        ``debian/tmp``, say), and without this ``shutil.copytree`` walks
        into its own output, nesting the destination inside itself until
        ``RecursionError``. Skipping, not raising, leaves each skipped
        name's parent directories created as normal -- possibly empty, if
        nothing else lived there -- since :func:`_copy_ignore` only ever
        adds the exact skipped path to ``copytree``'s own ignore list, not
        a prefix. A source that equals or sits inside ``dst`` (an in-place
        build, or a nested merge) is unaffected: that case merges, and
        never reaches the recursion this guards against.

        Checked as ``src_real in real.parents`` (excluding ``real ==
        src_real`` itself, an in-place build), the same containment idiom
        used elsewhere in this module.
        """
        src_real = Path(os.path.realpath(src))
        candidates = [dst, Path(self.buildroot)]
        if not self._no_file_db():
            candidates.append(Path(self.db))
        skip: typing.Set[str] = set()
        for candidate in candidates:
            real = Path(os.path.realpath(candidate))
            if real == src_real or src_real not in real.parents:
                continue
            skip.add(real.relative_to(src_real).as_posix())
        return frozenset(skip)

    def _stage_directory(self, src: Path, dst: Path) -> Path:
        """Stage a directory (or archive) source at ``dst``.

        Returns ``dst`` once its final content is in place (a merge, or
        creating an empty directory, leaves nothing further for the caller
        to swap); returns a sibling temp directory when the caller must
        still ``os.replace`` it onto ``dst`` (a fresh, non-merging archive
        extraction).
        """
        if not src:
            # -t directory, no source: an empty directory. May already
            # exist (a re-run); nothing to extract or merge.
            dst.mkdir(exist_ok=True)
            return dst

        if src == DEFAULT or not src.is_dir():
            # Extract an archive source into a fresh temp directory first,
            # so a filter rejection or a truncated archive never reaches
            # dst directly.
            tmp = Path(
                tempfile.mkdtemp(
                    dir=os.fspath(dst.parent),
                    prefix=f".{dst.name}.",
                    suffix=".pkgforge-tmp",
                )
            )
            os.chmod(tmp, 0o777 & ~_umask())
            try:
                archive.extract(src, tmp, self._logger_)
                if dst.exists():
                    shutil.copytree(
                        tmp,
                        dst,
                        symlinks=True,
                        ignore=_copy_ignore(tmp, dst, None),
                        dirs_exist_ok=True,
                    )
                    shutil.rmtree(tmp)
                    return dst
                return tmp
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise

        if self.method == "move" and not dst.exists() and not self.exclude:
            # Fast path: the whole tree becomes the destination in one
            # rename -- nothing left to stage further, and (unlike the
            # merge path below) a single reversible unit a failure in
            # apply/replace/record can still move back onto self.source.
            os.rename(src, dst)
            self._move_atomic = True
            return dst

        # A directory source: copytree onto dst, merging if dst already
        # exists (also every move that isn't the fast path above -- an
        # existing dst, or an --exclude that must leave some source files
        # behind). Only roll back dst on failure if this call created it --
        # a partial merge into a pre-existing directory is not rolled back;
        # for a move specifically, files already consumed from self.source
        # into dst by the time of that failure would be lost outright if
        # dst were then deleted too, so a move never does that cleanup.
        created = not dst.exists()
        try:
            dst.mkdir(exist_ok=True)
            matcher = PathMatch(self.exclude, src) if self.exclude else None
            skip = self._self_copy_skips(src, dst)
            copy_function_kwargs = {}
            if self.method == "move":
                copy_function_kwargs["copy_function"] = self._move_copy_function
            elif self.method == "link":
                copy_function_kwargs["copy_function"] = self._link_copy_function
            # copytree stamps the top directory's own stat (mode, mtime)
            # once it finishes, unconditionally -- a copystat here first
            # would only be overwritten by that one, so there is none. The
            # dangling-symlink flag copytree also accepts is not passed
            # either: it only has any effect when symlinks=False, so it
            # would be a silent no-op here (symlinks=True below).
            shutil.copytree(
                src,
                dst,
                symlinks=True,
                ignore=_copy_ignore(src, dst, matcher, skip=skip, meta=dict(self.meta)),
                dirs_exist_ok=True,
                **copy_function_kwargs,
            )
            if self.method == "move":
                # Every file copy_function actually moved is already gone
                # from src; a symlink was recreated fresh at dst instead
                # (copytree never routes one through copy_function) and so
                # is still in src -- only directories left with nothing
                # else in them are removed, so an --exclude (or a symlink)
                # leaves exactly its own path behind, as documented.
                self._remove_empty_dirs(src)
        except BaseException:
            if created and self.method != "move":
                shutil.rmtree(dst, ignore_errors=True)
            raise
        return dst

    def install(self, src: Path, dst: Path) -> Path:
        """Stage ``src`` at ``dst`` and return the path holding the final
        content: either ``dst`` itself (nothing further to do -- a merge,
        an in-place no-op, or an already-empty directory) or a sibling temp
        the caller must ``os.replace`` onto ``dst`` after applying the
        entry. Writes nothing to ``dst`` directly for a file or symlink
        type, so a failure here never touches a previously staged copy.
        """
        self._logger_.info(
            "Installing %s at %s", DEFAULT if src is None else src, self.buildpath(dst)
        )
        if (
            src not in [DEFAULT, None]
            and os.path.lexists(dst)
            and os.path.samestat(os.lstat(src), os.lstat(dst))
        ):
            # src already IS the staged dst (e.g. an in-place build, or a
            # hardlink left by an earlier run): nothing to stage. Uses
            # samestat on lstat, not resolve()/exists(), so a stale host
            # symlink that merely resolves to src does not count as "same".
            if self.type == FileType.Symlink:
                self.meta = {**self.meta, "target": os.fspath(src.readlink())}
            return dst

        if self.type == FileType.File:
            _refuse_real_directory_dest(dst)
            return self._stage_file(src, dst)
        elif self.type == FileType.Symlink:
            _refuse_real_directory_dest(dst)
            return self._stage_symlink(src, dst)
        elif self.type == FileType.Directory:
            return self._stage_directory(src, dst)
        else:
            raise NotImplementedError(self.type)
