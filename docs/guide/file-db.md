# File DB & entries

The file DB is an append-only **JSON Lines** log: one JSON object per line, each
carrying a build-relative `path` plus the entry's fields (or
`{"path": ..., "_removed": true}` to mark a removal). `initdb` truncates it, each
`install`/`scan` appends a line, and on load the **last record for a path wins**.

```jsonl
{"group": "root", "meta": {}, "mode": "755", "owner": "root", "path": "/usr/bin/tool", "type": "file"}
{"group": "adm", "meta": {"rpmprefix": "%config(noreplace)"}, "mode": "640", "owner": "root", "path": "/etc/tool/config", "type": "file"}
```

The append-log shape is fast to write and to parse (JSON, not YAML), and stays
greppable — `grep '"/usr/bin/tool"' files.jsonl` finds every record for a path.

!!! note "Legacy YAML DBs"
    Databases written in the older single-document YAML format are still read
    transparently (auto-detected on load) and are upgraded to JSON Lines in
    place the next time an entry is written.

## Compaction

Because the DB is an append log, a path installed twice leaves two records (the
later wins) and a removal leaves a tombstone. `BuildUtil.compactdb()` rewrites
the file with one line per live path, dropping superseded records and removals.
`initdb` starts a fresh, empty DB.

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
