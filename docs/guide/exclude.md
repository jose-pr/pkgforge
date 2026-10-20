# Exclude grammar

`--exclude`/`-X` (on `scan`, `install`, and `dbdump`) takes a match statement:
an optional leading `!` (negate), zero or more inline tests, and a trailing
glob pattern.

```
(?type:file)**/*.pyc            # every .pyc file, at any depth
!(?meta:keep=1)**/tmp/**        # keep entries tagged keep=1 under tmp/ ...
**/tmp/**                       # ... paired with a broader exclude (see below)
```

## Structure

```
[!] [(?[!]name:arg)...] <glob>
```

- **`!`** at the very start negates the whole statement: a plain statement
  *excludes* a matching path, a `!` one *keeps* it.
- **`(?name:arg)`** is an inline test; prefix the name with `!`
  (`(?!type:file)`) to invert just that test. A test's `arg` cannot contain
  `(` or `)`.
- **`<glob>`** is a path glob. `*`/`?` never cross a `/`; `[...]`/`[!...]` is
  a character class. A whole `**` segment matches zero or more path
  segments; a *trailing* `**` (or a bare `**`) matches one or more segments
  — the contents of a directory, never the directory itself, so
  `/opt/app/**` excludes everything below `/opt/app` but keeps `/opt/app`
  itself. A relative glob (no leading `/`) matches at any depth; an absolute
  one (a leading `/`) is rooted at the input — see below.

Statements are evaluated in order; the first that applies decides. With no
`--exclude`, or when no statement applies, the path is kept.

**Repeat the flag for each statement** — `-X` takes exactly one per
occurrence, so they never run together with the paths that follow:

```bash
pkgforge install -p -d -X '**/*.pyc' -X '(?type:directory)**/tmp' build /opt/app
```

Space-separating them after one flag (`-X '**/*.pyc' '**/tmp'`) does not add a
second statement: the extra word is read as a positional argument.

A malformed statement (an unknown test name, an invalid `(?type:...)`
argument, a `(?meta:...)` missing `=`, or an unterminated `(?...`) exits 2
naming the problem, instead of silently matching nothing.

## Tests

| Test | Matches when |
| --- | --- |
| `(?type:file\|directory\|symlink)` | the entry's type equals the argument |
| `(?meta:key=value)` | the entry's `meta[key]` equals `value` |

`(?meta:...)` sees `-O`/`--meta` on `install` and `scan` (the same values for
every path in that run) and the entry's *stored* meta on `dbdump`.

## Patterns name the input

A pattern is matched against the names of what the command reads, the way
`tar --exclude` matches member names. A leading `/` roots the pattern at that
input; without one it matches trailing segments at any depth.

| Command | Input | `-X /x` means | `-X x` means |
| --- | --- | --- | --- |
| `install SRC... DEST` | each SRC: a directory's tree, an archive's member names (as `tar -tf` lists them) | `SRC/x` | any path ending in `x`, below SRC |
| `scan PATH` | the tree at PATH | `PATH/x` | the same, below PATH |
| `dbdump` | the DB | the DB path `/x` | the same, any depth in the DB path |

The destination never takes part: `install -X /tmp build /opt/app` excludes
`build/tmp` (staged as `/opt/app/build/tmp`), and `-X /opt/app/build/tmp`
matches nothing. A pattern written with the destination prefix (an install
path) has to drop it. The same text can therefore select different files on
`install` and on `dbdump`: on `dbdump` the DB paths are the package paths.

Statements ending in `/**` are relative like any other: `-X '/docs/**'`
excludes everything below `SRC/docs` and keeps `SRC/docs` itself.

Pruning and `(?meta:...)` still differ by command:

| Command | An excluded directory | `(?meta:...)` sees |
| --- | --- | --- |
| `install` | pruned: nothing under it is copied | this run's `-O` values |
| `scan` | pruned: nothing under it is walked or recorded | this run's `-O` values |
| `dbdump` | dropped as its own row only: the DB is a flat key list, not a tree. Add a trailing `/**` to also drop everything below it | the entry's *stored* meta |

### A directory source

`build/` holds `tmp/x`, `lib/tmp/y` and `bin/app`:

```bash
pkgforge install -p -d -X /tmp build /opt/app
```

excludes `build/tmp` only, so `/opt/app/build/lib/tmp/y` is installed. With
`-X tmp` (no leading `/`) both `tmp` directories are excluded.

### An archive source

`install -X` filters an archive's extracted members and directories alike:
the archive is extracted into a temporary directory first, matched members
(and directories, with their whole subtree) are pruned there, and only what
survives is merged or moved onto DESTINATION. Patterns name the members as
`tar -tf` lists them:

```
$ tar -tf app-1.0.tgz
docs/
docs/readme.txt
bin/app
lib/docs/inner.txt
```

```bash
pkgforge install -p -d -D -X '/docs/**' app-1.0.tgz /opt/app
```

excludes `docs/readme.txt` and keeps `lib/docs/inner.txt`. A leading `./` on
the listed names (`tar -C dir -czf app.tgz .`) is not part of a name: write
`/docs/**` all the same. A device node, FIFO or socket found in the archive
is still refused (exit 1) even when `-X` matches it -- see the archive
extraction policy under [Commands](commands.md#install). With only file or
symlink sources `-X` logs a warning, since there is nothing to filter.

### `scan`

```bash
pkgforge scan -X /cache /usr/share/app
```

records everything below `/usr/share/app` except `/usr/share/app/cache`;
`/usr/share/app/sub/cache` is kept. With `--drop-stale`, an entry below PATH
is kept when its PATH-relative name matches, so `-X /ghost` protects
`/usr/share/app/ghost`.

### `dbdump`

```bash
pkgforge dbdump -f rpmspecfiles -X '/usr/share/doc/**' files.txt
```

matches the DB paths, so it drops every entry below `/usr/share/doc`.

`-X '*.fifo'` is the documented way to skip a FIFO or socket: a glob-only
statement never reads the file, so it can exclude a path `install`/`scan`
could not otherwise stage or record. A statement with an inline test
(`(?type:...)`/`(?meta:...)`) still has to read the file to evaluate it, so
one whose glob matches such a path still fails.

## The keep-rule pattern

A `!` statement only ever *keeps* a path — on its own it excludes nothing.
It only does something paired ahead of a broader exclude:

```bash
pkgforge install -p -d -X '!(?meta:keep=1)**/tmp/**' -X '**/tmp/**' build /opt/app
```

This drops everything under `tmp/` except entries whose `-O keep=1`
(`install`/`scan`) or stored `meta.keep == "1"` (`dbdump`) is set.
