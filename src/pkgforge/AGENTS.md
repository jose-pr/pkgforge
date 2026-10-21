# `pkgforge` — public API header

Header-file-style reference for the `pkgforge` package: every public export with
its signature, arguments, contract and gotchas, so the package can be used
without reading its source. It ships inside the package and is
self-contained. Development documentation lives with the source at
<https://github.com/jose-pr/pkgforge>.

`pkgforge` exports `Project`, `VerifyIssue`, `FileEntry`, `FileType`,
`FileEntries`, `FileRecords`, `StrPath`, `DbProvider`, `open_db`, `DumpFormat`,
`PackageFormat`, the exceptions (`PkgForgeError`, `UsageError`, `DbError`,
`DumpError`, `InvalidEntryError`, `UnsupportedOutputError`,
`ExcludeSyntaxError`) and `__version__`. The public modules are
`pkgforge.config`, `pkgforge.entry`, `pkgforge.db`, `pkgforge.exclude`,
`pkgforge.dbdump`, `pkgforge.package`, `pkgforge.testing` and
`pkgforge.exceptions`; each lists its names in `__all__`. A module or package
whose name starts with `_` is private, and `pkgforge.cli` is the command line,
not library API. `import pkgforge` loads neither `duho`, `anycfg` nor
`pkgforge.cli`.

`pip install pkgforge` (Python 3.9+). The `yaml` extra adds PyYAML for the YAML
file-DB backend; the `zstd` extra adds `zstandard` for zstd package compression
below Python 3.14, which has `compression.zstd`. Staging uses POSIX facilities
(`chmod`, `chown`, symlinks), so it runs on Linux; importing the package and
building the command line work on any platform.

**`pkgforge.__version__: str`** is the installed distribution version
(`importlib.metadata.version("pkgforge")`), or `"0.0.0"` when running from a
source checkout that is not installed.

## Project (`pkgforge`)

`pkgforge.Project` is the Python API for build scripts; the `pkgforge` command
line is a wrapper over it. Nothing in it prints, exits, reads `sys.argv` or reads
a `PKGFORGE_*` variable; it logs under the operation loggers `pkgforge.install`,
`pkgforge.scan`, `pkgforge.initdb`, `pkgforge.compact`, `pkgforge.dbdump`,
`pkgforge.mkdir`, `pkgforge.symlink`, `pkgforge.sysfiles`, `pkgforge.stage`,
`pkgforge.verify` and `pkgforge.package`. DB paths (`dest`, a scan `path`, a DB
key) are `/`-rooted POSIX `str`; local paths are `StrPath`. A method raises the
plain exception; the command line turns it into an exit status.

