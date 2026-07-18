# buildutils

Stage files into a build root and record their intended install metadata
(mode, owner, group, type, and free-form key/value meta) in a YAML *file DB*,
then dump that DB into packaging manifests such as an RPM `%files` list.

`buildutils` is a small, dependency-light helper for unattended build pipelines
on Linux: install a source into place, remember how it should be owned and
permissioned, and emit that record for the packager.

## Install

```sh
pip install buildutils
```

Requires Python 3.9+. The install/scan operations use POSIX facilities
(`chmod`, `chown`, symlinks, hardlinks, `bsdtar`), so runtime targets Linux;
the CLI and `--help` import cleanly on other platforms.

## Usage

```sh
buildutils [--db DB] [--buildroot DIR] <command> ...
```

Global options (also read from the environment):

| Option | Env | Meaning |
| --- | --- | --- |
| `--db PATH` | `BUILDUTILS_DB` | file DB to read/write (`-` for stdout/stdin) |
| `--buildroot DIR` | `BUILDROOT` | staging root that maps to `/` in the DB |

For unattended builds the environment variables are the primary mechanism —
set `BUILDROOT` and `BUILDUTILS_DB` once and every command picks them up. When
passing `--db`/`--buildroot` on the command line, place them **after** the
subcommand (`buildutils install --db X ... SRC DEST`).

### Commands

- **`initdb`** — create or reset (truncate) an empty file DB.
- **`install [opts] SOURCE... DEST`** — stage a source into the build root and
  record its entry. Supports files, directories (copytree or `bsdtar`
  extraction), symlinks, decompression (`-x`), `--parents`/`-p`,
  `--no-target-directory`/`-T` (and the `-D` shortcut for `-Tp`), `-d` for a
  directory, `--exclude`/`-X` filters, and `--chown` to apply recorded
  ownership.
- **`scan [opts] PATH`** — walk a path under the build root and record a
  `FileEntry` for each file (`--missing` only fills in entries absent from the
  DB; `--exclude`/`-X` skips matches).
- **`dbdump -f FORMAT [opts] [OUTPUT]`** — render the DB into a manifest.
  Formats: `rpmspecfiles` (emits `%attr(mode,owner,group) "path"` lines, with
  `%dir` for directories and an optional `rpmprefix` meta value).

### File entry fields

Each entry records `mode` (octal permission string, e.g. `644`), `owner`,
`group`, `type` (`file` / `directory` / `symlink`), and a `meta` map. The
sentinels `-` ("leave at OS default") and `--` ("resolve from the file on
disk") let a command defer a field to the staged file.

### Exclude / filter syntax

An `--exclude` statement is an optional leading `!` (negate), zero or more
inline tests, and a trailing glob:

```
(?type:file)**/*.pyc        # every .pyc file
!(?meta:keep=1)**/tmp/**    # keep entries tagged keep=1 under tmp/
```

Tests are `(?type:file|directory|symlink)` and `(?meta:key=value)`; prefix a
test name with `!` (`(?!type:file)`) to invert just that test.

## License

MIT — see [LICENSE](LICENSE).
