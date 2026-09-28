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

Each path is quoted for rpm's `%files -f` parser (targets rpm 4.19+, where
no spelling of `%` is literal inside or outside quotes): a backslash and a
double quote are escaped, and the whole path is written as UTF-8 (a
non-UTF-8 name round-trips its original bytes). Glob characters
(`* ? [ ]`) are never escaped -- rpm's own quoted-string globbing already
matches only the literal name. A `%` anywhere in the path is refused
outright (`DumpError`, `dbdump` exits 1): rpm macro-expands every `%files`
line before it looks at quoting, so an unescaped `%` let a staged filename
run as a macro -- including `%(...)`, which runs a shell command -- and
there is no quoting that makes it literal on every rpm version. Rename or
`--exclude` such a file, or write that one line by hand. A path containing
a control character (including a tab) is refused the same way: rpm cannot
represent it either.

!!! warning
    Every directory entry becomes a `%dir` **ownership** claim -- the built
    RPM installs that directory with the recorded mode/owner/group (or the
    build root's own, if unset). Never `scan` a directory the distro itself
    ships (`/usr`, `/usr/bin`, `/usr/share`, `/etc`, ...): if its mode/owner
    differs from the distro package that already owns it (e.g. Fedora's
    `filesystem` ships `/usr/bin` as `0555`), `rpm -U` refuses to install.
    Scan only a directory your package owns.

```bash
pkgforge dbdump -f rpmspecfiles files.txt
pkgforge dbdump -f rpmspecfiles -          # to stdout
```

## `debian`

Writes three artifacts into an output directory (created if needed):

- **`install`** — `dh_install`-style `<src> <dest-dir>` lines (one per
  non-directory entry), where the source is the build-root-relative path and the
  destination is the entry's parent directory:

    ```
    usr/bin/tool usr/bin
    etc/tool/config etc/tool
    ```

- **`permissions`** — a pkgforge-specific `<path> <mode> <owner> <group>`
  manifest for every entry that pins a non-default mode, owner, or group:

    ```
    /usr/bin/tool 755 root root
    /etc/tool/config 640 root adm
    ```

    The path is unescaped and may itself contain spaces, so parse a line
    right-to-left (everything before the last three fields is the path),
    not with a naive `read path mode owner group`.

- **`dirs`** — `dh_installdirs`-style lines, one per directory entry
  (dest-escaped; always written, even when empty), so a directory recorded
  with no files under it (e.g. `install -d` for an empty state directory)
  still reaches the package -- `rpmspecfiles`' `%dir` prefix covers the same
  case for RPM:

    ```
    var/lib/tool
    ```

`install` sources are escaped for `dh_install`/`dh_installdirs` (needs
debhelper compat 13): a backslash before each glob character (`\ * ? [ ] { }`)
so the name matches only itself, a leading `#` backslash-escaped (`dh_install`
treats a line starting with `#` as a comment), and a space written as
`${Space}`. Destinations are only ever `${Dollar}{`/`${Space}`-escaped, never
glob-escaped (`dh_install` takes the destination literally). A path
containing a control character, or a mode/owner/group containing whitespace,
stops `dbdump` with an error naming the problem; a non-UTF-8 name is written
as its original bytes.

```bash
pkgforge dbdump -f debian debian/          # writes debian/{install,permissions,dirs}
pkgforge dbdump -f debian -                # all three to stdout under "# === <name> ===" headers
```

!!! note
    The `debian` format produces inputs you wire into your packaging: drop
    `install` in as a `debian/<pkg>.install` file and `dirs` in as a
    `debian/<pkg>.dirs` file (dumping straight into `debian/` writes plain
    `debian/install`/`debian/dirs`, which debhelper also reads for the
    first binary package).

    `install`'s sources are relative to the build root, and `dh_install`
    looks for them only in the package directory and `debian/tmp`. Either
    stage with `PKGFORGE_ROOT=debian/tmp`, or add an
    `override_dh_install: dh_install --sourcedir=$(PKGFORGE_ROOT)` target to
    `debian/rules`.

    `permissions` is **not** `dpkg-statoverride` input -- that tool takes
    `user group mode path` (a different field order) and rejects `-`, while
    `permissions` writes `path mode owner group` and uses `-` for an
    unpinned field (e.g. `/usr/share/tool/share 755 - -` for a directory
    that pins only its mode). Packages also aren't supposed to
    `dpkg-statoverride` files they ship themselves -- that tool is for the
    local admin. Apply `permissions` instead from an `override_dh_fixperms`
    target in `debian/rules`, parsing each line right-to-left (the path is
    everything before the last three fields, so it may itself contain
    spaces) and skipping a `-` field:

    ```make
    override_dh_fixperms:
    	dh_fixperms
    	while read -r line; do \
    		mode=$${line##* }; rest=$${line% *}; \
    		group=$${rest##* }; rest=$${rest% *}; \
    		owner=$${rest##* }; path=$${rest% *}; \
    		[ "$$mode" = - ] || chmod "$$mode" "debian/tool$$path"; \
    		[ "$$owner" = - ] || chown "$$owner" "debian/tool$$path"; \
    		[ "$$group" = - ] || chgrp "$$group" "debian/tool$$path"; \
    	done < debian/permissions
    ```
