# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

[Unreleased]: https://github.com/jose-pr/buildutils/commits/main
