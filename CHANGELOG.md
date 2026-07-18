# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Pluggable file-DB backends** — the DB is now provider-agnostic with three
  interchangeable implementations behind one interface: `jsonl` (append-only
  JSON Lines, the default), `yaml` (append-only YAML), and `sqlite` (a real
  SQLite store, upserted in place). Every command works identically across all
  three. The backend is inferred from the `--db` file suffix
  (`.jsonl`/`.ndjson`, `.yaml`/`.yml`, `.db`/`.sqlite`/`.sqlite3`); `--db-format`
  / `BUILDUTILS_DB_FORMAT` overrides it, and reading auto-detects a file's actual
  format so a legacy/mislabeled DB still loads.
- **`compact` subcommand** — collapse an append-log DB (`jsonl`/`yaml`) to one
  record per live path, dropping superseded records and removal tombstones.
- **`register_provider()` extension seam** — third-party packages can register
  their own DB backend (a `DbProvider` subclass) with optional file suffixes and
  a content sniffer, selectable via `--db-format`, suffix, or content detection.
  The three built-in backends register themselves through the same API.
  `DbProvider`, `open_db`, and `register_provider` are exported from the package.

### Changed
- The file DB moved off the single-document-YAML format (which relied on a YAML
  parser tolerating duplicate mapping keys) to the JSON Lines default: spec-clean
  and much faster to parse.

### Fixed
- DB path keys are recorded verbatim as logical build paths and no longer
  round-tripped through `os.fspath`/`Path`, which flipped separators on Windows.

### Added
- **`debian` dump format** — emits `install` (dh_install-style path list) and
  `permissions` (dpkg-statoverride-friendly `path mode owner group`) artifacts
  from the file DB, alongside the existing `rpmspecfiles`.
- Package now exposes `__version__` (resolved from installed metadata).
- Documentation site (mkdocs-material) with a guide + API reference, a
  `benchmarks/` runner, `examples/stage_and_package.sh`, and CI workflows
  (`test.yml` + `release.yml`).

### Changed
- Directory/archive extraction now prefers stdlib `tarfile` for the tar family
  (`.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`, `.tar.xz`), using the safe `data`
  extraction filter where the interpreter supports it. `bsdtar` remains the
  fallback for stdin and formats `tarfile` can't open (e.g. `.iso`), so a
  tar-based build no longer needs an external archiver.
- The dbdump format registry now supports multi-artifact formats (a format may
  render several files into an output directory), not just per-entry line dumps.

### Added (earlier in this cycle)
- Packaged `src/` layout with a `buildutils` console entry point and
  `python -m buildutils` support.
- `--chown` flag on `install` to apply the recorded owner/group (off by
  default, so unprivileged builds are unaffected).
- `--version` and shell completion (`--print-completion`) on the root command.

### Changed
- Mode is stored in the file DB as an octal permission string (e.g. `644`)
  instead of a raw `st_mode` integer, so the DB round-trips and dumps are
  correct.
- The CLI now builds on the `duho` declarative CLI framework.

### Fixed
- Hard-link install path used `Path.link_to`, removed in Python 3.12 (and
  `Path.hardlink_to` only exists from 3.10); now uses `os.link`, which works on
  every supported Python.
- Inverted exclude tests (`(?!type:...)`) never inverted (a helper dropped its
  `return`); they now work.
- `install` forced `DEBUG` logging on every run, overriding `--loglevel`/`-q`.
- `install`'s relative-destination guard used `Path.absolute()` (always truthy)
  and never triggered; it now checks `is_absolute()`.
- `dbdump` no longer closes the shared stdout fd when writing to `-`.
- `scan` crashed (`TypeError`) when run without `--buildroot`/`BUILDROOT`: the
  buildroot default was the string `"."`, and `str / str` is unsupported. The
  default is now `Path(".")`.
- `--remove-source` on a directory source raised `IsADirectoryError`; it now
  uses `shutil.rmtree` for directories.

### Removed
- Dead `--expand-file` option (declared but never used).

[Unreleased]: https://github.com/jose-pr/buildutils/commits/main
