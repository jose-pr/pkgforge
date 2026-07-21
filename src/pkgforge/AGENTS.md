# `pkgforge` — public API header

Header-file-style reference for the `pkgforge` package: every `__all__`
export with its signature, arguments, contract, and gotchas, so this module
can be consumed without reading its source. Kept current with the public
API. For the CLI overview and code layout, see the repo-root overview doc.

`pkgforge.__all__`: `PkgForgeCmd`, `PkgForge`, `DbProvider`, `FileEntry`,
`FileEntryArgs`, `FileType`, `__version__`, `main`, `open_db`,
`register_provider`, plus the leaf-command submodules themselves (`compact`,
`dbdump`, `initdb`, `install`, `scan` — importing `pkgforge` runs each
module's `_register()` call, attaching it to the `PkgForge` subcommand tree).

## Entry point

- **`main(argv=None) -> int`** — build the parser and dispatch the selected
  subcommand (`duho.main(PkgForge, argv)`). Bound as the `pkgforge` console
  script and `python -m pkgforge`.

## Core types (`common.py`)

- **`FileType(str, enum.Enum)`** — `File`, `Directory`, `Symlink`. Sentinel
  member `_AUTO = "--"` means "determine from the file on disk".
  `FileType.from_path(path) -> FileType` inspects a real path (raises
  `TypeError` if it's none of the three).
- **`FileEntry(typing.TypedDict)`** — one DB record: `mode` (octal permission
  **string**, e.g. `"644"`, not a raw `st_mode` int), `owner`, `group`,
  `type`, `meta: dict[str, str]`. Classmethods/methods:
  - `FileEntry.from_args(args: FileEntryArgs, **overwrite) -> FileEntry` —
    build from a parsed CLI mixin.
  - `FileEntry.from_path(path, meta=None) -> FileEntry` — build by `lstat`-ing
    a real path; owner/group resolve via `pwd`/`grp` (fall back to `"-"` if
    those modules are unavailable, i.e. non-POSIX).
  - `.resolve_for(path, lookupval="--", **overwrite) -> FileEntry` — replace
    every field equal to `lookupval` (default the `AUTO` sentinel) with the
    on-disk value for `path`.
  - `.apply(path, chown=False, *, logger=None, usedefault="-")` — `chmod`
    (always, unless `mode == usedefault`) and, if `chown=True`, `chown`
    (raises `RuntimeError` if `pwd`/`grp` are unavailable).
- **`FileEntryArgs(duho.Cmd)`** — CLI mixin supplying `--mode/-m`,
  `--group/-g`, `--owner/-o` (each default `"-"`), `--type/-t`
  (`Optional[FileType]`, default `None`), `-O/--meta KEY=VALUE` (repeatable,
  merges into a `dict[str, str]`).
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
  subcommand extends. Fields: `--db PATH` (default from `PKGFORGE_DB`),
  `--db-format FMT` (default from `PKGFORGE_DB_FORMAT`), `--buildroot/-r DIR`
  (default from `PKGFORGE_ROOT`, else `.`). Helpers: `localpath(buildpath) ->
  Path` / `buildpath(localpath) -> Path` (build-root ⇄ `/`-rooted path
  translation), `loaddb() -> dict[str, FileEntry | None]`, `initdb()`,
  `compactdb()`, `add_entry(buildpath, entry)`, `remove_entry(buildpath)`.
  When `--db` is unset or `"-"`, DB-writing methods emit one JSON Lines
  record to stdout instead of touching a file. `_register()` (classmethod)
  attaches the class to `PkgForge`'s subcommand tree.
- **`PkgForge(PkgForgeCmd, duho.Cli)`** — the application root (the
  `pkgforge` command). Adds `--version`/completion via `duho.Cli`
  (`_version_ = duho.AUTO`, `_distribution_ = "pkgforge"`,
  `_completion_ = True`).

## DB backends (`db.py`)

- **`Db`** — type alias `dict[str, FileEntry | None]` (a loaded DB; `None`
  marks a removed path).
- **`DbProvider(abc.ABC)`** — storage backend bound to a filesystem `path`
  (`provider_cls(path)`). Abstract methods: `load() -> Db`, `add(path,
  entry)`, `remove(path)`, `compact()`, `init()`. Class attr `format: str`.
- **`open_db(path, fmt=None, *, for_read=False) -> DbProvider`** — resolve
  and construct the provider. Precedence: explicit `fmt` wins; else, when
  `for_read` and `path` already exists, its content is sniffed (so a
  mislabeled/legacy file still loads correctly); else the `path` suffix
  decides, defaulting to `"jsonl"`. Raises `ValueError` for an unknown
  format name.
- **`register_provider(name, provider_cls, *, suffixes=(), sniff=None) ->
  type[DbProvider]`** — the extension seam for third-party backends. `name`
  is used by `--db-format` and error messages; `suffixes` (lowercase, no
  leading-dot requirement enforced but conventionally `.ext`) infer the
  format from a `--db` path; `sniff(head: bytes) -> bool` inspects a file's
  first 16 bytes to claim it by content (newer registrations are tried
  first). Returns `provider_cls` (usable as a decorator). Re-registering a
  name replaces the previous class.
- **Built-in backends** (all registered at import time): **`JsonlDb`**
  (`format="jsonl"`, suffixes `.jsonl`/`.ndjson`, default when unset) —
  append-only JSON Lines, one object per line, last record per path wins on
  load; **`YamlDb`** (`format="yaml"`, suffixes `.yaml`/`.yml`) —
  append-only YAML, concatenated single-key documents; **`SqliteDb`**
  (`format="sqlite"`, suffixes `.db`/`.sqlite`/`.sqlite3`, sniffed by the
  SQLite file magic) — a real upserted-in-place table, no append log
  (`compact()` drops removed rows + `VACUUM`s).
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
  fileentry) -> bool | None` — `None` means "statement doesn't apply, keep
  evaluating"; a directory that fails a recursive (`**`) pattern short-circuits
  to `False` rather than falling through.
- **`PathMatch(list[PathMatchStmt])`** — an ordered set of statements bound
  to an optional `root` (rewrites each statement's absolute pattern relative
  to `root`). `.match(path, entry=None, _default=None, **overrides) -> bool
  | None` — evaluates statements in order, first non-`None` result wins;
  `entry=None` derives one via `FileEntry.from_path`; an empty `PathMatch`
  always matches (`True`).

## `dbdump` format registry (`dbdump.py`)

- **`dump_formats() -> list[str]`** — every registered format name, sorted.
- **`PER_ENTRY_FORMATS: dict[str, PerEntryDumper]`** — one-line-per-entry
  formats, each `dumper(path, entry) -> bytes`. Built in: `"rpmspecfiles"`
  (RPM `%files` lines: `%attr(mode,owner,group) "path"`, `%dir` prefix for
  directories, `meta["rpmprefix"]` prepended if set).
- **`MULTI_ARTIFACT_FORMATS: dict[str, Callable[[Entries], dict[str,
  bytes]]]`** — formats that render several named artifacts, each
  `render(entries) -> {filename: bytes}`. Built in: `"debian"` — `install`
  (`dh_install`-style `<src> <dest-dir>` lines, non-directory entries only)
  + `permissions` (`<path> <mode> <owner> <group>` lines, only entries that
  pin a non-default mode/owner/group).
- **`Entries`** — type alias `list[tuple[str, FileEntry]]` (surviving DB
  entries after `--exclude` filtering), the shared input shape for both
  registries above.

## CLI subcommands

Each is a `duho.Cmd` subclass self-registered onto `PkgForge`; use them via
the CLI (`pkgforge <name> ...`) rather than instantiating directly unless
you're embedding the CLI layer itself:

- **`install.Install(FileEntryArgs, PkgForgeCmd)`** (`pkgforge install`) —
  stage a source (file / directory / symlink / tar-family archive /
  decompress-on-copy) into the build root, apply mode/ownership, and record
  the entry. `-D` = `-Tp` shortcut, `-d` = `--type directory` shortcut.
- **`scan.ScanCmd(FileEntryArgs, PkgForgeCmd)`** (`pkgforge scan`) — walk a
  path under the build root, recording an entry per file; `--missing` only
  fills gaps not already in the DB.
- **`dbdump.DbDump(PkgForgeCmd)`** (`pkgforge dbdump -f FORMAT [output]`) —
  render surviving (post-`--exclude`) DB entries via the format registry
  above.
- **`initdb.InitDb(PkgForgeCmd)`** (`pkgforge initdb`) — create or truncate
  an empty DB; no-op for an unset/stdout DB.
- **`compact.Compact(PkgForgeCmd)`** (`pkgforge compact`) — collapse an
  append-log DB to one record per live path, dropping removals and
  superseded history; no-op for backends without an append log.

## Environment variables

- **`PKGFORGE_ROOT`** — default `--buildroot`.
- **`PKGFORGE_DB`** — default `--db`.
- **`PKGFORGE_DB_FORMAT`** — default `--db-format`.

## Gotchas

- `mode` is always an octal **permission string** (`"644"`), never a raw
  `st_mode` int — `apply()` converts with `int(mode, 8)`.
- The append-log backends (`jsonl`, `yaml`) never truncate on write; `load()`
  keeps only the last record per path. Call `compact()` (or `pkgforge
  compact`) to reclaim space / drop history. `sqlite` has no log to compact
  beyond dropping removed rows.
- Hardlink install uses `os.link` rather than `Path.link_to`/`hardlink_to`
  for full Python 3.9–3.13 coverage (`link_to` was removed in 3.12,
  `hardlink_to` only exists from 3.10).
- `chown` (owner/group) requires the Unix `pwd`/`grp` stdlib modules; both
  import guarded to `None` off POSIX, so `.apply(chown=True, ...)` raises
  `RuntimeError` there. Parser/`--help` construction still works everywhere.
- A `PathMatchStmt`/`PathMatch` result of `None` is not "no match" — it
  means "keep evaluating"; only `PathMatch.match`'s exhausted fallthrough
  (`_default`) is a real default.
