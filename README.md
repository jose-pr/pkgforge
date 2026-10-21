# pkgforge

[![PyPI](https://img.shields.io/pypi/v/pkgforge.svg)](https://pypi.org/project/pkgforge/)
[![Python](https://img.shields.io/pypi/pyversions/pkgforge.svg)](https://pypi.org/project/pkgforge/)
[![CI](https://github.com/jose-pr/pkgforge/actions/workflows/test.yml/badge.svg)](https://github.com/jose-pr/pkgforge/actions/workflows/test.yml)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-blue)](https://jose-pr.github.io/pkgforge/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/jose-pr/pkgforge/blob/main/LICENSE)

Stage files into a *build root* and record their intended install metadata
(mode, owner, group, type, and free-form key/value `meta`) in a *file DB*
(JSON Lines, YAML, or SQLite),
then dump that DB into packaging manifests — an RPM `%files` list or Debian
`install` + `dirs` + `fixperms` files.

`pkgforge` is a small, dependency-light helper for unattended build pipelines
on Linux: install a source into place, remember how it should be owned and
permissioned, and emit that record for the packager.

## Install

```sh
pip install pkgforge
pip install 'pkgforge[yaml]'   # also the YAML DB backend (PyYAML)
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
work="$(mktemp -d)"
export PKGFORGE_ROOT="$work/stage" PKGFORGE_DB="$work/files.jsonl"
mkdir -p "$PKGFORGE_ROOT"

pkgforge initdb
pkgforge install -p -m 755 -o root -g root ./build/tool /usr/bin
pkgforge install -p -m 640 -o root -g adm -k config-noreplace ./tool.conf /etc
pkgforge install -D -d -m 755 -o root -g root --record-tree ./share /usr/share/tool
pkgforge dbdump -f rpmspecfiles rpm-files.txt
pkgforge dbdump -f debian debian/
```

See [`examples/stage_and_package.sh`](https://github.com/jose-pr/pkgforge/blob/main/examples/stage_and_package.sh)
for a runnable end-to-end walkthrough, and
[`examples/hello.spec`](https://github.com/jose-pr/pkgforge/blob/main/examples/hello.spec)
and [`examples/debian/`](https://github.com/jose-pr/pkgforge/tree/main/examples/debian)
for packages that build from a file DB with `rpmbuild` and `dpkg-buildpackage`.

## Project file

A package staged the same way on every build can describe it once in
`pkgforge.cfg` and stage it with one command:

```toml
[defaults]
owner = "root"
group = "root"

[[rules]]
match = ["/etc/tool/**"]
mode = "640"

[[stage]]
src = "build/tool"
dest = "/usr/bin"
mode = "755"

[[stage]]
src = "tool.conf"
dest = "/etc/tool"
```

```sh
pkgforge stage --init
```

See the [project file guide](https://jose-pr.github.io/pkgforge/guide/config/)
for every key, the rules that decide an entry's mode, owner, group, kind and capabilities, the accounts a package declares, and the
owned directories.

## Commands

| Command | Purpose |
| --- | --- |
| `initdb` | create or reset (truncate) the file DB |
| `install [opts] SRC… DEST` | stage a source and record its entry |
| `scan [opts] PATH` | walk PATH, recording every file and directory below it (never PATH itself); `-m` applies to files only (directories take `--dir-mode`, symlinks never get a mode); fields default to `-` unless `--mode=--`/`--dir-mode=--`/`--owner=--`/`--group=--` reads them from disk; replaces existing entries unless `--missing`; `--drop-stale` removes entries whose files are gone |
| `compact` | collapse an append-log DB to one record per live path |
| `stage` | run the `[[stage]]` steps of `pkgforge.cfg` |
| `mkdir [opts] DEST…` | create directories and record their entries |
| `symlink [opts] TARGET LINK` | create a symbolic link and record its entry |
| `sysfiles` | write the `sysusers.d` and `tmpfiles.d` files for the declared accounts and ghost directories, and record them |
| `dbdump -f FORMAT [OUT]` | render the DB into a packaging manifest |

`install --method {copy,link,move}` (env `PKGFORGE_INSTALL_METHOD`) stages a
file or directory source by hardlinking or moving it instead of the default
copy; `install --record-tree` (env `PKGFORGE_INSTALL_RECORD_TREE`) also
records a directory or archive install's own contents, not just DESTINATION
itself -- see the [commands guide](https://jose-pr.github.io/pkgforge/guide/commands/#install).

Global options (also read from the environment):

| Option | Env | Meaning |
| --- | --- | --- |
| `--db PATH` | `PKGFORGE_DB` | file DB to read/write; `-` writes records to stdout for `install`/`scan` and reads them from stdin for `dbdump` (`compact` and `scan --drop-stale` refuse it) |
| `--db-format FMT` | `PKGFORGE_DB_FORMAT` | backend: `jsonl` / `yaml` / `sqlite` (else from the `--db` suffix) |
| `--buildroot DIR` | `PKGFORGE_ROOT` | staging root that maps to `/` in the DB; DESTINATION/PATH must resolve inside it |
| `--own PREFIX` | | a directory the package owns (repeatable); `dbdump --dirs owned` keeps only those |
| `--config FILE` | `PKGFORGE_CONFIG` | the project file; else `pkgforge.cfg` in the working directory. Before the command only |

Global flags (all but `--config`) work before or after the subcommand. With no `--buildroot` or
`PKGFORGE_ROOT`, the default is the current directory -- except that a cwd of
`/` is refused (exit 2); pass `--buildroot /` to target the live filesystem
on purpose.

## Exit status

| Status | Meaning |
| --- | --- |
| `0` | success |
| `1` | a runtime failure; one `pkgforge: error: ...` line on stderr (a closed output pipe is silent) |
| `2` | a usage mistake; one `pkgforge: error: ...` line on stderr |
| `130` | Ctrl-C, silently |

Set `PKGFORGE_TRACEBACK=1` to also print the traceback before the error line.

## Storage backends

The file DB has three interchangeable backends — every command behaves the same
regardless of which is used:

| Format | Extensions | Model |
| --- | --- | --- |
| `jsonl` (default) | `.jsonl`, `.ndjson` | append-only JSON Lines |
| `yaml` | `.yaml`, `.yml` | append-only YAML (needs `pip install 'pkgforge[yaml]'`) |
| `sqlite` | `.db`, `.sqlite`, `.sqlite3` | SQLite store, upserted in place |

The backend is picked from the `--db` extension (override with `--db-format`);
reading an existing file auto-detects its actual format.

## Dump formats

| Format | Aliases | Output |
| --- | --- | --- |
| `rpmspecfiles` | `rpm`, `rpmspec` | RPM `%files` lines (`%attr(...)`, `%dir`, `%caps(...)`, and `%config`, `%doc`, `%license` or `%ghost` from the entry kind) to a file or `-`; rpm 4.19+ |
| `rpmspecfiles-pre419` | `rpm-pre419` | Same, for rpm older than 4.19 (measured on 4.14/4.16/4.18): refuses a space, a glob character, or `%` instead of packaging the wrong file |
| `debian` | `deb` | `install`, `dirs`, `fixperms`, `conffiles`, `not-installed`, `postinst` and `postrm` files into an output directory (or `-`, sectioned) |

Output of the Quick start above:

```
$ pkgforge dbdump -f rpmspecfiles -
%config(noreplace) %attr(640,root,adm) "/etc/tool.conf"
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
# === dirs ===
usr/share/tool
# === fixperms ===
#!/bin/sh
# Generated by pkgforge dbdump -f debian. Usage: sh fixperms PACKAGE-DIR
set -e
d=${1:?usage: sh fixperms PACKAGE-DIR}
p() {
  for f in "$1" "$1.gz"; do
    if [ -e "$f" ] || [ -L "$f" ]; then printf '%s\n' "$f"; return; fi
  done
  echo "fixperms: skipping missing $1" >&2
  return 1
}
if t=$(p "$d"/etc/tool.conf); then chown -- root:adm "$t"; chmod -- 640 "$t"; fi
if t=$(p "$d"/usr/bin/tool); then chown -- root:root "$t"; chmod -- 755 "$t"; fi
if t=$(p "$d"/usr/share/tool); then chown -- root:root "$t"; chmod -- 755 "$t"; fi
if t=$(p "$d"/usr/share/tool/data.txt); then chown -- root:root "$t"; chmod -- 644 "$t"; fi
# === conffiles ===
# === not-installed ===
# === postinst ===
#!/bin/sh
# Generated by pkgforge dbdump -f debian
set -e

#DEBHELPER#
# === postrm ===
#!/bin/sh
# Generated by pkgforge dbdump -f debian
set -e

#DEBHELPER#
```

## File entries

Each entry records `mode` (octal string, e.g. `644`), `owner`, `group`, `type`
(`file`/`directory`/`symlink`), `kind` (how packaging treats it: `file`,
`config`, `config-noreplace`, `doc`, `license` or `ghost`; `-k`), `caps` (file
capabilities for the package to set on install; `--caps`) and a `meta` map. Two sentinels defer a field to
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
trailing `**` means "the contents of this directory"). Patterns name the input,
like `tar --exclude`: on `install` an absolute pattern is relative to each
source (`install -X /tmp ./build /opt/app` excludes `./build/tmp`; an
archive's members count as the source), on `scan` to PATH, and on `dbdump` it
is the DB path; a pattern without a leading `/` matches at any depth.
`install`/`scan` prune an excluded directory's subtree, while `dbdump`
decides entry by entry on its flat key list. See the
[exclude grammar guide](https://jose-pr.github.io/pkgforge/guide/exclude/)
for the full grammar and worked examples.

## Documentation

Full docs at **<https://jose-pr.github.io/pkgforge/>** — command reference,
file-DB model, exclude grammar, dump formats, and the API reference.

## API overview

The modules below are the importable surface; the command line, `pkgforge.cli`, is not library API.
See the full contract in `pkgforge/AGENTS.md`, shipped inside the installed
package, or the [API reference](https://jose-pr.github.io/pkgforge/api/reference/).

| Module | Purpose |
| --- | --- |
| `pkgforge` | `Project`, the Python API for build scripts, `__version__`, and the errors (`PkgForgeError`, `UsageError`) |
| `pkgforge.entry` | `FileEntry`/`FileType` records and their mode/owner/group resolution |
| `pkgforge.db` | the file-DB backend registry (`jsonl`/`yaml`/`sqlite`, plus your own) |
| `pkgforge.dbdump` | the packaging-manifest format registry (`rpmspecfiles`/`debian`, plus your own) |
| `pkgforge.exclude` | the `--exclude` match grammar shared by `install`/`scan`/`dbdump` |

## Development

```sh
git clone https://github.com/jose-pr/pkgforge && cd pkgforge
python -m venv .venv/3.14-posix-$(uname -m) && . .venv/3.14-posix-$(uname -m)/bin/activate
pip install -e ".[dev,docs]"

black src tests benchmarks      # format (Python 3.10+)
pytest -q                       # tests
python benchmarks/run.py        # benchmarks (add --save to record; see `benchmarks/README.md`)
mkdocs serve                    # docs preview at http://127.0.0.1:8000
```

Name the venv `<version>-<os>-<arch>` (`<os>` is `posix`/`nt`/`darwin`) if you
keep more than one interpreter around, e.g. to also test the `>=3.9` floor.

### Releasing

This project follows [Semantic Versioning](https://semver.org/) and keeps a
[`CHANGELOG.md`](https://github.com/jose-pr/pkgforge/blob/main/CHANGELOG.md).
Pushing a tag matching `v*` triggers the release workflow: test gate → build
(checking the tag names the version built) → a strict docs build as a gate →
GitHub release → publish. The release workflow never deploys the docs site
itself: for a final release its last job dispatches the docs workflow at the
tag, which owns every Pages deploy. Before 1.0, a MINOR version bump means
the documented API broke; everything else (fixes, additions) is a PATCH.

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

MIT — see [LICENSE](https://github.com/jose-pr/pkgforge/blob/main/LICENSE).
