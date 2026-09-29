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
  one (a leading `/`) is anchored at the install path — see below.

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

## Every command matches the install path

An absolute pattern is matched against the same coordinate on every
command: the `/`-rooted install path — the path an entry already has in the
file DB (`dbdump`) or will get there (`install`/`scan`) — never a
command-local root. They still prune differently, and `(?meta:...)` still
reads from a different source:

| Command | An excluded directory | `(?meta:...)` sees |
| --- | --- | --- |
| `install` | pruned: nothing under it is copied | this run's `-O` values |
| `scan` | pruned: nothing under it is walked or recorded | this run's `-O` values |
| `dbdump` | dropped as its own row only — the DB is a flat key list, not a tree. Add a trailing `/**` (e.g. `-X '/usr/lib/debug/**'`) to also drop everything below it | the entry's *stored* meta |

A relative pattern (no leading `/`) matches by name at any depth against the
install path, the same on every command — it can span segments that come
from DESTINATION/PATH itself, not only ones under the copied/scanned tree.

`install -X` filters a directory source's copy and an archive source's
extracted members alike: an archive is extracted into a temporary
directory first, matched members (and directories -- their whole subtree)
are pruned there, and only what survives is merged or moved onto
DESTINATION. A device node, FIFO or socket found in the archive is still
refused (exit 1) even when `-X` matches it -- see the archive extraction
policy under [Commands](commands.md#install). With only file or symlink
sources `-X` logs a warning, since there is nothing to filter.

Examples, run with statements that target the same install path
(`install`/`scan` stage `build` at `/opt/app/build`):

```bash
pkgforge install -p -d -X /opt/app/build/tmp build /opt/app
pkgforge scan -X /opt/app/build/tmp /opt/app
pkgforge dbdump -X /opt/app/build/tmp -X '/opt/app/build/tmp/**'
```

A pattern written for the *old* source-/scan-root anchor (e.g. a bare
`-X /tmp` meant to reach a source's own top-level `tmp/`) can no longer
match any install path below a different DESTINATION/PATH; `install` and
`scan` each log one WARNING naming such a pattern instead of silently
matching nothing:

```
-X '/tmp' matches install paths; nothing below /opt/app/build can match it
```

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
