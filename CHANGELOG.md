# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- A `-` source on a terminal or closed stdin, or a terminal character-device
  path, exits 2 instead of staging an empty file or waiting; `install` keeps
  stdin open instead of closing fd 0, and only one `-` source is allowed per
  invocation. `<(cmd)`, `/dev/stdin` and named pipes now stage as files
  (previously a dangling symlink to the pipe, or a crash for a plain FIFO).
- `-x`/`--decompress` takes `gz`, `xz`, `bz2`, `zst`, `lzma` or a decompressor
  tool name (`gzip`/`gunzip`, `xz`/`unxz`, `bzip2`/`bunzip2`, `zstd`/`unzstd`,
  `lzma`/`unlzma`), matched case-insensitively; any other kind, or a bare
  `-x` on a source suffix that names none of them, exits 2 before anything
  is staged, instead of silently compressing the file, leaving the old
  suffix on the destination, or running an arbitrary word on `PATH` as a
  command. `-x gzip` now decompresses (it used to compress); a resolved
  kind whose tool is missing from `PATH` is named in one error line.
- `Install(...)` built directly from Python (not through the CLI parser) and
  given no `decompress=` argument now stages the source unchanged, instead of
  silently inferring a decompressor from its suffix and running it as a
  subprocess (which, for a `.sh` source, executed it).
- `install` copies file sources instead of hardlinking them: `-m`/`--chown`
  no longer change the source, and a build root on another filesystem (e.g.
  a tmpfs `/tmp`) or a source the caller doesn't own no longer fails.
  `-o --`/`-g --` (AUTO) now record the staged copy's owner, not the
  source's.
- `--help` describes every option, names the `PKGFORGE_*` variables and the
  DB and dump formats, and no longer shows developer notes.
- An unknown `--db-format` or `PKGFORGE_DB_FORMAT` exits 2 with one line
  naming the valid formats, before anything is staged (was a traceback and
  exit 1, after `install` had staged the file), also when `--db` is unset
  (was ignored). Constructing a command with an unknown `db_format` raises
  `UsageError` (a `ValueError`) immediately.
- Usage, errors, `--version` and completion name the command `pkgforge` (was
  `PkgForge`), so completion binds. Regenerate installed ones.

### Changed
- `scan` logs one INFO summary; per-path `Updating file entry for:` lines need
  `-v`.
- Commands log as `pkgforge.<command>` (was `<command>`, e.g. `scan`). Use the
  new name in `--loglevel`, e.g. `--loglevel pkgforge.scan:WARNING`; in
  Python, `logging.getLogger("pkgforge")` controls all of them.
- The documentation site is now rebuilt from `main` whenever the docs change,
  and from the release tag after each final release. A pre-release tag leaves
  it unchanged.
- Pre-release tags (`vX.Y.Z-rc.N`) now produce a GitHub pre-release only and
  are no longer uploaded to PyPI. Install a pre-release from the wheel
  attached to its GitHub release.
- The `dev` extra installs `black` on Python 3.10 and later.
- The sdist no longer includes `benchmarks/`.
- `pyyaml` is declared as `>=6.0,<7`; it was unbounded.
- Building from source requires `hatchling` 1.27 or later.
- The `docs` extra is bounded to mkdocs 1.x, mkdocs-material 9.x and mkdocstrings below 2.
- `twine` and `hatchling` are no longer in the `dev` extra; install them directly if you used them from it.
- Requires `duho>=0.6.0,<0.7` (was `>=0.5.0,<0.6`). `DUHO_TRACEBACK` now
  follows duho's boolean tokens: `n` and `f` turn it off (they used to turn it
  on). `--loglevel` accepts `[NAME:]LEVEL[,...]` and rejects a malformed value
  with exit 2; `-v`/`-q` gain `--verbose`/`--quiet`; log output is colored only
  on a terminal and never when `NO_COLOR` is set.
- `benchmarks/run.py` times the `scan` command end to end per DB backend
  (`scan.cmd_jsonl`, `scan.cmd_yaml`, `scan.cmd_sqlite`, `scan.cmd_auto_owner`).
  `scan.walk`, which timed only `os.walk`, is renamed `fs.walk_baseline`. The
  result schema is documented in `benchmarks/README.md`.

### Added
- Python 3.14 classifier.
- `PKGFORGE_MCP=stdio` serves pkgforge's commands as MCP tools over stdio
  (from duho 0.6). Unset, nothing changes.
- `entry_from_args`, `entry_from_path`, `resolve_entry`, `apply_entry`, typed
  forms of the `FileEntry` helpers (entries are dicts; `.resolve_for`/`.apply`
  on an entry never worked).
