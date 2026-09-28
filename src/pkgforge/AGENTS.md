# `pkgforge` — public API header

Header-file-style reference for the `pkgforge` package: every `__all__`
export with its signature, arguments, contract, and gotchas, so this module
can be consumed without reading its source. Kept current with the public
API. For the CLI overview and code layout, see the shipped `README.md`, or <https://github.com/jose-pr/pkgforge>.

`pkgforge.__all__`: `PkgForgeCmd`, `PkgForge`, `PkgForgeError`, `DbError`,
`DbProvider`, `FileEntry`, `FileEntryArgs`, `FileType`, `UsageError`,
`__version__`, `apply_entry`, `entry_from_args`, `entry_from_path`,
`normalize_mode`, `resolve_entry`, `main`, `open_db`, `register_provider`, plus the
leaf-command submodules themselves
(`compact`, `dbdump`, `initdb`, `install`, `scan` — importing `pkgforge` runs
each module's `_register()` call, attaching it to the `PkgForge` subcommand
tree).

## Entry point

- **`main(argv=None) -> int`** — build the parser and dispatch the selected
  subcommand (`duho.main(PkgForge, argv)`). Bound as the `pkgforge` console
  script and `python -m pkgforge`. This is pkgforge's only error boundary:
  a `UsageError` prints one `pkgforge: error: ...` line to stderr and returns
  2; any other `PkgForgeError`, `OSError` or `subprocess.CalledProcessError`
  prints the same and returns 1; a `BrokenPipeError` (a closed output pipe)
  returns 1 silently; anything else propagates with its traceback. Set
  `DUHO_TRACEBACK` to a true value (duho's boolean tokens: `1`/`true`/`yes`/
  `on`; `0`/`false`/`no`/`off`/`n`/`f` are off) to also print the traceback
  before that one line.
  Calling a command directly (not through `main()`) still raises the plain
  exception — the boundary only wraps the CLI entry point.

## Core types (`common.py`)

- **`PkgForgeError(Exception)`** — base class for pkgforge's own runtime
  failures; caught by `main()` and mapped to exit 1.
- **`UsageError(PkgForgeError, ValueError)`** — an argument-shaped mistake
  (a bad or missing value); caught by `main()` and mapped to exit 2. Also a
  `ValueError`, so existing `except ValueError`/`pytest.raises(ValueError)`
  code keeps working. Raised by `install` for: a missing source, a `-`
  (stdin) source without `-T`/`-D`, a symlink source/type with no target
  (no `-O target=PATH`), an unresolvable stdin compression kind, a
  `--decompress` value that looks like a path, and an unknown `--chown`
  owner or group.
- **`FileType(str, enum.Enum)`** — `File`, `Directory`, `Symlink`. Sentinel
  member `_AUTO = "--"` means "determine from the file on disk".
  `FileType.from_path(path) -> FileType` inspects a real path (raises
  `TypeError` if it's none of the three — missing or a special file such as
  a FIFO or socket).
- **`FileEntry(typing.TypedDict)`** — one DB record: `mode` (octal permission
  **string**, e.g. `"644"`, not a raw `st_mode` int), `owner`, `group`,
  `type`, `meta: dict[str, str]`. A `FileEntry` value is a **plain dict** at
  runtime (it is a `TypedDict`), so it carries no methods of its own; use the
  module functions below. For back-compat, `FileEntry.from_args`,
  `FileEntry.from_path`, `FileEntry.resolve_for` and `FileEntry.apply` remain
  as aliases for those functions, called **unbound** through the class
  (`FileEntry.resolve_for(entry, path, ...)`, `FileEntry.apply(entry, path,
  ...)`) — never `entry.resolve_for(...)`/`entry.apply(...)`, which raise
  `AttributeError` on a plain dict.
  - **`entry_from_args(args: FileEntryArgs, **overwrite) -> FileEntry`** —
    build from a parsed CLI mixin. `type` is converted via `FileType` only
    when `overwrite` does not itself supply `type`. `mode` is normalized via
    `normalize_mode`; `owner`/`group` map Python 3.9's stripped `[]` (an
    attached `--` argparse consumes before any converter runs) to `AUTO`,
    and an explicit empty string to `DEFAULT`.
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
  - **`normalize_mode(value: str | int | list) -> str`** — normalize a
    `mode` value to an octal permission string. An `int` renders via
    `mode_to_octal`; `"-"`/`""` mean `DEFAULT`; `"--"`, `"auto"` or Python
    3.9's stripped `[]` mean `AUTO`; a string of 1-4 octal digits normalizes
    (`"0644"` -> `"644"`). Anything else raises `UsageError`. `auto` is a
    mode-only alias — `owner`/`group` get none, since `auto` can be a real
    account name.
