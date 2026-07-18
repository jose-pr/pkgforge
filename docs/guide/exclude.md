# Exclude grammar

`--exclude`/`-X` (on `scan`, `install`, and `dbdump`) takes a match statement:
an optional leading `!` (negate), zero or more inline tests, and a trailing
glob pattern.

```
(?type:file)**/*.pyc        # every .pyc file
!(?meta:keep=1)**/tmp/**     # keep entries tagged keep=1 under tmp/
```

## Structure

```
[!] [(?[!]name:arg)...] <glob>
```

- **`!`** at the very start negates the whole statement.
- **`(?name:arg)`** is an inline test; prefix the name with `!`
  (`(?!type:file)`) to invert just that test.
- **`<glob>`** is a path glob (`*`, `**`, `?`); an absolute pattern is anchored
  to the scan/copy root.

## Tests

| Test | Matches when |
| --- | --- |
| `(?type:file\|directory\|symlink)` | the entry's type equals the argument |
| `(?meta:key=value)` | the entry's `meta[key]` equals `value` |

Multiple statements are evaluated in order; the first that yields a definite
match/skip decides. A statement with no definite result falls through to the
next, and an empty exclude set matches everything (nothing is excluded).
