# Install

```bash
pip install pkgforge
```

pkgforge requires **Python 3.9+**. Its runtime dependencies, as declared in
`pyproject.toml`, are [duho](https://pypi.org/project/duho/)`>=0.6.0,<0.7`
(the CLI framework) and [PyYAML](https://pypi.org/project/pyyaml/)`>=6.0,<7`
(for the `yaml` DB backend; JSON Lines, the default backend, needs no
third-party parser).

## Platform support

pkgforge is a **Linux** tool: the `install`, `scan`, and metadata-apply paths
use POSIX facilities — `os.chmod`, `os.chown` (with `follow_symlinks=False`),
and symlinks. The CLI itself (parsing, `--help`, the dump formats)
imports and runs on any platform, so you can develop and unit-test on Windows or
macOS; the file-staging operations expect a POSIX filesystem.

`install --method link`/`move` (see [Commands](commands.md#install)) use
`os.link`/`os.rename`, which work the same way anywhere Python runs; crossing
a filesystem boundary falls back to a copy (`link`) or a copy-then-delete
(`move`) rather than failing.

`install --record-tree` (see [Commands](commands.md#install)) is a plain
filesystem walk over the already-staged tree -- it reads each child's mode
and type from disk (`os.lstat`), no extra POSIX facility beyond what
staging itself already needs.

Archive extraction prefers stdlib `tarfile` for the tar family
(`.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`, `.tar.xz`) and falls back to the
`bsdtar` binary (needs libarchive 3.3+) for stdin sources -- even a tar
stream, since `tarfile` needs a real path -- for any other format
(`.zip`, `.iso`, `.cpio`, ...), and for an interpreter whose `tarfile` has
no extraction filter (PEP 706; below 3.9.17/3.10.12/3.11.4); so a plain
tar-based build from a real file on a current Python needs no external
archiver installed. Both paths apply the same extraction policy: an
archive's ownership, setuid/setgid, group/other write, xattrs, ACLs and
file flags are never restored, and a device node, FIFO or socket is
refused. On the `tarfile` path, a symlink member's target is kept exactly
as stored (absolute or climbing targets included), but a write through any
symlink ancestor -- even one resolving back inside the destination -- is
refused, and a hardlink member's target must resolve inside the
destination; re-extracting the same archive over an existing tree replaces
a stale entry at each member's path instead of failing. `install -X`
prunes matched members (files, symlinks, and whole directory subtrees)
from the extracted tree before anything reaches DESTINATION, the same as
it filters a directory source's copy.

## From source

```bash
git clone https://github.com/jose-pr/pkgforge
cd pkgforge
python -m venv .venv/3.14-posix-$(uname -m) && . .venv/3.14-posix-$(uname -m)/bin/activate
pip install -e ".[dev]"
pytest -q
```

Name the venv `<version>-<os>-<arch>` (`<os>` is `posix`/`nt`/`darwin`) if you
keep more than one interpreter around, e.g. to also test the `>=3.9` floor.

## Invocation

pkgforge installs a `pkgforge` console script and is also runnable as a
module:

```bash
pkgforge --help
python -m pkgforge --help
```
