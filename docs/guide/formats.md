# Dump formats

`pkgforge dbdump -f FORMAT [OUTPUT]` renders the file DB into a packaging
manifest. `null` (removed) entries and `--exclude` matches are skipped.

| Format | Shape | Output |
| --- | --- | --- |
| `rpmspecfiles` | per-entry lines | a file or `-` (stdout) |
| `debian` | multiple artifacts | a **directory**, or `-` (stdout, sectioned) |

## `rpmspecfiles`

Emits one RPM `%files` line per entry:

```
%attr(755,root,root) "/usr/bin/tool"
%dir %attr(-,-,-) "/etc/tool"
%config(noreplace) %attr(640,root,adm) "/etc/tool/config"
```

Directories get a `%dir` prefix; an entry's `meta.rpmprefix` (e.g.
`%config(noreplace)`) is prepended.

```bash
pkgforge dbdump -f rpmspecfiles files.txt
pkgforge dbdump -f rpmspecfiles -          # to stdout
```

## `debian`

Writes two artifacts into an output directory (created if needed):

- **`install`** — `dh_install`-style `<src> <dest-dir>` lines (one per
  non-directory entry), where the source is the build-root-relative path and the
  destination is the entry's parent directory:

    ```
    usr/bin/tool usr/bin
    etc/tool/config etc/tool
    ```

- **`permissions`** — `<path> <mode> <owner> <group>` lines
  (`dpkg-statoverride`-friendly) for every entry that pins a non-default mode,
  owner, or group:

    ```
    /usr/bin/tool 755 root root
    /etc/tool/config 640 root adm
    ```

```bash
pkgforge dbdump -f debian debian/          # writes debian/install + debian/permissions
pkgforge dbdump -f debian -                # both to stdout under "# === <name> ===" headers
```

!!! note
    The `debian` format produces inputs you wire into your packaging: drop
    `install` in as a `debian/<pkg>.install` file, and feed `permissions` to
    `dpkg-statoverride` (or a `debian/rules` override) to pin ownership/modes
    that `dh_fixperms` would otherwise normalize.
