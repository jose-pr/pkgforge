# Project file: pkgforge.cfg

A package that is staged the same way on every build can describe that once in
`pkgforge.cfg`: where the build root and the file DB are, which directories the
package owns, the owner, group and modes its entries get, and the steps that
stage it. `pkgforge stage` then runs the steps, and the commands read the rest.

## Where the file is found

pkgforge reads one file, chosen in this order:

1. `--config FILE` (`-c FILE`), given **before** the command:
   `pkgforge --config build/pkgforge.cfg stage`.
2. The `PKGFORGE_CONFIG` environment variable. It must name an existing file
   (exit 2 otherwise); an empty value reads no file at all, not even a
   `pkgforge.cfg` in the working directory.
3. `pkgforge.cfg` in the working directory, when there is one.

With none of these, pkgforge runs on its flags and environment alone. The file
describes one package, so there is no per-user or system file.

A setting that can come from several places takes the first of: the command-line
flag, the environment variable, the file, the default. `buildroot`, `db`,
`db_format` and `own` are the settings the file and the command line share;
`own` has no environment variable.

## Format

The file is TOML. A first line `#!yaml` makes it YAML instead (this needs the
`yaml` extra, `pip install 'pkgforge[yaml]'`). In TOML the top-level keys come
before the first table. A relative `buildroot`, `db` (unless it is `"-"`) and a
`[[stage]]` step's `src` are relative to the directory of the file, so
`pkgforge --config /work/pkg/pkgforge.cfg stage` behaves the same from any
directory. A `dest`, `dir`, `file` and `symlink` are paths in the package, below
the build root, and are not.

Any other key is an error, in every table. The file describes one package, so
`!include` and `!load` in a YAML file are refused (exit 2).

```toml
buildroot = "build/root"
db = "build/files.jsonl"
own = ["/opt/tool"]

[package]
name = "tool"
version = "1.0"

[[stage]]
src = "bin/tool"
dest = "/opt/tool/bin"
mode = "755"
```

## Keys

