# File DB & entries

The file DB maps a build-relative **path → entry** (or a removal). It has three
interchangeable storage **backends** — every command works the same regardless
of which is used, because they all load to the same shape.

## Backends

| Format | Extensions | Model |
| --- | --- | --- |
| `jsonl` (default) | `.jsonl`, `.ndjson` | append-only JSON Lines, last record per path wins |
| `yaml` | `.yaml`, `.yml` | append-only YAML: a single mapping, appended key by key; last duplicate key wins; needs `pip install 'pkgforge[yaml]'` |
| `sqlite` | `.db`, `.sqlite`, `.sqlite3` | a real SQLite store, upserted in place |

The backend is `--db-format` (or `PKGFORGE_DB_FORMAT`) when given, else the
`--db` file's **extension**, else `jsonl`. The file's content is never
inspected to choose: a file that is not in the chosen format (a YAML file named
`.jsonl`, a text file named `.db`) fails to load with a `DbError` that names the
format and suggests `--db-format`. A flow-style YAML file (`{...}`) must be
compacted (`pkgforge --db FILE compact`) before appending.
`jsonl` and `yaml` files are UTF-8, with a leading byte-order mark ignored; a
malformed one raises `DbError` naming the file (and line). JSON Lines records
split on `\n` only.

Every backend enforces the same record schema when it loads, and coerces or
defaults nothing: a path is a string starting with `/`; a record has exactly
the [entry fields](#entry-fields) below: `mode`, `owner`, `group`, `caps` and
`package` (strings), `type` (`file`, `directory` or `symlink`), `kind` (one of the
six kinds) and `meta` (string to string); a removal is `{"path": ..., "_removed": true}`
(jsonl), `null` (yaml) or a flagged row (sqlite). Anything else raises
`DbError` naming the file and the path, so a bad DB is one error at load time
instead of a crash while rendering. Reading a `sqlite` DB never writes to it: an empty or
schema-less file loads as an empty DB, and one with other tables but no
`entries` table (not a pkgforge DB) raises `DbError` instead of getting a
table added to it. A path or a field value that is not valid UTF-8 (an
undecodable file name, or a `pwd`/`grp` entry with one) is rejected with
`DbError`; `jsonl`/`yaml` store such a name as an escaped string instead.

```jsonl
# jsonl
{"caps": "cap_net_bind_service=ep", "group": "root", "kind": "file", "meta": {}, "mode": "755", "owner": "root", "package": "tool", "path": "/usr/bin/tool", "type": "file"}
{"caps": "-", "group": "adm", "kind": "config-noreplace", "meta": {}, "mode": "640", "owner": "root", "package": "tool", "path": "/etc/tool/config", "type": "file"}
```

The JSON Lines default is fast to write and parse and stays greppable —
`grep '"/usr/bin/tool"' files.jsonl` finds every record for a path. SQLite suits
very large DBs or when you want to query with SQL; YAML is the most
human-editable.

## Compaction

The `jsonl` and `yaml` backends are append logs: a path installed twice leaves
two records (the later wins) and a removal leaves a tombstone. The
[`compact`](commands.md) command rewrites the log with one record per live path.
The `sqlite` backend upserts in place, but still records a removal as a
flagged row rather than deleting it outright; `compact` deletes those rows
and `VACUUM`s to reclaim the space. `initdb` starts a fresh, empty DB in any
backend.

`compact` writes a temp file beside the DB and renames it into place, so the
directory must be writable; a failure (a full disk, a kill) leaves the
original DB unchanged instead of truncated or torn. On POSIX, writers
(`install`, `scan`) and `compact` `flock` the DB file, so `compact` can run
safely alongside another process appending to the same DB. A symlinked
`--db` keeps its link (its target is what gets replaced); a hardlinked one
is detached from the hardlink by the next compact.

## Adding a backend

Backends are pluggable. A third-party package adds its own by subclassing
`DbProvider` with its own `NAME` and advertising it in the `pkgforge.dbs`
entry-point group — core pkgforge never needs to know about it:

```python
import pkgforge

class TomlDb(pkgforge.DbProvider):
    NAME = "toml"
    SUFFIXES = (".toml",)                        # infer from a --db suffix
    def load(self): ...
    def add(self, path, entry): ...
    def remove(self, path): ...
    def compact(self): ...
    def init(self): ...
```

```toml
# the plugin's pyproject.toml
[project.entry-points."pkgforge.dbs"]
toml = "tomlplug:TomlDb"        # name = module:Class, the name equal to NAME
```

Once the package is installed, the format is selectable with
`--db-format toml` and by a `.toml` `--db` suffix, with no import in the
calling script. A `DbProvider` is constructed as `provider_cls(path)` and must
implement `load`/`add`/`remove`/`compact`/`init`; `load()` returns
`{path: entry-or-None}` like the built-ins. Look one up directly with
`DbProvider.get("toml")`, or list every name with `DbProvider.names()` (which
includes advertised backends without importing them). A script can also
define the subclass itself: defining it registers it for that process.

A `NAME`, alias or suffix that another backend already holds is a
`ValueError` when the class is created, and the new class is not registered.
An advertised backend that fails to import, or whose entry point does not
name a `DbProvider` subclass with that `NAME`, stops the command with an error
naming the entry point; while inferring a format from a suffix it is logged as
a warning and skipped.

A `SUFFIXES` entry without a leading dot gets one (`"toml"` registers the same
as `".toml"`); a multi-dot suffix (e.g. `".tar.gz"`) is refused (raises
`ValueError` when the class is created), since it could never match a `--db`
path's suffix — and, like an invalid `NAME`/`ALIASES` clash, leaves the class
entirely unregistered rather than half-registered.

`batch()` is optional: a context manager yielding the provider around a run
of many writes (e.g. `scan`'s walk); the default does nothing. `sqlite`
overrides it to hold one connection open and commit every 1000 rows instead
of connecting and committing once per write, which is what makes `scan`
into a `sqlite` DB fast; the default is fine for a backend that doesn't need
this (an append-log provider's own `add`/`remove` already write immediately
either way).

## Entry fields

| Field | Meaning |
| --- | --- |
| `mode` | octal permission string, e.g. `"644"` |
| `owner` / `group` | user / group name |
| `type` | `file`, `directory`, or `symlink` |
| `kind` | how packaging treats the entry: `file`, `config`, `config-noreplace`, `doc`, `license` or `ghost`; set with `-k/--kind` or a rule, never `-` in a DB |
| `caps` | file capabilities for the package to set on install (one `setcap` clause), or `-` for none; set with `--caps` or a rule |
| `package` | the package the entry ships in: the `[package]` name or a `[[packages]]` name, or `-` when the configuration has no `[package]`; set with `-P/--package` or a rule, else the `[package]` name |
| `meta` | free-form string map (e.g. a symlink `target`), set with [`-O/--meta KEY=VALUE`](commands.md#install) |

A `yaml` DB's scalars load as strings (`mode: 0755` is `"0755"`).

A record has exactly these eight fields. A DB written before `package`
existed fails to load with an error ending `this DB was written by an older
pkgforge: run initdb and record again`; there is no migration.

## Sentinels

Two sentinel values let a command defer a field to the staged file:

- `-` — **leave at the OS default**: do not set this field (e.g. don't chown).
- `--` — **resolve from the file on disk**: read the actual value from the
  staged file when the entry is recorded.

For example, `install --mode=-- ...` (or `-m--`, or `-m auto`) records whatever
mode the staged file already has, while `install -m 644 ...` records and
applies `644`. The sentinel may also be detached (`-m -- ...`), and `-m` has
the `auto` alias (`owner`/`group` get none, since `auto` can be a real account
name).

`-m` is validated before anything is staged: it accepts 1-4 octal digits
(normalized, so `0644` is stored as `644`), `-`, `--` or `auto`. An explicit
empty value (`-m ""`) also exits 2. Empty `mode`/`owner`/`group` fields already
in a DB (e.g. written by an older pkgforge) are read back as `-`.

## Build root mapping

`--buildroot` is the staging directory that maps to `/` in the DB. Installing
`app.conf` to `/etc` under `--buildroot build/root` stages the file at
`build/root/etc/app.conf` and records it under the key `/etc/app.conf`. This
keeps the DB independent of where the staging happened, so a dump produces
absolute target paths a packager expects.

DESTINATION/PATH must resolve inside `--buildroot`: a `..` that climbs above
it, or a symlinked path component leading outside it, is refused before
anything is written or recorded, instead of silently landing (or reading and
writing) outside the build root. An in-root `..` (e.g.
`/usr/share/../lib/x`) is normalized, both on disk and in the recorded key.
