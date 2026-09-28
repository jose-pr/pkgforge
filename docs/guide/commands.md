# Commands

```
pkgforge [--db DB] [--buildroot DIR] <command> ...
```

Global options are read from the command line or the environment:

| Option | Env | Meaning |
| --- | --- | --- |
| `--db PATH` | `PKGFORGE_DB` | file DB to read/write (`-` for stdout/stdin) |
| `--db-format FMT` | `PKGFORGE_DB_FORMAT` | backend: `jsonl` / `yaml` / `sqlite` (else inferred from the `--db` suffix) |
| `--buildroot DIR` | `PKGFORGE_ROOT` | staging root that maps to `/` in the DB; DESTINATION/PATH must resolve inside it |
| `-v, --verbose` | | raise the running command's log level (repeatable) |
| `-q, --quiet` | | lower the running command's log level (repeatable) |
| `--loglevel [NAME:]LEVEL[,...]` | | set a logger's level directly; `NAME` is a logger name (e.g. `pkgforge.scan`), omitted for the running command |

Global flags work either before or after the subcommand
(`pkgforge --db X install …` or `pkgforge install --db X …`). An empty
environment variable (e.g. `PKGFORGE_DB_FORMAT=""`, as a CI pipeline commonly
exports an unset input) counts as unset, same as leaving it out.

**Exit status**: `0` success; `2` a usage mistake (a bad or missing argument
value, e.g. a missing source or an unknown `--chown` owner); `1` any other
failure (including a closed output pipe, e.g. `pkgforge dbdump ... | head`).
On failure, one `pkgforge: error: ...` line goes to stderr; set
`DUHO_TRACEBACK=1` to also print the traceback.

## `initdb`

Create or reset (truncate) an empty file DB.

```bash
pkgforge --db files.jsonl initdb
```

## `install`

Stage a source into the build root and record its entry.

```bash
pkgforge install [options] SOURCE... DESTINATION
```

| Option | Meaning |
| --- | --- |
| `-m, --mode` | 1-4 octal digits (`0644` is stored as `644`), `-` (leave default), `--`/`auto` (resolve from the staged file); ignored on disk for a symlink (recorded but not applied). Any other value, or an explicit empty value, exits 2 before anything is staged. Write `--mode=--`, `-m--` or `-m auto` -- a detached `-m --` is read as end of options and exits 2 |
| `-o, --owner` / `-g, --group` | owner / group to record |
| `-t, --type` | `file` / `directory` / `symlink`, in any case (auto-detected from the source if unset, or if given as `--`) |
| `-d` | shortcut for `--type directory`; not allowed with `-t`/`--type` (exit 2) |
| `-p, --parents` | create missing parent directories of the destination |
| `-T, --no-target-directory` | treat DESTINATION as the final path, not a directory (else, for an extracted archive, its archive suffix -- `.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`/`.tbz`, `.tar.xz`/`.txz`, `.iso`, `.zip`, matched case-insensitively -- is dropped from the destination name; a directory source keeps its own name unchanged) |
| `-D` | shortcut for `-Tp` |
| `-x, --decompress [KIND]` | decompress the source (`gz`, `xz`, `bz2`, `zst`, `lzma`, or a decompressor tool name such as `gunzip`/`unxz`, matched case-insensitively; inferred from the suffix if KIND is omitted) |
| `-X, --exclude PATTERN` | exclude matches when copying a directory source |
| `--chown` | apply the recorded owner/group (off by default); an unknown owner/group name exits 2 before anything is staged (a recorded-only name, without `--chown`, is never resolved) |
| `--remove-source` | delete the source after staging (files or directories), only once the entry is applied and recorded -- never when the source IS the staged destination, and refused (exit 2) up front when a directory source contains the resolved destination or the `--db` file |
| `--noentry` | stage but do not record a DB entry |

`--mode`, `--chown`'s owner/group names, and the `--db` directory (and,
without `-p`, the destination's parent directory) are all checked before
anything is staged, so a bad argument exits 2 with nothing on disk.

KIND is optional and consumes the next token: write `-x KIND SRC DST`,
`--decompress=KIND`, or `-x` after the paths. `-x SRC DST` makes `SRC` the
kind and is rejected. An unknown kind, or a bare `-x` whose source suffix
names none of the kinds above, exits 2 before anything is staged.

DESTINATION must resolve inside `--buildroot`: a `..` that climbs above the
root, or a symlinked path component that leads outside it, exits 2 before
anything is staged, instead of writing or recording outside it. An in-root
`..` (e.g. `/usr/share/../lib/x`) is normalized both on disk and in the
recorded key.

With several SOURCEs, two that resolve to the same non-directory destination
(e.g. sharing a basename, or forced onto one path with `-T`/`-D`) exit 2
before anything is staged; directory (and archive) sources sharing a
destination still merge into it, as they always have.

A tar-family archive given as a `directory`-typed source is extracted with
stdlib `tarfile`; other archive types (and a `-` stdin source, even a tar
stream) fall back to `bsdtar`. `tarfile` extraction also needs its
extraction filter (PEP 706; Python 3.9.17+/3.10.12+/3.11.4+, or 3.12+) --
without it, a tar-family source routes to `bsdtar` too, and if `bsdtar`
isn't installed either, extraction is refused (exit 1) rather than
extracting unfiltered.

**Archive extraction policy** (both paths): an archive's ownership,
setuid/setgid bit, group/other write bit, extended attributes, ACLs and
file flags are never restored on the staged tree -- even when running as
root -- and a device node, FIFO or socket found in an archive is refused
(exit 1); apply the mode/ownership you want with `-m`/`--chown` instead. The
`bsdtar` path always runs with `--no-same-owner --no-same-permissions
--no-xattrs --no-acls --no-fflags` (needs libarchive 3.3+) and then checks
the extracted tree for a special file. The `tarfile` path uses a staging
filter, not stdlib's `'data'`/`'tar'`: a symlink member's target is kept
exactly as stored, absolute or climbing above the destination included
(ordinary content for a build root), but writing through any symlink
between the destination and a member's own parent is refused, even one
that resolves back inside the destination; a hardlink member's target is
resolved against the destination and must stay inside it. Re-extracting
the same archive (or one archive after another) over an existing tree
replaces a stale entry at each member's path instead of failing.

Re-running a directory install onto an existing destination always works:
any stale destination symlink (from an earlier run, or left there by
something else) is replaced rather than causing a `FileExistsError` or,
for a regular-file source, being written through to wherever it points.

## `scan`

Walk a path under the build root and record a `FileEntry` for each file.

```bash
pkgforge scan [--missing] [-X PATTERN] PATH
```

`--missing` only fills in entries absent from the DB (leaving existing ones
untouched); `-X/--exclude` skips matching paths and prunes an excluded
directory's subtree (nothing below it is walked or recorded), the same as
`install`. See [Exclude grammar](exclude.md). `scan` always records each
entry's type from the file on disk; it has no `--type` option of its own.
PATH must resolve inside `--buildroot`, the same as `install`'s DESTINATION.

## `compact`

Collapse an append-log DB (`jsonl`/`yaml`) to one record per live path,
dropping superseded records and removal tombstones.

```bash
pkgforge --db files.jsonl compact
```

A no-op for a `sqlite` DB (it upserts in place) or a stdout/unset DB.

## `dbdump`

Render the file DB into a packaging manifest. See [Dump formats](formats.md).

```bash
pkgforge dbdump -f FORMAT [-X PATTERN] [OUTPUT]
```
