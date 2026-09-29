# Commands

```
pkgforge [--db DB] [--buildroot DIR] <command> ...
```

Global options are read from the command line or the environment:

| Option | Env | Meaning |
| --- | --- | --- |
| `--db PATH` | `PKGFORGE_DB` | file DB to read/write (`-` = write records to stdout; reads see an empty DB; `dbdump --stdin` reads records from stdin instead) |
| `--db-format FMT` | `PKGFORGE_DB_FORMAT` | backend: `jsonl` / `yaml` / `sqlite` (else inferred from the `--db` suffix) |
| `--buildroot DIR` | `PKGFORGE_ROOT` | staging root that maps to `/` in the DB; DESTINATION/PATH must resolve inside it |
| `-v, --verbose` | | raise the running command's log level (repeatable) |
| `-q, --quiet` | | lower the running command's log level (repeatable) |
| `--loglevel [NAME:]LEVEL[,...]` | | set a logger's level directly; `NAME` is a logger name (e.g. `pkgforge.scan`), omitted for the running command |

Global flags work either before or after the subcommand
(`pkgforge --db X install …` or `pkgforge install --db X …`). An empty
environment variable (e.g. `PKGFORGE_DB_FORMAT=""`, as a CI pipeline commonly
exports an unset input) counts as unset, same as leaving it out.

See [Exit codes and completion](unattended.md#exit-codes-and-completion) for
the exit-status contract and how to install shell completion.

## `initdb`

Create or reset (truncate) an empty file DB.

```bash
pkgforge -r stage --db stage.files.jsonl initdb
```

Keep the DB (and any `dbdump` output) outside `--buildroot`: `scan` skips its
own configured DB file (and, for `sqlite`, its `-journal`/`-wal`/`-shm`
sidecars) if it finds them inside the scanned tree, logging a warning, but
it cannot recognize an output file `dbdump` wrote there earlier.

With no `--db` (unset or `-`), `initdb` logs a WARNING and does nothing --
there's no file to create or reset. `install` and `scan` are unaffected by
that case: with no `--db`, they still stage/scan normally and print each
recorded entry as a JSON Lines line on stdout instead of writing to a file.

## `install`

Stage a source into the build root and record its entry. A directory or
archive install stages the whole tree but records **one** entry, for the
destination itself, unless `--record-tree` is set: pass it to also record
every path below the destination the DB doesn't already hold (see the
option table below), or follow up with `pkgforge scan --missing DEST` for a
tree staged by something else (see `scan` below) -- skip both and the
tree's files never reach the `rpmspecfiles`/`debian` output at all
(`rpmspecfiles` renders the one entry as `%dir`; `debian` skips directory
entries in `install` entirely).

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
| `-X, --exclude PATTERN` | exclude matches (against the install path): pruned from a directory source's copy, or from an archive source's extracted members and directories before anything is merged onto DESTINATION; with only file or symlink sources, logs a warning (nothing to filter) |
| `--method {copy,link,move}` (env `PKGFORGE_INSTALL_METHOD`) | how to stage a file or directory source; `copy` (default) is the only method that leaves the source completely untouched |
| `--record-tree` (env `PKGFORGE_INSTALL_RECORD_TREE`) | after installing a directory or archive DESTINATION, also record every path below it not already in the DB: owner/group/meta from this install, mode and type from disk (symlinks `-`), honouring `-X`; ignored for a file/symlink source, and a no-op under `--noentry` |
| `--chown` | apply the recorded owner/group (off by default); an unknown owner/group name exits 2 before anything is staged (a recorded-only name, without `--chown`, is never resolved) |
| `--remove-source` | delete the source after staging (files or directories), only once the entry is applied and recorded -- never when the source IS the staged destination, and refused (exit 2) up front when a directory source contains the resolved destination or the `--db` file |
| `--noentry` | stage but do not record a DB entry |
| `-O, --meta KEY=VALUE` | record free-form metadata on the entry (repeatable); consumed by a dump format (e.g. `rpmspecfiles`' `meta.rpmprefix`) or by a symlink's `meta.target` |

`-O rpmprefix=VALUE` prepends `VALUE` to the entry's `rpmspecfiles` line (see
[Dump formats](formats.md)):

```bash
pkgforge install -p -m 640 -O rpmprefix='%config(noreplace)' tool.conf /etc/tool
```

