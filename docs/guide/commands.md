# Commands

```
pkgforge [--db DB] [--buildroot DIR] <command> ...
```

Global options are read from the command line or the environment:

| Option | Env | Meaning |
| --- | --- | --- |
| `--db PATH` | `PKGFORGE_DB` | file DB to read/write (`-` for stdout/stdin) |
| `--db-format FMT` | `PKGFORGE_DB_FORMAT` | backend: `jsonl` / `yaml` / `sqlite` (else inferred from the `--db` suffix) |
| `--buildroot DIR` | `PKGFORGE_ROOT` | staging root that maps to `/` in the DB |
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
| `-T, --no-target-directory` | treat DESTINATION as the final path, not a directory |
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

With several SOURCEs, two that resolve to the same non-directory destination
(e.g. sharing a basename, or forced onto one path with `-T`/`-D`) exit 2
before anything is staged; directory (and archive) sources sharing a
destination still merge into it, as they always have.

A tar-family archive given as a `directory`-typed source is extracted with
stdlib `tarfile`; other archive types fall back to `bsdtar`.

## `scan`

Walk a path under the build root and record a `FileEntry` for each file.

```bash
pkgforge scan [--missing] [-X PATTERN] PATH
```

`--missing` only fills in entries absent from the DB (leaving existing ones
untouched); `-X/--exclude` skips matching paths. See
[Exclude grammar](exclude.md). `scan` always records each entry's type from
the file on disk; it has no `--type` option of its own.

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