- `normalize_mode()`: normalizes a `mode` value (an `int`, an octal string, or
  the `-`/`--`/`auto` sentinels) to the octal permission string the file DB
  stores.
- `PkgForgeError`, `UsageError` (a `ValueError`). Errors now print one
  `pkgforge: error: ...` line and exit 2 for a usage mistake or 1 for a
  runtime failure, instead of a Python traceback (`DUHO_TRACEBACK=1` adds the
  traceback back); a closed output pipe now exits 1 silently. An unknown
  `--chown` owner or group now raises `UsageError` instead of a raw
  `KeyError`.

### Fixed
- The wheel and sdist never include files named `*.local.*` or `CLAUDE*`, even when built from a tree without `.gitignore`.
- `examples/stage_and_package.sh` is executable, so `./examples/stage_and_package.sh` runs in a fresh checkout.
- `install -m` on a symlink, and any `install -m` on glibc older than 2.32
  (e.g. RHEL/Rocky 8), no longer fails with `NotImplementedError`. A
  symlink's mode is recorded but never applied on disk (Linux ignores it).
- `--chown` no longer strips an existing setuid/setgid bit from the staged
  file: owner/group are now applied before mode, and a setuid/setgid/sticky
  bit already on disk is restored if the entry leaves mode at its default.
- `-m`/`--mode` (on both `install` and `scan`) is now validated before
  anything is staged: it accepts 1-4 octal digits (normalized, so `0644` is
  stored as `644`), `-`, `--` or `auto`, and rejects everything else,
  including an explicit empty value, with exit 2. Previously a Python-literal
  spelling like `0o644` was recorded verbatim and made rpm silently package
  the file with mode 000, and `scan -m` accepted any string at all with no
  check.
- An empty `mode`, `owner` or `group` is now treated like `-` (the documented
  default) everywhere it is read: applying the entry, and both the RPM and
  Debian dump formats. Previously the RPM format emitted `%attr(,-,-)`, which
  rpmbuild rejects, for an empty mode.
