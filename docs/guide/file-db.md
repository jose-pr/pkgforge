# File DB & entries

The file DB maps a build-relative **path → entry** (or a removal). It has three
interchangeable storage **backends** — every command works the same regardless
of which is used, because they all load to the same shape.

## Backends

| Format | Extensions | Model |
| --- | --- | --- |
| `jsonl` (default) | `.jsonl`, `.ndjson` | append-only JSON Lines, last record per path wins |
| `yaml` | `.yaml`, `.yml` | append-only YAML documents, last mapping key wins |
| `sqlite` | `.db`, `.sqlite`, `.sqlite3` | a real SQLite store, upserted in place |

The backend is chosen from the `--db` file's **extension**; `--db-format`
(or `PKGFORGE_DB_FORMAT`) overrides it. When *reading* an existing file, its
actual content is sniffed, so a legacy or mislabeled file still loads.

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
The `sqlite` backend upserts in place, so it never accumulates history (compact
just reclaims space). `initdb` starts a fresh, empty DB in any backend.

## Adding a backend

Backends are pluggable. A third-party package can register its own by
subclassing `DbProvider` and calling `register_provider` at import time — core
pkgforge never needs to know about it:

```python
import pkgforge

class TomlDb(pkgforge.DbProvider):
    format = "toml"
    def load(self): ...
    def add(self, path, entry): ...
    def remove(self, path): ...
    def compact(self): ...
    def init(self): ...

pkgforge.register_provider(
    "toml",
    TomlDb,
    suffixes=(".toml",),                       # infer from a --db suffix
    sniff=lambda head: head.startswith(b"#toml"),  # or from file content
)
```

Once registered, the format is selectable with `--db-format toml`, by a `.toml`
`--db` suffix, or by content sniffing on read. A `DbProvider` is constructed as
`provider_cls(path)` and must implement `load`/`add`/`remove`/`compact`/`init`;
`load()` returns `{path: entry-or-None}` like the built-ins.

## Entry fields

| Field | Meaning |
| --- | --- |
| `mode` | octal permission string, e.g. `"644"` |
| `owner` / `group` | user / group name |
| `type` | `file`, `directory`, or `symlink` |
| `meta` | free-form string map (e.g. `rpmprefix`, a symlink `target`) |

## Sentinels

Two sentinel values let a command defer a field to the staged file:

- `-` — **leave at the OS default**: do not set this field (e.g. don't chown).
- `--` — **resolve from the file on disk**: read the actual value from the
  staged file when the entry is recorded.

For example, `install -m -- ...` records whatever mode the source already has,
while `install -m 644 ...` records and applies `644`.

## Build root mapping

`--buildroot` is the staging directory that maps to `/` in the DB. Installing
`app.conf` to `/etc` under `--buildroot /tmp/stage` stages the file at
`/tmp/stage/etc/app.conf` and records it under the key `/etc/app.conf`. This
keeps the DB independent of where the staging happened, so a dump produces
absolute target paths a packager expects.
