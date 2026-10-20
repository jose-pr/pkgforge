# `pkgforge` — public API header

Header-file-style reference for the `pkgforge` package: every `__all__`
export with its signature, arguments, contract, and gotchas, so this module
can be consumed without reading its source. Kept current with the public
API. For the CLI overview see the shipped `README.md` or <https://github.com/jose-pr/pkgforge>.

`pkgforge.__all__`: `DbError`, `DbProvider`, `DumpError`, `DumpFormat`,
`ExcludeSyntaxError`, `FileEntries`, `FileEntry`, `FileRecords`, `FileType`,
`InvalidEntryError`, `PkgForgeError`, `Project`, `StrPath`,
`UnsupportedOutputError`, `UsageError`, `__version__`, `open_db`. The entry
functions (`apply_entry`, `entry_from_path`, `normalize_mode`,
`normalize_name`, `resolve_entry`) are in `pkgforge.entry`. `import pkgforge`
loads neither `duho` nor `pkgforge.cli`; the command line is documented under
[Command line](#command-line).

## Entry point

- **`pkgforge.cli:main(argv: Sequence[str] | None = None) -> int`** — run the
  command line: build the parser and dispatch the selected subcommand
  (`duho.run(PkgForge, argv, exit="parse")`: `--help`, `--version` and a
  parser usage error raise `SystemExit`, every other outcome is returned).
  Bound as the `pkgforge` console script and `python -m pkgforge`. This is
  pkgforge's only error boundary -- see [Exit status](#exit-status). Calling a
  library function (`Project.install` and the rest) raises the plain
  exception.
- **`__version__: str`** — the installed distribution version
  (`importlib.metadata.version("pkgforge")`); `"0.0.0"` when running from a
  source checkout that is not installed.

## Exit status

| Status | Meaning |
| --- | --- |
| `0` | success |
| `1` | a runtime failure (`PkgForgeError`, `OSError`), reported as one `pkgforge: error: ...` line on stderr; a closed output pipe also returns `1`, silently |
| `2` | a usage mistake (`UsageError`), reported as one `pkgforge: error: ...` line |
| `130` | Ctrl-C, silently |

Anything else propagates with its traceback. Pass `--debug`, or set
`PKGFORGE_TRACEBACK` to a true value (`1`/`true`/`yes`/`on`;
`0`/`false`/`no`/`off`/`n`/`f` are off), to also print the traceback before
the error line. External tools (`bsdtar`, the
decompressors) and `sqlite3` failures are reported the same way, with the
tool's last stderr line.

## Errors (`exceptions.py`)

- **`PkgForgeError(Exception)`** — base class for pkgforge's own runtime
  failures; caught by `pkgforge.cli.main()` and mapped to exit 1.
- **`UsageError(PkgForgeError, ValueError)`** — an argument-shaped mistake
  (a bad or missing value); caught by `pkgforge.cli.main()` and mapped to exit 2. Also a
  `ValueError`, since it reports malformed input. Raised by `install` for: a missing source, a `-`
  (stdin) source without `-T`/`-D`, a symlink source/type with no target
  (no `-O target=PATH`), an unresolvable stdin compression kind, a
  `--decompress` value that looks like a path, and an unknown `--chown`
  owner or group.
- **`DbError(PkgForgeError, ValueError)`** — a backend's on-disk content
  could not be read as one, or a value it was given could not be stored:
  for `jsonl`/`yaml`, a JSON/YAML parse error, non-UTF-8 bytes, a JSON Lines
  line or YAML top-level document that is not a mapping, or (once loaded) a
  field of the wrong type; for `sqlite`, a `.db` path that holds tables but
  no `entries` table (some other program's database, not one of pkgforge's
  own), or a path/mode/owner/group/type value that is not valid UTF-8 (an
  undecodable file name, or a `pwd`/`grp` entry containing one — `sqlite3`
  encodes `str` parameters strictly). The message names the DB file (and,
  for `jsonl`, the line number). Caught by `main()`'s error boundary
  like any `PkgForgeError` (one stderr line, exit 1); also a `ValueError`
  (malformed input). `jsonl`/`yaml` text
  I/O is always UTF-8, regardless of locale; an append to a DB whose last
  line lacks a trailing newline (e.g. a hand edit) repairs it first instead
  of fusing the new record onto the old one.
  Every loader (`jsonl`, `yaml`, `sqlite`) checks each record with the private
  `db._check_record(path, rec, where)` and coerces and defaults nothing: `path`
  is a `str` starting with `/`; a record is a mapping with exactly `mode`,
  `owner`, `group` (all `str`), `type` (`file`, `directory` or `symlink`) and
  `meta` (`str` to `str`), or a removal (jsonl `{"path": P, "_removed": true}`,
  yaml `null`, sqlite `removed=1`). Anything else raises `DbError` naming the
  file and the path. Content that is not in the chosen format (a YAML file read
  as `jsonl`, a text file read as `sqlite`) raises `DbError` that names the
  format and suggests `--db-format`. This never applies to a third-party
  `load()`. `YAMLDb`'s loader keeps only YAML's `null` implicit resolver
  (`~`/`null` still load as `None`, for tombstones) and drops
  int/float/bool/timestamp guessing, so an unquoted scalar loads as the text
  it was written as (`mode: 0755` is `"0755"`, not the int `493`). A UTF-8 byte
  order mark at the start of a text DB is ignored; lines split on `\n` only.
- **`ExcludeSyntaxError(UsageError)`** — raised for a malformed `-X` statement:
  an unknown inline test name, an invalid `(?type:...)`/`(?meta:...)` argument,
  or an unterminated `(?...`. Caught by `pkgforge.cli.main()`'s error boundary like
  any `UsageError` (one line, exit 2); the `-X` converter turns it into an
  argparse usage error at parse time.
- **`UnsupportedOutputError(UsageError, DumpError, NotImplementedError)`** —
  OUTPUT is the wrong shape for the chosen format (an existing file for
  `debian`, an existing directory for `rpmspecfiles`); raised by
  `check_output`. Caught by `main()`'s error boundary as a `UsageError`
  (one stderr line, exit 2, same as before); also a `DumpError` (the format
  itself is refusing this OUTPUT) and, per its name, a `NotImplementedError`.
- **`InvalidEntryError(UsageError, DumpError)`** — a DB entry that no format
  can render safely, found by `dbdump.dump` before any format renders (exit 2,
  naming the first offending entry in entry order and the field). The rules:
  the key is an absolute path with no empty, `.` or `..` segment and no
  control character; `type` is `file`, `directory` or `symlink`; `mode` is
  1-4 octal digits, `-` or `--`; `owner`/`group` are an account name
  (`[A-Za-z_][A-Za-z0-9_.-]*`, optionally ending in `$`), `-` or `--`; and
  `meta["rpmprefix"]` has no control character and no rpm expansion (`%(`,
  `%{`, `%[`). `%`, whitespace and
  control characters therefore cannot occur in mode/owner/group.
  `meta["rpmprefix"]` is otherwise written verbatim, as an rpm directive
  prefix. A format's `render()` called directly does not run this check; `dbdump.dump` does.
- **`DumpError(PkgForgeError, ValueError)`** — a DB path that a dump
  format's own tooling cannot represent (a `%` or a control character for
  `rpmspecfiles`; a control character for `debian`).
  Caught by `main()`'s error boundary like any `PkgForgeError` (one stderr
  line, exit 1). Every entry is rendered before OUTPUT is opened, so
  raising this leaves no partial file.