- **`Project(buildroot: StrPath, db: StrPath | DbProvider | None = None, *,
  db_format: str | None = None, config: ProjectConfig | None = None,
  config_path: StrPath | None = None)`** — a build root plus an optional file
  DB. `db_format` is a registered format name (else inferred from the `db`
  suffix, else `jsonl`). Validates and stores; opens nothing, holds no
  resource, so it is not a context manager, and is not thread-safe. With
  `db=None` no file is written and every operation returns the records it
  would have written. Attributes: `buildroot: Path`, `db: DbProvider | None`,
  `config: ProjectConfig` (`ProjectConfig()` when none was given) and
  `config_path: Path | None`, the absolute path of the file `config` was read
  from (`from_config` sets it; a generated `debian/rules` points at it).
  Raises `TypeError` (a non-path `buildroot`/`db`, or a `config` that is not a
  `ProjectConfig`), `ValueError` (`db="-"`, or a `DbProvider` together with
  `db_format`), `UsageError` (unknown `db_format`).
  Every write checks the entry's owner and group: one that is neither `-`/`--`,
  a base-passwd name nor declared in `config.users`/`config.groups` (a created
  user's own name counts as a group) is `UsageError` (`PATH: owner 'x' is
  neither a base-passwd account nor declared in [[users]]`), a name read from
  disk with `--` included. `install` and `scan` check their explicit arguments
  before staging or walking.
- **`Project.from_config(path: StrPath, *, buildroot: StrPath | None = None,
  db: StrPath | DbProvider | None = None, db_format: str | None = None) ->
  Project`** — a project described by the file at `path` (see
  [Configuration](#configuration-pkgforgeconfig)). An argument beats the file
  and the file beats the default (`buildroot` `"."`, no file DB; a file
  `db = "-"` means no file DB). `UsageError` for an invalid file,
  `FileNotFoundError` for a missing one.
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
  group="-", type: FileType | None = None, kind="-", caps="-", package="-",
  meta: Mapping[str, str] | None = None, into=True, parents=False,
  decompress: str | None = None, method="copy", exclude: Sequence[str] = (),
  record=True, record_tree=False, chown=False, remove_source=False) ->
  FileRecords`** — stage each source under the build root at `dest` and record
  it; what `pkgforge install` does (see [`pkgforge install`](#pkgforge-install)
  for staging, extraction and `method`). `sources` is a sequence (a bare
  `str`/`PathLike` is `TypeError`); `type` is a `FileType` or `None` (a `str` is
  `TypeError`: pass `FileType("directory")`); `exclude` takes statement text only
  (a parsed `PathMatchStmt` is `TypeError`); `"-"` reads `sys.stdin.buffer` (one
  source at most) and `""` means no source (an empty file, or with
  `type=FileType.Directory` an empty directory). `dest` is a `str` (`TypeError`
  otherwise): the directory to install into, or with `into=False` the final
  path. `mode`/`owner`/`group` take a value, `"-"` (leave the default) or `"--"`
  (read it from the staged file). `kind` is a `FileKind` value and `caps` a
  `setcap` clause; `"-"` takes the first matching rule's, else `file` and none,
  and a kind or caps that does not fit the type is `UsageError` before anything
  is staged. `package` is the `[package]` name or a `[[packages]]` name; `"-"`
  takes the first matching rule's, else the `[package]` name (`"-"` with none),
  and a name the configuration does not declare is `UsageError` before anything
  is staged. `decompress` is a kind (`gz`, `xz`, `bz2`, `zst`, `lzma`), `"auto"`
  (infer it from the source suffix) or `None`. `method` is `copy`, `link` or
  `move` (`UsageError` otherwise). `exclude` statements follow the
  [exclude grammar](#exclude-grammar-pkgforgeexclude); an absolute glob is
  relative to each source. `record=False` stages without writing an entry.
  `record_tree=True` also records every path below an installed directory or
  archive that the DB does not hold. Every source is validated and resolved
  before any is staged, so a bad argument or two sources resolving to one
  non-directory destination leaves nothing on disk. Returns what was written,
  in write order. Raises `UsageError`, `PkgForgeError` and `OSError` exactly
  where the command exits 2 or 1.
- **`mkdir(paths: Sequence[str], *, mode="-", owner="-", group="-", kind="-",
  package="-", meta: Mapping[str, str] | None = None, parents=False) ->
  FileRecords`** — record each path as a directory and create it under the
  build root, in order, with no source: one `install([""], path,
  type=FileType.Directory, into=False)` each. Running it again changes nothing.
  `paths` is a sequence (a bare `str` is `TypeError`); a missing parent is
  `UsageError` unless `parents=True`. Logs under `pkgforge.mkdir`.
- **`symlink(target: str, link: str, *, owner="-", group="-", kind="-",
  package="-", meta: Mapping[str, str] | None = None, parents=False) ->
  FileRecords`** — record `link` as a symbolic link to `target` and create it.
  `target` is stored as written in `meta["target"]` (relative or absolute, and
  it may name nothing); an empty `target`, or a `meta` that already holds
  `target`, is `UsageError`. A symlink has no mode. Logs under
  `pkgforge.symlink`.
- **`stage(*, init=False) -> FileRecords`** — run the `config.stage` steps in
  order, with missing parents created: `src` + `dest` is `install([src], dest,
  ...)` with the step's `type`, `into`, `decompress`, `method`, `exclude` and
  `record_tree`; `dir` is `mkdir`, `file` an empty file, `symlink` + `target`
  is `symlink`; a step's `mode`/`owner`/`group`/`kind`/`caps`/`package`/`meta`
  are the call's. A step's `exclude` is relative to its source, as
  `install`'s. Every `src` must exist before the first step runs
  (`UsageError: [[stage]] #N: source PATH does not exist`), and no steps is
  `UsageError`. `init=True` first resets the file DB (with no file DB, one
  WARNING and on). One owned-parent bookkeeping spans the whole call. After the
  steps it runs `sysfiles` (its records are appended to the result). Logs
  `[[stage]] #N: SRC-or-KIND -> DEST` per step, `generated N system file(s)` and
  `staged N step(s), M record(s)` under `pkgforge.stage` (INFO), beside the
  installs' own lines.
- **`sysfiles() -> FileRecords`** — write the install-time setup files under
  the build root and record them (`644 root:root`, kind `file`):
  `/usr/lib/sysusers.d/NAME.conf` (NAME is `[package] name`; a `g` line per
  created `[[groups]]`, a `u NAME - "DESCRIPTION" HOME SHELL` line per created
  `[[users]]` with `-` for an unset field, then an `m USER GROUP` line per
  supplementary group; `%` is written `%%`) and `/usr/lib/tmpfiles.d/NAME.conf`
  (a `d PATH MODE OWNER GROUP -` line per ghost directory in the file DB,
  sorted). A file with no lines is not written, and one recorded earlier is
  removed with its entry (`(path, None)` in the result). `UsageError` for lines
  but no `[package] name`, or a ghost directory path with whitespace, `"`,
  `'`, a backslash or a control character. These are payload files because rpm
  4.19+ creates the accounts of a shipped sysusers file by itself, and
  `dh_installsysusers` and `dh_installtmpfiles` find them in the package. Logs
  `wrote PATH` or `removed PATH` under `pkgforge.sysfiles` (INFO). `stage` runs
  it.
- **`scan(path: str, *, mode="-", owner="-", group="-", meta=None,
  dir_mode: str | None = None, missing=False, drop_stale=False,
  exclude: Sequence[str] = ()) -> FileRecords`** — record every path below
  `path` (never `path` itself) with its own on-disk type; what `pkgforge scan`
  does (see [`pkgforge scan`](#pkgforge-scan)). `mode` applies to files only,
  `dir_mode` to directories (default `"--"` when `mode` is `"--"`, else `"-"`);
  a symlink's mode is always `"-"`. `drop_stale=True` needs a file DB
  (`UsageError`).
- **Record-time resolution** — every entry `install` and `scan` write (and
  `install --record-tree` children) has its `mode`, `owner`, `group`, `kind`,
  `caps` and `package` that were left at `"-"` filled in, a value or flag never
  changed: first from the first `[[rules]]` element whose `match` fits the DB
  path (only the fields that rule sets), then (mode, owner, group) from
  `[defaults]`, else they stay `"-"` (`kind` becomes `file` and `package` the
  `[package]` name; `-k`/`--caps`/`-P` apply to the installed entry only, never
  to `--record-tree` children or owned parents; a `--record-tree` child's mode
  is then read from disk). A symlink never gets a mode. `install` without `-m`
  also chmods the staged file to the mode it resolved to, and `--record-tree`
  chmods each child to its resolved mode (mode only, no chown); owned parents
  and `scan` entries are recorded only. With no rule and no default nothing
  changes.
- **Owned directories** — with `config.own` set, an entry written at or below
  an `own` prefix is preceded, in the returned records and the DB, by each
  ancestor directory at or below a prefix (the prefix included) that the DB does
  not hold yet: type `directory`, resolved like any entry, recorded and never
  chmod'ed. The DB is read once per `install`, `scan`, `mkdir`, `symlink` or
  `stage` call, and only when an entry needs it. `record=False` and removals
  write none.
- **`dump(fmt: str, output: StrPath | None = None, *, exclude: Sequence[str] =
  (), dirs: str = "all", skeleton: bool = False, force: bool = False) ->
  Rendered`** — render the file DB with the format `fmt` (a name or alias).
  `skeleton=True` also writes the files a format's user edits (`debian`:
  `control`, `changelog`, `rules`, `source/format`) into a directory `output`;
  they and an `rpmspec` file are kept, with a WARNING, when they exist, unless
  `force=True` (`UsageError` for `skeleton` with a format that has none or
  without a directory `output`, for `force` with neither `skeleton` nor
  `rpmspec`, and for a skeleton with no `config_path`). `dirs="owned"` keeps a
  directory entry only at or below a prefix of `config.own` (`UsageError` for
  another value, or with no prefixes). The format and OUTPUT shape are checked
  first, then the DB must exist (`UsageError` for `db=None` or a missing file);
  with `output` the result is also written whole (a file, or a directory for a
  multi-artifact format, or an existing directory for a per-entry one), passing
  `config` so the packages are the project's. See
  [`pkgforge.dbdump.dump`](#dump-formats-pkgforgedbdump) for the rest and the
  errors.
- **`verify(*, exclude: Sequence[str] = ()) -> list[VerifyIssue]`** — compare
  the build root with the live entries of the file DB, as `rpmbuild` and
  `dh_missing` will see them. `unrecorded`: a file or symlink on disk with no
  entry (an unrecorded directory is not reported: the package owns only the
  directories it records); `missing`: an entry whose path does not exist (a
  `ghost` is never expected); `type`: the on-disk type differs from the
  recorded one; `target`: a symlink points elsewhere than `meta["target"]`.
  Mode, owner and group are not compared: the DB's values are what every dump
  format ships, and owned parents and `scan` entries keep their disk mode. The
  file DB found inside the build root is skipped (one WARNING). `exclude` is
  matched against DB paths, as `dump` does; a matching path is dropped from
  both sides and a matching directory is not descended. Sorted by path, then
  problem; `[]` when the tree matches. `UsageError` for `db=None`, a missing
  file DB or a missing build root; `ExcludeSyntaxError` for a bad statement.
  Logs `verified N entries: M issue(s)` under `pkgforge.verify` (INFO).
- **`package(fmt: str, output: StrPath = ".", *, arch: str | None = None,
  compression: str | None = None) -> list[Path]`** — build one package per
  declared package (the main first, then each `[[packages]]` element) in the
  format `fmt` into the directory `output` (created if missing) and return the
  paths written; see [Package formats](#package-formats-pkgforgepackage).
  Nothing is staged: every live non-ghost entry must exist in the build root
  with its recorded type, and files the DB does not record are not shipped.
  `UsageError` for no file DB, a missing DB file or build root, an unknown
  `fmt`, an `arch` or `compression` the format cannot take, a `[package]` key
  the format needs, an entry of an undeclared package, a recorded path that is
  missing or of another type (naming the path and `pkgforge verify`), or one
  whose parent is a symlink leading outside the build root (`PATH: a symlink
  leads outside --buildroot`); `InvalidEntryError`; `DumpError` for an entry
  the format cannot represent; `PkgForgeError` for a failed tool; `OSError`.
- **`VerifyIssue(path: str, problem: str, recorded: str | None, found: str |
  None)`** — a `typing.NamedTuple`: `path` a DB path, `problem` one of
  `unrecorded`, `missing`, `type`, `target`. `recorded` is the entry's type (or
  the recorded symlink target for `target`), `None` for `unrecorded`; `found`
  is the on-disk type (`special` for a fifo, socket or device) or the symlink's
  actual target, `None` for `missing`.
- A `dest` (`install`) or `path` (`scan`) must resolve inside the build root:
  a `..` that climbs above it, or a symlinked component that leads outside
  it, raises `UsageError` before anything is written or recorded. A relative
  build root whose realpath is `/` (the cwd default, run from `/`) raises
  `UsageError` unless it was spelled `/` explicitly. An in-root `..`
  (`/usr/share/../lib/x`) is normalized in the path that is staged and
  recorded (`/usr/lib/x`).

## Configuration (`pkgforge.config`)

`pkgforge.cfg` describes one package: a TOML document read through `anycfg`
(imported only when a file is read) into frozen dataclasses. A key the schema
does not list is an error, and so is a value of the wrong type: every text field
must be a quoted string (`mode = 644` and `mode = 0o644` are refused, write
`mode = "644"`). A YAML file's `!include` and `!load` are refused
(`UsageError`).

- **`load(path: StrPath) -> ProjectConfig`** — read and validate a file.
  `UsageError` (its text starts with the path, then `LINE:COL:` for a
  malformed file or `[at KEY (ProjectConfig)]` for a bad key or value),
  `FileNotFoundError` for a missing file, `OSError` for an unreadable one.
- **`ProjectConfig.from_dict(data: Mapping[str, Any]) -> ProjectConfig`** —
  validate a mapping shaped like the file (no `${...}` replacement, no path
  joining). `UsageError` as above, without the path.
- **`${...}`** — in any text value, `${package.name}` is replaced by that key of
  the file (`${table.key}`, `${list.0}`); `$${` is a literal `${`. An undefined
  reference is `UsageError` (`undefined reference`). The environment is not
  readable from the file.
- **Relative paths** — a relative `buildroot`, `db` (unless `"-"`) and each
  `[[stage]]` `src` are taken relative to the file's directory, so the file
  works from any working directory.

Tables, all optional (`ProjectConfig` fields in this order):

| Key | Type | Meaning |
| --- | --- | --- |
| `buildroot`, `db`, `db_format` | text | the options of the same name; `db = "-"` means no file DB |
| `own` | list of text | `/`-rooted directory prefixes the package owns (no `..`, not `/`; stored normalized) |
| `[package]` | `PackageInfo` | the main (and source) package: `name` (required, `[a-z0-9][a-z0-9+.-]+`), `version` (`[0-9][A-Za-z0-9.+~]*`), `release` (`[A-Za-z0-9.+~]+`), `arch` (`all` or `any`), `summary`, `license`, `homepage` (one line each), `maintainer` (`Name <address>`), `description` (no control character but newline), and the sub-tables `[package.rpm]` (`RpmInfo`: `requires`, a list) and `[package.debian]` (`DebianInfo`: `depends`, a list of items without `,`; `section`, lowercase; `priority`, one of `required`, `important`, `standard`, `optional`, `extra`) and `[package.pacman]` (`PacmanInfo`: `depends`, a list of one-word items such as `glibc` or `bash>=5`; named for the manager because `arch` is already a key) and `[package.apk]` (`ApkInfo`: `depends` as `PacmanInfo`); values to name once and reuse as `${package.KEY}` |
| `[[packages]]` | `SubPackageInfo` | a subpackage: `name` (required, the full package name such as `hello-doc`, unique and not the `[package]` name; needs `[package]`), `summary`, `description`, `arch` (unset: the main's), `rpm`, `debian`, `pacman`, `apk` as in `[package]` |
| `[defaults]` | `EntryDefaults` | `owner`, `group`, `file_mode`, `exec_mode`, `dir_mode` |
| `[[rules]]` | `EntryRule` | `match` (non-empty list of `-X` statements), `mode`, `owner`, `group`, `kind`, `caps`, `package` (the `[package]` name or a `[[packages]]` name) |
| `[[users]]` | `UserAccount` | `name` (required), `description`, `home`, `shell` (absolute, no whitespace or `\`), `groups` (list of supplementary groups), `create` (boolean, default `true`; `false` takes `name` only) |
| `[[groups]]` | `GroupAccount` | `name` (required), `create` (boolean, default `true`) |
| `[[stage]]` | `StageStep` | `src` + `dest`, or `dir`, or `file`, or `symlink` + `target`; `mode`, `owner`, `group`, `kind`, `package`, `meta` (table); `caps` with `src` or `file` only; with `src` only: `type`, `into`, `decompress`, `method`, `exclude`, `record_tree` (the `install` options) |

A mode is 1-4 octal digits and an owner or group an account name (`-`, `--` and
`auto` are refused: a file states values). A `[[stage]]` step names exactly one
of `src`, `dir`, `file`, `symlink`; `symlink` takes no `mode` and no
`meta.target` (`target` sets it). `type` is `file`, `directory` or `symlink`,
`decompress` a kind or `auto`, `method` `copy`, `link` or `move`. `[defaults]`
and `[[rules]]` apply when an entry is recorded: the rules are tried in order
against the entry's DB path with the `-X` grammar (inline tests such as
`(?type:directory)` see the entry; to exempt a path put a `!` statement before
the glob that would match it), the first match fills the `mode`, `owner` and
`group` it sets, and `[defaults]` fills what is still unset (`dir_mode` for a
directory, `exec_mode` for a file with an execute bit, else `file_mode`; `owner`
and `group` for every type). A flag always wins and a symlink never gets a mode.
`own` lists the directories the package owns: an entry recorded at or below one
also records the missing ancestor directories down from the prefix, and
`dbdump --dirs owned` keeps only directories at or below one. `[[stage]]` is
stored on `Project.config` and run by `Project.stage` (`pkgforge stage`).
Command tables such as `[install]` and any other key are not part of the
schema.

An owner or group an entry carries must be a base-passwd name
(`pkgforge.entry.BASE_USERS`/`BASE_GROUPS`) or declared. `[[users]]` and
`[[groups]]` declare them: `create = true` (the default) means the package makes
the account, `create = false` that it exists on the target already. A created
name matches `[a-z_][a-z0-9_-]{0,30}`, a created user's group of the same name is
implied, and a created account needs `[package] name`. Refused when the file is
read: a base-passwd name, a duplicate, a `[[groups]]` element named like a
created user, a supplementary group nothing defines, a `description` with a
control character, `"` or `\`, a `home` or `shell` that is not absolute or holds
whitespace or `\`, `description`/`home`/`shell`/`groups` on a `create = false`
user, and an owner or group named by `[defaults]`, `[[rules]]` or `[[stage]]`
that is neither base-passwd nor declared.

Which file the command line reads: `--config/-c FILE`, else `PKGFORGE_CONFIG`
(the file must exist; empty means no file), else `pkgforge.cfg` in the working
directory when it exists. There is no per-user or system file. Precedence for
`db`, `db_format` and `buildroot`: command line, environment, file, default.
The whole file is validated before any command runs: a problem is one
`pkgforge: error:` line, status 2.

## File entries (`pkgforge.entry`)

- **`FileType(str, enum.Enum)`** — `File`, `Directory`, `Symlink`.
  `FileType.from_path(path) -> FileType` inspects a real path (raises
  `TypeError` if it is none of the three — missing or a special file such as
  a FIFO or socket).
- **`FileKind(str, enum.Enum)`** — how packaging treats an entry, whatever the
  format: `File` (`"file"`, the default), `Config` (`"config"`),
  `ConfigNoReplace` (`"config-noreplace"`), `Doc` (`"doc"`), `License`
  (`"license"`) and `Ghost` (`"ghost"`, owned by the package, shipped by
  nothing). `config`, `config-noreplace`, `doc` and `license` need type `file`;
  `file` and `ghost` fit every type.
- **`FileEntry(typing.TypedDict)`** — one DB record: `mode` (octal permission
  **string**, e.g. `"644"`, not a raw `st_mode` int), `owner`, `group`,
  `type`, `kind` (a `FileKind` value; `"-"` only before the entry is
  recorded), `caps` (one `setcap` clause such as `cap_net_bind_service=ep`, or
  `"-"` for none; recorded only, never applied while staging), `package` (the
  package the entry ships in: the `[package]` name or a `[[packages]]` name;
  `"-"` only when the configuration has no `[package]`), `meta: dict[str,
  str]`. A DB record has exactly these eight keys. A `FileEntry` value is a
  **plain dict** at runtime (it is a `TypedDict`), so it carries no methods of
  its own; use the module functions below.
- **`entry_from_path(path: Path, meta: Optional[dict[str, str]] = None) ->
  FileEntry`** — build by `lstat`-ing a real path; owner/group resolve via
  `pwd`/`grp` (`"-"` where those modules are unavailable, i.e. non-POSIX).
- **`resolve_entry(entry: FileEntry, path: Path, lookupval: str = "--",
  **overwrite) -> FileEntry`** — replace every field of `entry` equal to
  `lookupval` (default the `AUTO` sentinel) with the on-disk value for `path`,
  from a single `lstat`. A `pwd`/`grp` name lookup only happens for
  `owner`/`group` fields that equal `lookupval`. uid/gid -> name lookups are
  memoized per process (negative results included), so repeated ids across a
  large tree cost one real lookup each.
- **`apply_entry(entry: FileEntry, path: Path, chown: bool = False, *,
  logger: Optional[logging.Logger] = None, usedefault: str = "-") -> None`**
  — if `chown=True`, `chown` first (raises `RuntimeError` if `pwd`/`grp` are
  unavailable; raises `UsageError` for an unknown owner/group), then `chmod`
  unless `mode` is `"-"` or empty (an empty mode/owner/group is treated like
  `"-"` everywhere this function reads them). Raises `UsageError` if `mode` is
  still the unresolved `AUTO` sentinel (`"--"`): call `resolve_entry` first.
  Chown runs before chmod because Linux clears a regular file's setuid/setgid
  bit on any `chown()`, even to the same owner; if `chown=True` and the entry
  leaves `mode` at `usedefault`, any setuid/setgid/sticky bit already on disk
  is restored after the chown. A symlink's mode is never set on disk (Linux
  ignores it); only its owner and group are applied.
- **`normalize_mode(value: str) -> str`** — normalize a `mode` value to an octal
  permission string; an `int` is not a mode and raises `UsageError`.
  `"-"`/`""` mean `DEFAULT`; `"--"` or `"auto"` mean `AUTO`; a string of 1-4
  octal digits normalizes (`"0644"` -> `"644"`). Anything else raises
  `UsageError`. `auto` is a mode-only alias — `owner`/`group` get none, since
  `auto` can be a real account name.
- **`normalize_name(value: str, field: str) -> str`** — validate an
  `owner`/`group` value (`field` names it in the error): an explicit empty
  string is `DEFAULT`, and anything but an account name
  (`[A-Za-z_][A-Za-z0-9_.-]*`, optionally ending in `$`) or the `-`/`--`
  sentinels raises `UsageError`.
- **`normalize_kind(value: str) -> str`** — `-`/`""` are `DEFAULT`; a
  `FileKind` value (or member) is returned as text; anything else is
  `UsageError` (`choose from file, config, ...`).
- **`normalize_caps(value: str) -> str`** — `-`/`""` are `DEFAULT`; otherwise
  one lowercase `setcap` clause without spaces (`cap_net_raw=ep`,
  `cap_net_raw,cap_net_admin+ep`), else `UsageError`.
- **`normalize_package(value: str) -> str`** — `-`/`""` are `DEFAULT`;
  otherwise a package name (`[a-z0-9][a-z0-9+.-]+`), else `UsageError`.
- **`check_meta(meta: Mapping[str, str]) -> None`** — raises `UsageError`
  for the key `rpmprefix`, which no dump reads: use `kind` or `caps`.
- **`BASE_USERS`** / **`BASE_GROUPS`** — `frozenset[str]` of the account names
  every Debian system has (base-passwd's static `passwd.master` and
  `group.master`, e.g. `root`, `daemon`, `www-data`; `adm` is a group only). An
  owner or group outside them must be declared in `pkgforge.cfg`
  (`[[users]]`/`[[groups]]`) before an entry carrying it is recorded.
- **`AUTO = "--"`** / **`DEFAULT = "-"`** — module-level sentinels: `AUTO`
  means "resolve from the file on disk" (used by `resolve_entry`); `DEFAULT`
  means "leave at the OS/system default, do not set explicitly" (used by
  `apply_entry`'s `usedefault`).
- **`mode_to_octal(mode: int) -> str`** — render a raw `st_mode` as a bare
  octal permission string (`"644"`).
- **`FileEntries`** — type alias `list[tuple[str, FileEntry]]` (the live DB
  entries after `--exclude` filtering), the input of every dump format.
- **`FileRecords`** — type alias `list[tuple[str, FileEntry | None]]`: what an
  operation wrote, in write order (`None` is a removal).
- **`StrPath`** — type alias `str | os.PathLike[str]`: a local filesystem path.

`FileEntry`, `FileType`, `FileEntries`, `FileRecords` and `StrPath` are also
exported by `pkgforge`.

## File DB (`pkgforge.db`)

- **`Db`** — type alias `dict[str, FileEntry | None]` (a loaded DB; `None`
  marks a removed path).
- **`DEFAULT_FORMAT = "jsonl"`** — the format of a `--db` path whose suffix no
  backend claims.
- **`DbProvider(abc.ABC)`** — storage backend bound to a filesystem `path`
  (`provider_cls(path)`). Abstract methods: `load() -> Db`, `add(path, entry)`,
  `remove(path)`, `compact()` (drop superseded records and the removals),
  `init()`. A subclass registers itself by declaring its own `NAME: str` (e.g.
  `NAME = "toml"`), or is advertised in the `pkgforge.dbs` entry-point group
  as `name = module:Class` (`name` equal to the class's `NAME`) and imported
  when first selected; `ALIASES: tuple[str, ...]` (extra selectable names) and
  `SUFFIXES: tuple[str, ...]` (file suffixes that infer this format from a
  `--db` path, case-insensitive; a missing leading dot is added, so `"toml"`
  registers the same as `".toml"`; a suffix with more than one dot, e.g.
  `".tar.gz"`, raises `ValueError` when the class is created and registers
  nothing) are optional. Only the attributes a class sets in its own body
  count: a subclass of a registered class inherits neither its aliases nor its
  suffixes, and a base class with no `NAME` registers nothing. A `NAME`, alias
  or suffix another class holds raises `ValueError` naming both classes, and
  the new class is not registered.
  - **`DbProvider.get(name) -> type[DbProvider]`** — `name` is a `NAME` or an
    `ALIASES` entry, else the name of an entry point in the group, which is
    imported and registered. Raises `UsageError` (also a `ValueError`) naming
    every registered name with its aliases for an unrecognized one, and
    `PkgForgeError` naming the entry point when it fails to import, is not a
    `DbProvider` subclass with that `NAME`, or clashes with a registered class.
    Lookups are case-sensitive.
  - **`DbProvider.names() -> list[str]`** — every registered `NAME` plus every
    advertised name, sorted; nothing is imported.
  - **`batch(self) -> ContextManager[DbProvider]`** — non-abstract; yields
    `self`. The default does nothing extra (every `add`/`remove` inside it
    writes exactly as it would outside one), so a provider that does not
    override it keeps working. `SQLiteDb` overrides it to hold one connection
    across the batch, committing every 1000 rows and once more on exit
    (exception included, so a killed batch keeps what it already committed).
    `Project.scan` wraps its walk and its `drop_stale` removals in one each, and
    an `install` with `record_tree` its entries.
- **`jsonl_line(path: str, entry: FileEntry | None) -> str`** — the one JSON
  Lines record (`None` is a removal marker), newline-terminated, that
  `JSONLinesDb` appends and the commands print with no file DB.
- **`parse_jsonl(text: str, source: str = "text") -> Db`** — parse such text
  (the last record per path wins); errors read `{source}:{line}: ...`
  (`DbError`).
- **`open_db(path, fmt=None) -> DbProvider`** — resolve and construct the
  provider: an explicit `fmt` wins, else the `path` suffix decides, defaulting
  to `"jsonl"`. The file's content is never inspected; a file in another
  format fails in `load()`. Raises `UsageError` (also a `ValueError`) for an
  unknown format name, as `DbProvider.get`.
- **`format_for_suffix(path: Path) -> str`** — suffix -> registered format
  name, else `DEFAULT_FORMAT`. A suffix no registered class holds imports every
  advertised `pkgforge.dbs` entry point not yet registered (a failure is one
  WARNING on logger `pkgforge.db`, then skipped) and looks again.

Built-in backends, registered when `pkgforge.db` is imported:

- **`JSONLinesDb`** — `NAME = "jsonl"`, `SUFFIXES = (".jsonl", ".ndjson")`, the
  default. Append-only JSON Lines, one object per line, the last record per
  path wins on load.
- **`YAMLDb`** — `NAME = "yaml"`, `SUFFIXES = (".yaml", ".yml")`. Append-only
  YAML: a single mapping, appended key by key (the last duplicate key wins). A
  flow-style top-level document (e.g. `{/usr/bin/x: {...}}`, or `{}`) is read
  fine, but `add`/`remove` raise `DbError` instead of appending a block-style
  mapping after it (which would be invalid YAML); `compact()` rewrites the file
  in block style. The loader resolves only YAML's `null` implicitly (`~` and
  `null` load as `None`, for removals); an unquoted scalar loads as the text it
  was written as (`mode: 0755` is `"0755"`, not the int `493`). Needs PyYAML
  (the `yaml` extra); without it, using the backend raises `PkgForgeError`
  naming `pip install 'pkgforge[yaml]'`. The libyaml-backed loader and dumper
  are used when the installed PyYAML has them.
- **`SQLiteDb`** — `NAME = "sqlite"`, `SUFFIXES = (".db", ".sqlite",
  ".sqlite3")`. A table updated in place, no append log (`compact()` drops
  removed rows and `VACUUM`s). `load()` and `compact()` never write: an empty or
  schema-less file loads as (or compacts as a no-op on) an empty DB, and a
  SQLite file that holds other tables but no `entries` table raises `DbError`
  (some other program's database) instead of getting one added. `add`/`remove`
  reject a `path` or a `mode`/`owner`/`group`/`type` value that is not valid
  UTF-8 with `DbError`, before any SQL runs for that record. An interpreter
  without `sqlite3` raises `PkgForgeError` naming the missing module.

`import pkgforge`, `--help` and the `jsonl` backend work on an interpreter that
lacks `sqlite3` or PyYAML.

**Record schema.** Every built-in loader checks each record and coerces and
defaults nothing: the path is a `str` starting with `/`; a record is a mapping
with exactly the eight `FileEntry` keys, `mode`, `owner`, `group`, `caps` and
`package` a `str`, `type` one of `file`, `directory`, `symlink`, `kind` a
`FileKind` value and `meta` a mapping of `str` to `str`; or a removal (jsonl
`{"path": P, "_removed": true}`, yaml `null`, sqlite `removed=1`). Anything else
raises `DbError` naming the file (with the line for `jsonl`) and the path; a
record missing `kind`, `caps` or `package` adds `this DB was written by an older
pkgforge: run initdb and record again`. Content that is not in the chosen
format (a YAML file read as `jsonl`, a text file read as `sqlite`) raises
`DbError` that names the format and suggests `--db-format`. A third-party
`load()` is not checked.

**Text DBs** (`jsonl`, `yaml`) are always UTF-8, whatever the locale; a leading
byte order mark is ignored and records split on `\n` only. An append to a file
whose last line lacks a trailing newline (a hand edit) adds it first. Writes and
`compact` take an advisory `flock` of the DB file (a no-op off POSIX), so
`compact` alongside another process appending to the same DB loses no record.
`compact` writes a temporary file, fsyncs it and renames it into place, so a
failed write (`ENOSPC`, a kill) leaves the original untouched; a symlinked
`--db` keeps its link, a hardlinked one is detached.

## Exclude grammar (`pkgforge.exclude`)

- **`PathMatchStmt(argparse.Namespace)`** — one parsed `--exclude` statement:
  `negate: bool`, `tests: list[PathTest]`, `pattern: str`.
  `PathMatchStmt.parse(s) -> PathMatchStmt` parses `[!](?name:arg)*<glob>` (a
  leading `!` negates the whole statement; each `(?name:arg)` or `(?!name:arg)`
  is an inline test, `!` inverting just that test); `ExcludeSyntaxError` for a
  malformed one. Test names: `type` (`(?type:file|directory|symlink)`) and
  `meta` (`(?meta:key=value)`). `.match(path, fileentry) -> bool | None` — a
  statement that applies returns `True` (exclude) or, when negated, `False`
  (keep); one that does not apply returns `None`, "keep evaluating", so a
  statement never vetoes the ones after it. `.anchored -> bool` is true when
  `pattern` is rooted (a leading `/`, or a Windows drive): it is matched from
  the root of the input rather than at any depth.
- **`PathTest`** — a `typing.Protocol`: one inline test, `__call__(path,
  entry: FileEntry) -> bool`; only `entry` is read.
- **Glob syntax** — `**` as a whole segment matches zero or more segments; a
  *trailing* `/**` (or a bare `**`) matches one or more segments — "the
  contents of this directory", never the directory itself. `*`/`?` never cross
  `/` and match in linear time; `[...]`/`[!...]` is a character class, with
  `&`, `~` and `|` literal in it. A run of `**` segments is one. A relative
  pattern matches at any depth. The result is the same on every supported
  Python (`PurePath.match` is not used).
- **`PathMatch(stmts: Iterable[PathMatchStmt], root: Path | None = None)`** — a
  `list` of statements matched against the names of the input a command reads,
  the way `tar --exclude` matches member names. With a `root`, a path is matched
  as `/` plus its root-relative POSIX text (`/` for the root itself), so an
  absolute pattern is relative to the root; a path outside `root` raises
  `ValueError` (a caller bug). With no `root` (`dbdump`, `verify`) the path is
  matched as its own POSIX text, a `str` DB key included. Statements are stored
  exactly as given. `.match(path, entry=None, **overrides) -> bool | None`
  evaluates the statements in order and returns the first non-`None` result
  (`None` when none applies); an empty `PathMatch` always returns `True`. The
  entry is derived with `entry_from_path` at most once, and only for a statement
  whose glob already matched and that has inline tests, so a glob-only statement
  never lstats the path and `-X '*.fifo'` excludes a FIFO instead of raising for
  it. `**overrides` are layered onto a copy of the entry, never the caller's
  dict; an `entry` of `{}` counts as supplied.

## Dump formats (`pkgforge.dbdump`)

- **`DumpFormat(abc.ABC)`** — a packaging manifest format, selected by
  `dbdump -f NAME`. Built as `DumpFormat(*, skeleton: bool = False)`
  (`UsageError("NAME has no skeleton")` for `skeleton` when the class has
  none). Class attributes `SKELETON: bool` (the format writes a skeleton with
  `skeleton=True`) and `KEEP_EXISTING: bool` (everything it writes is a
  skeleton: an existing OUTPUT is kept unless `force`) are `False` by default.
  A subclass registers itself by declaring its own `NAME`/`ALIASES`, or is
  advertised in the `pkgforge.dump_formats` entry-point group, with the same
  rules as `DbProvider`; `DumpFormat.get(name)` and `DumpFormat.names()` work
  the same way. Subclass one of the two shapes below; `RPMSpec` subclasses
  `DumpFormat` itself because it writes one file with its own `check_output`
  and `write`.
- The format contract: `render(entries: FileEntries, context: DumpContext) ->
  Rendered` (writes nothing), `check_output(output: Path) -> None` (raises
  `UnsupportedOutputError`; there is no `-` case) and `write(rendered, output:
  Path, *, force: bool = False) -> list[Path]` (whole or nothing; returns the
  files written, a kept skeleton file excepted).
- **`Rendered`** — type alias `bytes | dict[str, bytes]`: one file's bytes
  (a `PerEntryFormat`) or `{filename: bytes}` (a `MultiArtifactFormat`, or a
  `PerEntryFormat` given a directory).
- **`DumpContext(config: ProjectConfig = ProjectConfig(), config_path: Path |
  None = None, output: Path | None = None)`** — frozen dataclass handed to
  every `render`: the project configuration whose `[package]` and
  `[[packages]]` name the packages, the file it came from when known, and the
  OUTPUT being written (`None` for stdout). A format groups the entries by
  package, main first, an entry recorded as `-` in the main package, empty
  groups kept; a package the configuration does not declare is `UsageError`.
- **`dump(db: Mapping[str, FileEntry | None], fmt: str, output: StrPath | None
  = None, *, exclude: Sequence[str] = (), own: Sequence[str] | None = None,
  config: ProjectConfig | None = None, config_path: StrPath | None = None,
  skeleton: bool = False, force: bool = False) -> Rendered`** — the dump
  pipeline over an already-loaded DB: look up `fmt` (a name or alias), check
  `output`'s shape (before anything is rendered), drop removed entries, those
  matching an `exclude` statement (an absolute glob is relative to the DB path)
  and, with `own` (`/`-rooted prefixes), each directory entry that is not at or
  below a prefix (other types are untouched), sort by path (code-point order,
  so a staged tree gives byte-identical manifests on any filesystem or
  backend), validate (`InvalidEntryError`), render, and with `output` write it
  whole (a file, or a directory created if missing), logging `Wrote %s` per
  file on `pkgforge.dbdump`. Returns the rendered bytes. `UsageError` for an
  unknown format or statement, `UnsupportedOutputError` for a wrong-shape
  `output`, `DumpError` for an entry the format cannot represent.
  `config_path` is the file `config` came from (a skeleton's `debian/rules`
  names it). `skeleton` and `force` are as for `Project.dump`; a kept file is
  logged as WARNING `kept existing PATH (pass --force to replace it)` on
  `pkgforge.dbdump` and not returned by `write`. A format's `render()` called
  directly does not validate the entries.
- **`PerEntryFormat(DumpFormat)`** — renders one line per DB entry. Abstract
  `render_entry(path, entry) -> bytes`; concrete `render(entries, context)`:
  with `context.output` an existing directory it returns
  `{PACKAGE_FILE.format(package=name): lines}` for every declared package
  (`PACKAGE_FILE = "files-{package}.txt"`; an empty group is the class's
  `EMPTY`; `UsageError` without `[package] name`), otherwise the bytes of the
  one package that has entries (`UsageError` naming them when there are
  several: give a directory). `write(rendered, output)` takes bytes (a file) or
  the dict (a directory, created if missing), whole or not at all: a sibling
  `.<name>.pkgforge-tmp` is written first and renamed over it. `check_output`
  accepts a file, a directory or an absent path.
- **`RPMSpecFiles(PerEntryFormat)`** — `NAME = "rpmspecfiles"`, `ALIASES =
  ("rpm",)`. RPM `%files` lines: `%attr(mode,owner,group) "path"`, a `%dir`
  prefix for directories, `%caps(CAPS)` for an entry with `caps`, and the
  entry's kind first (`%config`, `%config(noreplace)`, `%doc`, `%license`,
  `%ghost`; nothing for `file`). A symlink is written with mode `-` (rpm
  ignores a symlink's mode and warns on an explicit one). The path is quoted
  for rpm's `%files -f` parser (rpm 4.19+): `\` and `"` are escaped, glob
  characters are not (rpm's quoting already matches only the literal name), and
  the line is UTF-8 with `surrogateescape` (a non-UTF-8 name round-trips its
  bytes). A `%` anywhere in the path raises `DumpError` (no rpm treats a quoted
  `%` as literal, and `%(...)` runs a shell command), as does a C0 control
  character or DEL. `render_entry(path, entry)` is byte-identical to rendering
  a one-entry list. `EMPTY = b"\n"`: rpm builds an empty package from a list of
  one empty line and refuses an empty or comment-only one.
- **`RPMSpecFilesPre419(RPMSpecFiles)`** — `NAME = "rpmspecfiles-pre419"`,
  `ALIASES = ("rpm-pre419",)`, for rpm older than 4.19, which expands a quoted
  name twice and matches glob characters unescaped. The path is written
  unquoted (rpm's bare-token reader passes a literal `"` or `\` through), and a
  path holding a space, a glob character (`* ? [ ] { }`), `%` (any form) or a
  non-UTF-8 byte raises `DumpError` instead of packaging the wrong file.
  Measured with rpmbuild 4.14.3, 4.16.1 and 4.18.2; pick the class that matches
  the build host's `rpm --version`.
- **`RPMSpec(DumpFormat)`** (`NAME = "rpmspec"`, `KEEP_EXISTING = True`) and
  **`RPMSpecPre419(RPMSpec)`** (`NAME = "rpmspec-pre419"`) — the whole RPM
  spec, written from `pkgforge.cfg`; OUTPUT a file or `-` (a directory is
  `UnsupportedOutputError`). The spec has, in order: marker comments; a
  `%{!?pkgforge_config:%{error:...}}` guard that stops `rpmbuild` unless the
  build is given `--define "pkgforge_config PATH"`, the path of `pkgforge.cfg`
  (no absolute path is embedded); `%global __os_install_post %{nil}` and
  `%global debug_package %{nil}` (the package holds the recorded files
  unchanged); `Name`, `Version`, `Release` (`release`, else `1`), `Summary`,
  `License`, `URL`, `BuildArch: noarch` (arch `all`), one `Requires:` per
  `[package.rpm] requires` item; `%description` (else the summary); per
  `[[packages]]` element `%package -n`, `Summary`, `BuildArch: noarch` when it
  is `all` and the main package is not, `Requires`, `%description -n`; a
  `%install` that runs `pkgforge stage --init` into `%{buildroot}` with the
  file DB in `%{_builddir}` and writes one `files-PACKAGE.txt` per package
  there with `dbdump -f rpmspecfiles` (`-pre419` for `RPMSpecPre419`); a
  `%post` (`systemd-tmpfiles --create /usr/lib/tmpfiles.d/NAME.conf`) in the
  package that records that file; in each package that records a systemd unit
  (a live regular file named `*.service`, `*.socket`, `*.timer` or `*.path`
  directly in `/usr/lib/systemd/system`, not a template or a ghost)
  `%{?systemd_post:%systemd_post U...}` as the first line of its one `%post`, a
  `%preun` with `%{?systemd_preun:%systemd_preun U...}` and a `%postun` with
  `%{?systemd_postun_with_restart:%systemd_postun_with_restart U...}` (they
  need systemd-rpm-macros, or EL8's systemd, when the package is built, and
  expand to nothing without them; no `BuildRequires`); and one `%files [-n P]
  -f ...` per package. `RPMSpecPre419` adds a `%pre` in the package that
  records `/usr/lib/sysusers.d/NAME.conf`: `systemd-sysusers -
  <<'PKGFORGE_SYSUSERS' || :` over the sysusers text (rpm 4.19+ creates the
  accounts of the shipped file before unpacking, so `%attr` can name them;
  older rpm needs the spec to do it). Every configuration text has `%` doubled,
  and a description line (or a summary standing in for one) whose first
  non-blank character is `%` or `#` is `UsageError`: rpm expands a line before
  it looks for a section or conditional, and drops a `#` line as a comment.
  Needs `[package]` `name`, `version`, `summary` and `license`, and a `summary`
  per `[[packages]]` element (`UsageError` listing what is missing, such as
  `rpmspec needs [package] version, license and [[packages]] 0 summary`); an
  `any` subpackage of an `all` main package is `UsageError`. An existing spec is
  kept, with a WARNING, unless `force`. `examples/hello.spec` in the source
  repository is the worked result.
- **`MultiArtifactFormat(DumpFormat)`** — renders several named artifacts into
  a directory. Abstract `render(entries, context) -> dict[str, bytes]`;
  concrete `write(rendered, output)` creates the directory, writes every
  artifact to a sibling `.<name>.pkgforge-tmp` and renames them all into place
  only once every write succeeded; a failure removes the temporary files and
  leaves earlier OUTPUT content as it was. `check_output` refuses an existing
  non-directory.
- **`DebianFiles(MultiArtifactFormat)`** — `NAME = "debian"`, `ALIASES =
  ("deb",)`, `SKELETON = True`. Needs `[package] name` (`UsageError`
  otherwise) and names every per-package file for its package, because
  debhelper reads `debian/PKG.NAME` before `debian/NAME` and refuses the
  nameless form from compat 15 in a source with several binary packages. For
  each declared package `P` (`[package]`, then each `[[packages]]` name; an
  entry recorded `-` is the main package's) it renders:
  - `P.install` — `dh_install`-style `<src> <dest-dir>` lines, non-directory
    entries only;
  - `P.dirs` — `dh_installdirs`-style lines, one per directory entry, always
    written even when empty, so a directory recorded with no files under it
    (e.g. `install -d` for an empty state directory) still reaches the package
    and children `--exclude` dropped are never re-copied;
  - `P.conffiles` — each `config`/`config-noreplace` path outside `/etc/`
    (debhelper adds those itself), one absolute path per line; whitespace in one
    is `DumpError`; no marker, since debhelper copies a comment into the package
    and `dpkg-deb` refuses it;
  - `P.postinst` — `#!/bin/sh`, a `# Generated by pkgforge dbdump -f debian`
    marker line, `set -e`, `#DEBHELPER#`, then under `if [ "$1" = configure ]`
    one guarded `dpkg-statoverride --update --add OWNER GROUP MODE PATH` per
    entry owned by an account the package creates (below), then one `setcap
    CAPS "$DPKG_ROOT"PATH` per `caps` entry;
  - `P.postrm` — the same header; under `if [ "$1" = purge ]` a guarded
    `dpkg-statoverride --remove PATH` per such entry;
  - `P.substvars` — the marker, then `pkgforge:Depends=libcap2-bin` when `P`
    has a `caps` entry, else `pkgforge:Depends=`; list `${pkgforge:Depends}` in
    the package's `Depends`. `dh_prep` deletes the file on every build, so it
    works only when the dump runs after `dh_prep`, as in the `debian/tmp`
    recipe.

  Once per source it renders `not-installed` (the marker, then every package's
  ghost entries that are not directories, sorted and escaped like `install`, so
  `dh_missing --fail-missing` accepts a staged ghost file; debhelper reads only
  the source-wide file) and `fixperms`: a POSIX `sh` script, always written,
  that applies every pinned mode/owner/group; run it from an
  `override_dh_fixperms` target, after `dh_fixperms`, as `sh debian/fixperms
  debian`. Per pinned entry: one `chown owner:group` (or `chown`/`chgrp` when
  only one is pinned), then `chmod`, since `chown` clears a regular file's
  setuid/setgid bit even to the same owner; a symlink gets `chown -h`/`chgrp -h`
  and never a `chmod` (POSIX `chmod` has no `-h` and would follow the link
  outside the package tree). Each path is `"$d"/` (the script's required first
  argument, the debian directory, guarded by `${1:?}`) plus the package name and
  the DB path, quoted for POSIX `sh`, and resolves to `<path>` or the
  `<path>.gz` dh_compress renamed it to; a path that is gone (dh_usrlocal) is
  named on stderr and skipped, exit status 0. With no pinned entry the script
  is its header. Every artifact is written for every declared package, so no
  stale file survives. A ghost entry is left out of `install`, `dirs` and
  `fixperms`.

  **Hand-written files**: `write` checks OUTPUT before it writes anything; a
  refusal is `UsageError`, exit 2, and leaves every file as it was. An existing
  `install`, `dirs`, `not-installed`, `postinst`, `postrm` or `fixperms`
  (package-named or not) whose first two lines hold no line starting with the
  marker is hand-written and never replaced; a nameless `install`, `dirs`,
  `conffiles`, `postinst` or `postrm` in OUTPUT is refused, marker or not,
  because debhelper silently ignores it beside `debian/P.NAME`; an existing
  `P.conffiles` listing a path the new one lacks is refused (mark that file
  `config`, or delete the line). A marked file and `P.substvars` are replaced.

  **Install-time entries**: an entry whose owner is not a base-passwd user or
  whose group is not a base-passwd group (so declared in `[[users]]` or
  `[[groups]]`) cannot be chowned while dpkg unpacks, because the account does
  not exist yet. `fixperms` leaves it alone (dh_fixperms keeps it root-owned)
  and `postinst` applies it: `if ! dpkg-statoverride --list PATH >/dev/null;
  then ...; fi`, so an administrator's override survives an upgrade, with
  `root` for an unset owner or group (`--list` reads a glob, so `\ * ? [` in
  PATH are backslash-escaped there). A symlink of that kind, or one with mode
  `-`, is `DumpError` (an override follows a symlink and needs a mode). The
  accounts come from the generated sysusers.d file, so the Debian build must run
  `dh $@ --with installsysusers` (debhelper 13.10+; `dh_installtmpfiles` is
  already in the compat-13 sequence) and the package gets `systemd-sysusers`
  from `${misc:Depends}`.

  **Escaping** (debhelper compat 13): an `install` source gets a backslash
  before each glob character (`\ * ? [ ] { }`, which also makes a literal `${`
  read as literal), a space as `${Space}`, and a leading `#` backslash-escaped
  (a line starting with `#` is a `dh_install` comment); `dirs` gets the
  leading-`#` case as `./#...` instead (`dh_installdirs` never globs). Every
  destination (`install`'s and `dirs`') is only `${Dollar}{`/`${Space}`-escaped,
  never glob-escaped, since `dh_install`/`dh_installdirs` take it literally. A
  control character in a path raises `DumpError`. Every artifact is UTF-8 with
  `surrogateescape` (a non-UTF-8 name round-trips its bytes), and a DB path is
  split with `posixpath`, so a backslash in a name stays part of it on Windows
  too.

  **Skeleton** (`skeleton=True`, `dbdump --skeleton`): it also renders the
  files its user edits, which need a directory OUTPUT and
  `context.config_path` (`UsageError` naming `--config`, `PKGFORGE_CONFIG` and
  `./pkgforge.cfg` otherwise), `[package]` `version`, `summary` and
  `maintainer` plus a `summary` per `[[packages]]` element, and no `${NAME}` in
  the `summary`, `description`, `homepage` or `maintainer` of any package
  (`UsageError`, before anything is written: dpkg-gencontrol replaces it and
  nothing escapes it; `depends` items are not checked): `control` (source
  stanza with `Section` from `[package.debian] section`, else `misc`,
  `Priority` else `optional`, `Build-Depends: debhelper-compat (= 13),
  dh-sequence-installsysusers`, `Rules-Requires-Root: binary-targets`; then a
  stanza per package, `Architecture` `all` or `any`, `Depends:
  ${misc:Depends}[, ${shlibs:Depends} for any], ${pkgforge:Depends}[, items]`,
  and the description), `changelog` (`NAME (VERSION[-RELEASE]) unstable;
  urgency=medium`, dated `SOURCE_DATE_EPOCH`, else the current time, in UTC),
  `rules` (mode 755; stages into `debian/tmp` in `override_dh_auto_install`,
  runs `dbdump -f debian debian/` there, after `dh_prep`, and `sh
  debian/fixperms debian` after `dh_fixperms`; the config path is relative to
  OUTPUT's parent when below it, else absolute) and `source/format` (`3.0
  (quilt)` when `release` is set, else `3.0 (native)`). A skeleton file that
  exists is kept, with a WARNING, unless `force`; the files built from the DB
  are always rewritten.

## Package formats (`pkgforge.package`)

Importing `pkgforge.package` loads no optional dependency.

- **`build(db: Mapping[str, FileEntry | None], fmt: str, output: StrPath, *,
  buildroot: StrPath, config: ProjectConfig | None = None, config_path:
  StrPath | None = None, arch: str | None = None, compression: str | None =
  None) -> list[Path]`** — what `Project.package` calls with the loaded DB:
  look up `fmt`, check `compression` (default: the format's first;
  `UsageError("FMT takes --compression a, b")`, or `"FMT takes no
  --compression"`) and `arch` (`[a-z0-9_]+`), check the `[package]` keys the
  format needs (`UsageError("tar needs [package] version")`), read
  `SOURCE_DATE_EPOCH` (an integer, else `UsageError`), drop removed entries,
  validate (`InvalidEntryError`), group the entries per package and call the
  format. Logs `Wrote PATH` per file and `built N package(s) with FMT` (INFO)
  under `pkgforge.package`.
- **`PackageFormat(abc.ABC)`** — a package format, selected by `package -f
  NAME`. A subclass registers by declaring its own `NAME`/`ALIASES`, or is
  advertised in the `pkgforge.package_formats` entry-point group, with the same
  rules and `get`/`names` as `DbProvider`. ClassVars: `COMPRESSIONS:
  tuple[str, ...]` (the payload compressions it offers, the default first;
  empty refuses `--compression`), `REQUIRED` (the `[package]` keys it needs
  besides `name`), `SUB_REQUIRED` (the keys of each `[[packages]]` element),
  `ALL_ARCH: str = "all"` (its name for an architecture-independent package)
  and `MACHINES: Mapping[str, str]` (`platform.machine()` in lowercase to its
  architecture name). `build(self, payloads: Sequence[PackagePayload],
  context: BuildContext) -> list[Path]` (abstract) writes one file per payload
  into `context.output`, creating each through `context.writer`, and returns
  the paths in payload order. `arch_name(self, payload, context) -> str`:
  `ALL_ARCH` for an `all` package, else `context.arch` verbatim, else
  `MACHINES[platform.machine().lower()]`; `UsageError("FMT: no architecture
  name for machine 'm': pass --arch")` for a machine the table lacks.
- **`BuildContext(config, buildroot, output, compression, arch, epoch,
  config_path=None, writer=PackageWriter())`** — frozen dataclass: the project
  configuration, the build root, the output directory (which need not exist
  yet), the chosen compression (`None` when the format offers none), `--arch`,
  the `SOURCE_DATE_EPOCH` seconds (`None` when unset), the configuration file
  and the `PackageWriter` a format creates its files through.
- **`PackageWriter()`** — the files of one build, all or nothing. `create(path:
  Path) -> BinaryIO` opens `.NAME.pkgforge-tmp` beside `path` for writing (the
  caller closes it), making `path`'s directory and any missing parent first;
  `move(source: Path, path: Path)` takes a finished file as `path`'s content;
  `commit()` renames every file over its path, in creation order, after
  checking that no target is a directory (`UsageError`, nothing renamed);
  `abort()` removes the temporary files and the directories `create` made.
  `build` calls `commit()` once the format returns and `abort()` on any
  exception (Ctrl-C included), so a failed build leaves no new directory and no
  partial file, and an existing package of the same name keeps its content
  until `commit()`. A format that writes into `context.output` any other way
  gets neither guarantee.
- **`PackagePayload(name, main, info, arch, entries)`** — a `NamedTuple`: the
  package name, the `[package]` table (`PackageInfo`, which holds the version
  and release), the package's own table (`PackageInfo` or `SubPackageInfo`),
  its architecture (`"all"` or `"any"`: its own, else the main's, else
  `"any"`) and its live entries sorted by DB path.
- **Memory** — a package is streamed: each file is read in chunks of at most
  1 MiB into the tar writer, through the compressor, into the output file, so
  memory does not grow with the payload. What stays in memory is proportional to
  the number of files, not their size (the control data, `md5sums`, Arch's
  `.MTREE`, the member list), plus the compressor's own state: about 100 MiB for
  xz (preset 6) and for zstd (level 19), a few MiB for gzip and `none`. A `.deb`
  is one ar file whose member sizes are filled in after each member is written;
  an apk's data member is first compressed to an unnamed scratch file beside the
  output, since `.PKGINFO` in front of it holds its SHA-256, and then copied.
  zstd is given the tar's exact length up front so the frame records it; the
  stream is deterministic, but its bytes are not those of zstd's one-shot call
  on the same tar.
- **Hardlinks** — within one package, regular files that share an inode and
  record the same mode, owner, group and capability are written as the first
  (by DB path) and, for each other one, a tar hardlink member (`LNKTYPE`, size
  0, naming the first as written in that archive: `./usr/bin/a` in a `.deb`,
  `usr/bin/a` elsewhere) in `tar`, `deb`, `arch` and `apk`; `rpm` gets them
  from `rpmbuild`, which sees the pair in its build root. A file that differs in
  any of those, or whose partner is in another package or not recorded, stays a
  copy. `Installed-Size`, `.PKGINFO` `size` and the apk `size` count a link as
  0; `md5sums`, `.MTREE` and the apk checksum list it like any file.
- **`ApkFormat`** — `NAME = "apk"`, `COMPRESSIONS = ("gz",)`, `REQUIRED =
  ("version", "summary")`, `SUB_REQUIRED = ("summary",)`, `ALL_ARCH =
  "noarch"`, `MACHINES` mapping `x86_64`/`amd64` to `x86_64`, `aarch64`/`arm64`
  to `aarch64`, `armv7l` to `armv7`, `i686`/`i386` to `x86`, and `riscv64`,
  `ppc64le`, `s390x` to themselves. One `NAME-VERSION-rREL.apk` per package
  (REL is `[package] release` or `0`, digits only; VERSION must read like an
  apk version, `1.2.3b` but not `1.0+git`: `UsageError`), an apk v2 file that
  apk-tools 2 and 3 install with `--allow-untrusted` (no signature; apk v3
  packages are not written). The file is two gzip members back to back: the
  control tar (USTAR, no end-of-archive blocks) holding `.PKGINFO` and the
  scripts, then the data tar (pax, no `./`, the payload sorted by DB path, the
  DB's owner names with uid and gid 0) with an `APK-TOOLS.checksum.SHA1` record
  (hex) on every regular file (its content) and symlink (its target), which
  apk-tools 3 requires. A path that is not UTF-8 is a `DumpError`. `.PKGINFO`
  (`key = value` lines): `pkgname`, `pkgver` (`VERSION-rREL`), `pkgdesc` (the
  summary), `url` (the main's `homepage`), `builddate`, `packager` (the main's
  `maintainer`, else `Unknown`), `size` (bytes of files and symlink targets),
  `arch`, `origin` (the main name), `maintainer` and `license` (the main's,
  when set), then `depend`: the `[package.apk] depends` items and
  `libcap-setcap` with a `caps` entry, each once, and `datahash`, the SHA-256
  of the data member (apk-tools 3 refuses a package without it). Scripts
  (`#!/bin/sh`, ending `exit 0`, mode 755) are written only with a command,
  each under both names: `.pre-install`/`.pre-upgrade` create the accounts the
  configuration creates (`addgroup -S` per created group, then per created user
  `addgroup -S`, `adduser -S -D -H -h HOME -s SHELL -G NAME [-g GECOS] NAME`
  with home `/` and shell `/sbin/nologin` by default, and `addgroup NAME GROUP`
  per supplementary group, each ending `2>/dev/null || :`) when the package
  ships `/usr/lib/sysusers.d/NAME.conf` or holds an entry such an account owns,
  since names resolve while the package is extracted;
  `.post-install`/`.post-upgrade` create each ghost directory (`mkdir -p`,
  `chown OWNER:GROUP`, `chmod MODE` when the mode is set; Alpine has no
  tmpfiles) then run `setcap CAPS PATH`, so `apk audit` reports `x` (an
  attribute the database lacks) for a path with a capability. Units are not
  enabled.
- **`ArchFormat`** — `NAME = "arch"`, `COMPRESSIONS = ("zst", "xz", "gz")`,
  `REQUIRED = ("version", "summary")`, `SUB_REQUIRED = ("summary",)`,
  `ALL_ARCH = "any"`, `MACHINES` mapping `x86_64`/`amd64` to `x86_64`,
  `aarch64`/`arm64` to `aarch64`, `armv7l` to `armv7h`, `i686`/`i386` to `i686`
  and `riscv64`. One `NAME-VERSION-PKGREL-ARCH.pkg.tar.{zst,xz,gz}` per
  package, written without makepkg or fakeroot; PKGREL is `[package] release`
  or `1` and must read `N` or `N.M` (`UsageError`). GNU tar with `.PKGINFO`,
  `.MTREE` and, only when it has a command, `.INSTALL`, then the payload with
  no `./` and no root entry. `.PKGINFO` (`key = value` lines): `pkgname`,
  `pkgbase` (the main name), `xdata = pkgtype=pkg` (`split` for a subpackage),
  `pkgver` (`VERSION-PKGREL`), `pkgdesc` (the summary), `url` (the main's
  `homepage`), `builddate`, `packager` (the main's `maintainer`, else `Unknown
  Packager`), `size` (bytes of files and symlink targets), `arch`, `license`
  (the main's), then `depend`: the `[package.pacman] depends` items, `systemd`
  when `.INSTALL` creates accounts or runs tmpfiles, `libcap` with a `caps`
  entry, each once. `.MTREE` is gzip (no time) of `#mtree` and one line per
  other member (`./PATH time=E.0 mode=M uid=0 gid=0 type=file|dir|link`, plus
  `size`, `md5digest`, `sha256digest` or `link=`; bytes outside `!`-`~`, and
  `\` and `#`, are written `\ooo`). pacman ignores the owner names of a tar, so
  every header is `root:root` with uid and gid 0 and `.INSTALL` applies the
  rest in `post_install` (`post_upgrade` calls it): `systemd-sysusers -` over a
  heredoc of the `sysusers.d` text when the package ships
  `/usr/lib/sysusers.d/NAME.conf` or holds an entry owned by an account the
  configuration creates, `systemd-tmpfiles --create NAME.conf` when it ships
  that file, `chown -h -- OWNER:GROUP PATH` (`chgrp -h -- GROUP PATH` when only
  the group is not root) per entry, `chmod` again for a setuid or setgid mode
  (chown drops it), then `setcap CAPS PATH`. `pacman -Qkk` therefore reports
  `UID mismatch`/`GID mismatch` for the paths `.INSTALL` re-owns. Units are not
  enabled (Arch packages leave that to the administrator).
- **`DebFormat`** — `NAME = "deb"`, `COMPRESSIONS = ("xz", "gz", "zst",
  "none")`, `REQUIRED = ("version", "summary", "maintainer")`, `SUB_REQUIRED =
  ("summary",)`, `ALL_ARCH = "all"`, `MACHINES` mapping `x86_64`/`amd64` to
  `amd64`, `aarch64`/`arm64` to `arm64`, `armv7l` to `armhf`, `i686`/`i386` to
  `i386`, `riscv64`, `ppc64le` to `ppc64el` and `s390x`. One
  `NAME_VERSION[-RELEASE]_ARCH.deb` per package, written without `dpkg-deb`,
  debhelper or fakeroot: an ar archive of `debian-binary` (`2.0`),
  `control.tar` and `data.tar` (GNU tar with a `./` root, compressed as
  `--compression`, default `xz`; dpkg refuses pax headers); a member longer
  than the ar header's 9999999999 bytes is `DumpError`, as dpkg-deb refuses it.
  Headers carry the DB's names, except an entry owned by an account the package
  creates at install time (outside the base-passwd set), which is `root:root`
  with its recorded mode and `postinst` applies it with `dpkg-statoverride`, as
  the debhelper recipe does. `control` holds `Package`, `Source` (a
  subpackage), `Version` (`VERSION-RELEASE`), `Architecture`, `Maintainer`,
  `Installed-Size` (`ceil(size / 1024)` per file and symlink, 1 per directory
  and for the root), `Depends`, `Section` and `Priority` (the package's
  `[package.debian]`, else the main's, else `misc`/`optional`), `Homepage` (the
  main's) and the description. `Depends` is `systemd |
  systemd-standalone-sysusers | systemd-sysusers` when the package ships
  `/usr/lib/sysusers.d/NAME.conf`, `libcap2-bin` with a `caps` entry, then the
  `[package.debian] depends` items with `${binary:Version}` and
  `${source:Version}` replaced by the full version (the cfg spells each
  `$${...}`, since `${` there is a reference); any other `${...}` is
  `UsageError`. `conffiles` lists the regular files below `/etc` and the config
  kinds elsewhere; `md5sums` every other regular file. `postinst` (under
  `configure`: `systemd-sysusers`, `systemd-tmpfiles --create` when the package
  ships `NAME.conf` for them, `deb-systemd-helper` and `deb-systemd-invoke` for
  each `*.service`/`*.socket`/`*.timer`/`*.path` directly in
  `/usr/lib/systemd/system`, the `dpkg-statoverride` lines, then `setcap`),
  `prerm` (stops the units on `remove`) and `postrm` (`daemon-reload` on
  `remove`; on `purge`, `deb-systemd-helper purge` and the statoverride
  removals) are written only with a command in them. A path with a control
  character is `DumpError`, as is a conffile name with whitespace outside
  `/etc`.
- **`RpmFormat`** — `NAME = "rpm"`, `COMPRESSIONS = ()` (rpm owns its payload,
  so `--compression` is `UsageError`), `REQUIRED = ("version", "summary",
  "license")`, `SUB_REQUIRED = ("summary",)`. One
  `NAME-VERSION-RELEASE.ARCH.rpm` per package, built by `rpmbuild -bb` (no
  `rpmbuild` on `PATH` is `PkgForgeError`, exit 1; a failed build is one with
  rpmbuild's last stderr line). It writes, in a temporary directory, a GNU tar
  of the recorded non-ghost entries (no implicit parent directories: the
  `%files` lists decide what rpm owns), the lists of `dbdump -f rpmspecfiles`
  and the spec of `dbdump -f rpmspec`, with `%install` unpacking the tar into
  `%{buildroot}` and copying the lists into place instead of running `pkgforge
  stage`, and no `pkgforge_config` guard. The variant is read from `rpmbuild
  --version`: below 4.19 the `-pre419` lists and the `%pre` that creates the
  accounts. `--arch` is passed as `--target`; `ALL_ARCH` and `MACHINES` are
  unused, since the spec's `BuildArch` and rpmbuild decide. The packages are
  moved into the output directory (a file of the same name is replaced), main
  package first, and the temporary directory is removed whether the build
  succeeded or not. When a package records a unit and `rpmbuild --eval
  '%{?systemd_post:1}'` is not `1`, a WARNING on `pkgforge.package` says the
  unit scriptlets are left out (install systemd-rpm-macros). The build is not
  byte-reproducible: rpmbuild stamps its own times.
- **`TarFormat`** — `NAME = "tar"`, `COMPRESSIONS = ("gz", "xz", "zst",
  "none")`, `REQUIRED = ("version",)`. One `NAME-VERSION[-RELEASE].tar[.gz|.xz|
  .zst]` per package (the main package's version and release, the package's own
  name). GNU tar, names without `./` and no entry for the root, so extracting
  over `/` touches nothing it does not ship; ghost entries are left out. A file
  capability cannot be stored: each caps entry logs a WARNING (`PATH: a tar
  carries no file capability; run setcap CAPS PATH after extracting`).

Every format shares one payload writer. A path ships when its entry is live and
not a `ghost`; it is read from the build root (`os.lstat`, never followed), and a
recorded path that is missing, or of another type than recorded, is a
`UsageError` naming the path and `pkgforge verify`. Headers carry the DB's user
and group names (`-` is `root`; a name over 32 UTF-8 bytes is `DumpError`, since
a GNU header would cut it) and uid and gid 0. The mode is the entry's; `-` is
the staged file's permission bits, and a symlink is `0777`. The time is the
staged mtime, at most `SOURCE_DATE_EPOCH` when it is set; with it set, every
build-time stamp is that value too and gzip headers carry no time, so two builds
of one build root are byte-identical (for one Python and one zstd library).
Every ancestor directory a package does not record is added as `755 root:root`
(not for `rpm`). Members are sorted by DB path.

Compression: `gz` is gzip level 9, `xz` is preset 6 with a CRC64 check, `zst` is
zstd level 19, `none` is the bare tar. zstd is `compression.zstd` on Python 3.14,
else the `zstandard` package of the optional `zstd` extra (`pip install
'pkgforge[zstd]'`); with neither, `UsageError` naming the extra and
`--compression xz`.

## Contract suite (`pkgforge.testing`)

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
- A format must: render `bytes` (`PerEntryFormat`, `RPMSpec`) or a `{name:
  bytes}` dict of non-empty values (`MultiArtifactFormat`) for the sample
  entries, which ship in the one package `contract` (the context names it, with
  a version, summary, license and maintainer); render the same entries to the
  same output twice; keep that type for no entries; refuse the wrong output
  shape with `UnsupportedOutputError` and accept the right one in
  `check_output` (a `PerEntryFormat` accepts a file, a directory and an absent
  path; a format that renders bytes accepts a file and an absent path); and
  `write` returns the paths it wrote with bytes equal to `render`.

```python
from pkgforge.testing import check_db_provider

def test_my_backend(tmp_path):
    check_db_provider(MyDb, tmp_path)
```

## Exceptions

All in `pkgforge.exceptions`, and exported by `pkgforge`. The command line maps
them to exit statuses ([Exit status](#exit-status)); a library call raises them.

- **`PkgForgeError(Exception)`** — base class for pkgforge's own runtime
  failures; exit 1.
- **`UsageError(PkgForgeError, ValueError)`** — an argument-shaped mistake (a
  bad or missing value); exit 2. Also a `ValueError`, since it reports
  malformed input. `install` raises it for a missing source, a `-` (stdin)
  source without `-T`/`-D`, a symlink source or type with no target (no `-O
  target=PATH`), an unresolvable stdin compression kind, a `--decompress` value
  that looks like a path, and an unknown `--chown` owner or group.
- **`DbError(PkgForgeError, ValueError)`** — a backend's on-disk content could
  not be read as one, or a value it was given could not be stored: a JSON or
  YAML parse error, non-UTF-8 bytes, a record that breaks the
  [record schema](#file-db-pkgforgedb), a nesting too deep to parse, a `.db`
  file that holds tables but no `entries` table, or a `sqlite` value that is not
  valid UTF-8 (an undecodable file name, or a `pwd`/`grp` entry containing one).
  The message names the DB file (and, for `jsonl`, the line); exit 1.
- **`ExcludeSyntaxError(UsageError)`** — a malformed `-X` statement: an unknown
  inline test name, an invalid `(?type:...)`/`(?meta:...)` argument, or an
  unterminated `(?...`; exit 2, reported while the arguments are parsed.
- **`DumpError(PkgForgeError, ValueError)`** — a DB path that a dump or package
  format's tooling cannot represent (a `%` or a control character for
  `rpmspecfiles`; a control character for `debian`; see each format); exit 1.
  Every entry is rendered before OUTPUT is opened, so raising this leaves no
  partial file.
- **`UnsupportedOutputError(UsageError, DumpError, NotImplementedError)`** —
  OUTPUT is the wrong shape for the chosen format (an existing file for
  `debian`); raised by `check_output`; exit 2.
- **`InvalidEntryError(UsageError, DumpError)`** — a DB entry that no format can
  render safely, found by `dbdump.dump` and `package.build` before any format
  renders (exit 2, naming the first offending entry in entry order and the
  field). The rules: the key is an absolute path with no empty, `.` or `..`
  segment and no control character; `type` is `file`, `directory` or
  `symlink`; `mode` is 1-4 octal digits, `-` or `--`; `owner`/`group` are an
  account name (`[A-Za-z_][A-Za-z0-9_.-]*`, optionally ending in `$`), `-` or
  `--`; `kind` is one of the six `FileKind` values and fits `type` (all but
  `file` and `ghost` need `file`); `caps` is `-` or one lowercase `setcap`
  clause on a regular file that is not a ghost; `package` is `-` or a package
  name (`[a-z0-9][a-z0-9+.-]+`). `%`, whitespace and control characters
  therefore cannot occur in mode/owner/group/caps/package.

## Command line

`pkgforge.cli` holds the root command, `main` and one module per subcommand,
each a thin call into one `Project` method. Use it through the `pkgforge`
console script or `python -m pkgforge`.

- **`pkgforge.cli.main(argv: Sequence[str] | None = None) -> int`** — run the
  command line: build the parser and dispatch the selected subcommand. `argv`
  is the arguments after the program name (`sys.argv[1:]` when `None`).
  Returns the exit status; `--help`, `--version` and an error in the arguments
  themselves raise `SystemExit`. Bound as the `pkgforge` console script and
  `python -m pkgforge`. It is the only place an exception becomes an exit
  status.

Global options: `--config/-c FILE` (the project file; before the subcommand
only, see [Configuration](#configuration-pkgforgeconfig)), `--db PATH` (env
`PKGFORGE_DB`; `-` or unset writes records to stdout, and `dbdump` reads them
from stdin), `--db-format FMT` (env `PKGFORGE_DB_FORMAT`; a built-in or
registered `DbProvider` `NAME`, else exit 2), `--buildroot/-r DIR` (env
`PKGFORGE_ROOT`, else `.`), `--own PREFIX` (repeatable; a `/`-rooted directory
the package owns, else exit 2; replaces the file's `own` list), `--debug`
(print the traceback of an error before its one-line message),
`-v`/`-q`/`--loglevel`, `--version` and `--print-completion
{bash,zsh,fish,powershell}`. All but `--config` work before or after the
subcommand. Precedence is the command line, then the environment, then
`pkgforge.cfg`, then the default; an empty environment value counts as unset.
`--help` of every subcommand lists them too. With no file DB, `install`,
`scan`, `mkdir`, `symlink`, `stage` and `sysfiles` print each record as one
JSON Lines line (`pkgforge.db.jsonl_line`) on stdout once the operation has
succeeded.

Each subcommand logs under `pkgforge.<command>`: control one with `--loglevel
pkgforge.<command>:LEVEL`, or the running command with `--loglevel LEVEL`. A
bare `--loglevel pkgforge:LEVEL` has no effect on a dispatched command, whose
own logger always has an explicit level (INFO by default).

With `PKGFORGE_MCP=stdio` the command tree is served as MCP tools
(`pkgforge.install`, ...) instead of running a command; standard input is the
protocol channel, so a `-` source of `install` and `dbdump --db -` are refused
there with a `UsageError`. See [Environment variables](#environment-variables).

### Exit status

| Status | Meaning |
| --- | --- |
| `0` | success |
| `1` | a runtime failure (`PkgForgeError`, `OSError`), reported as one `pkgforge: error: ...` line on stderr; a closed output pipe also returns `1`, silently |
| `2` | a usage mistake (`UsageError`, a bad configuration file included), reported as one `pkgforge: error: ...` line |
| `130` | Ctrl-C, silently (`KeyboardInterrupt` propagates under `PKGFORGE_TRACEBACK`) |

`pkgforge verify` also returns `1` when it found a difference, printing the
issues on stdout and no `pkgforge: error:` line. A tail after `--` is a usage mistake (`2`). The table is in the agent help
(`AGENT_HELP=1 pkgforge --help`) and in the result of a failed MCP tool call.
Any other exception propagates
with its traceback. Pass `--debug`, or set `PKGFORGE_TRACEBACK` to a true value
(`1`/`true`/`yes`/`on`; `0`/`false`/`no`/`off`/`n`/`f` are off), to also print
the traceback before the error line. External tools (`bsdtar`, the
decompressors, `rpmbuild`) and `sqlite3` failures are reported the same way,
with the tool's last stderr line.

### `pkgforge install`

`pkgforge install [opts] SOURCE... DESTINATION` maps onto `Project.install`:
stage a source (file, directory, symlink, tar-family archive, or a
decompress-on-copy) into the build root, apply its mode and ownership, and
record the entry. `-k/--kind` is one of `file`, `config`, `config-noreplace`,
`doc`, `license`, `ghost`; `--caps` one `setcap` clause; `-P/--package` the
`[package]` name or a `[[packages]]` name (also on `mkdir` and `symlink`). `-D`
is `-Tp`, `-d` is `--type directory`, and `--type=--` (or `-t --`) detects the
type from the source, as leaving `-t` unset does; `-d` with `-t`/`--type` is
exit 2. `--mode`, `--owner`, `--group`, `--type` and `--dir-mode` take the next
word as their value even when it is `--`.

- **Archives** — without `-T`, an extracted archive lands at
  `DESTINATION/<name minus its archive suffix>` (`.tar`, `.tar.gz`/`.tgz`,
  `.tar.bz2`/`.tbz2`/`.tbz`, `.tar.xz`/`.txz`, `.iso`, `.tar.zst`/`.tzst`,
  `.tar.lz4`, `.tar.lzma`/`.tlz`, `.tar.lz`, `.cpio`, `.zip`, matched
  case-insensitively, longest suffix first; any other name is unchanged); a
  directory source keeps its own name (`conf.tar.d` stays `conf.tar.d`).
  Extraction (the standard library's `tarfile` for a tar-family path, `bsdtar`
  for stdin, other formats and an interpreter without the PEP 706 extraction
  filter) never restores an archive's ownership, setuid/setgid bit,
  group/other write bit, extended attributes, ACLs or file flags, even as root;
  a device node, FIFO or socket in the archive is refused with `PkgForgeError`
  naming it, and nothing from that extraction is left on disk. `bsdtar` runs
  with `--no-same-owner --no-same-permissions --no-xattrs --no-acls
  --no-fflags` (libarchive 3.3+). With `tarfile`, a symlink member's target is
  kept exactly as stored, but a write through any symlink between the
  destination and a member's own parent is refused, and a hardlink member's
  target must stay inside the destination. Re-extracting the same archive over
  an existing tree replaces a stale entry at each member's path. PEP 706 is in
  Python 3.9.17+, 3.10.12+, 3.11.4+ and 3.12+; without it, and without
  `bsdtar`, a tar-family source is refused (exit 1) rather than extracted
  unfiltered.
- **`-x`/`--decompress [KIND]`** — `gz`, `xz`, `bz2`, `zst` or `lzma`, in any
  case (a tool name such as `unxz` is not a kind); another kind, or a bare `-x`
  whose source suffix matches none of them, is `UsageError`, and a kind whose
  tool is not on `PATH` is `PkgForgeError`, both before anything is staged.
  KIND is optional and consumes the next token: write `-x KIND SRC DST`,
  `--decompress=KIND`, or `-x` after the paths.
- **Standard input** — a `-` source reads to EOF and never closes the stream; an
  empty stdin (`/dev/null`, an empty pipe) stages an empty file, a terminal or
  a closed stdin is `UsageError` before anything is touched, and only one `-`
  source is allowed. A FIFO, `/dev/stdin` or a process-substitution path
  (`<(cmd)`) is detected as `file` and streamed, not staged as a symlink to its
  pipe; a terminal device path is refused like a terminal stdin, and a socket
  or block-device path is `UsageError`. An explicit `--type symlink` always
  copies the link text.
- **Several sources** — every clone is resolved (type, decompress kind,
  destination) before any is staged, so two sources resolving to the same
  non-directory destination are `UsageError` with nothing on disk; directory and
  archive sources sharing a destination merge, and a repeated source is a
  no-op. A symlink source's `meta["target"]` never reaches a later entry.
- **Failures** — a failed install leaves `DESTINATION` as it was: a file,
  stream or decompressed source is written to a sibling temporary next to it
  and `os.replace`d in only after the entry is applied, and the temporary is
  removed on any failure. A failed install removes the parent directories its
  own `-p` created (deepest first, exit 1); directories that already existed
  stay. `--mode`, the recorded `--owner`/`--group` names (account-name syntax
  only), `--kind` and `--caps` (a kind that does not fit the type, or caps on
  anything but a regular non-ghost file, is exit 2), `-O` (the key `rpmprefix`
  is exit 2: use `-k`/`--caps`), `--chown`'s names (only with `--chown`) and the
  `--db` directory are checked before anything is staged; without `-p`, a
  missing destination parent is `UsageError` the same way.
- **Re-running and merging** — a re-run replaces a staged file or symlink
  (never a real directory, which is `UsageError`). A directory copy never
  descends into its own resolved destination, the build root or the file DB
  when they sit strictly inside the source directory (their parents are still
  created, possibly empty); a source that equals or sits inside the destination
  merges. A directory or archive merge replaces a stale destination symlink at
  any name and never writes a regular file's content through one; a real
  destination directory is left alone (a source symlink colliding with one
  still raises).
- **`--remove-source`** — runs only after the entry is applied and recorded;
  skipped with a warning when the source is the destination (a `--method link`
  source is removed like any other); refused with `UsageError`, before any
  staging, when a directory source contains the resolved destination or the
  `--db` file. With `--method move` it is a no-op.
- **`--method {copy,link,move}`** (env `PKGFORGE_INSTALL_METHOD`; default
  `copy`) — how a file or directory source is staged; a `-` source,
  `-x`/`--decompress`, an archive and a symlink source or type ignore it.
  `copy` never touches the source: it copies content, permission bits and
  modification time (never BSD file flags or extended attributes such as an
  SELinux label). `link` hardlinks instead, so the staged copy shares the
  source's inode and `-m`/`-o`/`-g`/`--chown` change the source too; a file
  `os.link` cannot span (`EXDEV`, `EPERM`, `EMLINK`, `EACCES`: another
  filesystem, `fs.protected_hardlinks`, the link limit, permissions) is copied,
  with one WARNING per source. A directory is hardlinked file by file, and a
  symlink in it is recreated, never hardlinked. `move` consumes the source: a
  file is renamed (or moved across filesystems) into a sibling temporary; a
  directory whose destination does not exist yet and has no `-X` is renamed in
  one step, otherwise it moves file by file and removes the source directories
  left empty (an excluded file, its parent and any symlink stay in the source).
  If applying, replacing or recording then fails, a file move or a whole-tree
  rename is moved back onto the source before the error propagates (best
  effort, logged, never raised); a merge is not rolled back, as a partial copy
  merge is not. A directory move onto another filesystem takes the merge path.
- **`-X`** — relative to each source (the source directory, or an archive's
  extraction directory): `/x` means `SRC/x`, never the destination path. Its
  `(?meta:k=v)` test sees this run's `-O` values. It filters a directory copy
  and an archive's extracted members alike, before anything reaches the
  destination; the extraction rules still apply to every member first, so a
  special or escaping member is refused even when `-X` matches it. A FIFO or
  socket the copy would reach is `PkgForgeError` naming the path and the `-X`
  remedy, unless a glob-only `-X` excluded it. With only file or symlink
  sources it logs a warning, since there is nothing to filter.
- **`--record-tree`** (env `PKGFORGE_INSTALL_RECORD_TREE`; off by default) — also
  records, right after DESTINATION's own entry, every path below a directory or
  archive DESTINATION the DB does not already hold: `owner`/`group`/`meta` from
  this install's `-o`/`-g`/`-O` (`--` resolves per child), `type` from disk,
  and `mode` from a matching `[[rules]]` element or `[defaults]`, else from
  disk; DESTINATION's own `-m` is never inherited, and a symlink's mode is
  always `-`. Ignored for a file or symlink install and a no-op under
  `--noentry`. It only fills gaps: an entry another install already recorded
  (e.g. a setuid binary installed on its own first) is left alone, and a child
  two merging sources both produced is written once. `-X` applies relative to
  DESTINATION, which holds the same tree, and prunes an excluded directory,
  including a leftover from an earlier run. A `--record-tree` failure (e.g. an
  unsupported file type under DESTINATION) exits 1 with the tree staged and the
  top entry recorded. For a tree staged by something else, use `scan
  --missing`.

### `pkgforge scan`

`pkgforge scan [opts] PATH` maps onto `Project.scan`: record an entry for every
directory and file **below** PATH (never PATH itself); `--missing` only fills
gaps, else scan replaces an existing entry (e.g. one `install` recorded).
`-o`/`-g` are recorded on every entry as given: `-` (default) leaves the field
unset, `--` reads the on-disk name. `-m`/`--mode` applies to files only;
`--dir-mode` to directories (the same grammar, default `--` when `--mode` is
`--`, else `-`); a symlink's mode is always `-` (Linux ignores it, and rpm and
debian tools warn about or misreport one). `scan` has no `--type` (it records
each path's own type; `-t` is exit 2).

- `-X` is relative to PATH, for the walk and for `--drop-stale` alike; its
  `(?meta:k=v)` test sees this run's `-O` values; an excluded directory's
  subtree is pruned. A FIFO or socket `scan` cannot record is `PkgForgeError`
  naming the path and the `-X` remedy, unless a glob-only `-X` excluded it.
- Every directory scan records becomes an RPM `%dir` ownership claim in
  `rpmspecfiles`: never scan a directory a distribution package owns (`/usr`,
  `/usr/bin`, `/usr/share`, `/etc`); narrow PATH to one your package owns.
- A PATH that does not exist under the build root is exit 2 before anything
  runs. A symlink PATH is recorded as one `symlink` entry, never followed (a
  dangling one included), except when PATH is the build root itself (`/`),
  which is always walked.
- `scan` never records its own `--db` file (or, for `sqlite`, its
  `-journal`/`-wal`/`-shm` sidecars) found inside the tree: one WARNING, DEBUG
  for further matches. It cannot recognize a `dbdump` OUTPUT written there, so
  keep both outside `--buildroot`.
- `--drop-stale` reloads the DB after the walk and removes every live key
  strictly below PATH whose file is missing, skipping a key that matches
  `-X` (so a deliberately absent entry, e.g. an RPM `%ghost`, can be kept); it
  never touches disk, and is `UsageError` when `--db` is unset or `-`. A removed
  path counts as absent for a later `--missing`.

### `pkgforge dbdump`

`pkgforge dbdump -f FORMAT [OUTPUT]` maps onto `Project.dump`: render the live,
post-`--exclude` DB entries with a format from the registry. `-f` takes a `NAME`
or an alias (`rpm` for `rpmspecfiles`, `deb` for `debian`); an alias and its
name give identical output. The packages come from `[package]` and
`[[packages]]`: `debian` needs `[package] name` and writes `P.*` files per
package; a per-entry format writes one file (or `-`) for one package, or
`files-PACKAGE.txt` for each when OUTPUT is an existing directory (also needing
`[package] name`); an entry of an undeclared package, or of two packages into
one file, is exit 2.

- `--dirs {all,owned}` (default `all`) keeps, with `owned`, only the directory
  entries at or below an owned prefix (exit 2 with none set), also with
  `--db -`.
- `--skeleton` also writes the files a format's user edits (`debian`:
  `control`, `changelog`, `rules`, `source/format`); `--force` replaces an
  existing one, and an existing `rpmspec` file. Exit 2 for `--force` with
  neither, for `--skeleton` with a format that has none or without a directory
  OUTPUT, for a skeleton when no config file was found, and for a `${NAME}` in
  the `summary`, `description`, `homepage` or `maintainer` of a package.
- A DB must be given: with `--db` unset, `UsageError("no file DB configured;
  pass --db or set PKGFORGE_DB")`, and with a path that does not exist,
  `UsageError("no file DB at {path}; run pkgforge initdb")` (both exit 2). An
  existing empty DB logs a WARNING and dumps an empty manifest, exit 0, as does
  a DB whose entries all fall to `--exclude`.
- `--format` and OUTPUT are checked before the DB is read (an unknown format or
  a wrong-shape OUTPUT is exit 2), through a runtime registry lookup, so a
  format registered after import works.
- **`--db -`** reads the DB as JSON Lines from standard input (what `install`
  and `scan` write with `--db -`, so `install --db - ... | dbdump --db - -f rpm`
  works; `--db-format` must name a registered format but is not used). It runs
  after `--format`, OUTPUT and `--db-format` are checked, so a bad one exits 2
  without touching stdin. Stdin must be a pipe, socket or regular file: a closed
  stdin, a terminal or a character device such as `/dev/null` is `UsageError`,
  so an inherited `PKGFORGE_DB=-` never dumps an empty manifest; under MCP it
  is `UsageError` too. Otherwise it reads to EOF (blocking on a pipe that is
  never closed); empty input logs a WARNING and dumps an empty manifest, exit 0.
  A leading BOM is ignored; errors read `<stdin>:{line}: ...` (`DbError`), as
  `pkgforge.db.parse_jsonl` reports them.

### `pkgforge stage`, `sysfiles`, `mkdir`, `symlink`

- **`pkgforge stage [--init]`** — `Project.stage`: run the `[[stage]]` steps
  of `pkgforge.cfg` (`pkgforge --config FILE stage` names another). `--init`
  resets the file DB first, as `initdb` does. Exit 2 for no steps or a missing
  source, otherwise as `install`.
- **`pkgforge sysfiles`** — `Project.sysfiles`: write the sysusers.d and
  tmpfiles.d files (no options). Exit 2 as for the method. `stage` already
  runs it.
- **`pkgforge mkdir [-m MODE] [-o OWNER] [-g GROUP] [-k KIND] [-O KEY=VALUE]
  [-p] DEST...`** — `Project.mkdir`: record each `DEST` as a directory and
  create it under the build root. `-p/--parents` creates the missing parents;
  without it a missing parent is exit 2.
- **`pkgforge symlink [-o OWNER] [-g GROUP] [-k KIND] [-O KEY=VALUE] [-p]
  TARGET LINK`** — `Project.symlink`: record `LINK` as a symbolic link to
  `TARGET`. There is no `--mode`. Exit codes as for `mkdir`.

### `pkgforge initdb`, `compact`

- **`pkgforge initdb`** — create or truncate an empty DB; with an unset or
  stdout DB, a no-op with a WARNING. A `--db` naming a file that does not exist
  yet is created.
- **`pkgforge compact`** — collapse an append-log DB to one record per live
  path, dropping removals and superseded history; for `sqlite`, delete removal
  rows and `VACUUM`. `UsageError` (exit 2) for an unset `--db`, for `--db -`
  and for a file that does not exist (nothing is created).

### `pkgforge verify`

`pkgforge verify [-X STMT]... [-f {text,json}]` — `Project.verify`: check the
build root against the file DB. Exit `0` with no issue and no output, `1` with
any, printing one line per issue sorted by path, then problem: `PROBLEM PATH`,
or `PROBLEM PATH: recorded R, found F` for `type` and `target`. `-f json`
prints one document `{"issues": [{"path", "problem", "recorded", "found"}]}`
and a newline instead. `-X` takes DB paths, as in `dbdump`. Exit `2` for no
file DB (`--db` unset or `-`), a missing DB file or a missing build root.

### `pkgforge package`

`pkgforge package -f FORMAT [-o DIR] [--arch NAME] [--compression KIND]` —
`Project.package`: build one package per declared package from the file DB and
the build root. `-f` is `apk`, `arch`, `deb`, `rpm` or `tar`; `-o` defaults to
`.` and is created when missing. Prints each path written on stdout, one per
line, the main package first. `--arch` names the architecture of an `any`
package in the format's own spelling (default: this machine's);
`--compression` is one the format offers. Exit `2` for `--db -`, no file DB, a
missing DB file or build root, an unknown format, a bad `--arch` or
`--compression`, a missing `[package]` key, a missing `zstd` library, or a
recorded path missing from the build root; `1` for a file problem or an entry
the format cannot represent.

## Environment variables

The `PKGFORGE_*` variables are read when `pkgforge.cli.main()` runs (the
command line wins over the environment, which wins over the default); `Project`
never reads them, and neither does a command class built directly in Python. An
empty value counts as unset.

- **`PKGFORGE_CONFIG`** — the project file, like `--config` (which wins over
  it); a missing file is exit 2. An empty value means no file, the
  `pkgforge.cfg` in the working directory included.
- **`PKGFORGE_ROOT`** — default `--buildroot`.
- **`PKGFORGE_DB`** — default `--db`.
- **`PKGFORGE_DB_FORMAT`** — default `--db-format`.
- **`PKGFORGE_INSTALL_METHOD`** — default `install`'s `--method` (`copy`,
  `link` or `move`).
- **`PKGFORGE_INSTALL_RECORD_TREE`** — default `install`'s `--record-tree` (a
  boolean; `1 true yes on y t`, in any case, is on; anything else is off).
  `--record-tree`/`--no-record-tree` win over it.
- **`PKGFORGE_TRACEBACK`** — a true value (`1 true yes on y t`, in any case)
  prints the traceback before the `pkgforge: error:` line. See
  [Exit status](#exit-status).
- **`PKGFORGE_MCP`** — set to `stdio` to serve the command tree as MCP tools
  over stdio instead of running a command (any other value exits 2). It is
  removed from the environment as soon as it is read, so staged commands'
  children never inherit it.
- **`SOURCE_DATE_EPOCH`** — read by the library too, whenever a package is
  built (`Project.package`, `pkgforge.package.build`) or the debian skeleton's
  `changelog` is written: an integer number of seconds that clamps mtimes and
  fixes every build-time stamp; anything else is `UsageError`.

## Gotchas

- `mode` is always an octal **permission string** (`"644"`), never a raw
  `st_mode` int; `apply_entry` converts it with `int(mode, 8)`.
- The append-log backends (`jsonl`, `yaml`) never truncate on write; `load()`
  keeps only the last record per path. Call `compact()` (or `pkgforge compact`)
  to reclaim space and drop history. `sqlite` has no log to compact beyond
  dropping removed rows.
- File sources are copied by default (`--method copy`), never linked:
  `-m`/`--chown` apply to the staged copy only, and the source keeps its
  content, mode and ownership. `--method link`/`move` share or consume the
  source instead.
- `chown` (owner/group) needs the Unix `pwd`/`grp` modules; off POSIX they are
  missing, so `apply_entry(..., chown=True)` raises `RuntimeError` there.
  Building the parser and `--help` work everywhere.
- A tree or archive install records **one** entry, for the destination itself,
  unless `--record-tree` is set. For a tree staged by something else, `scan
  --missing --mode=-- -o OWNER -g GROUP <dest>` records it from disk. Skip both
  and `rpmspecfiles` gets only a `%dir` line and `debian`'s `install` artifact
  gets no files for the tree.
- A `PathMatchStmt` or `PathMatch` result of `None` is not "no match": it means
  "keep evaluating", and `PathMatch.match` returns it when no statement
  applies.
- A command class built directly in Python (`InitDb()`, not through
  `pkgforge.cli.main`) keeps its literal defaults and ignores the environment.