A symlink source (or an explicit `--type symlink`) records its `meta.target`
automatically from `readlink`; only a `-` (no) source needs `-O target=PATH`:

```bash
pkgforge install -D -t symlink -O target=/usr/lib/tool/bin - /usr/bin/tool
```

`--mode`, `--chown`'s owner/group names, and the `--db` directory (and,
without `-p`, the destination's parent directory) are all checked before
anything is staged, so a bad argument exits 2 with nothing on disk.

**`--method`** applies only to a filesystem file or directory source: a `-`
(stdin) source, `-x`/`--decompress`, an archive source and a symlink
source/type all ignore it, since none of them stage from an existing source
file the way a plain copy/link/move would -- setting
`PKGFORGE_INSTALL_METHOD=move` globally never breaks an archive install.

```bash
pkgforge install -p --method link ./build/tool /usr/bin
```

- `copy` (the default) never touches the source; this is the only method
  safe to use when the source is still needed afterwards.
- `link` hardlinks the source instead of copying its content -- fast, and
  free of disk use, but `-m`/`-o`/`-g`/`--chown` then change the *shared*
  inode, i.e. the source too. A file `os.link` can't span (a different
  filesystem, `fs.protected_hardlinks`, the per-inode link limit, or a
  permission error) falls back to a copy automatically, logging one WARNING
  per `install` invocation rather than one per file.
- `move` consumes the source: a file is renamed (or, across filesystems,
  copied then removed) into place, and a directory whose destination
  doesn't exist yet and has no `-X`/`--exclude` is renamed as a whole tree
  in one step; otherwise (an existing destination, or an `-X` that must
  leave some files behind) it moves file by file and removes any source
  directory left empty, keeping excluded files in place. If staging the
  entry (applying mode/ownership, or recording it) fails afterwards, the
  data is moved back onto the source before the error is reported -- except
  for the merge case above, which -- like a partial copy merge -- is not
  rolled back. `--remove-source` is redundant with `move` and a no-op there.

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