## File entries (`entry.py`)

- **`FileType(str, enum.Enum)`** — `File`, `Directory`, `Symlink`. 
  `FileType.from_path(path) -> FileType` inspects a real path (raises
  `TypeError` if it's none of the three — missing or a special file such as
  a FIFO or socket).
- **`FileEntry(typing.TypedDict)`** — one DB record: `mode` (octal permission
  **string**, e.g. `"644"`, not a raw `st_mode` int), `owner`, `group`,
  `type`, `meta: dict[str, str]`. A `FileEntry` value is a **plain dict** at
  runtime (it is a `TypedDict`), so it carries no methods of its own; use the
  module functions below.
  - **`entry_from_path(path: Path, meta: Optional[dict[str, str]] = None) ->
    FileEntry`** — build by `lstat`-ing a real path; owner/group resolve via
    `pwd`/`grp` (fall back to `"-"` if those modules are unavailable, i.e.
    non-POSIX).
  - **`resolve_entry(entry: FileEntry, path: Path, lookupval: str = "--",
    **overwrite) -> FileEntry`** — replace every field of `entry` equal to
    `lookupval` (default the `AUTO` sentinel) with the on-disk value for
    `path`, from a single `lstat`. A `pwd`/`grp` name lookup only happens for
    `owner`/`group` fields that actually equal `lookupval` -- an entry with
    an explicit owner/group never pays for one. uid/gid -> name lookups are
    memoized per process (`functools.lru_cache`, negative results included),
    so repeated ids across a large tree cost one real lookup each.
  - **`apply_entry(entry: FileEntry, path: Path, chown: bool = False, *,
    logger: Optional[logging.Logger] = None, usedefault: str = "-") -> None`**
    — if `chown=True`, `chown` first (raises `RuntimeError` if `pwd`/`grp` are
    unavailable; raises `UsageError` for an unknown owner/group), then
    `chmod` unless `mode` is `"-"` or empty (an empty mode/owner/group is
    treated like `"-"` everywhere this function reads them). Raises
    `UsageError` if `mode` is still the unresolved `AUTO` sentinel (`"--"`) —
    call `resolve_entry` first. Chown runs before chmod because Linux clears
    a regular file's setuid/setgid bit on any `chown()`, even to the same
    owner; if `chown=True` and the entry leaves `mode` at `usedefault`, any
    setuid/setgid/sticky bit already on disk is restored after the chown. A
    symlink's mode is never set on disk (skipped via `lstat`; Linux ignores
    it) — only its owner/group, and only its recorded `entry["mode"]`, are
    unaffected by this.
  - **`normalize_mode(value: str) -> str`** — normalize a
    `mode` value to an octal permission string; an `int` is not a mode and
    raises `UsageError`. `"-"`/`""` mean `DEFAULT`; `"--"` or `"auto"` mean
    `AUTO`; a string of 1-4 octal digits normalizes (`"0644"` -> `"644"`).
    Anything else raises `UsageError`. `auto` is a mode-only alias —
    `owner`/`group` get none, since `auto` can be a real account name.
  - **`normalize_name(value: str, field: str) -> str`** — validate an
    `owner`/`group` value (`field` names it in the error): an explicit empty
    string is `DEFAULT`, and anything but an account name
    (`[A-Za-z_][A-Za-z0-9_.-]*`, optionally ending in `$`) or the `-`/`--`
    sentinels raises `UsageError`.
  - **`check_meta(meta: Mapping[str, str]) -> None`** — raises `UsageError`
    for a control character or an rpm expansion (`%(`, `%{`, `%[`) in
    `meta["rpmprefix"]`.
