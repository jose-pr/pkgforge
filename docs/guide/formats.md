# Dump formats

`pkgforge dbdump -f FORMAT [OUTPUT]` renders the file DB into a packaging
manifest. `null` (removed) entries and `--exclude` matches are skipped.

| Format | Aliases | Shape | Output |
| --- | --- | --- | --- |
| `rpmspecfiles` | `rpm` | per-entry lines | a file or `-` (stdout), or an existing directory (one list per package) |
| `rpmspecfiles-pre419` | `rpm-pre419` | per-entry lines | a file or `-` (stdout), or an existing directory (one list per package) |
| `rpmspec` | | the whole spec (rpm 4.19+) | a file or `-` (stdout); needs `[package]` `name`, `version`, `summary` and `license`; kept if it exists, unless `--force` |
| `rpmspec-pre419` | | the whole spec (older rpm) | as `rpmspec` |
| `debian` | `deb` | multiple artifacts | a **directory**, or `-` (stdout, sectioned); needs `[package] name`; with `--skeleton` also `control`, `changelog`, `rules` and `source/format` |

`rpmspec`, `rpmspec-pre419` and `debian --skeleton` write the spec and the
`debian/` files for a whole package from `pkgforge.cfg`; the walk-through from
the project file to the built packages is
[From pkgforge.cfg to .rpm and .deb](packaging.md).

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

## Kinds and capabilities

An entry's `kind` says how the package manager treats the file, in the same
words for both formats (`install -k`, a rule's `kind`, a step's `kind`):

| Kind | rpm | Debian |
| --- | --- | --- |
| `file` | no directive | an `install` line |
| `config` | `%config` | a conffile: debhelper marks every file under `/etc/` itself, and a path elsewhere is listed in `conffiles` |
| `config-noreplace` | `%config(noreplace)` | as `config` |
| `doc` | `%doc` | an `install` line |
| `license` | `%license` | an `install` line |
| `ghost` | `%ghost` | not shipped: left out of `install`, `dirs` and `fixperms`, and a file is listed in `not-installed` |

