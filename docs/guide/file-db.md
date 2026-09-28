# File DB & entries

The file DB maps a build-relative **path → entry** (or a removal). It has three
interchangeable storage **backends** — every command works the same regardless
of which is used, because they all load to the same shape.

## Backends

| Format | Extensions | Model |
| --- | --- | --- |
| `jsonl` (default) | `.jsonl`, `.ndjson` | append-only JSON Lines, last record per path wins |
| `yaml` | `.yaml`, `.yml` | append-only YAML: a single mapping, appended key by key; last duplicate key wins |
| `sqlite` | `.db`, `.sqlite`, `.sqlite3` | a real SQLite store, upserted in place |

The backend is chosen from the `--db` file's **extension**; `--db-format`
(or `PKGFORGE_DB_FORMAT`) overrides it. When *reading* an existing file, its
actual content is sniffed, so a legacy or mislabeled file still loads. A
flow-style YAML file (`{...}`) must be compacted
(`pkgforge --db FILE compact`) before appending.
`jsonl` and `yaml` files are UTF-8; a malformed one raises `DbError` naming
the file (and line). Reading a `sqlite` DB never writes to it: an empty or
schema-less file loads as an empty DB, and one with other tables but no
`entries` table (not a pkgforge DB) raises `DbError` instead of getting a
table added to it. A path or a field value that is not valid UTF-8 (an
undecodable file name, or a `pwd`/`grp` entry with one) is rejected with
`DbError`; `jsonl`/`yaml` store such a name as an escaped string instead.

```jsonl
# jsonl
{"group": "root", "meta": {}, "mode": "755", "owner": "root", "path": "/usr/bin/tool", "type": "file"}
{"group": "adm", "meta": {"rpmprefix": "%config(noreplace)"}, "mode": "640", "owner": "root", "path": "/etc/tool/config", "type": "file"}
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

Backends are pluggable. A third-party package adds its own simply by
subclassing `DbProvider` with its own `NAME` — core pkgforge never needs to
know about it, and no separate registration call is needed:

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

    @staticmethod
    def sniff(head: bytes) -> bool:               # or claim a file by content
        return head.startswith(b"#toml")
```

Once defined, the format is selectable with `--db-format toml`, by a `.toml`
`--db` suffix, or by content sniffing on read (`sniff` counts only when a
class's own body defines it — a subclass that does not redeclare it is not
consulted). A `DbProvider` is constructed as `provider_cls(path)` and must
implement `load`/`add`/`remove`/`compact`/`init`; `load()` returns
`{path: entry-or-None}` like the built-ins. Look one up directly with
`DbProvider.lookup("toml")`, or list every registered name with
`DbProvider.names()`.

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
| `meta` | free-form string map (e.g. `rpmprefix`, a symlink `target`) |

A `yaml` DB's scalars load as strings (`mode: 0755` is `"0755"`); a missing
`meta` loads as `{}`, a missing `mode`/`owner`/`group` as `-`.

## Sentinels

Two sentinel values let a command defer a field to the staged file:

- `-` — **leave at the OS default**: do not set this field (e.g. don't chown).
- `--` — **resolve from the file on disk**: read the actual value from the
  staged file when the entry is recorded.

For example, `install --mode=-- ...` (or `-m--`, or `-m auto`) records whatever
mode the staged file already has, while `install -m 644 ...` records and
applies `644`. A detached `-m -- ...` is read by argparse as the end of
options, not a value, and exits 2 -- write the sentinel attached, or use the
`auto` alias (`-m` only: `owner`/`group` get no such alias, since `auto` can be
a real account name).

`-m` is validated before anything is staged: it accepts 1-4 octal digits
(normalized, so `0644` is stored as `644`), `-`, `--` or `auto`. An explicit
empty value (`-m ""`) also exits 2. Empty `mode`/`owner`/`group` fields already
in a DB (e.g. written by an older pkgforge) are read back as `-`.

## Build root mapping

`--buildroot` is the staging directory that maps to `/` in the DB. Installing
`app.conf` to `/etc` under `--buildroot /tmp/stage` stages the file at
`/tmp/stage/etc/app.conf` and records it under the key `/etc/app.conf`. This
keeps the DB independent of where the staging happened, so a dump produces
absolute target paths a packager expects.

DESTINATION/PATH must resolve inside `--buildroot`: a `..` that climbs above
it, or a symlinked path component leading outside it, is refused before
anything is written or recorded, instead of silently landing (or reading and
writing) outside the build root. An in-root `..` (e.g.
`/usr/share/../lib/x`) is normalized, both on disk and in the recorded key.