### Top level

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `buildroot` | text | `.` | the build root; same as `--buildroot` |
| `db` | text | no file DB | the file DB; `"-"` means none. Same as `--db` |
| `db_format` | text | from the `db` suffix | `jsonl`, `yaml`, `sqlite` or a registered backend. Same as `--db-format` |
| `own` | list of text | none | directory prefixes the package owns, see [Owned directories](#owned-directories) |

### `[package]`

Facts about the package. Nothing reads them but `${package.KEY}` in the rest of
the file.

| Key | Type | Default |
| --- | --- | --- |
| `name` | text | required |
| `version`, `release`, `arch`, `summary`, `license`, `maintainer`, `description` | text | none |
| `depends` | list of text | none |

### `[defaults]`

What an entry gets when neither a flag nor a rule sets it.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `owner`, `group` | text | none | for every entry |
| `file_mode` | text | none | a regular file |
| `exec_mode` | text | none | a regular file with an execute bit on the staged file |
| `dir_mode` | text | none | a directory |

### `[[rules]]`

Any number of rules. Each sets `mode`, `owner`, `group`, `kind` and `caps` for the
entries whose path matches.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `match` | list of text | required, not empty | `-X` statements, see the [exclude grammar](exclude.md). A rule fits an entry when its statements match the entry's path in the file DB, which is always `/`-rooted |
| `mode` | text | none | |
| `owner`, `group` | text | none | |
| `kind` | text | none | `file`, `config`, `config-noreplace`, `doc`, `license` or `ghost`; all but `file` and `ghost` fit a regular file only, so such a rule should match `(?type:file)` |
| `caps` | text | none | one `setcap` clause such as `cap_net_bind_service=ep`; a regular file that is not a ghost |

`/etc/hello/**` matches what is below `/etc/hello`, not the directory itself.

### `[[stage]]`

Each element is one step. A step names what it stages with exactly one of
`src`, `dir`, `file` and `symlink`; see [Steps](#steps).

| Key | Type | Default | Applies to | Meaning |
| --- | --- | --- | --- | --- |
| `src` | text | | `src` | the source to install, relative to the file |
| `dest` | text | required with `src` | `src` | where to install it: a directory, or the final path with `into = false` |
| `dir` | text | | `dir` | a directory to create and record |
| `file` | text | | `file` | an empty file to create and record |
| `symlink` | text | | `symlink` | the link to create and record |
| `target` | text | required with `symlink` | `symlink` | what the link points to, stored as written |
| `mode` | text | none | all but `symlink` | a link has no mode |
| `owner`, `group` | text | none | all | |
| `kind` | text | none | all | as `install -k`; a `dir` fits `file` or `ghost`, a `symlink` too |
| `caps` | text | none | `src`, `file` | as `install --caps` |
| `meta` | table of text | none | all | free-form metadata, as `install -O` |
| `type` | text | from the source | `src` | `file`, `directory` or `symlink` |
| `into` | boolean | `true` | `src` | `false` treats `dest` as the final path, as `install -T` |
| `decompress` | text | none | `src` | `auto` (from the source suffix), `gz`, `xz`, `bz2`, `zst` or `lzma` |
| `method` | text | `copy` | `src` | `copy`, `link` or `move` |
| `exclude` | list of text | none | `src` | `-X` statements; an absolute pattern is relative to the source |
| `record_tree` | boolean | `false` | `src` | also record everything below `dest`, as `install --record-tree` |

A key that does not apply to a step's kind is an error.

### `[[users]]` and `[[groups]]`

Accounts the package's files belong to. An owner or group that is not in Debian's
base-passwd set must be declared here, or recording it is an error (exit 2), a name
read from disk with `--` included.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | text | required | the account name. A created name matches `[a-z_][a-z0-9_-]{0,30}` and is not a base-passwd name |
| `description` | text | none | `[[users]]` only: no control character and no `"` |
| `home`, `shell` | text | none | `[[users]]` only: absolute, with no whitespace |
| `groups` | list of text | none | `[[users]]` only: supplementary groups, base-passwd or declared |
| `create` | boolean | `true` | `false` when the account already exists on the target |

A created account needs `[package] name`. A created user's group of the same name is
implied, so a `[[groups]]` element may not repeat it.

## Accounts

Debian's base-passwd accounts (`pkgforge.entry.BASE_USERS` and `BASE_GROUPS`:
`root`, `daemon`, `www-data`, `nobody`, the groups `adm`, `tty`, `disk`, `staff`,
and so on) exist on every target and need no declaration. Any other owner or group must be
declared, or recording it is an error (exit 2):

```text
pkgforge: error: install: owner 'jose' is neither a base-passwd account nor declared in [[users]]
```

The check runs when the file is loaded (`[defaults]`, `[[rules]]`, `[[stage]]`,
`[[users]].groups`), before `install` and `scan` stage anything, and again for every
record written, so an owner read from disk with `--` is checked too. With no
project file only base names pass.

`create = true` (the default) makes the package create the account at install
time; `create = false` names one a dependency creates, which writes no line but
still owns files at install time. A created name must match
`[a-z_][a-z0-9_-]{0,30}`; a base-passwd name is refused as a declaration.

```toml
[package]
name = "tool"

[[users]]
name = "tool"
description = "tool daemon"
home = "/var/lib/tool"
groups = ["adm"]

[[groups]]
name = "web"
create = false
```

`pkgforge sysfiles` records `/usr/lib/sysusers.d/NAME.conf` for the created
accounts and `/usr/lib/tmpfiles.d/NAME.conf` for the ghost directories, and
`pkgforge stage` runs it after the steps, so a package needs no other step for
them. How each format uses the accounts is in
[Build time vs install time ownership](formats.md#build-time-vs-install-time-ownership).

## Values

- A mode is text of one to four octal digits: `mode = "0644"`, never `0o644` or
  `644`. The sentinels `-`, `--` and `auto` are refused, since a file states
  values and never asks for one to be read from disk.
- An owner or group is an account name (`[A-Za-z_][A-Za-z0-9_.-]*`).
- Text stays text. A number or a boolean where text is expected is refused
  instead of being turned into some other text (`expected a quoted string, not
  int`); a TOML integer such as `0o644` would otherwise become the text `420`.
  Only `into` and `record_tree` are booleans.
- `${package.name}` is replaced by that key of the file, anywhere in a text value:
  any `${table.key}` works, and `${list.0}` an element of a list. `$${` is a
  literal `${`. A reference to something the file does not define is an error.
  The environment is never read.

```toml
[package]
name = "hello"

[defaults]
owner = "root"
group = "${package.name}"

[[users]]
name = "${package.name}"
```

## How an entry gets its mode, owner and group

For each of the three, in this order, the first that has a value wins:

1. The value given for the entry: a flag (`-m`, `-o`, `-g`) or the step's own
   `mode`, `owner` and `group`. `--`/`auto` reads the value from the staged file.
2. The first `[[rules]]` element whose `match` fits the entry's path, if it sets
   that field. Only the first fitting rule counts.
3. `[defaults]`: `owner` and `group`; for the mode, `dir_mode` for a directory,
   else `exec_mode` for a file with an execute bit, else `file_mode`.
4. Nothing: the field stays `-`, which dump formats leave to the OS. A symlink
   never gets a mode.

`kind` and `caps` follow the same order without step 3: a flag or the step's own
value, then the first fitting rule, then `file` and no capabilities.
An installed source, and what a `dir` or `file` step creates, is staged with the
mode that was recorded. The entries below a `record_tree = true` step are chmod'ed to the
mode they resolved to (a rule or `[defaults]`; with neither, they keep the mode
they have on disk). The owned directories and the entries `scan` records are
recorded only: they keep the mode they have on disk.

```toml
[defaults]
owner = "root"
group = "root"
file_mode = "644"
exec_mode = "755"

[[rules]]
match = ["/etc/hello/**"]
mode = "640"
group = "adm"

[[stage]]
src = "src/hello"
dest = "/usr/bin"

[[stage]]
src = "src/hello.conf"
dest = "/etc/hello"
```

`/usr/bin/hello` fits no rule; it is executable, so it is recorded
`755 root:root` from `[defaults]`. `/etc/hello/hello.conf` fits the rule, which
sets the mode and the group, and `[defaults]` supplies the owner: `640 root:adm`.
Giving the second step `mode = "600"` would record `600 root:adm`.

## Owned directories

`own` lists the directories the package owns. When an entry at or below an owned
prefix is recorded, each directory above it that is at or below an owned prefix
(the prefix included) is recorded first, top-down, unless the file DB holds it
already. Nothing is created or changed on disk for them. Each is resolved like any
other entry, so a rule and `[defaults]` give it its mode, owner and group.

```toml
own = ["/opt/tool", "/etc/tool"]

[defaults]
dir_mode = "755"

[[stage]]
src = "bin/tool"
dest = "/opt/tool/bin"
```

Staging `/opt/tool/bin/tool` records `/opt/tool` and `/opt/tool/bin` as directories
too. A prefix is a `/`-rooted path with no `..`, and not `/` itself.

`--own PREFIX` (repeatable) is the same setting on any command and replaces the
file's list. `pkgforge dbdump --dirs owned` keeps a directory entry only at or
below an owned prefix and leaves every other entry alone, so a manifest does not
claim `/usr` or `/usr/bin`; it is an error with no `own`.

## Steps

`pkgforge stage` runs the steps in order. Missing parent directories are created.
Every `src` is checked before the first step runs, so a missing source stages
nothing.

- `src` with `dest`: install the source into the directory `dest`, as
  `pkgforge install`. A directory or archive records one entry unless
  `record_tree = true`.
- `dir`: create the directory and record it, as `pkgforge mkdir`.
- `file`: create an empty file and record it. It is how a file that only the
  installed system creates, such as a log, gets an entry (with
  `kind = "ghost"`).
- `symlink` with `target`: create the link and record it, as `pkgforge symlink`.

`pkgforge stage --init` resets the file DB first, as `initdb` does; with no file
DB it logs a warning and goes on. It is the way to make a build start clean.
Each step logs one line on `pkgforge.stage`, and a last line reports
`staged N step(s), M record(s)`. With a file DB the records are in it. With none,
they are printed to standard output as JSON Lines. A file with no `[[stage]]`
steps is an error for `stage`.

## Errors

A problem with the file is exit 2 and one line naming the file and, where known,
the line or the key:

```text
pkgforge: error: b.cfg:3:9: Expected newline or end of document after a statement
pkgforge: error: j.cfg: expected a quoted string, not int [at rules[0].mode (ProjectConfig)]
pkgforge: error: a.cfg: ProjectConfig has no field(s) 'dbb'
pkgforge: error: d.cfg: dest: required with src [at stage[0] (ProjectConfig)]
pkgforge: error: PKGFORGE_CONFIG names no file: nope.cfg
pkgforge: error: a.cfg: defaults: owner 'jose' is neither a base-passwd account nor declared in [[users]] [(ProjectConfig)]
pkgforge: error: b.cfg: [[users]] and [[groups]] that create an account need [package] name [(ProjectConfig)]
pkgforge: error: c.cfg: kind: config needs a regular file, not a directory [at stage[0] (ProjectConfig)]
pkgforge: error: d.cfg: caps: invalid caps 'cap_net_raw=ep cap_net_admin=p': expected one lowercase setcap clause without spaces, such as cap_net_bind_service=ep [at rules[0] (ProjectConfig)]
pkgforge: error: e.cfg: name: 'daemon' is a base-passwd account: it needs no entry [at users[0] (ProjectConfig)]
pkgforge: error: g.cfg: groups[0]: 'svc' is the group of the created user of the same name: drop the [[groups]] element [(ProjectConfig)]
pkgforge: error: h.cfg: refusing !include: the policy does not allow includes [in h.cfg; included from h.cfg, line 1]
```

The whole file is checked before any command runs, whichever command it is. A
file that cannot be read is exit 1.

## The example package

`examples/pkgforge.cfg` describes the `hello` package that `examples/hello.spec` and
`examples/debian/` build:

```toml
--8<-- "examples/pkgforge.cfg"
```
