# pkgforge

[![Version](https://img.shields.io/pypi/v/pkgforge.svg)](https://pypi.org/project/pkgforge/)
[![Python versions](https://img.shields.io/pypi/pyversions/pkgforge.svg)](https://pypi.org/project/pkgforge/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/jose-pr/pkgforge/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-latest-blue.svg)](https://jose-pr.github.io/pkgforge/)
[![CI](https://img.shields.io/github/actions/workflow/status/jose-pr/pkgforge/test.yml)](https://github.com/jose-pr/pkgforge/actions/workflows/test.yml)

pkgforge stages files into a *build root* and records how each one is installed
— mode, owner, group, type, kind, capabilities and package — in a *file DB*.
**One staged tree and one record give you an RPM `%files` list or spec, a
`debian/` directory, or finished `.deb`, `.rpm`, Arch, Alpine and tar
packages.** Full documentation: <https://jose-pr.github.io/pkgforge/>.

## Features

- **Staging that records as it goes.** `pkgforge install` puts a file,
  directory, archive or standard input into the build root (copy, hardlink or
  move; decompress on the way; extract tar-family archives without restoring
  ownership or special files) and records its entry.
- **A project file.** `pkgforge.cfg` declares the package and its subpackages,
  default modes and owners, rules by path, the accounts the package creates, the
  directories it owns and the staging steps that `pkgforge stage` runs.
- **Manifests for the distribution tools.** `pkgforge dbdump` writes RPM
  `%files` lists, a whole RPM spec, and the debhelper files of a `debian/`
  directory, with a skeleton for `control`, `changelog` and `rules`.
- **Packages without the distribution tools.** `pkgforge package` writes `.deb`,
  Arch, Alpine and tar packages itself, and `.rpm` through `rpmbuild`;
  byte-identical builds with `SOURCE_DATE_EPOCH`.
- **A check before the build.** `pkgforge verify` lists the files the DB does not
  record, the entries whose files are missing, and changed types and symlink
  targets.
- **Install-time accounts.** `pkgforge sysfiles` writes the `sysusers.d` and
  `tmpfiles.d` files for the declared accounts and ghost directories; the rpm,
  Debian, Arch and Alpine outputs create them on install.
- **Three DB backends, or your own.** JSON Lines (default), YAML and SQLite; a
  third-party backend, dump format or package format is found through an entry
  point, and `pkgforge.testing` checks a backend or dump format against the
  contract the built-in ones pass.
- **A Python API.** `pkgforge.Project` does what the commands do, returns the
  records it wrote and never prints.
- **Unattended.** The global options read `PKGFORGE_*` variables, errors are one
  line with a fixed exit status, and `PKGFORGE_MCP=stdio` serves the commands as
  MCP tools.

## Installation

```sh
pip install pkgforge
```

| Extra | Adds |
| --- | --- |
| `yaml` | the YAML file-DB backend (PyYAML) |
| `zstd` | zstd package compression below Python 3.14, which has `compression.zstd` (zstandard) |
| `dev` | both extras above, plus black, build and pytest |
| `docs` | mkdocs, mkdocs-material and mkdocstrings, to build the docs site |

Requires Python 3.9+. Staging uses POSIX facilities (`chmod`, `chown`,
symlinks), so it runs on Linux; the command line and `--help` work on any
platform. A tar-family archive given as a path is extracted with the standard
library; standard input and other formats (such as `.iso`) need `bsdtar`.

## Quick start

```sh
work="$(mktemp -d)"
export PKGFORGE_ROOT="$work/stage" PKGFORGE_DB="$work/files.jsonl"
mkdir -p "$PKGFORGE_ROOT"
printf '[package]\nname = "tool"\n' > "$work/pkgforge.cfg"
export PKGFORGE_CONFIG="$work/pkgforge.cfg"

pkgforge initdb
pkgforge install -p -m 755 -o root -g root ./build/tool /usr/bin
pkgforge install -p -m 640 -o root -g adm -k config-noreplace ./tool.conf /etc
pkgforge install -D -d -m 755 -o root -g root --record-tree ./share /usr/share/tool
pkgforge dbdump -f rpmspecfiles rpm-files.txt
pkgforge dbdump -f debian debian/
```

The same package, described once in `pkgforge.cfg`:

```toml
[package]
name = "tool"
version = "1.0"
summary = "An example tool"
license = "MIT"
maintainer = "Jane Doe <jane@example.org>"

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

Stage it, check it and build the packages:

```sh
pkgforge stage --init
pkgforge verify
pkgforge package -f deb -o dist
pkgforge package -f rpm -o dist
```

From Python:

```python
from pkgforge import Project

project = Project.from_config("pkgforge.cfg", buildroot="stage", db="files.jsonl")
project.stage(init=True)
assert project.verify() == []
print(project.package("tar", "dist"))
```

[`examples/stage_and_package.sh`](https://github.com/jose-pr/pkgforge/blob/main/examples/stage_and_package.sh)
is a runnable walkthrough. The two-package example
[`examples/pkgforge.cfg`](https://github.com/jose-pr/pkgforge/blob/main/examples/pkgforge.cfg)
has its spec,
[`examples/hello.spec`](https://github.com/jose-pr/pkgforge/blob/main/examples/hello.spec),
and its [`examples/debian/`](https://github.com/jose-pr/pkgforge/tree/main/examples/debian)
written by `pkgforge dbdump -f rpmspec` and `pkgforge dbdump -f debian --skeleton`;
[From pkgforge.cfg to packages](https://jose-pr.github.io/pkgforge/guide/packaging/)
builds both, and the same packages with `pkgforge package`. The
[project file guide](https://jose-pr.github.io/pkgforge/guide/config/) lists every
key.

## Command line

| Command | Purpose |
| --- | --- |
| `install [opts] SRC… DEST` | stage a source into the build root and record its entry |
| `scan [opts] PATH` | record every file and directory below PATH (never PATH itself); `--missing` only fills gaps, `--drop-stale` removes entries whose files are gone |
| `initdb` | create or reset (truncate) the file DB |
| `compact` | collapse an append-log DB to one record per live path |
| `dbdump -f FORMAT [OUT]` | render the DB into a packaging manifest |
| `stage [--init]` | run the `[[stage]]` steps of `pkgforge.cfg` |
| `mkdir [opts] DEST…` | create directories and record their entries |
| `symlink [opts] TARGET LINK` | create a symbolic link and record its entry |
| `sysfiles` | write and record the `sysusers.d` and `tmpfiles.d` files for the declared accounts and ghost directories |
| `verify [-f {text,json}]` | check the build root against the DB: unrecorded, missing and changed paths (exit 1 when there are any) |
| `package -f FORMAT [-o DIR]` | build one package per declared package: `deb`, `rpm` (through `rpmbuild`), `arch`, `apk` or `tar`; prints each path |

`install --method {copy,link,move}` (env `PKGFORGE_INSTALL_METHOD`) stages a
file or directory source by hardlinking or moving it instead of copying it;
`install --record-tree` (env `PKGFORGE_INSTALL_RECORD_TREE`) also records the
contents of a directory or archive, not just DESTINATION itself. `scan -m`
applies to files, `--dir-mode` to directories, and a symlink never gets a mode;
`--mode=--`, `--owner=--` and `--group=--` read the value from disk. The
[commands guide](https://jose-pr.github.io/pkgforge/guide/commands/) has every
option.

Global options, before or after the subcommand (`--config` before it only):

| Option | Env | Meaning |
| --- | --- | --- |
| `--db PATH` | `PKGFORGE_DB` | file DB to read and write; `-` writes the records to stdout for `install` and `scan` and reads them from stdin for `dbdump` |
| `--db-format FMT` | `PKGFORGE_DB_FORMAT` | backend: `jsonl`, `yaml` or `sqlite` (else from the `--db` suffix) |
| `--buildroot DIR` | `PKGFORGE_ROOT` | staging root that maps to `/` in the DB; DESTINATION and PATH must resolve inside it |
| `--own PREFIX` | | a directory the package owns (repeatable); `dbdump --dirs owned` keeps only those |
| `--config FILE` | `PKGFORGE_CONFIG` | the project file; else `pkgforge.cfg` in the working directory |

The command line wins over the environment, which wins over `pkgforge.cfg`.
With no `--buildroot`, the build root is the working directory, and a working
directory of `/` is refused (exit 2); pass `--buildroot /` to target the live
filesystem on purpose.

### Exit status

| Status | Meaning |
| --- | --- |
| `0` | success |
| `1` | a runtime failure; one `pkgforge: error: ...` line on stderr (a closed output pipe is silent) |
| `2` | a usage mistake; one `pkgforge: error: ...` line on stderr |
| `130` | Ctrl-C, silently |

`--debug` or `PKGFORGE_TRACEBACK=1` also prints the traceback before the error
line.

## API overview

The importable surface; `pkgforge.cli`, the command line, is not library API.
The full contract is `pkgforge/AGENTS.md`, shipped inside the installed package,
and the [API reference](https://jose-pr.github.io/pkgforge/api/reference/).

| Module | Purpose |
| --- | --- |
| `pkgforge` | `Project`, the Python API for build scripts (every command's operation), `VerifyIssue`, the shared types, the exceptions and `__version__` |
| `pkgforge.config` | the `pkgforge.cfg` schema (`ProjectConfig` and its tables) and `load` |
| `pkgforge.entry` | `FileEntry`, `FileType`, `FileKind` and the functions that resolve, check and apply an entry |
| `pkgforge.db` | the file-DB backends (`jsonl`, `yaml`, `sqlite`, plus your own) and `open_db` |
| `pkgforge.exclude` | the `--exclude` match grammar shared by `install`, `scan`, `dbdump` and `verify` |
| `pkgforge.dbdump` | the manifest formats (`rpmspecfiles`, `rpmspec`, `debian`, plus your own) and `dump` |
| `pkgforge.package` | the package formats (`deb`, `rpm`, `arch`, `apk`, `tar`, plus your own) and `build` |
| `pkgforge.testing` | the contract checks a third-party DB backend or dump format must pass |
| `pkgforge.exceptions` | `PkgForgeError` and its subclasses |

## Dump formats

| Format | Aliases | Output |
| --- | --- | --- |
| `rpmspecfiles` | `rpm` | RPM `%files` lines (`%attr(...)`, `%dir`, `%caps(...)`, and `%config`, `%doc`, `%license` or `%ghost` from the entry kind) to a file or `-` (one package), or into an existing directory as one `files-PACKAGE.txt` per package; rpm 4.19+ |
| `rpmspecfiles-pre419` | `rpm-pre419` | the same for rpm older than 4.19: refuses a space, a glob character or `%` instead of packaging the wrong file |
| `rpmspec` | | the whole spec, from `pkgforge.cfg` (rpm 4.19+): one `%files -f` per package and a `%install` that runs `pkgforge stage`; an existing file is kept unless `--force` |
| `rpmspec-pre419` | | the same for rpm older than 4.19 (a `%pre` creates the accounts) |
| `debian` | `deb` | per package `P.install`, `P.dirs`, `P.conffiles`, `P.postinst`, `P.postrm` and `P.substvars`, and once `not-installed` and `fixperms`, into a directory (or `-`, sectioned); needs `[package] name`. With `--skeleton` also `control`, `changelog`, `rules` and `source/format`, kept unless `--force` |

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
# === tool.install ===
# Generated by pkgforge dbdump -f debian
etc/tool.conf etc
usr/bin/tool usr/bin
usr/share/tool/data.txt usr/share/tool
# === tool.dirs ===
# Generated by pkgforge dbdump -f debian
usr/share/tool
# === tool.conffiles ===
# === tool.postinst ===
#!/bin/sh
# Generated by pkgforge dbdump -f debian
set -e

#DEBHELPER#
# === tool.postrm ===
#!/bin/sh
# Generated by pkgforge dbdump -f debian
set -e

#DEBHELPER#
# === tool.substvars ===
# Generated by pkgforge dbdump -f debian
pkgforge:Depends=
# === not-installed ===
# Generated by pkgforge dbdump -f debian
# === fixperms ===
#!/bin/sh
# Generated by pkgforge dbdump -f debian. Usage: sh fixperms DEBIAN-DIR
set -e
d=${1:?usage: sh fixperms DEBIAN-DIR}
p() {
  for f in "$1" "$1.gz"; do
    if [ -e "$f" ] || [ -L "$f" ]; then printf '%s\n' "$f"; return; fi
  done
  echo "fixperms: skipping missing $1" >&2
  return 1
}
if t=$(p "$d"/tool/etc/tool.conf); then chown -- root:adm "$t"; chmod -- 640 "$t"; fi
if t=$(p "$d"/tool/usr/bin/tool); then chown -- root:root "$t"; chmod -- 755 "$t"; fi
if t=$(p "$d"/tool/usr/share/tool); then chown -- root:root "$t"; chmod -- 755 "$t"; fi
if t=$(p "$d"/tool/usr/share/tool/data.txt); then chown -- root:root "$t"; chmod -- 644 "$t"; fi
```

## File entries

Each entry records `mode` (octal string, e.g. `644`), `owner`, `group`, `type`
(`file`/`directory`/`symlink`), `kind` (how packaging treats it: `file`,
`config`, `config-noreplace`, `doc`, `license` or `ghost`; `-k`), `caps` (file
capabilities set on install; `--caps`), `package` (the package it ships in;
`-P`) and a `meta` map. Two sentinels defer a field to the staged file: `-`
("leave at the OS default") and `--` ("read it from disk", also spelled `auto`
for `-m`). `-m` accepts 1-4 octal digits or a sentinel and is checked before
anything is staged.

The file DB has three backends, and every command behaves the same with each:

| Format | Extensions | Model |
| --- | --- | --- |
| `jsonl` (default) | `.jsonl`, `.ndjson` | append-only JSON Lines |
| `yaml` | `.yaml`, `.yml` | append-only YAML (needs the `yaml` extra) |
| `sqlite` | `.db`, `.sqlite`, `.sqlite3` | SQLite table, updated in place |

The backend is `--db-format`, else the `--db` suffix, else `jsonl`; the file's
content is never sniffed.

## Exclude grammar

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
source (`install -X /tmp ./build /opt/app` excludes `./build/tmp`; an archive's
members count as the source), on `scan` to PATH, and on `dbdump` and `verify` it
is the DB path; a pattern without a leading `/` matches at any depth.
`install` and `scan` prune an excluded directory's subtree. See the
[exclude grammar guide](https://jose-pr.github.io/pkgforge/guide/exclude/).

## Development

```sh
git clone https://github.com/jose-pr/pkgforge && cd pkgforge
python -m venv .venv/3.14-posix-$(uname -m) && . .venv/3.14-posix-$(uname -m)/bin/activate
pip install -e ".[dev,docs]"

black src tests benchmarks      # format (Python 3.10+)
pytest -q                       # tests
python benchmarks/run.py        # benchmarks (add --save to record; see benchmarks/README.md)
mkdocs serve                    # docs preview at http://127.0.0.1:8000
```

Name the venv `<version>-<os>-<arch>` (`<os>` is `posix`/`nt`/`darwin`) if you
keep more than one interpreter around, e.g. to also test the `>=3.9` floor. The
[benchmarks README](https://github.com/jose-pr/pkgforge/blob/main/benchmarks/README.md)
lists the metrics and how results are named.

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