- `install -t`/`--type` now accepts `file`, `directory` or `symlink` in any
  case and rejects anything else with exit 2 instead of a Python traceback;
  `--type=--` (or the Python API's `FileType._AUTO`) now auto-detects from
  the source instead of raising `NotImplementedError`. `scan --type` is
  hidden from `--help` (`scan` always records each path's own on-disk type)
  and now warns instead of silently doing nothing when given a value.
- `PKGFORGE_ROOT`/`PKGFORGE_DB`/`PKGFORGE_DB_FORMAT` are now read when
  `pkgforge.main()` (or `duho.parse`) runs, instead of once when the package
  is imported. A build driver or test that sets one of these after importing
  `pkgforge` (e.g. via `monkeypatch.setenv`, or a long-lived process that
  changes its own environment) is now honored; a `--db`/`--buildroot`/
  `--db-format` on the command line still wins over the environment. A
  command constructed directly in Python, not through `main()`, is
  unaffected and keeps using the value from import time, as before.
- An empty `PKGFORGE_DB_FORMAT` (a common CI idiom for "unset") no longer
  makes every DB command fail with `unknown db format ''`; it is now treated
  as unset, same as an empty `PKGFORGE_DB`/`PKGFORGE_ROOT` already was.
- `PkgForgeCmd.localpath()` accepts a `str` as well as a `Path`, and a
  relative input is now taken as already build-relative instead of silently
  dropping its first path component.

## [0.1.2] - 2026-08-16

### Changed
- The `duho` dependency is now `>=0.5.0,<0.6`. 0.1.1 capped it at `<0.4` to
  stay installable; the commands are now declared against duho's current
  argument model instead, so the cap is gone and pkgforge tracks the
  supported duho line again.
- `install`'s `source` is declared as a `list[Path]` rather than a
  `Union[list[Path], Path]`. duho resolves a union by composing its members'
  scalar converters, so a collection member has no way to keep its own
  argparse action and the parser refused to build. The command still accepts
  a bare `Path` from the Python API; only the annotation narrowed.
- `--exclude` on `install`, `scan` and `dbdump` is declared with
  `duho.Append`, making it a repeatable single-value option.

### Fixed
- `--exclude` works through the command line again. It previously combined a
  greedy `nargs` with an append action, so `install --exclude=P` produced a
  list of lists (`AttributeError: 'list' object has no attribute 'pattern'`)
  and the bare `-X P SRC DST` form swallowed the positionals. Both forms now
  parse to a flat statement list; the multi-source regression test drives the
  option through argv rather than assigning it directly.

### Notes
- `-x/--decompress` still takes an optional argument and therefore still
  consumes the following token, so `install -x SRC DST` reads `SRC` as the
  compression kind. That is argparse's own behavior for this shape and the
  0.1.1 guard rejecting a path-shaped kind remains in place and necessary.
- The suite also passes against duho 0.4.x, but 0.5 is the series exercised
  and therefore the one declared. CI now runs one job pinned to the declared
  floor so it is verified rather than assumed.

## [0.1.1] - 2026-08-16

### Added
- `py.typed` marker, so the `Typing :: Typed` classifier is honored by type
  checkers (PEP 561).

### Changed
- The `duho` dependency is now `>=0.3.2,<0.4`. It was unbounded, so a fresh
  install of 0.1.0 resolved a duho the parser cannot build against and every
  `pkgforge` invocation failed.
- `sniff_format()` reads only the 16 bytes it inspects instead of the whole
  file.

### Fixed
- `--exclude` with an absolute pattern now works for every source of a
  multi-source `install`. The parsed statements were rewritten in place when
  bound to a source root, so the second source re-prefixed an already-rebased
  pattern (`/a/**` -> `/src2/src1/a/**`) and silently excluded nothing.
- An exclude statement that does not apply no longer vetoes the statements
  after it. A directory that failed a recursive (`**`) pattern returned a
  definite "keep" instead of falling through, making the outcome depend on
  statement order (`-X '**/*.pyc' -X '(?type:directory)**/tmp'` never reached
  the second statement).
- `install -x` with a stdin source (`-`) raises a clear
  `cannot infer compression from stdin; pass -x TYPE` instead of an
  `AttributeError`.
- `install -x` given a path as its kind is rejected with an explanation.
  `-x` takes an optional argument, so `install -x SRC DST` parses `SRC` as the
  compression kind; with further sources the positionals silently shifted along
  by one and the source path was then run as a decompressor command.

### Notes
- duho 0.4 and newer are not supported. From 0.4.0 the argument model rejects
  `Install.source`'s `Union[List[Path], Path]` and the parser fails to build,
  so the cap above is what makes this release installable; supporting 0.4+
  requires re-declaring `source` and `exclude` against that model.
- On the supported duho range, `--exclude` passed through the command line
  parses to a nested list and `install` then fails; the exclude fixes above
  are reachable through the Python API.

## [0.1.0] - 2026-07-18

First release. pkgforge stages files into a build root, records their intended
install metadata in a file DB, and renders that DB into RPM/Debian packaging
manifests. Built on the [duho](https://pypi.org/project/duho/) declarative CLI
framework; Python 3.9+, Linux runtime.

### Added
- **Commands**: `install` (stage a source and record its entry — files,
  directories, symlinks, decompression, hardlinks, `--exclude`, `--chown`,
  `--remove-source`), `scan` (walk a tree and record entries; `--missing` fills
  gaps), `dbdump` (render the DB to a packaging manifest), `initdb`, and
  `compact` (collapse an append-log DB to one record per live path).
- **Dump formats**: `rpmspecfiles` (RPM `%files` lines with `%attr`/`%dir` and a
  `meta.rpmprefix`) and `debian` (`install` + `permissions` artifacts). The
  format registry supports multi-artifact formats.
- **Pluggable file-DB backends** behind one `DbProvider` interface: `jsonl`
  (append-only JSON Lines, the default), `yaml` (append-only YAML), and `sqlite`
  (a real SQLite store, upserted in place). Every command behaves identically
  across all three. The backend is inferred from the `--db` suffix
  (`.jsonl`/`.ndjson`, `.yaml`/`.yml`, `.db`/`.sqlite`/`.sqlite3`); `--db-format`
  / `PKGFORGE_DB_FORMAT` overrides it, and reading auto-detects a file's actual
  format. `register_provider()` lets a third-party package add its own backend;
  `DbProvider`, `open_db`, and `register_provider` are exported from the package.
- Environment-driven configuration for unattended builds (`PKGFORGE_ROOT`,
  `PKGFORGE_DB`, `PKGFORGE_DB_FORMAT`), `--version`, and shell completion.
- Tar-family archives extract via stdlib `tarfile` (safe `data` filter where
  supported); `bsdtar` is only a fallback for other formats (e.g. `.iso`).
- Documentation site (mkdocs-material) with a guide + API reference, a
  `benchmarks/` runner, an end-to-end example, and CI (`test.yml`/`release.yml`).

### Notes
- File-DB entries store `mode` as an octal permission string; two sentinels
  (`-` = OS default, `--` = resolve from disk) defer a field to the staged file.
- Hardlink install uses `os.link` for portability across Python 3.9–3.13
  (`Path.link_to` was removed in 3.12; `Path.hardlink_to` only exists from 3.10).

[Unreleased]: https://github.com/jose-pr/pkgforge/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/jose-pr/pkgforge/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jose-pr/pkgforge/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jose-pr/pkgforge/releases/tag/v0.1.0
