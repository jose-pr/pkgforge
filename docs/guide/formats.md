# Dump formats

`pkgforge dbdump -f FORMAT [OUTPUT]` renders the file DB into a packaging
manifest. `null` (removed) entries and `--exclude` matches are skipped.

| Format | Aliases | Shape | Output |
| --- | --- | --- | --- |
| `rpmspecfiles` | `rpm`, `rpmspec` | per-entry lines | a file or `-` (stdout) |
| `rpmspecfiles-pre419` | `rpm-pre419` | per-entry lines | a file or `-` (stdout) |
| `debian` | `deb` | multiple artifacts | a **directory**, or `-` (stdout, sectioned) |

## `rpmspecfiles`

Emits one RPM `%files` line per entry:

```
%attr(755,root,root) "/usr/bin/tool"
%dir %attr(-,-,-) "/etc/tool"
%config(noreplace) %attr(640,root,adm) "/etc/tool/config"
```

Directories get a `%dir` prefix; an entry's `meta.rpmprefix` (set with
[`-O rpmprefix=VALUE`](commands.md#install), e.g. `%config(noreplace)`) is
prepended.

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

### rpm older than 4.19

`rpmspecfiles` targets rpm 4.19+. Below that, a quoted `%files -f` name is
macro-expanded twice and an unquoted name's glob characters are matched by
rpm's own globbing before pkgforge's escaping is ever consulted, so the same
quoting is not safe there. Measured 2026-09-29 against real `rpmbuild` runs
on rpm 4.14.3 (Rocky Linux 8), 4.16.1 (Rocky Linux 9) and 4.18.2 (Ubuntu's
`rpm` package), against the rpm 4.19+ baseline of 6.0.2 (Fedora):

| class | fedora (6.0) | rocky 8 (4.14) | rocky 9 (4.16) | ubuntu (4.18) |
| --- | --- | --- | --- | --- |
| plain | exact | exact | exact | exact |
| space | exact | exact | exact | exact |
| utf8 | exact | exact | exact | exact |
| non-UTF-8 byte | loud | exact | loud | loud |
| `"` (dquote) | exact | loud | loud | loud |
| `\` (backslash) | exact | loud | loud | loud |
| `*` (star) | exact | overmatch | overmatch | overmatch |
| `?` (qmark) | exact | overmatch | overmatch | overmatch |
| `[ ]` (bracket) | exact | wrong | wrong | wrong |
| `{ }` (brace) | exact | wrong | wrong | wrong |
| `%` (any form) | refused | refused | refused | refused |

`overmatch` means the build also packages an unrelated sibling file;
`wrong` means it packages a different file than the one named. `rpm
--version` on the build host decides which `-f` to use:

- **rpm 4.19 or newer:** `-f rpmspecfiles`, as documented above.
- **older than 4.19:** `-f rpmspecfiles-pre419` (alias `rpm-pre419`). It
  writes each path completely unquoted (rpm's bare-token reader passes a
  literal `"`/`\` straight through, unescaped and literal), and refuses a
  path containing a space, a glob character (`* ? [ ] { }`), `%` (any
  form), or a non-UTF-8 byte, instead of risking the wrong file:

    | class | fedora (6.0) | rocky 8 (4.14) | rocky 9 (4.16) | ubuntu (4.18) |
    | --- | --- | --- | --- | --- |
    | plain | exact | exact | exact | exact |
    | utf8 | exact | exact | exact | exact |
    | `"` (dquote) | exact | exact | exact | exact |
    | `\` (backslash) | exact | exact | exact | exact |
    | space, glob chars, `%`, non-UTF-8 | refused | refused | refused | refused |

Using the wrong format for the rpm that actually builds the package: with
rpm below 4.19, `rpmspecfiles` fails the build loudly for a `dquote`/
`backslash`/non-UTF-8 name, silently packages an extra sibling for a `star`/
`qmark` name, and silently packages the wrong file for a `bracket`/`brace`
name. `rpmspecfiles-pre419` used with rpm 4.19 or newer either fails the
build for a name rpm 4.19+ could package, or packages it correctly -- never
the wrong file.

```bash
pkgforge dbdump -f rpmspecfiles-pre419 files.txt
```

## `debian`

Writes four artifacts into an output directory (created if needed):

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

- **`fixperms`** — a POSIX `sh` script, always written, that applies every
  pinned mode/owner/group directly to a package directory (see the recipe
  below): it starts with

    ```sh
    #!/bin/sh
    # Generated by pkgforge dbdump -f debian. Usage: sh fixperms PACKAGE-DIR
    set -e
    d=${1:?usage: sh fixperms PACKAGE-DIR}
    ```

    followed by one `chown`/`chgrp`/`chmod` line per pinned field (owner and
    group before mode, since `chown` clears a regular file's setuid/setgid
    bit even to the same owner), for every entry `permissions` also covers.
    A symlink gets `chown -h`/`chgrp -h` and never a `chmod` (POSIX `chmod`
    has no `-h`, and would otherwise follow the link to a target outside the
    package tree). With no pinned entry, the script is just its own
    four-line header.

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
pkgforge dbdump -f debian debian/          # writes debian/{install,permissions,dirs,fixperms}
pkgforge dbdump -f debian -                # all four to stdout under "# === <name> ===" headers
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
    local admin, and parsing `permissions` by hand means reading each line
    right-to-left (the path is everything before the last three fields, so
    it may itself contain spaces) and skipping a `-` field yourself. Run the
    generated `fixperms` script instead, from an `override_dh_fixperms`
    target in `debian/rules`, after `dh_fixperms` (which would otherwise
    strip the very bits being pinned, including a setuid/setgid mode):

    ```make
    override_dh_fixperms:
    	dh_fixperms
    	sh debian/fixperms debian/<pkg>
    ```

    Pinning an owner or group needs root at `binary` time: add
    `Rules-Requires-Root: binary-targets` to `debian/control`'s source
    stanza so `dpkg-buildpackage` runs `binary` under fakeroot. A mode-only
    pin needs no root. Building with `dh_builddeb` directly, outside
    `dpkg-buildpackage`, needs `DEB_RULES_REQUIRES_ROOT=binary-targets`
    exported by hand -- `dpkg-buildpackage` is what normally reads
    `Rules-Requires-Root` and exports it for you.
