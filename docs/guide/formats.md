# Dump formats

`pkgforge dbdump -f FORMAT [OUTPUT]` renders the file DB into a packaging
manifest. `null` (removed) entries and `--exclude` matches are skipped.

| Format | Aliases | Shape | Output |
| --- | --- | --- | --- |
| `rpmspecfiles` | `rpm`, `rpmspec` | per-entry lines | a file or `-` (stdout) |
| `rpmspecfiles-pre419` | `rpm-pre419` | per-entry lines | a file or `-` (stdout) |
| `debian` | `deb` | multiple artifacts | a **directory**, or `-` (stdout, sectioned) |

Before any format renders, every surviving entry is checked: the key must
be an absolute path with no empty, `.` or `..` segment; `type` must be
`file`, `directory` or `symlink`; `mode` is 1-4 octal digits, `-` or `--`;
`owner` and `group` are an account name (`[A-Za-z_][A-Za-z0-9_.-]*`,
optionally ending in `$`), `-` or `--`; `kind` is one of `file`, `config`,
`config-noreplace`, `doc`, `license` and `ghost`, and all but `file` and
`ghost` need `type` `file`; `caps` is `-` or one lowercase `setcap` clause
(`cap_net_raw=ep`) on a regular file that is not a ghost. A bad entry stops
`dbdump` with exit 2 and one line naming the entry and the field, and writes
nothing. `%`, whitespace and control characters cannot occur in mode, owner,
group or caps.

A file OUTPUT is written whole or not at all: `dbdump` writes each file to
a sibling `.<name>.pkgforge-tmp` and renames them into place only when every
write has succeeded.

## `rpmspecfiles`

Emits one RPM `%files` line per entry:

```
%attr(755,root,root) "/usr/bin/tool"
%dir %attr(-,-,-) "/etc/tool"
%config(noreplace) %attr(640,root,adm) "/etc/tool/config"
```