A directory copy never descends into its own resolved destination, the
build root, or the file DB, when any of them sits inside the source
directory (a project tree commonly contains its own build root, e.g.
Debian's `debian/tmp`) -- their parent directories are still created, just
possibly empty, instead of `shutil.copytree` recursing into its own output
until `RecursionError`.

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

With `-X/--exclude`, every member is extracted into a temporary directory
first (as it always is), then a matched file, symlink, or directory (with
its whole subtree) is removed there before anything is merged or moved
onto DESTINATION -- the same `-X` statements and install-path matching a
directory install's own copy uses. The extraction policy above still
applies to every member first: an archive holding a device node, FIFO or
socket is refused (exit 1) even when `-X` matches it, since a device node
or FIFO on disk is never how the exclusion is expressed.

Re-running a directory install onto an existing destination always works:
any stale destination symlink (from an earlier run, or left there by
something else) is replaced rather than causing a `FileExistsError` or,
for a regular-file source, being written through to wherever it points.

## `scan`

Walk a path under the build root and record a `FileEntry` for every
directory and file **below** PATH -- never PATH itself.

```bash
pkgforge scan [-m MODE] [--dir-mode MODE] [-o OWNER] [-g GROUP] [-O KEY=VALUE]... [--missing] [--drop-stale] [-X PATTERN] PATH
```

| Option | Meaning |
| --- | --- |
| `-m, --mode` | recorded on every **file** entry as given; `-` (default) leaves it unset, `--`/`auto` (write it as `--mode=--` or `-m--` -- a detached `-m --` is read as end of options and exits 2) reads the on-disk mode. Never applies to a directory or a symlink |
| `--dir-mode` | recorded on every **directory** entry instead of `-m`; same 1-4-octal-digit/`-`/`--`/`auto` grammar. Default: `--` (from disk) when `-m`/`--mode` is itself `--`, else `-` -- an explicit `-m` value is never inherited by directories |
| `-o, --owner` / `-g, --group` | recorded on every entry (file, directory or symlink) as given; `-` (default) leaves it unset, `--` reads the on-disk owner/group name |
| `-O, --meta KEY=VALUE` | recorded on every entry (file, directory or symlink), same as `-m`/`-o`/`-g` (repeatable) |
| `--missing` | only fill in entries absent from the DB, leaving existing ones (e.g. ones `install` already recorded) untouched -- without it, scan replaces them. A path whose entry was previously removed (e.g. by `--drop-stale`) is treated as absent and re-added if the file is still (or again) on disk; use `-X` to keep such a path out for good |
| `--drop-stale` | after scanning, remove (tombstone) each DB entry below PATH whose file is no longer on disk, so `dbdump` stops listing it. An entry matching `-X` is kept even if its file is gone (protects a deliberately-absent entry, e.g. an RPM `%ghost`). Needs a real `--db` file (exits 2 for an unset or `-` DB); never touches disk, only the DB |
| `-X, --exclude PATTERN` | skip paths matching the install path and prune an excluded directory's subtree (nothing below it is walked or recorded), the same as `install`; also protects a matching entry from `--drop-stale` |

`scan` always records each entry's type from the file on disk; it accepts
`-t/--type` for symmetry with `install`, but the option is hidden from
`--help` and has no effect. A symlink's mode is always recorded as `-`,
whatever `-m`/`--dir-mode` say -- Linux ignores a symlink's mode, and rpm
warns about (Debian's `permissions` manifest would misreport) an explicit
one. `-m`/`--dir-mode`/`-o`/`-g` default to `-` (unset), not the on-disk
value -- pass `--mode=--`/`--dir-mode=--`/`--owner=--`/`--group=--` to read
them from disk instead. **Never scan a directory the distro itself owns** (e.g.
`/usr`, `/usr/bin`, `/usr/share`, `/etc`): every directory scan walks
becomes an RPM `%dir` claim in `rpmspecfiles`, and a shared directory's
mode/owner/group there can conflict with the one the distro's own package
ships (see [Dump formats](formats.md)). Narrow PATH to a directory your
package alone owns, e.g. `scan --missing --mode=-- /usr/share/mypkg`, or
stage that directory explicitly first (`install -D -d ...`).

PATH must resolve inside `--buildroot`, the same as `install`'s DESTINATION.
A PATH that does not exist under the build root exits 2 with one message,
before anything is touched. A symlink PATH is recorded as one `symlink`
entry, never followed -- including a symlinked `--buildroot` itself for
`PATH /`, which is always walked. `scan` never records its own configured
DB file (or, for `sqlite`, its `-journal`/`-wal`/`-shm` sidecars) if it
finds it inside the scanned tree -- it logs one warning and continues.
Keep the DB, and any `dbdump` output, outside `--buildroot`.

## `compact`

Collapse an append-log DB (`jsonl`/`yaml`) to one record per live path,
dropping superseded records and removal tombstones.

```bash
pkgforge -r stage --db stage.files.jsonl compact
```

For `sqlite` it deletes removal rows and `VACUUM`s (it never accumulates an
append log to collapse). A no-op, with a WARNING, for a stdout/unset `--db`
or one naming a file that doesn't exist yet -- nothing is created.

## `dbdump`

Render the file DB into a packaging manifest. See [Dump formats](formats.md).
`FORMAT` is a format's own name (`rpmspecfiles`, `debian`) or one of its
aliases (`rpm`/`rpmspec` for `rpmspecfiles`, `deb` for `debian`) -- either
spelling produces identical output.

```bash
pkgforge dbdump -f FORMAT [-X PATTERN] [OUTPUT]
```

`--db` unset or `-`, or naming a file that doesn't exist, logs a WARNING
and dumps an empty manifest (exit 0 unchanged -- a missing DB has always
read as empty; this just makes that visible). A `--db` with entries but
none surviving `--exclude` also warns.

`dbdump --stdin` reads the file DB as JSON Lines from standard input
instead of `--db`/`PKGFORGE_DB` (both are ignored when given): pipe an
`install`/`scan` run with no `--db` configured straight into `dbdump` to
render a manifest without ever writing a DB file.

```bash
pkgforge install -D -m 644 ./build/tool /usr/bin/tool | pkgforge dbdump --stdin -f rpmspecfiles rpm-files.txt
```

```bash
pkgforge dbdump --stdin -f rpmspecfiles rpm-files.txt < records.jsonl
```

No environment variable enables `--stdin` -- it only ever comes from that
invocation's own command line. A terminal or already-closed stdin exits 2
immediately instead of waiting; otherwise it reads to EOF, so a pipe that is
never closed blocks (the caller's to close, same as any other stdin source).
Empty input logs a WARNING and dumps an empty manifest, exit 0.