- **`FileEntryArgs(duho.Cmd)`** — CLI mixin supplying `--mode/-m` (validated
  at parse time: 1-4 octal digits, `-`, `--` or `auto`; an explicit empty
  value exits 2; normalized, so `0644` is stored as `644`), `--group/-g`,
  `--owner/-o` (each default `"-"`, no octal validation), `--type/-t`
  (`Optional[FileType]`, default `None`; validated at parse time: `file`,
  `directory` or `symlink` in any case, or `-`/`--` for the sentinels;
  anything else, including `auto`/`_AUTO`, exits 2), `-O/--meta KEY=VALUE`
  (repeatable, merges into a `dict[str, str]`; a value missing `=` exits 2
  with `expected KEY=VALUE, got '...'`).
- **`AUTO = "--"`** / **`DEFAULT = "-"`** — module-level sentinels: `AUTO`
  means "resolve from the file on disk" (used by `resolve_for`); `DEFAULT`
  means "leave at the OS/system default, do not set explicitly" (used by
  `apply`'s `usedefault`).
- **`parsepath(path: str) -> str | Path | None`** — CLI path coercion:
  `"-"` stays `"-"` (stdin/stdout), `""` becomes `None`, anything else
  becomes a `Path`.
- **`mode_to_octal(mode: int) -> str`** — render a raw `st_mode` as a bare
  octal permission string (`"644"`).
- **`PkgForgeCmd(duho.LoggingArgs, duho.Cmd)`** — common base every
  subcommand extends. Fields: `--db PATH` (from `PKGFORGE_DB`),
  `--db-format FMT` (from `PKGFORGE_DB_FORMAT`; a built-in or
  `register_provider`-registered name, else `UsageError` -- checked in
  `__init__`, so it also covers direct Python-API construction),
  `--buildroot/-r DIR` (from `PKGFORGE_ROOT`, else `.`; a relative build root
  whose realpath is `/` -- the ordinary cwd default with no `PKGFORGE_ROOT`
  set, run from a cwd of `/` -- raises `UsageError` unless it was spelled
  explicitly as `--buildroot /`/`PKGFORGE_ROOT=/`; an unset/empty build root
  from the Python API also raises). The three env vars are read when
  `pkgforge.main()`/`duho.parse` runs (precedence CLI > env > the class
  default), not once at import time; a bare `_parser_().parse_args()` does
  **not** apply them (duho's env layer lives in `main`/`parse`, not raw
  argparse); a command constructed directly in Python (not through
  `main()`/`parse`) uses each var's value **as of import** (its own class
  default), same as before. An empty value counts as unset for all three.
  Each leaf command declares `_logger_name_ = "pkgforge.<command>"`
  (`pkgforge.install`, `pkgforge.scan`, `pkgforge.dbdump`, `pkgforge.initdb`,
  `pkgforge.compact`), so `logging.getLogger("pkgforge")` controls every
  command when it is constructed directly (not through `main()`/`duho.parse`).
  Through the CLI, `main()` always sets the *dispatched* command's own logger
  to an explicit level (INFO by default), which then wins over inherited
  propagation: use `--loglevel LEVEL` (the running command) or
  `--loglevel pkgforge.<command>:LEVEL`; a bare `--loglevel pkgforge:LEVEL`
  has no effect on a CLI-dispatched command (the command's own INFO already
  wins), though it still reaches one built directly in Python and left at
  its default (NOTSET) level.
  Helpers: `localpath(buildpath: str | os.PathLike) -> Path` (accepts a
  `/`-rooted OR build-relative path; an absolute input has its leading `/`
  stripped, a relative one is taken as already build-relative) /
  `buildpath(localpath: Path) -> Path` (the reverse, `/`-rooted; POSIX only —
  `Path("/", ...)` resolves against the current drive on Windows),
  `loaddb() -> dict[str, FileEntry | None]`, `initdb()`, `compactdb()`,
  `add_entry(buildpath, entry)`, `remove_entry(buildpath)`. When `--db` is
  unset or `"-"`, DB-writing methods emit one JSON Lines record to stdout
  instead of touching a file. `_register()` (classmethod) attaches the class
  to `PkgForge`'s subcommand tree.
  A DESTINATION (`install`) or PATH (`scan`) must resolve inside
  `--buildroot`: a `..` that climbs above the root, or a symlinked
  component that leads outside it, raises `UsageError` before anything is
  written or recorded; an in-root `..` (`/usr/share/../lib/x`) is
  normalized in the path that is actually staged and recorded
  (`/usr/lib/x`), never left verbatim.
