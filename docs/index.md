# pkgforge

**pkgforge** stages files into a *build root* and records their intended
install metadata — mode, owner, group, type, and free-form key/value `meta` — in
a YAML *file DB*. That DB can then be dumped into packaging manifests such as an
RPM `%files` list or Debian `install` + `permissions` files.

It is a small, dependency-light helper for unattended build pipelines on Linux:
install a source into place, remember how it should be owned and permissioned,
and emit that record for the packager.

## Why pkgforge

- **One record, many packagers.** Stage once, record once, dump to RPM or
  Debian from the same file DB.
- **Unattended-friendly.** Environment-driven configuration
  (`PKGFORGE_ROOT` / `PKGFORGE_DB`), no interactive prompts, resilient defaults.
- **Pythonic and portable.** Tar-family archives extract via stdlib `tarfile`
  (no external binary); `bsdtar` is only a fallback for formats it can't handle.
- **Built on [duho](https://github.com/jose-pr/duho).** A declarative CLI with
  `--version`, shell completion, and layered configuration for free.

## Install

```bash
pip install pkgforge
```

Requires Python 3.9+. Runtime targets Linux (it uses POSIX `chmod`/`chown`,
symlinks, and hardlinks); the CLI and `--help` import cleanly on any platform.

## At a glance

```bash
export PKGFORGE_ROOT=/tmp/stage PKGFORGE_DB=/tmp/files.jsonl

pkgforge initdb
pkgforge install -p -m 644 -o root -g root ./app.conf /etc
pkgforge scan --missing /etc
pkgforge dbdump -f rpmspecfiles -          # RPM %files to stdout
pkgforge dbdump -f debian ./debian         # debian/install + permissions
```

See the [Guide](guide/install.md) for the full command and format reference.