- **`AUTO = "--"`** / **`DEFAULT = "-"`** — module-level sentinels: `AUTO`
  means "resolve from the file on disk" (used by `resolve_entry`); `DEFAULT`
  means "leave at the OS/system default, do not set explicitly" (used by
  `apply_entry`'s `usedefault`).
- **`mode_to_octal(mode: int) -> str`** — render a raw `st_mode` as a bare
  octal permission string (`"644"`).
- **`FileEntries`** — type alias `list[tuple[str, FileEntry]]` (surviving DB
  entries after `--exclude` filtering), the shared input shape for both dump
  format shapes.
- **`FileRecords`** — type alias `list[tuple[str, FileEntry | None]]`: what an
  operation wrote, in write order (`None` is a removal).
- **`StrPath`** — type alias `str | os.PathLike[str]`: a local filesystem path.

## Project (`project.py`)

The Python API for build scripts; the `pkgforge` command line is a wrapper
over it. Nothing in it prints, exits or reads `sys.argv`; it logs under the
operation loggers `pkgforge.install`, `pkgforge.scan`, `pkgforge.initdb`,
`pkgforge.compact` and `pkgforge.dbdump`. DB paths (`dest`, a scan `path`, a
DB key) are `/`-rooted POSIX `str`; local paths are `StrPath`.

- **`Project(buildroot: StrPath, db: StrPath | DbProvider | None = None, *,
  db_format: str | None = None)`** — a build root plus an optional file DB.
  `db_format` is a registered format name (else inferred from the `db`
  suffix, else `jsonl`). Validates and stores; opens nothing, holds no
  resource, so it is not a context manager, and is not thread-safe. With
  `db=None` no file is written and every operation returns the records it
  would have written. Attributes: `buildroot: Path`, `db: DbProvider | None`.
  Raises `TypeError` (a non-path `buildroot`/`db`), `ValueError` (`db="-"`, or
  a `DbProvider` together with `db_format`), `UsageError` (unknown `db_format`).
- **`localpath(dbpath: str) -> Path`** / **`dbpath(localpath: StrPath) -> str`**
  — translate between a DB path and the local path under the build root
  (`ValueError` when `localpath` is outside it).
- **`init() -> None`** — create or reset (truncate) an empty file DB, creating
  its parent directory. `UsageError` when `db` is `None`.
- **`load() -> Db`** — the whole DB, a removed path mapping to `None`; `{}`
  with no file DB or a file that does not exist yet. `DbError` for a malformed
  file. **`entries() -> FileEntries`** — the live entries sorted by path.
- **`compact() -> None`** — collapse the DB to one record per live path.
  `UsageError` when `db` is `None` or its file does not exist.
- **`install(sources: Sequence[StrPath], dest: str, *, mode="-", owner="-",
  group="-", type: FileType | None = None, meta: Mapping[str, str] | None = None,
  into=True, parents=False, decompress: str | None = None, method="copy",
  exclude: Sequence[str] = (), record=True, record_tree=False, chown=False,
  remove_source=False) -> FileRecords`** — stage each source under the build
  root at `dest` and record it; the `pkgforge install` command's behaviour.
  `sources` is a sequence (a bare `str`/`PathLike` is `TypeError`); `"-"` reads
  `sys.stdin.buffer` (one source at most) and `""` means no source (an empty
  file, or with `type=FileType.Directory` an empty directory). `dest` is a
  `str` (`TypeError` otherwise): the directory to install into, or with
  `into=False` the final path. `mode`/`owner`/`group` take a value, `"-"` (leave
  the default) or `"--"` (read it from the staged file). `decompress` is a kind
  (`gz`, `xz`, `bz2`, `zst`, `lzma`), `"auto"` (infer it from the source
  suffix) or `None`. `method` is `copy`, `link` or `move` (`UsageError`
  otherwise). `exclude` statements follow the exclude grammar below; an
  absolute glob is relative to each source. `record=False` stages without
  writing an entry. `record_tree=True` also records every path below an
  installed directory or archive that the DB does not hold. Every source is
  validated and resolved before any is staged, so a bad argument or two
  sources resolving to one non-directory destination leaves nothing on disk.
  Returns what was written, in write order. Raises `UsageError`,
  `PkgForgeError` and `OSError` exactly where the command exits 2 or 1.
- **`scan(path: str, *, mode="-", owner="-", group="-", meta=None,
  dir_mode: str | None = None, missing=False, drop_stale=False,
  exclude: Sequence[str] = ()) -> FileRecords`** — record every path below
  `path` (never `path` itself) with its own on-disk type; `mode` applies to
  files only, `dir_mode` to directories (default `"--"` when `mode` is `"--"`,
  else `"-"`); a symlink's mode is always `"-"`. `drop_stale=True` needs a file
  DB (`UsageError`).
- **`dump(fmt: str, output: StrPath | None = None, *, exclude: Sequence[str] =
  ()) -> Rendered`** — render the file DB with the format `fmt` (a name or
  alias). The format and OUTPUT shape are checked first, then the DB must
  exist (`UsageError` for `db=None` or a missing file); with `output` the
  result is also written whole (a file, or a directory for a multi-artifact
  format). See `dbdump.dump` for the rest and the errors.
- A `dest` (`install`) or `path` (`scan`) must resolve inside the build root:
  a `..` that climbs above it, or a symlinked component that leads outside
  it, raises `UsageError` before anything is written or recorded. A relative
  build root whose realpath is `/` (the cwd default, run from `/`) raises
  `UsageError` unless it was spelled `/` explicitly. An in-root `..`
  (`/usr/share/../lib/x`) is normalized in the path that is staged and
  recorded (`/usr/lib/x`).

## DB backends (`db/`)

- **`Db`** — type alias `dict[str, FileEntry | None]` (a loaded DB; `None`
  marks a removed path).
- **`DbProvider(pkgforge._registry.Registered, abc.ABC)`** — storage backend
  bound to a filesystem `path` (`provider_cls(path)`). Abstract methods:
  `load() -> Db`, `add(path, entry)`, `remove(path)`, `compact()`, `init()`.
  A subclass registers itself by declaring its own `NAME: str` (e.g.
  `NAME = "toml"`), or is advertised in the `pkgforge.dbs` entry-point group
  as `name = module:Class` (`name` equal to the class's `NAME`) and imported
  when first selected; `ALIASES: tuple[str, ...]` (extra selectable names) and
  `SUFFIXES: tuple[str, ...]` (file suffixes that infer this format from a
  `--db` path -- case-insensitive; a missing leading dot is added, e.g.
  `"toml"` registers the same as `".toml"`; a suffix with more than one dot,
  e.g. `".tar.gz"`, raises `ValueError` when the class is created, and
  registers nothing at all, not a half-registered class) are optional. A
  `NAME`, alias or suffix another class holds raises `ValueError` naming both
  classes, and the new class is not registered.
  Two classmethods (from `Registered`, shared with
  `dbdump.DumpFormat`): `DbProvider.get(name) -> type[DbProvider]`
  (`name` is `NAME` or an `ALIASES` entry, else the name of an entry point in
  the group, which is imported and registered; raises `UsageError`, also a
  `ValueError`, naming every registered name with its aliases for an
  unrecognized one, and `PkgForgeError` naming the entry point when it fails
  to import, is not a subclass of the family base with that `NAME`, or clashes
  with a registered class) and `DbProvider.names() -> list[str]` (every
  registered `NAME` plus every advertised name, sorted, nothing imported).
  Lookups are case-sensitive.
  **`batch(self) -> ContextManager[DbProvider]`** — non-abstract; a
  `contextlib.contextmanager` yielding `self`. The default does nothing
  extra (every `add`/`remove` inside it still writes exactly as it would
  outside one), so an existing or third-party provider that doesn't
  override it keeps working unchanged. `SQLiteDb` overrides it to hold one
  connection open across the whole batch, committing every 1000 rows and
  once more on exit (exception included, so a killed batch keeps whatever
  it already committed) instead of connecting, creating the schema and
  committing once per call. `scan` wraps its walk in one (`Project`'s
  private `_batch()`); a direct `add()`/`remove()` outside a batch, and
  every other command, are unaffected.
- **`jsonl_line(path: str, entry: FileEntry | None) -> str`** — the one JSON
  Lines record (`None` is a removal marker), newline-terminated, that
  `JSONLinesDb` appends and the commands print with no file DB.
  **`parse_jsonl(text: str, source: str = "text") -> Db`** — parse such text
  (the last record per path wins); errors read `{source}:{line}: ...`
  (`DbError`).
- **`open_db(path, fmt=None) -> DbProvider`** — resolve and construct the
  provider: an explicit `fmt` wins, else the `path` suffix decides, defaulting
  to `"jsonl"`. The file's content is never inspected; a file in another
  format fails in `load()`. Raises `UsageError` (also a `ValueError`) for an
  unknown format name -- see `DbProvider.get` above.
- **Built-in backends** (each its own module, imported -- and so registered
  -- at `pkgforge.db` import time): **`JSONLinesDb`** (`db/_jsonl.py`;
  `NAME = "jsonl"`, `SUFFIXES = (".jsonl", ".ndjson")`, default when unset) —
  append-only JSON Lines, one object per line, last record per path wins on
  load; **`YAMLDb`** (`db/_yaml.py`; `NAME = "yaml"`,
  `SUFFIXES = (".yaml", ".yml")`) —
  append-only YAML: a single mapping, appended key by key (the last
  duplicate key wins); a flow-style
  top-level document (e.g. `{/usr/bin/x: {...}}`, or `{}`) is read fine, but
  `add`/`remove` raise `DbError` instead of appending a block-style mapping
  after it (which would be invalid YAML) -- `compact()` rewrites the file in
  block style, unblocking further appends; **`SQLiteDb`**
  (`db/_sqlite.py`; `NAME = "sqlite"`, `SUFFIXES = (".db", ".sqlite", ".sqlite3")`)
  — a real upserted-in-place table, no append log
  (`compact()` drops removed rows + `VACUUM`s). `load()`/`compact()` never
  write: an empty or schema-less file loads as (or compacts as a no-op on)
  an empty DB, and a SQLite file that holds other tables but no `entries`
  table raises `DbError` (some other program's database) instead of
  getting one added to it. `add`/`remove` reject a `path` or a
  `mode`/`owner`/`group`/`type` value that is not valid UTF-8 with
  `DbError`, before any SQL runs for that record.
  `JSONLinesDb`/`YAMLDb` writes (`add`/`remove`/`init`) and `compact` serialize
  on an advisory `flock` of the DB file (a no-op off POSIX), so running
  `compact` alongside another pkgforge process appending to the same DB no
  longer drops that append. `compact` replaces the file with a new inode (a
  temp file written, fsynced and renamed into place) instead of truncating
  it in place, so a failed write (`ENOSPC`, a kill) leaves the original file
  untouched; a symlinked `--db` keeps its link, a hardlinked one is detached.
  `sqlite3` and PyYAML are both imported lazily (inside `SQLiteDb._connect`
  and a private `db._yaml._yaml_io()` respectively), not at module top:
  `import pkgforge`, `--help` and the `jsonl` backend all work on an
  interpreter that lacks one of them. Actually using the `sqlite`/`yaml`
  backend on such an interpreter raises `PkgForgeError` naming the missing
  module, instead of a bare `ImportError`; for `yaml` the message is
  `pip install 'pkgforge[yaml]'`, since PyYAML is the optional `yaml` extra
  and not a core dependency. `_yaml_io()` prefers PyYAML's
  libyaml-backed `CSafeLoader`/`CSafeDumper` over the pure-Python
  `SafeLoader`/`SafeDumper` when the installed build has them (several
  times faster; the on-disk format and duplicate-key last-wins behavior
  are unaffected either way).
- **`db/_appendlog.py`** — machinery shared by `JSONLinesDb`/`YAMLDb`:
  `AppendLogDb(DbProvider)` (declares no `NAME` of its own, so subclassing
  it alone registers nothing) implements `add`/`remove`/`compact`/`init` in
  terms of two hooks each backend supplies: `_record(path, entry) -> str`
  (one record's text) and `_render_all(live) -> str`
  (the whole compacted file's text for the surviving records). Also holds
  the on-disk mechanics: `_locked`, `_compact_lock`, `_atomic_write_text`,
  `_append_text` and `_read_text`.
- **`format_for_suffix(path: Path) -> str`** — suffix → registered format
  name, else `DEFAULT_FORMAT` (`"jsonl"`). Only a class's OWN `SUFFIXES`
  count (never one inherited from a parent class). A suffix no registered
  class holds imports every advertised `pkgforge.dbs` entry point not yet
  registered (a failure is one WARNING on logger `pkgforge.db`, then skipped)
  and looks again.

## Exclude / filter grammar (`exclude.py`)

- **`PathMatchStmt(argparse.Namespace)`** — one parsed `--exclude` statement: `negate:
  bool`, `tests: list[PathTest]`, `pattern: str`. `PathMatchStmt.parse(s) ->
  PathMatchStmt` parses `[!](?name:arg)*<glob>` (leading `!` negates the
  whole statement; each `(?name:arg)` or `(?!name:arg)` is an inline test,
  `!` inverts just that test). Registered test names: `type` (`(?type:file|
  directory|symlink)`) and `meta` (`(?meta:key=value)`). `.match(path,
  fileentry) -> bool | None` — a statement that applies returns `True`
  (exclude) or, when negated, `False` (keep); one that does not apply always
  returns `None`, "keep evaluating", so a statement never vetoes the ones
  after it and evaluation is order-independent for non-overlapping
  statements. `.anchored -> bool` is true when `pattern` is rooted (a
  leading `/`, or a Windows drive): it is matched from the root of the
  input rather than at any depth.
- **Glob engine**: `pattern` is matched with a private, cached
  `_glob_regex(pattern) -> re.Pattern`, not `PurePath.match` (whose `**` is a
  single non-recursive segment and disagrees between Python versions on a
  root-level key). `**` as a whole segment matches zero or more segments; a
  *trailing* `/**` (or a bare `**`) matches one or more segments — "the
  contents of this directory", never the directory itself. `*`/`?` never
  cross `/` and a `*` is translated as `fnmatch` does (linear time);
  `[...]`/`[!...]` is a character class, with `&`, `~` and `|` literal in
  it. A run of `**` segments is one. A relative pattern gets an implicit
  "at any depth" prefix.
- **`PathMatch(list[PathMatchStmt], root=None)`** — an ordered set of
  statements matched against the names of the input a command reads, the way
  `tar --exclude` matches member names. With a `root`, `.match` matches `/`
  plus the path's root-relative POSIX text (`/` for the root itself), so an
  absolute pattern is relative to the root; a path outside `root` raises
  `ValueError` (a caller bug). With no `root` (`dbdump`) the path is matched
  as its own POSIX text, a `str` DB key included. Statements are stored
  exactly as given. `.match(path, entry=None, _default=None, **overrides) ->
  bool | None` builds ONE candidate string for the call, then evaluates
  statements in order against it, first non-`None` result wins. The entry is
  derived lazily via `entry_from_path`, at most once, and only for a
  statement whose glob already matched and that actually has inline tests --
  a glob-only statement never lstats, types or does a pwd/grp lookup on the
  path, so `-X '*.fifo'` excludes a FIFO or socket instead of raising for
  it. `**overrides` are layered onto a COPY of the entry (never the caller's
  own dict, e.g. a live DB record in `dbdump`); an explicit `entry` of `{}`
  still counts as "the caller supplied one" (`is not None`, not a
  truthiness check). An empty `PathMatch` always matches (`True`).
## `dbdump` format registry (`dbdump/`)

- **`DumpFormat(pkgforge._registry.Registered, abc.ABC)`** — a packaging
  manifest format, selected by `dbdump -f NAME`. A subclass registers
  itself by declaring its own `NAME`/`ALIASES`, or is advertised in the
  `pkgforge.dump_formats` entry-point group (same mechanism and duplicate rule
  as `DbProvider`, see above; `DumpFormat.get(name)`/`DumpFormat.names()`
  work the same way too). Subclass one of the two shapes below, not
  `DumpFormat` directly.
- **`Rendered`** — type alias `bytes | dict[str, bytes]`: one file's bytes
  (a `PerEntryFormat`) or `{filename: bytes}` (a `MultiArtifactFormat`).
- **`dump(db: Mapping[str, FileEntry | None], fmt: str, output: StrPath | None
  = None, *, exclude: Sequence[str] = ()) -> Rendered`** — the dump pipeline
  over an already-loaded DB: look up `fmt` (a name or alias), check `output`'s
  shape (before anything is rendered), drop removed entries and those matching
  an `exclude` statement (an absolute glob is relative to the DB path), sort by
  path, validate (`InvalidEntryError`), render, and with `output` write it
  whole (a file, or a directory created if missing) logging `Wrote %s` per file
  on `pkgforge.dbdump`. Returns the rendered bytes. `UsageError` for an unknown
  format or statement, `UnsupportedOutputError` for a wrong-shape `output`,
  `DumpError` for an entry the format cannot represent.
- The format contract: `render(entries: FileEntries) -> Rendered` (writes
  nothing), `check_output(output: Path) -> None` (raises
  `UnsupportedOutputError`; there is no `-` case) and `write(rendered, output:
  Path) -> list[Path]` (whole or nothing; returns the files written).
- **`PerEntryFormat(DumpFormat)`** — renders one line per DB entry into one
  file. Abstract `render_entry(path, entry) -> bytes`;
  concrete `render(entries) -> bytes` (default: joins every `render_entry`
  call -- a format MAY override `render` itself to validate/render a whole
  batch at once, as `RPMSpecFiles` does below) and
  `write(rendered, output)` (OUTPUT whole or not at all: a sibling
  `.<name>.pkgforge-tmp` is written first and renamed over OUTPUT).
  `check_output` refuses an existing directory.
  Built in: **`RPMSpecFiles`** (`dbdump/_rpm.py`; `NAME = "rpmspecfiles"`,
  `ALIASES = ("rpm", "rpmspec")`) — RPM `%files` lines:
  `%attr(mode,owner,group) "path"`, `%dir` prefix for directories,
  `meta["rpmprefix"]` prepended if set. The path is quoted for rpm's
  `%files -f` parser (targets rpm 4.19+): `\` and `"` are escaped, the line
  is written as UTF-8 with `surrogateescape` (a non-UTF-8 name round-trips
  its original bytes), and glob characters are never escaped (rpm's own
  quoting already matches only the literal name). A `%` anywhere in the
  path raises `DumpError` -- no version of rpm treats a quoted `%` as
  literal, and `%(...)` runs a shell command -- as does any C0 control
  character or DEL. `RPMSpecFiles` overrides `render(entries)`: it
  validates the whole batch's paths in one pass (a single check over every
  path joined together decides whether ANY entry needs the slower per-entry
  rejection at all -- a clean batch, the common case, never runs a
  rejection check per entry) before rendering every line; `render_entry`
  renders a one-entry batch (`self.render([(path, entry)])`), so the two
  are always byte-identical. A symlink entry is written with mode `-`
  (rpm ignores a symlink's mode and warns on an explicit one). The
  escaping/rejection behavior itself is three class-attribute hooks
  (`_escape`, `_reject`, `_batch_reject_needed`), so a subclass changes only those, never
  `render`/`render_entry` themselves. Targets rpm 4.19+; below that, a
  quoted name is macro-expanded twice and glob characters are matched
  unescaped, so this quoting is not safe there -- use
  **`RPMSpecFilesPre419`** (`NAME = "rpmspecfiles-pre419"`,
  `ALIASES = ("rpm-pre419",)`) instead. It subclasses `RPMSpecFiles`,
  overriding only the three hooks above: the path is written completely
  unquoted (rpm's bare-token reader passes a literal `"`/`\` straight
  through, unescaped), and it raises `DumpError` for a path containing a
  space, a glob character (`* ? [ ] { }`), `%` (any form), or a non-UTF-8
  byte, instead of risking a silently wrong or overmatched package.
  Measured against real rpmbuild runs on rpm 4.14.3, 4.16.1 and 4.18.2 (see
  the formats guide's rpm-older-than-4.19 table for the full per-class
  verdicts); choose whichever of the two matches the build host's own
  `rpm --version`.
- **`MultiArtifactFormat(DumpFormat)`** — renders several named artifacts
  into a directory. Abstract `render(entries) -> dict[str, bytes]`; concrete
  `write(rendered, output)` (creates the directory, writes every artifact to a
  sibling `.<name>.pkgforge-tmp` and renames them all into place only once every
  write succeeded; a failure removes the temp files and leaves earlier OUTPUT
  content as it was). `check_output` refuses an existing non-directory.
  Built in: **`DebianFiles`** (`dbdump/_debian.py`; `NAME = "debian"`, `ALIASES = ("deb",)`) —
  `install` (`dh_install`-style `<src> <dest-dir>` lines, non-directory
  entries only) + `dirs`
  (`dh_installdirs`-style dest-escaped lines, one
  per directory entry, always written even when empty -- routed there and
  never through `install`, which would re-copy any children `--exclude`
  dropped -- so a directory recorded with no files under it, e.g. `install
  -d` for an empty state directory, still reaches the package) + `fixperms`
  (a POSIX `sh` script, always written, that applies every pinned
  mode/owner/group directly: run it from an `override_dh_fixperms` target, after
  `dh_fixperms` -- e.g. `sh debian/fixperms debian/<pkg>`. Per pinned
  entry: one `chown owner:group` (or `chown`/`chgrp` when only one is
  pinned), then `chmod` (`chown`
  before `chmod`, since `chown` clears a regular file's setuid/setgid bit
  even to the same owner); a symlink gets `chown -h`/`chgrp -h` and never a
  `chmod` (POSIX `chmod` has no `-h` and would follow the link to a target
  outside the package tree). Each path is written as `"$d"` (the script's
  required first argument, guarded by `${1:?}`) plus the DB path,
  `shlex.quote`d for POSIX `sh`; owner/group/mode go through a
  `functools.lru_cache`d quoting call, since those values repeat far more
  than paths do. Each entry resolves its path through a header function
  (`<path>` or the `<path>.gz` dh_compress renamed it to); a path that is
  gone (dh_usrlocal) is named on stderr and skipped, exit status 0. With
  no pinned entry, the script is just its own header). `install`'s
  source is debhelper-escaped (needs compat 13): a backslash before each
  glob character (`\ * ? [ ] { }`, which also makes a literal `${` read as
  literal since `{`/`}` get escaped), a space as `${Space}`, and a leading
  `#` backslash-escaped (a line starting with `#` is a `dh_install`
  comment); `dirs` gets the same leading-`#` case as `./#...` instead
  (`dh_installdirs` never globs, so there is nothing else to escape there).
  Every destination (`install`'s and `dirs`') is only ever
  `${Dollar}{`/`${Space}`-escaped, never glob-escaped -- `dh_install`/
  `dh_installdirs` take it literally. A control character in a path raises
  `DumpError` (one batch check over every path joined together; a clean
  batch runs no per-entry check). Each `install`/`dirs` line is escaped per
  entry by `_dh_src`/`_dh_dest`. Every
  artifact is written as UTF-8 with `surrogateescape` (a non-UTF-8 name
  round-trips its original bytes). The install destination is split with
  `posixpath`, not `os.path` -- DB paths are always POSIX-style
  ("/"-separated), and `ntpath` also treats a literal backslash as a
  separator, which split a path containing one (e.g. a source named
  `back\slash`) in the wrong place on Windows.

## Contract suite (`testing.py`)

- **`check_db_provider(cls: type[DbProvider], directory: StrPath) -> None`**
  and **`check_dump_format(cls: type[DumpFormat], directory: StrPath) ->
  None`** run every check against `cls` (a backend is built as `cls(path)` in
  a file named `contract` plus its first `SUFFIXES` entry, else `.db`; a
  format is built as `cls()`), each in its own subdirectory of `directory`.
  A failure is an `AssertionError` naming the class and the check; pytest is
  not imported. **`DB_PROVIDER_CHECKS`** and **`DUMP_FORMAT_CHECKS`** are
  tuples of the individual checks, each `(cls, directory) -> None`, for a
  runner that wants one test per check.
- A backend must: load `{}` after `init()`; round-trip an entry of each type
  with `meta` and a non-ASCII path exactly (`type` as its string value); let a
  second `add` of a path win; load a removed path as `None`; after `compact()`
  load every live entry and no `None`; and have every write made inside
  `batch()` visible to a fresh instance after it exits.
- A format must: render `bytes` (`PerEntryFormat`) or a `{name: bytes}` dict of
  non-empty values (`MultiArtifactFormat`); render the same entries to the same
  output twice; keep that type for no entries; refuse the wrong output shape
  with `UnsupportedOutputError` and accept the right one in `check_output`;
  and `write` returns the paths it wrote with bytes equal to `render`.

```python
from pkgforge.testing import check_db_provider

def test_my_backend(tmp_path):
    check_db_provider(MyDb, tmp_path)
```

## Command line

`pkgforge.cli` is not library API: it holds the root command, `main` and one
module per subcommand, each a thin call into one `Project` method. Use it through the `pkgforge` console
script or `python -m pkgforge`.

Global options, written before the subcommand: `--db PATH` (env `PKGFORGE_DB`;
`-` or unset writes records to stdout, and `dbdump` reads them from stdin),
`--db-format FMT` (env `PKGFORGE_DB_FORMAT`; a built-in or registered
`DbProvider` `NAME`, else exit 2), `--buildroot/-r DIR` (env `PKGFORGE_ROOT`,
else `.`), `--debug` (print the traceback of an error before its one-line
message), `-v`/`-q`/`--loglevel`, `--version` and
`--print-completion {bash,zsh,fish,powershell}`. Precedence is CLI, then the
environment, then the default; an empty environment value counts as unset.
`--help` of every subcommand lists them too. With no file DB, `install` and
`scan` print each record as one JSON Lines line (`db.jsonl_line`) on stdout
once the operation has succeeded.

Each subcommand logs under `pkgforge.<command>` (`pkgforge.install`,
`pkgforge.scan`, `pkgforge.dbdump`, `pkgforge.initdb`, `pkgforge.compact`):
control one with `--loglevel pkgforge.<command>:LEVEL`, or the running command
with `--loglevel LEVEL`. A bare `--loglevel pkgforge:LEVEL` has no effect on a
CLI-dispatched command, whose own logger always has an explicit level (INFO by
default).

With `PKGFORGE_MCP=stdio` the command tree is served as MCP tools
(`pkgforge.install`, ...) instead of running a command; standard input is the
protocol channel, so a `-` source of `install` and `dbdump --db -` are refused
there with a `UsageError`. See [Environment variables](#environment-variables).

- **`pkgforge install SOURCE... DESTINATION`** — maps the command line onto `Project.install`; the
  staging lives in the private `pkgforge._install` package (the per-source
  worker, the `-x` kind table, the tar-family/bsdtar extraction policy, the
  staging mixins and the `--record-tree` walk on the shared
  `pkgforge._tree.TreeRecorder`) —
  stage a source (file / directory / symlink / tar-family archive /
  decompress-on-copy) into the build root, apply mode/ownership, and record
  the entry. `-D` = `-Tp` shortcut, `-d` = `--type directory` shortcut.
  `--type=--` auto-detects from the
  source, same as leaving `-t` unset. Without `-T`, an extracted archive
  lands at `DESTINATION/<name minus its archive suffix>` (`.tar`,
  `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`/`.tbz`, `.tar.xz`/`.txz`, `.iso`,
  `.tar.zst`/`.tzst`, `.tar.lz4`, `.tar.lzma`/`.tlz`, `.tar.lz`, `.cpio`,
  `.zip`, matched case-insensitively, longest suffix first; any other name
  is unchanged); a directory source keeps its own
  name unchanged (`conf.tar.d` stays `conf.tar.d`).
  `-x`/`--decompress [KIND]` accepts `gz`, `xz`, `bz2`, `zst`, `lzma`
  (matched case-insensitively; a tool name such as `unxz` is not a kind); any other
  kind, or a bare `-x` whose source suffix matches none of them, raises
  `UsageError`, and a resolved kind whose tool is missing from `PATH` raises
  `PkgForgeError` -- both before anything is staged. KIND is optional and
  consumes the next token: write `-x KIND SRC DST`, `--decompress=KIND`, or
  `-x` after the paths.
  A `-` (stdin) source reads to EOF and never closes the underlying stream;
  an empty stdin (e.g. `/dev/null` or an empty pipe) stages an empty file,
  but a terminal or a closed stdin raises `UsageError` before anything is
  touched, and only one `-` source is allowed per invocation. A FIFO, a
  named pipe path, `/dev/stdin` or a process-substitution path (`<(cmd)`) is
  auto-detected as `file` and streamed, not staged as a symlink to its
  pipe target; a terminal character-device path is rejected the same way a
  terminal stdin is, and a socket or block-device path raises `UsageError`.
  An explicit `--type symlink` always copies the link text regardless.
  With several SOURCEs, two that resolve to the same non-directory
  destination raise `UsageError` before anything is staged (every clone is
  resolved -- type, decompress kind, destination -- up front, before any
  of them is staged); directory (and archive) sources sharing a
  destination still merge, and the same source path repeated is a
  harmless no-op. A symlink source's recorded `meta["target"]` never
  leaks into a later entry -- the symlink branch rebinds `meta` rather
  than mutating it in place.
  Archive extraction (both the stdlib `tarfile` path for a real tar-family
  path and the `bsdtar` fallback for stdin -- any format, since `tarfile`
  needs a real path -- other formats, and an interpreter whose `tarfile`
  has no extraction filter) never restores an archive's ownership,
  setuid/setgid bit, group/other write bit, extended attributes, ACLs or
  file flags, even when running as root; a device node, FIFO or socket
  found in the archive is refused with `PkgForgeError` naming it and the
  kind, and nothing from that extraction is left on disk. `bsdtar` always
  runs with `--no-same-owner --no-same-permissions --no-xattrs --no-acls
  --no-fflags` (libarchive 3.3+). The `tarfile` path uses a private staging
  filter, not stdlib's `'data'`/`'tar'`: a symlink member's target is kept
  exactly as stored (absolute or climbing above the destination included),
  but a write through any symlink between the destination and a member's
  own parent is refused, even one resolving back inside the destination; a
  hardlink member's target is resolved against the destination and must
  stay inside it (`tar_filter` itself never checks a hardlink's target at
  all). Re-extracting the same archive over an existing tree replaces a
  stale entry at each member's path instead of failing. Requires PEP 706
  (Python 3.9.17+/3.10.12+/3.11.4+, or 3.12+); without it a tar-family
  source routes to `bsdtar` too, and extraction is refused outright (exit
  1) if `bsdtar` isn't installed either, rather than extracting unfiltered.
  A failed install leaves `DESTINATION` exactly as it was: file, stream and
  decompress staging write a sibling temp next to it and `os.replace` it in
  only after the entry is applied, removing the temp on any failure.
  A directory copy never descends into its own resolved destination, the
  build root, or the file DB, whichever of them sit strictly inside the
  source directory (a project tree commonly contains its own build root);
  each skipped name's own parent directories are still created, possibly
  empty, instead of recursing into the destination until `RecursionError`.
  A source that equals or sits inside the destination (an in-place build,
  or a nested merge) is unaffected -- that case merges, as before.
  Re-running an install always works: it replaces a staged file or symlink
  (never a real directory, which raises `UsageError` instead), and several
  directory/archive sources sharing a destination keep merging as before.
  A directory (or archive-merge) copy replaces a stale destination symlink
  at any name instead of failing with `FileExistsError`, and never writes a
  regular-file source's content through a leftover destination symlink; a
  real destination directory is left alone (a source symlink colliding with
  one still raises).
  `--mode`, the recorded `--owner`/`--group` names (account-name syntax
  only), `-O rpmprefix=` (no control character or rpm expansion), `--chown`'s owner/group
  names (only when `--chown` is set; a recorded-only name is never resolved)
  and the `--db` directory are validated before anything is staged; without `-p`, a missing destination
  parent directory raises `UsageError` the same way. `--remove-source`
  runs only after the entry is applied and recorded (never on a failure
  above it), is skipped with a warning (not removed) only when the source IS
  the destination (a `--method link` source is removed like any other), and
  is refused with `UsageError`, before any staging, when a directory source
  contains the resolved destination or the `--db` file. A failed install
  removes the parent directories its own `-p` created (deepest first, exit
  1); directories that already existed stay.
  `--method {copy,link,move}` (env `PKGFORGE_INSTALL_METHOD`; default
  `copy`) controls how a filesystem file or directory source is staged; a
  `-` (stdin) source, `-x`/`--decompress`, an archive source and a symlink
  source/type all ignore it (they never stage from an existing source
  file). `copy` (unchanged from before) never touches the source. `link`
  hardlinks (`os.link`) instead of copying; the staged copy then shares the
  source's inode, so `-m`/`-o`/`-g`/`--chown` change the source too. A file
  `os.link` can't span (`OSError` with `errno` `EXDEV`/`EPERM`/`EMLINK`/
  `EACCES` -- a different filesystem, `fs.protected_hardlinks`, the
  per-inode link limit, or a permission error) falls back to a copy for
  that file, logging one WARNING per source (i.e. once per
  command, for the common single-source case) rather than once per file; a
  directory source hardlinks file by file the same way, via `copytree`'s
  own `copy_function`, and a symlink in the tree is still recreated fresh
  (never hardlinked). `move` consumes the source: a file is `os.replace`d
  (same filesystem) or `shutil.move`d (across filesystems) into a sibling
  temp; a directory whose destination doesn't exist yet and has no
  `-X`/`--exclude` is `os.rename`d as a whole tree in one step, otherwise
  it moves file by file via the same `copy_function` mechanism and removes
  any source directory left empty afterwards (an excluded file, and its
  parent directory, stay in the source; a symlink is recreated fresh at
  the destination, like `copy`/`link`, and also stays in the source). If
  applying the entry, replacing it onto the destination, or recording it
  then fails, a `move` that staged as a single reversible unit (a file, or
  the whole-tree rename) is moved back onto the source before the
  exception propagates -- best effort, logged rather than raising, so it
  never shadows the real failure; the merge case is not rolled back, same
  as a partial copy merge. A directory `move` onto another filesystem
  falls back to that merge path (file by file, metadata kept, symlinks
  moved, source removed). `--remove-source` is redundant with `move` (the
  source is already gone) and a no-op there, rather than an error.
  `-d` cannot be combined with `-t`/`--type` (a declared `conflicts=`
  group; exit 2, enforced by argparse itself before the command
  runs). `-X` is source-relative: `_Staging._exclude_matcher(root)`
  builds a `PathMatch(self.exclude, root)` once per source (`root` the
  source directory, or an archive's extraction temp dir), so `/x` means
  `SRC/x`, never the destination path. `-X`'s `(?meta:k=v)` inline test
  sees this run's `-O` values; a FIFO or socket the copy itself would
  otherwise reach raises `PkgForgeError` naming the path and the `-X`
  remedy, unless a glob-only `-X` already excluded it first. `-X` filters
  a directory source's copy and an archive source's extracted members
  alike: `archive._prune_excluded(root, matcher, meta)` walks the
  archive's already-extracted temp dir (`os.walk`, `followlinks=False`)
  and removes every matched file/symlink (`os.unlink`, never followed) or
  directory (`shutil.rmtree`, dropped from the walk) before anything is
  merged or renamed onto the destination -- the extraction policy (device
  nodes, FIFOs, sockets, escaping members) still applies to every member
  first, so a special or escaping member is refused even when `-X`
  matches it. With only file or symlink sources it logs a warning, since
  there is nothing to filter.
  `--record-tree` (env `PKGFORGE_INSTALL_RECORD_TREE`; off by default) also
  records, right after DESTINATION's own entry, every path below a
  directory or archive DESTINATION the DB does not already hold (a `-o`/`-g`
  of `--` still resolves per child from disk, same as any entry): `owner`/
  `group`/`meta` come from this install's own `-o`/`-g`/`-O`; `mode` and
  `type` always come from disk (`AUTO`) for both files and directories --
  DESTINATION's own `-m` never inherits down, and there is no `--dir-mode`
  equivalent; a symlink's mode is always `-`. Silently ignored for a file or
  symlink install, and a no-op under `--noentry` (nothing recorded, no
  error). Only fills gaps (a fresh `loaddb()` per clone) -- an entry a
  different install already recorded for the same key (e.g. a setuid binary
  installed on its own beforehand) is left alone. `-X` applies the same way
  it does everywhere else: `PathMatch(self.exclude, dest)` (DESTINATION
  holds the same tree as the source); an excluded directory's subtree is
  pruned, including a leftover from an earlier run still sitting in
  DESTINATION. With several merging SOURCEs, each clone's own
  `--record-tree` pass walks the same shared destination, but gap-filling
  means a child two sources both produced is still written only once. The
  DB write batches (`Project._batch()`, a no-op except sqlite) so
  DESTINATION's own entry and every child share one connection; a
  `--record-tree` failure (e.g. an unsupported file type already sitting
  under DESTINATION) exits 1 with the tree staged and the top entry
  recorded -- nothing is rolled back, since staging itself already
  succeeded. Replaces the `scan --missing DEST` step for a tree `install`
  itself just staged; `scan --missing` is still what to use for a tree
  staged by something else.
- **`pkgforge scan PATH`** — walk
  PATH, recording an entry for every directory and file **below** it (never
  PATH itself); `--missing` only fills gaps not already in the DB, else scan
  replaces an existing entry (e.g. one `install` already recorded).
  `-o`/`-g` are recorded on every entry (file, directory or symlink)
  exactly as given: `-` (default) leaves the field unset, `--` reads the
  on-disk owner/group name instead. `-m`/`--mode` applies to **file**
  entries only; a new `--dir-mode` applies to **directory** entries instead
  (same 1-4-octal-digit/`-`/`--`/`auto` grammar, defaulting to `--` when
  `--mode` is itself `--`, else `-` -- an explicit `-m` is never inherited
  by directories); a **symlink** entry's mode is always `-`, whatever
  `-m`/`--dir-mode` say (Linux ignores it; rpm/debian consumers warn about
  or misreport an explicit one). `scan` has no `--type` (it always records each path's own on-disk
  type; `-t` exits 2). `-X` is
  relative to PATH: `PathMatch(self.exclude, scanpath)`, for the walk and
  for `--drop-stale`'s kept-entry check alike. `-X`'s
  `(?meta:k=v)` inline test sees this run's `-O` values, same as `install`;
  a FIFO or socket `scan` cannot record raises `PkgForgeError` naming the
  path and the `-X` remedy, unless a glob-only `-X` already excluded it
  first. `-X` prunes an excluded directory's subtree, like `install`
  (`os.walk`'s `dirs` is filtered in place); it does not descend into it,
  so nothing below it is recorded either. `--missing`'s "already in the
  DB" skip still descends into a directory already recorded. Every
  directory entry scan records becomes an RPM `%dir` ownership claim in
  `rpmspecfiles` -- never scan a directory a distro package already owns
  (`/usr`, `/usr/bin`, `/usr/share`, `/etc`); narrow PATH to a directory
  your own package owns instead. PATH that does not exist under the build
  root raises `UsageError` (exit 2, one message) before anything else runs.
  A symlink PATH is recorded as a single `symlink` entry, never followed --
  including a dangling one, and an absolute target that would otherwise
  walk the build host's own filesystem -- except when PATH normalizes to
  the build root itself (`/`), which is always walked even when
  `--buildroot` resolves through a symlink. `scan` never records its own
  configured `--db` file (or, for `sqlite`, its `-journal`/`-wal`/`-shm`
  sidecars) if found inside the scanned tree -- it logs one WARNING (DEBUG
  for any further match in the same run) and moves on; it cannot recognize
  a `dbdump` OUTPUT file written there earlier, so keep both outside
  `--buildroot`. `--drop-stale` reloads the DB after the walk (independent
  of `--missing`) and removes (tombstones) every non-removed key strictly
  below PATH whose `localpath` no longer exists on disk, skipping a key
  that matches `-X` (so a deliberately-absent entry, e.g. an RPM `%ghost`,
  can be protected) -- never touches disk itself, and raises `UsageError`
  when `--db` is unset or `-`. The removals run in one `_batch()`, so
  `sqlite` holds a single connection for all of them. `--missing` is unaffected: a tombstoned path
  still counts as absent and is re-added if its file exists.
- **`pkgforge dbdump -f FORMAT [output]`** —
  render surviving (post-`--exclude`) DB entries via the format registry
  above. `-f` accepts a `NAME` or an `ALIASES` entry (`rpm`/`rpmspec` for
  `rpmspecfiles`, `deb` for `debian`); an alias and its canonical name always
  produce identical output. A DB must be given: with `--db` unset it raises
  `UsageError("no file DB configured; pass --db or set PKGFORGE_DB")` and with
  a path that does not exist `UsageError("no file DB at {path}; run pkgforge
  initdb")` (both exit 2); an existing empty DB logs a WARNING and dumps an
  empty manifest, exit 0, as does a DB whose entries all fall to `--exclude`.
  `--format`/OUTPUT are validated before the DB is read (an unknown format, or
  OUTPUT of the wrong shape for the chosen format, raise `UnsupportedOutputError`
  or a plain `UsageError`, both exit 2), so a format typo never surfaces as
  whatever the DB load happens to raise first; the check is a runtime registry
  lookup (never `duho.Choice`), so a format registered after import still
  works. Entries are emitted sorted by DB path (code-point order), never
  backend or filesystem order, so the same staged tree gives byte-identical
  manifests on any filesystem or DB backend.
- **`dbdump --db -`** — read the DB as JSON Lines from standard input (the
  `--db -` that `install`/`scan` write to stdout, so
  `install --db - ... | dbdump --db - -f rpm` works; `--db-format` must name a
  registered format but is not used). Runs after `--format`, OUTPUT and
  `--db-format` are validated, so a bad one exits 2 without touching stdin.
  Stdin must be a pipe, socket or regular file (`os.fstat` of its descriptor; a stream with no descriptor passes): a closed
  stdin (`sys.stdin is None`), a terminal or a character device such as
  `/dev/null` raises `UsageError` (exit 2), so an inherited `PKGFORGE_DB=-` never
  dumps an empty manifest. In an MCP tool call it is a `UsageError` too (stdin
  is the protocol channel). Otherwise it reads to EOF, blocking on a pipe that
  is never closed. Empty input logs a WARNING and dumps an empty manifest,
  exit 0. A leading BOM is ignored. Parsing is shared with the `jsonl` backend
  via `db.parse_jsonl(text, source)` (`source` is the file path
  or `"<stdin>"`), so both report an error the same way
  (`{source}:{lineno}: ...`); non-UTF-8 input raises `DbError` prefixed
  `<stdin>:`.
- **`pkgforge initdb`** — create or truncate
  an empty DB; a no-op with a WARNING for an unset/stdout DB. A `--db`
  naming a file that doesn't exist yet is not this case -- creating it is
  exactly `initdb`'s job.
- **`pkgforge compact`** — collapse an
  append-log DB to one record per live path, dropping removals and
  superseded history; for `sqlite`, deletes removal rows and `VACUUM`s.
  Raises `UsageError` (exit 2) for an unset `--db`, for `--db -` (no file to
  rewrite) and for a file that does not exist (nothing is created, for every
  backend).
## Environment variables

Read when `pkgforge.cli.main()` runs (CLI wins over env, which wins over
the default); the `Project` API never reads the environment. An empty value
counts as unset.

- **`PKGFORGE_ROOT`** — default `--buildroot`.
- **`PKGFORGE_DB`** — default `--db`.
- **`PKGFORGE_DB_FORMAT`** — default `--db-format`.
- **`PKGFORGE_INSTALL_METHOD`** — default `install`'s `--method` (`copy`,
  `link` or `move`; an empty value counts as unset, same as the three
  above). CLI wins over it the same way.
- **`PKGFORGE_INSTALL_RECORD_TREE`** — default `install`'s `--record-tree`
  (a boolean; truthy tokens `1 true yes on y t`, case-insensitive; anything
  else, including empty/unset, is off). CLI (`--record-tree`/
  `--no-record-tree`) wins over it the same way.
- **`PKGFORGE_TRACEBACK`** — a true value (`1 true yes on y t`,
  case-insensitive) prints the traceback before the `pkgforge: error:` line.
  See [Exit status](#exit-status).
- **`PKGFORGE_MCP`** — set to `stdio` to serve the command tree as MCP tools
  over stdio instead of running a command (duho's launch trigger; any other
  value exits 2). Unset, it does nothing. It is removed from the environment
  as soon as it is read, so staged commands' children never inherit it.

## Gotchas

- `mode` is always an octal **permission string** (`"644"`), never a raw
  `st_mode` int — `apply()` converts with `int(mode, 8)`.
- The append-log backends (`jsonl`, `yaml`) never truncate on write; `load()`
  keeps only the last record per path. Call `compact()` (or `pkgforge
  compact`) to reclaim space / drop history. `sqlite` has no log to compact
  beyond dropping removed rows.
- File sources are copied by default (`--method copy`), never linked: the
  copy carries the source's content, permission bits and modification time
  (never its BSD file flags or extended attributes, e.g. an SELinux label);
  `-m`/`--chown` apply to the staged copy only, and the source keeps its
  original content, mode and ownership. `--method link`/`move` opt into
  sharing or consuming the source instead -- see `pkgforge install` above.
- `chown` (owner/group) requires the Unix `pwd`/`grp` stdlib modules; both
  import guarded to `None` off POSIX, so `.apply(chown=True, ...)` raises
  `RuntimeError` there. Parser/`--help` construction still works everywhere.
- A tree or archive install records **one** entry, for the destination
  itself, unless `--record-tree` is set: it also records every path below
  the destination the DB doesn't already hold, with owner/group/meta from
  this install and mode/type from disk, honouring `-X`. For a tree staged
  by something else, `scan --missing --mode=-- -o OWNER -g GROUP <dest>`
  does the same from disk-derived attributes. Skip both and `rpmspecfiles`
  gets only a `%dir` line and `debian`'s `install` artifact gets no files
  for the tree.
- A `PathMatchStmt`/`PathMatch` result of `None` is not "no match" — it
  means "keep evaluating"; only `PathMatch.match`'s exhausted fallthrough
  (`_default`) is a real default.
