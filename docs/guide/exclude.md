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
  itself. A relative glob (no leading `/`) matches at any depth, the same as
  today; an absolute one (a leading `/`) is anchored — see the table below
  for what each command anchors it to.

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

## Per-command differences

The same `-X /src/tmp` statement means a different root on each command, and
they prune differently:

| Command | An absolute pattern anchors at | An excluded directory |
| --- | --- | --- |
| `install` | the SOURCE directory | pruned: nothing under it is copied |
| `scan` | `<buildroot>/PATH` (the scanned path) | pruned: nothing under it is walked or recorded |
| `dbdump` | `/` (the DB key) | dropped as its own row only — the DB is a flat key list, not a tree. Add a trailing `/**` (e.g. `-X '/usr/lib/debug/**'`) to also drop everything below it |

A relative pattern (no leading `/`) sidesteps the anchor question entirely —
it matches by name at any depth within whichever root applies.

Examples, run with the same statement to show the difference:

```bash
pkgforge install -p -d -X /src/tmp SRC /opt/app   # anchored at SRC
pkgforge scan -X /src/tmp /opt                    # anchored at <buildroot>/opt, not /opt/src/tmp
pkgforge dbdump -X '/usr/lib/debug/**'             # anchored at '/', the DB key; drops the whole subtree
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
