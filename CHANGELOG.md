# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/jose-pr/pkgforge/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/jose-pr/pkgforge/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jose-pr/pkgforge/releases/tag/v0.1.0
