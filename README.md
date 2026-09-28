# pkgforge

[![CI](https://github.com/jose-pr/pkgforge/actions/workflows/test.yml/badge.svg)](https://github.com/jose-pr/pkgforge/actions/workflows/test.yml)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-blue)](https://jose-pr.github.io/pkgforge/)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://pypi.org/project/pkgforge/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Stage files into a *build root* and record their intended install metadata
(mode, owner, group, type, and free-form key/value `meta`) in a *file DB*
(JSON Lines, YAML, or SQLite),
then dump that DB into packaging manifests — an RPM `%files` list or Debian
`install` + `permissions` + `dirs` files.

`pkgforge` is a small, dependency-light helper for unattended build pipelines
on Linux: install a source into place, remember how it should be owned and
permissioned, and emit that record for the packager.

## Install

```sh
pip install pkgforge
```

Requires Python 3.9+. Runtime operations use POSIX facilities (`chmod`, `chown`,
symlinks), so runtime targets Linux; the CLI and `--help` import
cleanly on any platform. A tar-family archive given as a real path extracts
via stdlib `tarfile` — no external archiver needed; a `-` (stdin) source and
other formats (e.g. `.iso`) fall back to `bsdtar`. Neither path restores an
archive's ownership or special bits as root, and a device node, FIFO or
socket in the archive is refused.

## Quick start

```sh
export PKGFORGE_ROOT=/tmp/stage PKGFORGE_DB=/tmp/files.jsonl

pkgforge initdb
pkgforge install -p -m 755 -o root -g root ./build/tool /usr/bin
pkgforge install -p -m 640 -o root -g adm -O rpmprefix=%config ./tool.conf /etc
pkgforge install -D -d -m 755 -o root -g root ./share /usr/share/tool
pkgforge scan --missing --mode=-- -o root -g root /usr/share/tool
pkgforge dbdump -f rpmspecfiles rpm-files.txt
pkgforge dbdump -f debian debian/
```

See [`examples/stage_and_package.sh`](examples/stage_and_package.sh) for a
runnable end-to-end walkthrough.

## Commands

| Command | Purpose |
| --- | --- |
| `initdb` | create or reset (truncate) the file DB |
| `install [opts] SRC… DEST` | stage a source and record its entry |
| `scan [opts] PATH` | walk PATH, recording every file and directory below it (never PATH itself); `-m` applies to files only (directories take `--dir-mode`, symlinks never get a mode); fields default to `-` unless `--mode=--`/`--dir-mode=--`/`--owner=--`/`--group=--` reads them from disk; replaces existing entries unless `--missing`; `--drop-stale` removes entries whose files are gone |
| `compact` | collapse an append-log DB to one record per live path |
| `dbdump -f FORMAT [OUT]` | render the DB into a packaging manifest |

Global options (also read from the environment):

| Option | Env | Meaning |
| --- | --- | --- |
| `--db PATH` | `PKGFORGE_DB` | file DB to read/write (`-` = write records to stdout; reads see an empty DB) |
| `--db-format FMT` | `PKGFORGE_DB_FORMAT` | backend: `jsonl` / `yaml` / `sqlite` (else from the `--db` suffix) |
| `--buildroot DIR` | `PKGFORGE_ROOT` | staging root that maps to `/` in the DB; DESTINATION/PATH must resolve inside it |

Global flags work before or after the subcommand. With no `--buildroot` or
`PKGFORGE_ROOT`, the default is the current directory -- except that a cwd of
`/` is refused (exit 2); pass `--buildroot /` to target the live filesystem
on purpose.

## Storage backends

The file DB has three interchangeable backends — every command behaves the same
regardless of which is used:

| Format | Extensions | Model |
| --- | --- | --- |
| `jsonl` (default) | `.jsonl`, `.ndjson` | append-only JSON Lines |
| `yaml` | `.yaml`, `.yml` | append-only YAML |
| `sqlite` | `.db`, `.sqlite`, `.sqlite3` | SQLite store, upserted in place |

The backend is picked from the `--db` extension (override with `--db-format`);
reading an existing file auto-detects its actual format.

## Dump formats

| Format | Aliases | Output |
| --- | --- | --- |
| `rpmspecfiles` | `rpm`, `rpmspec` | RPM `%files` lines (`%attr(...)`, `%dir`, `meta.rpmprefix`) to a file or `-` |
| `debian` | `deb` | `install` + `permissions` + `dirs` files into an output directory (or `-`, sectioned) |

Output of the Quick start above:

```
$ pkgforge dbdump -f rpmspecfiles -
%config %attr(640,root,adm) "/etc/tool.conf"
%attr(755,root,root) "/usr/bin/tool"
%dir %attr(755,root,root) "/usr/share/tool"
%attr(644,root,root) "/usr/share/tool/data.txt"
```

```
$ pkgforge dbdump -f debian -
# === install ===
etc/tool.conf etc
usr/bin/tool usr/bin
usr/share/tool/data.txt usr/share/tool
# === permissions ===
/etc/tool.conf 640 root adm
/usr/bin/tool 755 root root
/usr/share/tool 755 root root
/usr/share/tool/data.txt 644 root root
# === dirs ===
usr/share/tool
```

## File entries

Each entry records `mode` (octal string, e.g. `644`), `owner`, `group`, `type`
(`file`/`directory`/`symlink`), and a `meta` map. Two sentinels defer a field to
the staged file: `-` ("leave at OS default") and `--` ("resolve from disk",
also spelled `auto` for `-m`). `-m` accepts only 1-4 octal digits or a
sentinel and is validated before anything is staged.

## Exclude / filter syntax

An `--exclude` statement is an optional leading `!` (negate), zero or more inline
tests, and a trailing glob:

```
(?type:file)**/*.pyc            # every .pyc file, at any depth
!(?meta:keep=1)**/tmp/**        # keep entries tagged keep=1 under tmp/ ...
**/tmp/**                       # ... paired with a broader exclude
```

Tests are `(?type:file|directory|symlink)` and `(?meta:key=value)`; prefix a test
name with `!` (`(?!type:file)`) to invert just that test. `**` recurses (a
trailing `**` means "the contents of this directory"); an absolute pattern
anchors at a different root per command (`install`: the source directory;
`scan`: `<buildroot>/PATH`; `dbdump`: `/`, the DB key) and `install`/`scan`
prune an excluded directory's subtree, while `dbdump` decides entry by entry
on its flat key list. See the [exclude grammar guide](https://jose-pr.github.io/pkgforge/guide/exclude/)
for the full per-command table and worked examples.

## Documentation

Full docs at **<https://jose-pr.github.io/pkgforge/>** — command reference,
file-DB model, exclude grammar, dump formats, and the API reference.

## Development

```sh
git clone https://github.com/jose-pr/pkgforge && cd pkgforge
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,docs]"

black src tests benchmarks      # format (Python 3.10+)
pytest -q                       # tests
python benchmarks/run.py        # benchmarks (add --save to record; see `benchmarks/README.md`)
mkdocs serve                    # docs preview at http://127.0.0.1:8000
```

`tests/smoke_installed.py` is not collected by pytest. Run it with a
*non-editable* install's interpreter (e.g. from a built wheel in a fresh venv)
to check the installed CLI surface (`--version`, `--help`, completion, the
example) and the shipped files (`AGENTS.md`, `README.md`, `py.typed`):

```sh
python -m build --wheel --outdir dist
python -m venv /tmp/smoke && /tmp/smoke/bin/pip install dist/*.whl
/tmp/smoke/bin/python tests/smoke_installed.py
```

## License

MIT — see [LICENSE](LICENSE).
