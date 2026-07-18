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

## Design notes for unattended use

- **No prompts.** Commands never wait for input. `install` reading from `-`
  (stdin) checks `isatty()` and skips cleanly when there is no piped data.
- **Resilient defaults.** `--buildroot` defaults to the current directory and
  `--db` to `PKGFORGE_DB`; a missing DB reads as empty rather than erroring.
- **Ownership is opt-in.** `install` records owner/group but only *applies* them
  with `--chown`, so an unprivileged build doesn't fail trying to `chown`.
- **Version-agnostic hardlinks.** The file-install fast path uses `os.link`,
  which works across every supported Python (unlike `Path.link_to` /
  `Path.hardlink_to`, which changed across 3.10–3.12).
- **No external archiver required** for tar-family sources — stdlib `tarfile`
  handles them; `bsdtar` is only needed for other formats (e.g. `.iso`).
