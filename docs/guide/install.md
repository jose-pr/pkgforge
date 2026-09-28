# Install

```bash
pip install pkgforge
```

pkgforge requires **Python 3.9+**. Its only runtime dependencies are
[duho](https://pypi.org/project/duho/) (the CLI framework) and
[PyYAML](https://pypi.org/project/pyyaml/) (the file DB format).

## Platform support

pkgforge is a **Linux** tool: the `install`, `scan`, and metadata-apply paths
use POSIX facilities — `os.chmod`, `os.chown` (with `follow_symlinks=False`),
and symlinks. The CLI itself (parsing, `--help`, the dump formats)
imports and runs on any platform, so you can develop and unit-test on Windows or
macOS; the file-staging operations expect a POSIX filesystem.

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
a stale entry at each member's path instead of failing.

## From source

```bash
git clone https://github.com/jose-pr/pkgforge
cd pkgforge
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## Invocation

pkgforge installs a `pkgforge` console script and is also runnable as a
module:

```bash
pkgforge --help
python -m pkgforge --help
```
