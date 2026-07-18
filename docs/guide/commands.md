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

Global flags work either before or after the subcommand
(`pkgforge --db X install …` or `pkgforge install --db X …`).

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
| `-m, --mode` | octal mode to apply/record (`-` = leave default, `--` = from disk) |
| `-o, --owner` / `-g, --group` | owner / group to record |
| `-t, --type` | `file` / `directory` / `symlink` (auto-detected from the source if unset) |
| `-d` | shortcut for `--type directory` |
| `-p, --parents` | create missing parent directories of the destination |
| `-T, --no-target-directory` | treat DESTINATION as the final path, not a directory |
| `-D` | shortcut for `-Tp` |
| `-x, --decompress [KIND]` | decompress the source (`gz`/`xz`/`bz2`; inferred if omitted) |
| `-X, --exclude PATTERN` | exclude matches when copying a directory source |
| `--chown` | apply the recorded owner/group (off by default) |
| `--remove-source` | delete the source after staging (files or directories) |
| `--noentry` | stage but do not record a DB entry |

A tar-family archive given as a `directory`-typed source is extracted with
stdlib `tarfile`; other archive types fall back to `bsdtar`.

## `scan`

Walk a path under the build root and record a `FileEntry` for each file.

```bash
pkgforge scan [--missing] [-X PATTERN] PATH
```

`--missing` only fills in entries absent from the DB (leaving existing ones
untouched); `-X/--exclude` skips matching paths. See
[Exclude grammar](exclude.md).

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