- **`PkgForge(PkgForgeCmd, duho.Cli)`** — the application root (the
  `pkgforge` command). Adds `--version`/completion via `duho.Cli`
  (`_version_ = duho.AUTO`, `_distribution_ = "pkgforge"`,
  `_completion_ = True`). `_parsername_ = "pkgforge"` names usage, errors,
  `--version` and the generated shell-completion scripts; without it duho
  falls back to the class name (`PkgForge`), which shell completion cannot
  bind to (case-sensitive lookup on bash/zsh/fish).

## DB backends (`db.py`)

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
  like any `PkgForgeError` (one stderr line, exit 1); also a `ValueError`, so
  an existing `except ValueError` caller is unaffected. `jsonl`/`yaml` text
  I/O is always UTF-8, regardless of locale; an append to a DB whose last
  line lacks a trailing newline (e.g. a hand edit) repairs it first instead
  of fusing the new record onto the old one.
  `JsonlDb.load`/`YamlDb.load` run every live (non-removed) record through a
  private `_normalize`: a missing `meta` becomes `{}`, a missing
  `mode`/`owner`/`group` becomes `"-"`, a missing `type` becomes `None`
  (matching pkgforge's own explicit `null` for an unset `type`); a literal
  JSONL int `mode`/`owner`/`group` (JSON has no leading-zero int, so this
  only affects `jsonl`) becomes its string form, and for `mode` only when
  that string is 1-4 octal digits. A `bool`, a `float`, a non-octal-digit
  int `mode`, a non-string `type`, or a record that is not a mapping raises
  `DbError` naming the file and the record's path. This never applies to a
  third-party `load()`. `YamlDb`'s loader additionally keeps only YAML's
  `null` implicit resolver (`~`/`null` still load as `None`, for tombstones)
  and drops int/float/bool/timestamp guessing, so an unquoted scalar loads
  as the text it was written as (`mode: 0755` is `"0755"`, not the int
  `493`) instead of PyYAML's YAML-1.1 typing.
- **`Db`** — type alias `dict[str, FileEntry | None]` (a loaded DB; `None`
  marks a removed path).
- **`DbProvider(abc.ABC)`** — storage backend bound to a filesystem `path`
  (`provider_cls(path)`). Abstract methods: `load() -> Db`, `add(path,
  entry)`, `remove(path)`, `compact()`, `init()`. Class attr `format: str`.
  **`batch(self) -> ContextManager[DbProvider]`** — non-abstract; a
  `contextlib.contextmanager` yielding `self`. The default does nothing
  extra (every `add`/`remove` inside it still writes exactly as it would
  outside one), so an existing or third-party provider that doesn't
  override it keeps working unchanged. `SqliteDb` overrides it to hold one
  connection open across the whole batch, committing every 1000 rows and
  once more on exit (exception included, so a killed batch keeps whatever
  it already committed) instead of connecting, creating the schema and
  committing once per call. `scan` wraps its walk in one (`PkgForgeCmd`'s
  private `_db_batch()`); a direct `add()`/`remove()` outside a batch, and
  every other command, are unaffected.
- **`open_db(path, fmt=None, *, for_read=False) -> DbProvider`** — resolve
  and construct the provider. Precedence: explicit `fmt` wins; else, when
  `for_read` and `path` already exists, its content is sniffed (so a
  mislabeled/legacy file still loads correctly); else the `path` suffix
  decides, defaulting to `"jsonl"`. Raises `ValueError` for an unknown
  format name. `for_read=True` means "sniff an existing file's content",
  not "this call only reads" -- `PkgForgeCmd._write_entry` passes it on
  writes too, so an append keeps the file's actual format instead of
  writing JSON Lines into, say, a legacy YAML file under a `.jsonl` suffix.
- **`register_provider(name, provider_cls, *, suffixes=(), sniff=None) ->
  type[DbProvider]`** — the extension seam for third-party backends. `name`
  is used by `--db-format` and error messages; `suffixes` (case-insensitive;
  a missing leading dot is added, e.g. `"toml"` registers the same as
  `".toml"` -- `Path.suffix` always includes one, so a dotless entry could
  never match otherwise; an empty string is left alone, matching an
  extensionless path; a suffix with more than one dot, e.g. `".tar.gz"`,
  raises `ValueError` at registration time, since `Path.suffix` only ever
  returns the last dot-segment) infer the format from a `--db` path;
  `sniff(head: bytes) -> bool` inspects a file's first 16 bytes to claim it
  by content (newer registrations are tried first). Returns `provider_cls`
  (usable as a decorator). Re-registering a name replaces the previous
  class; a rejected (multi-dot) suffix registers nothing at all, not a
  half-registered name.
- **Built-in backends** (all registered at import time): **`JsonlDb`**
  (`format="jsonl"`, suffixes `.jsonl`/`.ndjson`, default when unset) —
  append-only JSON Lines, one object per line, last record per path wins on
  load; sniffed by a quoted first key (`re.match(r'\s*\{\s*"', head)`, so
  pkgforge's own output and a hand-written `{ "path": ...}` both match, but a
  flow-style YAML mapping with a plain key -- what `yaml.safe_dump` emits --
  does not); **`YamlDb`** (`format="yaml"`, suffixes `.yaml`/`.yml`) —
  append-only YAML: a single mapping, appended key by key (the last
  duplicate key wins); a flow-style
  top-level document (e.g. `{/usr/bin/x: {...}}`, or `{}`) is read fine, but
  `add`/`remove` raise `DbError` instead of appending a block-style mapping
  after it (which would be invalid YAML) -- `compact()` rewrites the file in
  block style, unblocking further appends; **`SqliteDb`**
  (`format="sqlite"`, suffixes `.db`/`.sqlite`/`.sqlite3`, sniffed by the
  SQLite file magic) — a real upserted-in-place table, no append log
  (`compact()` drops removed rows + `VACUUM`s). `load()`/`compact()` never
  write: an empty or schema-less file loads as (or compacts as a no-op on)
  an empty DB, and a SQLite file that holds other tables but no `entries`
  table raises `DbError` (some other program's database) instead of
  getting one added to it. `add`/`remove` reject a `path` or a
  `mode`/`owner`/`group`/`type` value that is not valid UTF-8 with
  `DbError`, before any SQL runs for that record.
  `JsonlDb`/`YamlDb` writes (`add`/`remove`/`init`) and `compact` serialize
  on an advisory `flock` of the DB file (a no-op off POSIX), so running
  `compact` alongside another pkgforge process appending to the same DB no
  longer drops that append. `compact` replaces the file with a new inode (a
  temp file written, fsynced and renamed into place) instead of truncating
  it in place, so a failed write (`ENOSPC`, a kill) leaves the original file
  untouched; a symlinked `--db` keeps its link, a hardlinked one is detached.
  `sqlite3` and PyYAML are both imported lazily (inside `SqliteDb._connect`
  and a private `_yaml_io()` respectively), not at module top: `import
  pkgforge`, `--help` and the `jsonl` backend all work on an interpreter
  that lacks one of them. Actually using the `sqlite`/`yaml` backend on
  such an interpreter raises `PkgForgeError` naming the missing module,
  instead of a bare `ImportError`. `_yaml_io()` prefers PyYAML's
  libyaml-backed `CSafeLoader`/`CSafeDumper` over the pure-Python
  `SafeLoader`/`SafeDumper` when the installed build has them (several
  times faster; the on-disk format and duplicate-key last-wins behavior
  are unaffected either way).
- **`format_for_suffix(path: Path) -> str`** — suffix → registered format
  name, else `DEFAULT_FORMAT` (`"jsonl"`).
- **`sniff_format(path: Path) -> str | None`** — detect an existing file's
  format from its first 16 bytes via registered sniffers (newest-first);
  falls back to `"yaml"` for any non-empty content no sniffer claims, `None`
  for an empty/unreadable file.

## Exclude / filter grammar (`exclude.py`)

- **`PathMatchStmt(duho.NS)`** — one parsed `--exclude` statement: `negate:
  bool`, `tests: list[PathTest]`, `pattern: str`. `PathMatchStmt.parse(s) ->
  PathMatchStmt` parses `[!](?name:arg)*<glob>` (leading `!` negates the
  whole statement; each `(?name:arg)` or `(?!name:arg)` is an inline test,
  `!` inverts just that test). Registered test names: `type` (`(?type:file|
  directory|symlink)`) and `meta` (`(?meta:key=value)`). `.match(path,
  fileentry) -> bool | None` — a statement that applies returns `True`
  (exclude) or, when negated, `False` (keep); one that does not apply always
  returns `None`, "keep evaluating", so a statement never vetoes the ones
  after it and evaluation is order-independent for non-overlapping
  statements. `.rebased(root) -> PathMatchStmt` returns a copy with an
  absolute pattern re-rooted under `root` (itself if the pattern is
  relative); it never mutates, since parsed statements are shared.
  `.anchored -> bool` is true when `pattern` is rooted (a leading `/`, or a
  Windows drive).
- **Glob engine**: `pattern` is matched with a private, cached
  `_glob_regex(pattern) -> re.Pattern`, not `PurePath.match` (whose `**` is a
  single non-recursive segment and disagrees between Python versions on a
  root-level key). `**` as a whole segment matches zero or more segments; a
  *trailing* `/**` (or a bare `**`) matches one or more segments — "the
  contents of this directory", never the directory itself. `*`/`?` never
  cross `/`; `[...]`/`[!...]` is a character class. A relative pattern gets
  an implicit "at any depth" prefix, same as before.
- **`PathMatch(list[PathMatchStmt])`** — an ordered set of statements bound
  to an optional `root` (stores `stmt.rebased(root)` copies, leaving the
  caller's statements untouched — a multi-source `install` constructs one
  `PathMatch` per source from the same parsed list). `.match(path,
  entry=None, _default=None, **overrides) -> bool | None` — evaluates
  statements in order, first non-`None` result wins; the entry is derived
  lazily via `FileEntry.from_path`, at most once, and only for a statement
  whose glob already matched and that actually has inline tests -- a
  glob-only statement never lstats, types or does a pwd/grp lookup on the
  path, so `-X '*.fifo'` excludes a FIFO or socket instead of raising for
  it. `**overrides` are layered onto a COPY of the entry (never the
  caller's own dict, e.g. a live DB record in `dbdump`); an explicit `entry`
  of `{}` still counts as "the caller supplied one" (`is not None`, not a
  truthiness check). An empty `PathMatch` always matches (`True`). With a
  root, an anchored statement matches the candidate's own absolute
  path; a relative statement matches the path taken relative to the root
  (by name, for the single-file-scan case where the path equals the root;
  unchanged, for a path outside the root entirely). With no root (`dbdump`),
  every statement sees the path exactly as given.
- **`ExcludeArgs(duho.Cmd)`** — the `--exclude`/`-X` field, declared once and
  shared: `Install`, `ScanCmd` and `DbDump` all take `--exclude` from this
  mixin instead of each declaring it separately. `--help` shows the
  grammar (`[!][(?[!]test:arg)...]GLOB`).
- **`ExcludeSyntaxError(UsageError, argparse.ArgumentTypeError)`** — raised
  for a malformed `-X` statement: an unknown inline test name, an invalid
  `(?type:...)`/`(?meta:...)` argument, or an unterminated `(?...` (which
  used to silently become a literal glob that excluded nothing). Caught by
  `pkgforge.main()`'s error boundary like any `UsageError` (one line, exit
  2); also an `argparse.ArgumentTypeError`, so argparse itself reports it
  cleanly rather than letting it escape as a traceback from the `-X`
  `type=` converter.

## `dbdump` format registry (`dbdump.py`)

- **`dump_formats() -> list[str]`** — every registered format name, sorted.
- **`PER_ENTRY_FORMATS: dict[str, PerEntryDumper]`** — one-line-per-entry
  formats, each `dumper(path, entry) -> bytes`. Built in: `"rpmspecfiles"`
  (RPM `%files` lines: `%attr(mode,owner,group) "path"`, `%dir` prefix for
  directories, `meta["rpmprefix"]` prepended if set). The path is quoted for
  rpm's `%files -f` parser (targets rpm 4.19+): `\` and `"` are escaped, the
  line is written as UTF-8 with `surrogateescape` (a non-UTF-8 name
  round-trips its original bytes), and glob characters are never escaped
  (rpm's own quoting already matches only the literal name). A `%` anywhere
  in the path raises `DumpError` -- no version of rpm treats a quoted `%`
  as literal, and `%(...)` runs a shell command -- as does any C0 control
  character or DEL.
- **`DumpError(PkgForgeError, ValueError)`** — a DB entry that a dump
  format's own tooling cannot represent (a `%` or a control character for
  `rpmspecfiles`; see `MULTI_ARTIFACT_FORMATS` below for `debian`). Caught by
  `main()`'s error boundary like any `PkgForgeError` (one stderr line, exit
  1). Every entry is rendered before OUTPUT is opened, so raising this
  leaves no partial file.
- **`MULTI_ARTIFACT_FORMATS: dict[str, Callable[[Entries], dict[str,
  bytes]]]`** — formats that render several named artifacts, each
  `render(entries) -> {filename: bytes}`. Built in: `"debian"` — `install`
  (`dh_install`-style `<src> <dest-dir>` lines, non-directory entries only)
  + `permissions` (a pkgforge-specific `<path> <mode> <owner> <group>`
  manifest, unescaped -- parse right-to-left, since the path may itself
  contain spaces -- for only the entries that pin a non-default
  mode/owner/group). `install`'s source is debhelper-escaped (needs compat
  13): a backslash before each glob character (`\ * ? [ ] { }`, which also
  makes a literal `${` read as literal since `{`/`}` get escaped), a space as
  `${Space}`, and a leading `#` backslash-escaped (a line starting with `#`
  is a `dh_install` comment). The destination is only ever
  `${Dollar}{`/`${Space}`-escaped, never glob-escaped -- `dh_install` takes it
  literally. Every entry is validated first (a control character in the
  path, or whitespace in mode/owner/group, raises `DumpError` before either
  artifact is built), and both artifacts are written as UTF-8 with
  `surrogateescape` (a non-UTF-8 name round-trips its original bytes).
