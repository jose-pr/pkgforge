# File DB & entries

The file DB is a YAML mapping of **build-relative path → entry** (or `null` to
mark a removal). It is append-only: `initdb` truncates it, each `install`/`scan`
appends, and on load the last write for a path wins.

```yaml
/usr/bin/tool:
  mode: "755"
  owner: root
  group: root
  type: file
  meta: {}
/etc/tool/config:
  mode: "640"
  owner: root
  group: adm
  type: file
  meta:
    rpmprefix: "%config(noreplace)"
```

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
