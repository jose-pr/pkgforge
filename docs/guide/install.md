# Install

```bash
pip install pkgforge
```

pkgforge requires **Python 3.9+**. Its only runtime dependencies are
[duho](https://pypi.org/project/duho/) (the CLI framework) and
[PyYAML](https://pypi.org/project/pyyaml/) (the file DB format).

## Platform support

pkgforge is a **Linux** tool: the `install`, `scan`, and metadata-apply paths
use POSIX facilities — `os.chmod`, `os.chown`, symlinks, and hardlinks with
`follow_symlinks=False`. The CLI itself (parsing, `--help`, the dump formats)
imports and runs on any platform, so you can develop and unit-test on Windows or
macOS; the file-staging operations expect a POSIX filesystem.

Archive extraction prefers stdlib `tarfile` for the tar family
(`.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`, `.tar.xz`) and only falls back to the
`bsdtar` binary for formats `tarfile` cannot open (e.g. `.iso`) — so a plain
tar-based build needs no external archiver installed.

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