Directories get a `%dir` prefix, a symlink's mode is written as `-`
(rpm ignores it and warns on an explicit one), and an entry's kind (set with
[`-k/--kind`](commands.md#install)) starts the line: `%config`,
`%config(noreplace)`, `%doc`, `%license` or `%ghost`, and nothing for `file`.
An entry with file capabilities (`--caps`) gets `%caps(CAPS)` before its
`%attr`:

```
%config(noreplace) %attr(640,root,adm) "/etc/tool/config"
%caps(cap_net_bind_service=ep) %attr(755,root,root) "/usr/bin/tool"
%ghost %dir %attr(750,tool,tool) "/run/tool"
```

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

Writes six artifacts into an output directory (created if needed):

- **`install`** — `dh_install`-style `<src> <dest-dir>` lines (one per
  non-directory entry), where the source is the build-root-relative path and the
  destination is the entry's parent directory:

    ```
    usr/bin/tool usr/bin
    etc/tool/config etc/tool
    ```

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
    p() { ...resolves a path to itself or to its .gz... }
    ```

    followed by, per pinned entry, an `if t=$(p "$d"/path); then ...; fi`
    that runs one `chown owner:group` (or `chown` / `chgrp` when only one is
    pinned) and then `chmod` (owner and group before mode, since `chown`
    clears a regular file's setuid/setgid bit even to the same owner).
    `dh_compress` renames a man page
    or a large doc to `<path>.gz` and `dh_usrlocal` removes directories under
    `/usr/local`; the pin follows the rename, and a path that is gone is
    named on stderr and skipped, so the script still exits 0.
    A symlink gets `chown -h`/`chgrp -h` and never a `chmod` (POSIX `chmod`
    has no `-h`, and would otherwise follow the link to a target outside the
    package tree). With no pinned entry, the script is just its own
    header.

- **`conffiles`** — the `config` and `config-noreplace` entries outside
  `/etc/` (debhelper lists everything under `/etc/` itself), one absolute
  path per line; always written, empty when there is none. A path with
  whitespace in it stops `dbdump` with exit 1.

- **`not-installed`** — the `ghost` entries that are not directories, escaped
  like `install`, so `dh_missing --fail-missing` accepts a ghost file that is
  staged but never shipped. A ghost entry is left out of `install`, `dirs` and
  `fixperms`.

- **`postinst`** — a maintainer script that holds `#DEBHELPER#` and, for each
  entry with `caps`, `setcap -- CAPS "$DPKG_ROOT"PATH` under
  `if [ "$1" = configure ]`. Capabilities are not kept in a `.deb`'s payload,
  so a package with a `caps` entry needs `Depends: libcap2-bin`. Its second
  line marks it as generated: `dbdump` replaces such a file, and refuses
  (exit 2, nothing written) a directory whose `postinst` lacks the marker, so
  a hand-written script is never overwritten. A package with its own
  `postinst` dumps into another directory and merges the lines by hand.

`install` sources are escaped for `dh_install`/`dh_installdirs` (needs
debhelper compat 13): a backslash before each glob character (`\ * ? [ ] { }`)
so the name matches only itself, a leading `#` backslash-escaped (`dh_install`
treats a line starting with `#` as a comment), and a space written as
`${Space}`. Destinations are only ever `${Dollar}{`/`${Space}`-escaped, never
glob-escaped (`dh_install` takes the destination literally). A path
containing a control character stops `dbdump` with an error naming the
problem; a non-UTF-8 name is written
as its original bytes.

```bash
pkgforge dbdump -f debian debian/          # writes debian/{install,dirs,fixperms,conffiles,not-installed,postinst}
pkgforge dbdump -f debian -                # all six to stdout under "# === <name> ===" headers
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

    Packages aren't supposed to `dpkg-statoverride` files they ship
    themselves -- that tool is for the local admin. Run the
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

### Debian recipes

Both recipes assume `debian/control` has `Rules-Requires-Root:
binary-targets` (owner and group pins need root at `binary` time), a
`debhelper-compat (= 13)` build dependency, and a `stage.sh` that runs the
`pkgforge install` calls. Replace `<pkg>` with the binary package name. Either way, `override_dh_fixperms` runs `dh_fixperms`
first and the generated `fixperms` after it, because `dh_fixperms` would
otherwise strip the very bits being pinned.

**Separate stage root.** Stage into a directory of your own and hand it to
`dh_install` with `--sourcedir`. `dh_prep` wipes `debian/tmp`, so a root of
your own survives to `dh_install`; the build target runs before `dh_prep` and
`dh_installdirs`, so `dirs` already exists when `dh_installdirs` reads it.

```make
#!/usr/bin/make -f
export PKGFORGE_ROOT := $(CURDIR)/stage
export PKGFORGE_DB := $(CURDIR)/debian/pkgforge.jsonl

%:
	dh $@ --with installsysusers

override_dh_auto_build:
	sh stage.sh
	pkgforge dbdump -f debian debian/

override_dh_install:
	dh_install --sourcedir=$(PKGFORGE_ROOT)

override_dh_fixperms:
	dh_fixperms
	sh debian/fixperms debian/<pkg>
```

**Stage into `debian/tmp`.** `dh_install` searches `debian/tmp` by itself, so
no `--sourcedir` is needed. Stage in `override_dh_auto_install`, which runs
after `dh_prep` has wiped `debian/tmp`. `dh_installdirs` has already run by
then, before `dirs` existed, so call it again after `dbdump`; without that
call a directory recorded empty (and any directory pinned only for its mode)
never reaches the package.

```make
#!/usr/bin/make -f
export PKGFORGE_ROOT := $(CURDIR)/debian/tmp
export PKGFORGE_DB := $(CURDIR)/debian/pkgforge.jsonl

%:
	dh $@ --with installsysusers

override_dh_auto_install:
	sh stage.sh
	pkgforge dbdump -f debian debian/
	dh_installdirs

override_dh_fixperms:
	dh_fixperms
	sh debian/fixperms debian/<pkg>
```

## Adding a format

A package adds a format by subclassing `PerEntryFormat` (one file of lines) or
`MultiArtifactFormat` (several files in a directory) with its own `NAME`, and
advertising it in the `pkgforge.dump_formats` entry-point group:

```toml
[project.entry-points."pkgforge.dump_formats"]
lines = "myplug:LinesFormat"     # name = module:Class, the name equal to NAME
```

Once installed, `dbdump -f lines` finds it, and `DumpFormat.names()` lists it
before anything is imported. A `NAME` or alias another format holds is a
`ValueError` when the class is created. A format that fails to load stops the
command with an error naming the entry point.
