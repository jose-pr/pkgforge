# buildutils

[![CI](https://github.com/jose-pr/buildutils/actions/workflows/test.yml/badge.svg)](https://github.com/jose-pr/buildutils/actions/workflows/test.yml)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-blue)](https://jose-pr.github.io/buildutils/)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://pypi.org/project/buildutils/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Stage files into a *build root* and record their intended install metadata
(mode, owner, group, type, and free-form key/value `meta`) in a *file DB*
(JSON Lines, YAML, or SQLite),
then dump that DB into packaging manifests — an RPM `%files` list or Debian
`install` + `permissions` files.

`buildutils` is a small, dependency-light helper for unattended build pipelines
on Linux: install a source into place, remember how it should be owned and
permissioned, and emit that record for the packager.

## Install

```sh
pip install buildutils
```

Requires Python 3.9+. Runtime operations use POSIX facilities (`chmod`, `chown`,
symlinks, hardlinks), so runtime targets Linux; the CLI and `--help` import
cleanly on any platform. Tar-family archives extract via stdlib `tarfile` — no
external archiver needed; `bsdtar` is only a fallback for other formats (e.g.
`.iso`).

## Quick start

```sh
export BUILDROOT=/tmp/stage BUILDUTILS_DB=/tmp/files.jsonl

buildutils initdb
buildutils install -p -m 755 -o root -g root ./build/tool /usr/bin
buildutils install -p -m 640 -o root -g adm  ./tool.conf  /etc
buildutils scan --missing /usr
buildutils dbdump -f rpmspecfiles rpm-files.txt
buildutils dbdump -f debian debian/
```

See [`examples/stage_and_package.sh`](examples/stage_and_package.sh) for a
runnable end-to-end walkthrough.

## Commands

| Command | Purpose |
| --- | --- |
| `initdb` | create or reset (truncate) the file DB |
| `install [opts] SRC… DEST` | stage a source and record its entry |
| `scan [opts] PATH` | walk a path and record an entry per file (`--missing` fills gaps) |
| `compact` | collapse an append-log DB to one record per live path |
| `dbdump -f FORMAT [OUT]` | render the DB into a packaging manifest |

Global options (also read from the environment):

| Option | Env | Meaning |
| --- | --- | --- |
| `--db PATH` | `BUILDUTILS_DB` | file DB to read/write (`-` for stdout/stdin) |
| `--db-format FMT` | `BUILDUTILS_DB_FORMAT` | backend: `jsonl` / `yaml` / `sqlite` (else from the `--db` suffix) |
| `--buildroot DIR` | `BUILDROOT` | staging root that maps to `/` in the DB |

Global flags work before or after the subcommand.

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

| Format | Output |
| --- | --- |
| `rpmspecfiles` | RPM `%files` lines (`%attr(...)`, `%dir`, `meta.rpmprefix`) to a file or `-` |
| `debian` | `install` + `permissions` files into an output directory (or `-`, sectioned) |

```
# rpmspecfiles
%attr(755,root,root) "/usr/bin/tool"
%config %attr(640,root,adm) "/etc/tool.conf"

# debian/install            # debian/permissions
usr/bin/tool usr/bin        /usr/bin/tool 755 root root
etc/tool.conf etc           /etc/tool.conf 640 root adm
```

## File entries

Each entry records `mode` (octal string, e.g. `644`), `owner`, `group`, `type`
(`file`/`directory`/`symlink`), and a `meta` map. Two sentinels defer a field to
the staged file: `-` ("leave at OS default") and `--` ("resolve from disk").

## Exclude / filter syntax

An `--exclude` statement is an optional leading `!` (negate), zero or more inline
tests, and a trailing glob:

```
(?type:file)**/*.pyc        # every .pyc file
!(?meta:keep=1)**/tmp/**     # keep entries tagged keep=1 under tmp/
```

Tests are `(?type:file|directory|symlink)` and `(?meta:key=value)`; prefix a test
name with `!` (`(?!type:file)`) to invert just that test.

## Documentation

Full docs at **<https://jose-pr.github.io/buildutils/>** — command reference,
file-DB model, exclude grammar, dump formats, and the API reference.

## Development

```sh
git clone https://github.com/jose-pr/buildutils && cd buildutils
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,docs]"

pytest -q                       # tests
python benchmarks/run.py        # benchmarks (add --save to record)
mkdocs serve                    # docs preview at http://127.0.0.1:8000
```

## License

MIT — see [LICENSE](LICENSE).
