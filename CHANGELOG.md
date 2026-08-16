# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