`caps` is one `setcap` clause such as `cap_net_bind_service=ep`. rpm stores it
with `%caps(CAPS)`. A `.deb` keeps no file capabilities in its payload, so the
generated `postinst` runs `setcap` on `configure`, and the package needs
`Depends: libcap2-bin` (`${pkgforge:Depends}` names it, see [`debian`](#debian)).

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

### One list per package

A file (or `-`) holds the entries of one package; a DB with entries of two
packages is refused (exit 2) unless OUTPUT is an existing directory. Into a
directory, `rpmspecfiles` writes `files-PACKAGE.txt` for every package that
`pkgforge.cfg` declares (`[package]` and each `[[packages]]`; an entry recorded
as `-` belongs to the main package), so a spec reads each package's list with
`%files -n PACKAGE -f .../files-PACKAGE.txt`. A package with no entry gets a
list of one empty line, which rpm builds as an empty package (it refuses a
list of no bytes, or of comments only). Writing into a directory needs
`[package] name`.

```bash
mkdir lists && pkgforge dbdump -f rpmspecfiles lists   # lists/files-hello.txt, lists/files-hello-doc.txt
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

## `rpmspec`

Writes a whole spec to a file or `-`, from `[package]` and `[[packages]]`;
`rpmspec-pre419` is the same for rpm older than 4.19. It needs `name`,
`version`, `summary` and `license`, and a `summary` for each subpackage (exit 2
naming what is missing). In order it holds: a guard that stops `rpmbuild` unless
`pkgforge_config` is defined; `__os_install_post` and `debug_package` set to
nothing; the preamble (`Name`, `Version`, `Release`, `Summary`, `License`, `URL`,
`BuildArch`, `Requires`) and `%description`; a `%package -n` block per
subpackage; an `%install` that runs `pkgforge stage --init` and writes the lists
with `rpmspecfiles` (or `rpmspecfiles-pre419`); a `%post` that runs
`systemd-tmpfiles` for a package that records a `tmpfiles.d` file (and, for
`rpmspec-pre419`, a `%pre` that creates the accounts); and one `%files -n NAME -f`
per package. Text taken from the file has `%` doubled, so it is never a macro.
A description line whose first non-blank character is `%` or `#` exits 2: rpm
would read it as a section such as `%post`, a conditional or a comment, even
with the `%` doubled. An existing spec is kept with a WARNING unless `--force`. Build it with
`rpmbuild -bb --define "pkgforge_config PATH" NAME.spec`.

## `debian`

Needs `[package] name` in `pkgforge.cfg`: debhelper reads `debian/PKG.NAME`
before `debian/NAME`, and refuses the nameless form for a source with more than
one binary package from compat 15, so every per-package file is named for its
package. For each package `P` (`[package]` first, then each `[[packages]]`
name; an entry recorded as `-` belongs to the main package) it writes into an
output directory (created if needed):

- **`P.install`** — `dh_install`-style `<src> <dest-dir>` lines (one per
  non-directory entry), where the source is the build-root-relative path and the
  destination is the entry's parent directory:

    ```
    # Generated by pkgforge dbdump -f debian
    usr/bin/tool usr/bin
    etc/tool/config etc/tool
    ```

- **`P.dirs`** — `dh_installdirs`-style lines, one per directory entry
  (dest-escaped; always written, even when it holds no line), so a directory
  recorded with no files under it (e.g. `install -d` for an empty state
  directory) still reaches the package -- `rpmspecfiles`' `%dir` prefix covers
  the same case for RPM:

    ```
    # Generated by pkgforge dbdump -f debian
    var/lib/tool
    ```

- **`P.conffiles`** — the `config` and `config-noreplace` entries outside
  `/etc/` (debhelper lists everything under `/etc/` itself), one absolute
  path per line; always written, empty when there is none. A path with
  whitespace in it stops `dbdump` with exit 1. It carries no marker line:
  debhelper copies a comment into the package, and `dpkg-deb` refuses it.

- **`P.postinst`** — a maintainer script that holds `#DEBHELPER#` and, under
  `if [ "$1" = configure ]`, first one guarded `dpkg-statoverride --update
  --add OWNER GROUP MODE PATH` for each entry owned by an account the package
  creates (see [Build time vs install time ownership](#build-time-vs-install-time-ownership)),
  then `setcap CAPS "$DPKG_ROOT"PATH` for each entry with `caps`. Capabilities
  are not kept in a `.deb`'s payload, so a package with a `caps` entry needs
  `Depends: libcap2-bin`.

- **`P.postrm`** — the same header and, under `if [ "$1" = purge ]`, a guarded
  `dpkg-statoverride --remove PATH` for each of those entries.

- **`P.substvars`** — `pkgforge:Depends=libcap2-bin` when `P` has an entry with
  `caps`, else `pkgforge:Depends=` with an empty value. List `${pkgforge:Depends}`
  in the package's `Depends` and the dependency follows the entries. `dh_prep`
  deletes `debian/P.substvars` on every build, so this works only when `dbdump`
  runs after `dh_prep` (the *stage into `debian/tmp`* recipe below); with the
  other recipe, list `libcap2-bin` in `Depends` yourself.

And once for the source package:

- **`not-installed`** — the `ghost` entries of every package that are not
  directories, sorted and escaped like `install`, so `dh_missing
  --fail-missing` accepts a ghost file that is staged but never shipped.
  debhelper reads only the source-wide `debian/not-installed`. A ghost entry
  is left out of `install`, `dirs` and `fixperms`.

- **`fixperms`** — a POSIX `sh` script, always written, that applies every
  pinned mode/owner/group directly to the package directories under the
  debian directory (see the recipes below): it starts with

    ```sh
    #!/bin/sh
    # Generated by pkgforge dbdump -f debian. Usage: sh fixperms DEBIAN-DIR
    set -e
    d=${1:?usage: sh fixperms DEBIAN-DIR}
    p() { ...resolves a path to itself or to its .gz... }
    ```

    followed by, per pinned entry, an `if t=$(p "$d"/P/path); then ...; fi`
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

`install`, `dirs`, `not-installed`, `substvars`, `postinst`, `postrm` and
`fixperms` carry a marker line, `# Generated by pkgforge dbdump -f debian` (the
first line of a list, the line after the shebang of a script). `dbdump` always
rewrites every artifact for every declared package, so no stale file survives a
package's removal from the configuration, and it never replaces a hand-written
debhelper file silently. Before it writes anything it refuses, with exit 2 and
nothing written:

1. an existing `install`, `dirs`, `not-installed`, `postinst`, `postrm` or
   `fixperms` of those names that lacks the marker: a hand-written file is
   never overwritten. Merge the lines by hand into your own copy, or dump into
   another directory;
2. a nameless `install`, `dirs`, `conffiles`, `postinst` or `postrm` in the
   output directory: debhelper ignores it, without a word, once `P.install`
   exists. Move its lines into `pkgforge.cfg` (or into the package-named file)
   and delete it;
3. an existing `P.conffiles` that lists a path the new one lacks: mark that
   file `config` or `config-noreplace`, or delete the line.

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
pkgforge dbdump -f debian debian/          # writes debian/P.{install,dirs,conffiles,postinst,postrm,substvars}, debian/not-installed and debian/fixperms
pkgforge dbdump -f debian -                # all of them to stdout under "# === <name> ===" headers
```

!!! note
    `install`'s sources are relative to the build root, and `dh_install`
    looks for them only in the package directory and `debian/tmp`. Either
    stage with `PKGFORGE_ROOT=debian/tmp`, or add an
    `override_dh_install: dh_install --sourcedir=$(PKGFORGE_ROOT)` target to
    `debian/rules`.

    Run the generated `fixperms` script from an `override_dh_fixperms`
    target in `debian/rules`, after `dh_fixperms` (which would otherwise
    strip the very bits being pinned, including a setuid/setgid mode). It
    takes the debian directory, so a package added later needs no change to
    `debian/rules`. It pins the base-passwd owners and groups; a file owned
    by an account the package creates is left to `postinst`:

    ```make
    override_dh_fixperms:
    	dh_fixperms
    	sh debian/fixperms debian
    ```

    Pinning an owner or group needs root at `binary` time: add
    `Rules-Requires-Root: binary-targets` to `debian/control`'s source
    stanza so `dpkg-buildpackage` runs `binary` under fakeroot. A mode-only
    pin needs no root. Building with `dh_builddeb` directly, outside
    `dpkg-buildpackage`, needs `DEB_RULES_REQUIRES_ROOT=binary-targets`
    exported by hand -- `dpkg-buildpackage` is what normally reads
    `Rules-Requires-Root` and exports it for you.

### The skeleton

`--skeleton` (a directory OUTPUT, and the project file) also writes the files a
package needs around those: `control` (a stanza per package, `Depends:
${misc:Depends}, ${pkgforge:Depends}` plus the declared `depends`),
`changelog` (one entry, dated from `SOURCE_DATE_EPOCH` when set, else now),
`rules` (the *stage into `debian/tmp`* recipe below, calling `pkgforge --config
CFG`) and `source/format`. It needs `name`, `version`, `summary` and
`maintainer`, and a `summary` for each subpackage. These are meant to be edited,
so an existing one is kept with a WARNING unless `--force`. The walk-through is
[From pkgforge.cfg to .rpm and .deb](packaging.md#4-build-the-deb).

### Debian recipes

Both recipes assume `debian/control` has `Rules-Requires-Root:
binary-targets` (owner and group pins need root at `binary` time), a
`debhelper-compat (= 13)` build dependency, a `pkgforge.cfg` with `[package]
name` in the source root (the `pkgforge` commands read `./pkgforge.cfg`), and a
`stage.sh` that runs the `pkgforge install` calls. Either way,
`override_dh_fixperms` runs `dh_fixperms` first and the generated `fixperms`
after it, because `dh_fixperms` would otherwise strip the very bits being
pinned. `dh $@ --with installsysusers` needs debhelper 13.10 or later (Debian
12, Ubuntu 24.04); it installs the accounts' `sysusers.d` file into `postinst`.

**Separate stage root.** Stage into a directory of your own and hand it to
`dh_install` with `--sourcedir`. `dh_prep` wipes `debian/tmp`, so a root of
your own survives to `dh_install`; the build target runs before `dh_prep` and
`dh_installdirs`, so `P.dirs` already exists when `dh_installdirs` reads it.
`dh_prep` also deletes `debian/P.substvars`, so a package with a `caps` entry
lists `libcap2-bin` in `Depends` by hand.

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
	sh debian/fixperms debian
```

**Stage into `debian/tmp`.** `dh_install` searches `debian/tmp` by itself, so
no `--sourcedir` is needed. Stage in `override_dh_auto_install`, which runs
after `dh_prep` has wiped `debian/tmp` and deleted the substvars files, so the
`dbdump` there writes the `P.substvars` that `${pkgforge:Depends}` reads (list
it in `Depends`, with `${misc:Depends}`). `dh_installdirs` has already run by
then, before `P.dirs` existed, so call it again after `dbdump`; without that
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
	sh debian/fixperms debian
```

## Build time vs install time ownership

An owner or group in Debian's base-passwd set (`root`, `daemon`, `adm`,
`www-data`, ...) exists on every target, so it is pinned at build time: rpm's
`%attr`, and the `fixperms` script for a `.deb`. Any other name must be declared
in [`[[users]]` or `[[groups]]`](config.md#accounts) and is owned at
install time, because the account does not exist while the files are unpacked.
`pkgforge sysfiles` (and `stage`) writes the payload files that create it:

```text
# /usr/lib/sysusers.d/hello.conf
u hello - "hello example daemon" /var/lib/hello -
# /usr/lib/tmpfiles.d/hello.conf
d /run/hello 0755 hello hello -
```

The first has a line for each account the package creates; the second a `d`
line for each ghost directory, so `kind = "ghost"` on a directory is how a
package gets a runtime directory such as `/run/hello`.

- **rpm 4.19 and later** reads the `sysusers.d` file from the payload and
  creates the accounts before it unpacks, so the plain `%attr` line owns the
  file. Every non-root name in an `%attr` also becomes a `user()` or `group()`
  requirement, so a base-passwd name the target's distribution does not provide
  (`www-data` on Fedora) makes the install fail its dependency check.
- **Debian** skips such an entry in `fixperms`. `#DEBHELPER#` in `postinst`
  runs `systemd-sysusers`, then `systemd-tmpfiles` (build with `dh $@ --with
  installsysusers`, debhelper 13.10+); after it the generated lines run
  `dpkg-statoverride --update --add` for the entry, unless the path already has
  an override, so an administrator's own override is not replaced. `postrm`
  removes the override on purge. An entry of this kind needs a recorded mode
  and cannot be a symlink (`dbdump` exits 1), since `dpkg-statoverride` needs
  a mode and follows a link.

### Accounts on rpm older than 4.19

Older rpm does not read the `sysusers.d` file, so the spec creates the accounts
itself in `%pre`, and the tmpfiles directories in `%post`. `%pre` is expanded
when the spec is parsed, before `%install` has run, so it asks `pkgforge
sysfiles` for the text instead of reading a staged file. From
`examples/hello.spec`:

```spec
# rpm below 4.19 reads a pre-install script file when it parses the spec,
# before the install step has written anything, so the sysusers text is
# expanded into the script here instead.
%pre
systemd-sysusers - <<'PKGFORGE_SYSUSERS' || :
%(d=$(mktemp -d) && PKGFORGE_ROOT=$d PKGFORGE_DB=- pkgforge -q --config %{proj}/pkgforge.cfg sysfiles >/dev/null && cat $d/usr/lib/sysusers.d/hello.conf; rm -rf $d)
PKGFORGE_SYSUSERS

%post
systemd-tmpfiles --create /usr/lib/tmpfiles.d/hello.conf >/dev/null 2>&1 || :
```

On rpm 4.19 and later the accounts exist already and the script changes
nothing.

## Adding a format

A package adds a format by subclassing `PerEntryFormat` (one file of lines) or
`MultiArtifactFormat` (several files in a directory) with its own `NAME`, and
advertising it in the `pkgforge.dump_formats` entry-point group:

```toml
[project.entry-points."pkgforge.dump_formats"]
lines = "myplug:LinesFormat"     # name = module:Class, the name equal to NAME
```

A format renders with `render(entries, context)`, where `context` is a
`DumpContext`: the project configuration whose `[package]` and `[[packages]]`
name the packages, the config file's path when known, and the OUTPUT being
written. A `PerEntryFormat` gets the per-package grouping and the
`files-PACKAGE.txt` directory output for free.

Once installed, `dbdump -f lines` finds it, and `DumpFormat.names()` lists it
before anything is imported. A `NAME` or alias another format holds is a
`ValueError` when the class is created. A format that fails to load stops the
command with an error naming the entry point.
