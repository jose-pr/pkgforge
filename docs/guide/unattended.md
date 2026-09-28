# Unattended builds

pkgforge is built to run non-interactively inside a build pipeline. The
environment variables are the primary configuration mechanism — set them once
and every command picks them up:

```bash
export PKGFORGE_ROOT=/tmp/stage
export PKGFORGE_DB="$PKGFORGE_ROOT.files.jsonl"
```

A typical staging sequence in a build script:

```bash
pkgforge initdb

# stage binaries, config, and a whole tree
pkgforge install -p -m 755 -o root -g root ./build/tool /usr/bin
pkgforge install -p -m 640 -o root -g adm  ./config     /etc/tool
pkgforge install -p -d -m 755 ./share /usr/share/tool

# fill in any files that landed without an explicit entry
pkgforge scan --missing /usr

# emit packaging manifests
pkgforge dbdump -f rpmspecfiles rpm-files.txt
pkgforge dbdump -f debian debian/
```

## Logging

Every command logs under its own logger, `pkgforge.<command>` (`pkgforge.install`,
`pkgforge.scan`, `pkgforge.dbdump`, `pkgforge.initdb`, `pkgforge.compact`), so
`logging.getLogger("pkgforge")` controls all of them from Python.

- `-v, --verbose` (repeatable) raises the running command's own log level;
  `-q, --quiet` (repeatable) lowers it. They offset each other in one combined
  count.
- `--loglevel LEVEL` sets the running command's level directly; `--loglevel
  pkgforge.<command>:LEVEL[,...]` targets one or more loggers by name (e.g.
  `--loglevel pkgforge.scan:WARNING`). A bare `--loglevel pkgforge:LEVEL` does
  **not** change a command's output: the CLI always sets the dispatched
  command's own logger level explicitly, and that explicit level wins over
  anything inherited from the `pkgforge` parent logger.
- A malformed `--loglevel` value (an unknown level name, or the wrong
  `NAME:LEVEL` separator) exits 2 with a usage error, before the command runs.
- Log color follows the standard `NO_COLOR`/`FORCE_COLOR` convention and
  whether stderr is a terminal, so output redirected to a file or a CI log is
  always plain text.

## Design notes for unattended use

- **No prompts.** Commands never wait for input. `install` reading from `-`
  (stdin) checks `isatty()` and skips cleanly when there is no piped data.
- **Resilient defaults.** `--buildroot` defaults to the current directory and
  `--db` to `PKGFORGE_DB`; a missing DB reads as empty rather than erroring.
- **Ownership is opt-in.** `install` records owner/group but only *applies* them
  with `--chown`, so an unprivileged build doesn't fail trying to `chown`.
- **Sources are never modified.** File sources are copied (`shutil.copy2`), so
  `-m`/`--chown` never touch the source, and the build root may sit on any
  filesystem (including a tmpfs `/tmp`).
- **No external archiver required** for tar-family sources — stdlib `tarfile`
  handles them; `bsdtar` is only needed for other formats (e.g. `.iso`).