- **`Entries`** — type alias `list[tuple[str, FileEntry]]` (surviving DB
  entries after `--exclude` filtering), the shared input shape for both
  registries above.

## CLI subcommands

Each is a `duho.Cmd` subclass self-registered onto `PkgForge`; use them via
the CLI (`pkgforge <name> ...`) rather than instantiating directly unless
you're embedding the CLI layer itself. Each declares `_logger_name_ =
"pkgforge.<command>"`: control one with `--loglevel pkgforge.<command>:LEVEL`
or all of them with `logging.getLogger("pkgforge")` (a bare `--loglevel
pkgforge:LEVEL` has no effect on a CLI-dispatched command; see `PkgForgeCmd`
above).

- **`install.Install(FileEntryArgs, ExcludeArgs, PkgForgeCmd)`** (`pkgforge install`) —
  stage a source (file / directory / symlink / tar-family archive /
  decompress-on-copy) into the build root, apply mode/ownership, and record
  the entry. `-D` = `-Tp` shortcut, `-d` = `--type directory` shortcut.
  `--type=--` (or the Python API's `FileType._AUTO`) auto-detects from the
  source, same as leaving `-t` unset. Without `-T`, an extracted archive
  lands at `DESTINATION/<name minus its archive suffix>` (`.tar`,
  `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`/`.tbz`, `.tar.xz`/`.txz`, `.iso`,
  `.zip`, matched case-insensitively, longest suffix first; a bsdtar-only
  tar variant with no fixed entry, e.g. `.tar.zst`, falls back to cutting
  at the last `tar`/`iso` dot-segment); a directory source keeps its own
  name unchanged (`conf.tar.d` stays `conf.tar.d`).
  `-x`/`--decompress [KIND]` accepts `gz`, `xz`, `bz2`, `zst`, `lzma` or a
  decompressor tool name (`gzip`/`gunzip`, `xz`/`unxz`, `bzip2`/`bunzip2`,
  `zstd`/`unzstd`, `lzma`/`unlzma`), matched case-insensitively; any other
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
  `--mode`, `--chown`'s owner/group names (only when `--chown` is set; a
  recorded-only name is never resolved) and the `--db` directory are
  validated before anything is staged; without `-p`, a missing destination
  parent directory raises `UsageError` the same way. `--remove-source`
  runs only after the entry is applied and recorded (never on a failure
  above it), is skipped with a warning (not removed) when the source IS the
  staged destination, and is refused with `UsageError`, before any staging,
  when a directory source contains the resolved destination or the `--db`
  file.
  `-d` cannot be combined with `-t`/`--type` (a declared `conflicts=`
  group; exit 2, enforced by argparse itself before `Install` is
  constructed). `-X`'s `(?meta:k=v)` inline test sees this run's `-O`
  values; a FIFO or socket the copy itself would otherwise reach raises
  `PkgForgeError` naming the path and the `-X` remedy, unless a glob-only
  `-X` already excluded it first. `-X` only ever filters a real directory
  source: with an archive source it raises `UsageError` (exit 2) instead
  of extracting every member unfiltered; with only file or symlink
  sources it logs a warning, since there is nothing to filter.
- **`scan.ScanCmd(FileEntryArgs, ExcludeArgs, PkgForgeCmd)`** (`pkgforge scan`) — walk
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
  or misreport an explicit one). `--type/-t` is hidden from `--help` and never
  applied (scan always records each path's own on-disk type); an explicit
  value logs a warning instead of doing nothing silently. `-X`'s
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
  when `--db` is unset or `-`. `--missing` is unaffected: a tombstoned path
  still counts as absent and is re-added if its file exists.
- **`dbdump.DbDump(ExcludeArgs, PkgForgeCmd)`** (`pkgforge dbdump -f FORMAT [output]`) —
  render surviving (post-`--exclude`) DB entries via the format registry
  above. Logs a WARNING (exit code and output unchanged: an empty manifest,
  exit 0) when `--db` is unset, `-`, or names a file that does not exist,
  and when the DB has entries but none survive `--exclude`. `--format`/OUTPUT
  are validated before the DB is read (an unknown format, a file OUTPUT for a
  multi-artifact format, or a directory OUTPUT for a per-entry format all
  raise `UsageError`, exit 2), so a format typo never surfaces as whatever
  the DB load happens to raise first; the check is a runtime registry lookup
  (never `duho.Choice`), so a format registered after import still works.
  Entries are emitted sorted by DB path (code-point order), never backend or
  filesystem order, so the same staged tree gives byte-identical manifests
  on any filesystem or DB backend.
- **`initdb.InitDb(PkgForgeCmd)`** (`pkgforge initdb`) — create or truncate
  an empty DB; a no-op, now with a WARNING, for an unset/stdout DB. A `--db`
  naming a file that doesn't exist yet is not this case -- creating it is
  exactly `initdb`'s job.
- **`compact.Compact(PkgForgeCmd)`** (`pkgforge compact`) — collapse an
  append-log DB to one record per live path, dropping removals and
  superseded history; for `sqlite`, deletes removal rows and `VACUUM`s. A
  no-op, now with a WARNING, for an unset/stdout DB or one naming a file
  that does not exist yet (nothing is created, for every backend).
- **`PkgForgeCmd._no_db_reason() -> str | None`** — a human-readable reason
  there is no real DB *file* configured (`--db` unset, or `-`), or `None` if
  `self.db` names one (existing or not -- a nonexistent path is a separate
  case each of the three commands above checks for itself, since it means
  something different to each). Used by `dbdump`/`initdb`/`compact` for
  their warnings above; distinct from `_no_file_db() -> bool`, which the DB
  read/write helpers use and which answers the same "unset or `-`" question
  as a plain boolean.

## Environment variables

Read when `pkgforge.main()`/`duho.parse` runs (CLI wins over env, which wins
over the class default); a command built directly in Python instead uses the
value as of import. An empty value counts as unset for all three.

- **`PKGFORGE_ROOT`** — default `--buildroot`.
- **`PKGFORGE_DB`** — default `--db`.
- **`PKGFORGE_DB_FORMAT`** — default `--db-format`.
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
- File sources are copied, never linked: the copy carries the source's
  content, permission bits and modification time (never its BSD file flags
  or extended attributes, e.g. an SELinux label); `-m`/`--chown` apply to
  the staged copy only, and the source keeps its original content, mode
  and ownership.
- `chown` (owner/group) requires the Unix `pwd`/`grp` stdlib modules; both
  import guarded to `None` off POSIX, so `.apply(chown=True, ...)` raises
  `RuntimeError` there. Parser/`--help` construction still works everywhere.
- A tree or archive install records **one** entry, for the destination
  itself; `scan --missing --mode=-- -o OWNER -g GROUP <dest>` records its
  contents, honouring `-X`, with real (not default) attributes -- skip it
  and `rpmspecfiles` gets only a `%dir` line and `debian`'s `install`
  artifact gets no files for the tree.
- A `PathMatchStmt`/`PathMatch` result of `None` is not "no match" — it
  means "keep evaluating"; only `PathMatch.match`'s exhausted fallthrough
  (`_default`) is a real default.
