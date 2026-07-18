# buildutils

**buildutils** stages files into a *build root* and records their intended
install metadata — mode, owner, group, type, and free-form key/value `meta` — in
a YAML *file DB*. That DB can then be dumped into packaging manifests such as an
RPM `%files` list or Debian `install` + `permissions` files.

It is a small, dependency-light helper for unattended build pipelines on Linux:
install a source into place, remember how it should be owned and permissioned,
and emit that record for the packager.

## Why buildutils

- **One record, many packagers.** Stage once, record once, dump to RPM or
  Debian from the same file DB.
- **Unattended-friendly.** Environment-driven configuration
  (`BUILDROOT` / `BUILDUTILS_DB`), no interactive prompts, resilient defaults.
- **Pythonic and portable.** Tar-family archives extract via stdlib `tarfile`
  (no external binary); `bsdtar` is only a fallback for formats it can't handle.
- **Built on [duho](https://github.com/jose-pr/duho).** A declarative CLI with
  `--version`, shell completion, and layered configuration for free.

## Install

```bash
pip install buildutils
```

Requires Python 3.9+. Runtime targets Linux (it uses POSIX `chmod`/`chown`,
symlinks, and hardlinks); the CLI and `--help` import cleanly on any platform.

## At a glance

```bash
export BUILDROOT=/tmp/stage BUILDUTILS_DB=/tmp/files.yaml

buildutils initdb
buildutils install -p -m 644 -o root -g root ./app.conf /etc
buildutils scan --missing /etc
buildutils dbdump -f rpmspecfiles -          # RPM %files to stdout
buildutils dbdump -f debian ./debian         # debian/install + permissions
```

See the [Guide](guide/install.md) for the full command and format reference.
