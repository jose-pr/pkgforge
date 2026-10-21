# From pkgforge.cfg to packages

One `pkgforge.cfg` describes a package: the files, their modes and owners, and
who the package is. From it pkgforge stages the files and checks them. There are
then two routes to the packages:

- the generated spec and `debian/` directory, which `rpmbuild` and
  `dpkg-buildpackage` build (sections 3 and 4); or
- `pkgforge package`, which writes the `.deb`, Arch, Alpine and tar packages
  itself and runs `rpmbuild` for the `.rpm` (section 5).

This page walks both with the `hello` example, which builds two packages:
`hello` and its documentation, `hello-doc`.

You need `pkgforge` on `PATH` (the spec and `debian/rules` call it). The spec
route needs `rpmbuild` for the `.rpm`, and `dpkg-buildpackage`, `debhelper` and
`fakeroot` for the `.deb`. `pkgforge package` needs none of them except
`rpmbuild` for `-f rpm`. Each build is independent; do the one you need.

## 1. Start from the example

Copy the example project file and its sources from a checkout of
[the repository](https://github.com/jose-pr/pkgforge/tree/main/examples) into a
directory of your own:

```sh
mkdir -p work/hello && cd work/hello
cp -r ~/src/pkgforge/examples/pkgforge.cfg ~/src/pkgforge/examples/src .
```

`pkgforge.cfg` is described in [Project file](config.md). The part that matters
here is `[package]` (who the package is), `[[packages]]` (a second package,
`hello-doc`), and a `[[rules]]` element that sends everything under
`/usr/share/doc/hello-doc` to it.

## 2. Stage and check

`stage` runs the `[[stage]]` steps into a build root and records each entry in
the file DB. The build root defaults to the current directory, so name one:

```sh
export PKGFORGE_ROOT="$PWD/build/root" PKGFORGE_DB="$PWD/build/files.jsonl"
mkdir -p "$PKGFORGE_ROOT"
pkgforge stage --init
pkgforge verify
```

`verify` compares the build root with the DB: a file on disk that no entry
records, an entry whose file is missing, a changed type or symlink target. It
exits 0 when they agree and 1 when they do not, one line per problem, which is
what `rpmbuild` ("Installed (but unpackaged) file(s)") and `dh_missing` would
otherwise tell you later. See [`verify`](commands.md#verify).

To see what each package will ship, dump the per-package lists:

```sh
mkdir lists && pkgforge dbdump -f rpmspecfiles lists
```

`lists/files-hello.txt` and `lists/files-hello-doc.txt` hold one `%files` line
per entry of that package.

## 3. Build the .rpm

`-f rpmspec` writes the whole spec from `pkgforge.cfg`; `rpmbuild` gets the
project file's path with a macro, because the spec embeds no absolute path:

```sh
pkgforge dbdump -f rpmspec hello.spec
rpmbuild -bb --define "pkgforge_config $PWD/pkgforge.cfg" hello.spec
```

The packages are `~/rpmbuild/RPMS/noarch/hello-1.0-1.noarch.rpm` and
`hello-doc-1.0-1.noarch.rpm`. rpm warns that `%changelog` has no entries; the
spec has none to give.

On rpm older than 4.19 use `-f rpmspec-pre419` instead; it differs in how it
quotes the file lists and in creating the accounts with a `%pre` script.

The spec is short. Its `%install` stages again into rpm's own build root, with
`pkgforge stage --init`, then writes one list per package with `dbdump -f
rpmspecfiles`; each `%files -n PACKAGE -f` reads its list. It sets
`__os_install_post` to nothing because rpm's default post-install step
(`brp-compress` among others) would rename or strip files after `%install`, and
the lists name the files as staged. It sets `debug_package` to nothing for the
same reason: the package ships the files unchanged.

## 4. Build the .deb

`--skeleton` writes `debian/control`, `changelog`, `rules` and `source/format`
beside the per-package files that every `-f debian` dump writes:

```sh
pkgforge dbdump -f debian --skeleton debian
dpkg-buildpackage -b -us -uc -d
```

The packages are `../hello_1.0-1_all.deb` and `../hello-doc_1.0-1_all.deb`:
`dpkg-buildpackage` writes next to the source directory, which is why the
example sits in `work/hello`. `-d` skips the build-dependency check, for a host
that does not have `debhelper-compat` and `dh-sequence-installsysusers`
installed as packages; drop it where they are.

The generated `debian/rules` stages into `debian/tmp` in
`override_dh_auto_install`, which runs after `dh_prep`, then dumps the
per-package files and calls `dh_installdirs`. The order matters: `dh_prep`
deletes `debian/P.substvars`, the file that adds `libcap2-bin` to `Depends`
when a package has a file capability, and `dh_installdirs` must run after
`P.dirs` exists. `override_dh_fixperms` runs `dh_fixperms` and then the
generated `fixperms`, which pins the recorded modes and owners that
`dh_fixperms` would strip.

`control` asks for `debhelper-compat (= 13)` and `dh-sequence-installsysusers`,
which debhelper 13.9 and later (Debian 12, Ubuntu 24.04) provide. With no
`release` in `[package]` the source is `3.0 (native)` and the version is
`version`; with one, it is `3.0 (quilt)` and `version-release`.

## 5. Or build the packages directly

`pkgforge package -f FORMAT` writes the package files itself, from the build
root and the file DB that section 2 left: no spec, no `debian/` directory and no
build tool. It stages nothing and ships only what the DB records, so run
`stage` and `verify` first; a recorded path that is missing from the build root,
or of another type, or below a symlink that leads outside the build root, exits 2
naming the path. It writes one file per declared
package, the main package first, into the `-o` directory (created when missing)
and prints each path on its own line:

```sh
pkgforge package -f deb -o dist
pkgforge package -f arch -o dist
pkgforge package -f apk -o dist
pkgforge package -f tar -o dist
```

For `hello` that is `dist/hello_1.0-1_all.deb` and `dist/hello-doc_1.0-1_all.deb`,
`dist/hello-1.0-1-any.pkg.tar.zst`, `dist/hello-1.0-r1.apk` and
`dist/hello-1.0-1.tar.gz`, each with its `hello-doc` twin. The `.rpm` is built
by `rpmbuild` from a spec and a payload that `pkgforge` writes in a temporary
directory, so `-f rpm` needs `rpmbuild` on `PATH`:

```sh
pkgforge package -f rpm -o dist
```

This writes `dist/hello-1.0-1.noarch.rpm` and `dist/hello-doc-1.0-1.noarch.rpm`.
`rpmbuild` stamps its own times, so an `.rpm` is not byte-for-byte reproducible.

- `SOURCE_DATE_EPOCH` (an integer) fixes every time the package records, so two
  builds of the same build root produce identical `deb`, `arch`, `apk` and `tar`
  files. Without it, a file keeps its staged modification time and the build
  time is now.
- A build is all or nothing: every package is written beside its final name and
  renamed into place when all are built, and the `-o` directory is created only
  with the first file. A failed build, `rpm` without `rpmbuild` included, leaves
  no new directory, and a package already in `-o` keeps its content.
- Files of one package that are hardlinks in the build root (one inode, and the
  same recorded mode, owner, group and capability) ship as one file and
  hardlinks to it in `deb`, `arch`, `apk` and `tar`, and `rpm` keeps them too, so
  they install as one inode. A file whose partner is in another package ships as
  a copy.
- `--arch NAME` names the architecture of a package whose `arch` is `any`, in
  the format's own spelling (`arm64` for a `.deb`, `aarch64` for Arch and Alpine);
  without it, the architecture of the machine running `pkgforge`. An `all`
  package gets the format's architecture-independent name (`all`, `any`,
  `noarch`), `rpm` passes the value to `rpmbuild --target`, and `tar` has none.
- `--compression KIND` picks the payload compression from the table below. `zst`,
  which is Arch's default, needs Python 3.14 or the `zstd` extra (`pip install
  "pkgforge[zstd]"`); with neither the command exits 2 and suggests
  `--compression xz`.

What each format needs and writes. Besides `name`, a format checks the keys
listed and exits 2 naming what is missing; each subpackage also needs its own
`summary`, except for `tar`.

| Format | Needs | File | Compression, default first |
| --- | --- | --- | --- |
| `deb` | `version`, `summary`, `maintainer` | `NAME_VERSION[-RELEASE]_ARCH.deb` | `xz`, `gz`, `zst`, `none` |
| `rpm` | `version`, `summary`, `license` | `NAME-VERSION-RELEASE.ARCH.rpm` | rpm's own; `--compression` exits 2 |
| `arch` | `version`, `summary` | `NAME-VERSION-PKGREL-ARCH.pkg.tar.zst` | `zst`, `xz`, `gz` |
| `apk` | `version`, `summary` | `NAME-VERSION-rREL.apk` | `gz` |
| `tar` | `version` | `NAME-VERSION[-RELEASE].tar.gz` | `gz`, `xz`, `zst`, `none` |

What runs when the package is installed. An account the package creates does
not exist while the files are unpacked, so where a file belongs to one, the
install step creates the account and gives the file its owner (see
[build time vs install time ownership](formats.md#build-time-vs-install-time-ownership)).

| Format | Accounts and ownership | Units | File capabilities |
| --- | --- | --- | --- |
| `deb` | `postinst` runs `systemd-sysusers`, then `dpkg-statoverride` pins an entry owned by a created account (it is `root:root` in the file) | enabled and started in `postinst`, stopped in `prerm` | `postinst` runs `setcap`; `Depends` gains `libcap2-bin` |
| `rpm` | `%attr`; rpm 4.19 and later creates the accounts from the shipped `sysusers.d` file, older rpm in `%pre` | the `%systemd_*` scriptlets, when the macros are installed | `%caps` |
| `arch` | every file is `root:root` in the package (pacman keeps no names); `post_install` creates the accounts, then runs `chown`, `chgrp` and `chmod` by name | not enabled | `post_install` runs `setcap`; `depend` gains `libcap` |
| `apk` | names in the package; `.pre-install` creates the accounts with `addgroup -S` and `adduser -S` | none (Alpine has no systemd) | `.post-install` runs `setcap`; `depend` gains `libcap-setcap` |
| `tar` | names only; nothing runs | none | not stored: a WARNING names the `setcap` command to run |

A ghost directory is created at install time from the shipped `tmpfiles.d` file
by `systemd-tmpfiles` (`deb`, `rpm`, `arch`), and by `.post-install` for `apk`,
since Alpine has none. A `tar` leaves ghosts out.

The result differs from the other routes in a few places:

- A `.deb` from `pkgforge package` has no `usr/share/doc/PKG/changelog.Debian.gz`,
  which debhelper adds on the `debian/` route; otherwise it holds what that
  route builds from the same project file.
- pacman keeps no owner names, so `post_install` re-owns by name, and `pacman
  -Qkk` warns `UID mismatch` or `GID mismatch` for each path it re-owned. The
  files on disk are owned as recorded.
- `apk audit` reports `x` for a file that has a capability, an extended
  attribute the package database does not list.
- An `.apk` is an apk v2 package, unsigned, which `apk add --allow-untrusted`
  installs. Its version must read like an apk version (`1.2.3`, `1.2.3b`); the
  project file's `version` grammar cannot spell a `_rc` or `_git` suffix.
- Signing is not done for any format.

## What the project file must say

Each generator checks, when it runs, that the keys it needs are set, and exits 2
naming what is missing.

| Key | `.spec` | `debian/` and `-f deb` | `-f arch` | `-f apk` | Default |
| --- | --- | --- | --- | --- | --- |
| `name` | `Name` | `Source`, `Package` | `pkgname`, `pkgbase` | `pkgname`, `origin` | required |
| `version` | `Version` | changelog version | `pkgver` | `pkgver` | required |
| `release` | `Release` | changelog `version-release`, `3.0 (quilt)` | `pkgrel` | `-rN` | rpm `1`; Debian none, `3.0 (native)`; Arch `1`; Alpine `0` |
| `arch` | `BuildArch: noarch` for `all` | `Architecture: all` or `any` | `any` for `all` | `noarch` for `all` | arch-specific |
| `summary` | `Summary` | first `Description` line | `pkgdesc` | `pkgdesc` | required |
| `license` | `License` | not used | `license` | `license` | required for the spec |
| `maintainer` | not used | `Maintainer`, changelog trailer | `packager` | `packager`, `maintainer` | required for Debian |
| `description` | `%description` | rest of `Description` | not used | not used | the summary |
| `homepage` | `URL` | `Homepage` | `url` | `url` | none |
| `[package.rpm] requires` | one `Requires:` each | | | | none |
| `[package.debian] depends` | | added to `Depends` | | | none |
| `[package.debian] section`, `priority` | | `Section`, `Priority` | | | `misc`, `optional` |
| `[package.pacman] depends` | | | one `depend` each | | none |
| `[package.apk] depends` | | | | one `depend` each | none |

The same keys under `[[packages]]` describe a subpackage, which needs its own
`summary`. Its `arch` defaults to the main package's. In `-f deb`, a
`${binary:Version}` or `${source:Version}` in `depends` is replaced by the full
version.

## Subpackages

Each `[[packages]]` element becomes a `%package -n NAME` in the spec and a
`Package:` stanza in `control`, with its own file list (`files-NAME.txt`) and
its own `NAME.install`, `NAME.dirs`, `NAME.postinst` and so on; `pkgforge
package` writes one file for it. An entry ships
in the first of these that names a package:

1. the flag: `-P NAME` on `install`, `mkdir` and `symlink`, or the `package` of
   the `[[stage]]` step;
2. the first matching `[[rules]]` element's `package`;
3. the `[package]` name.

Adding a package is a `[[packages]]` element and a rule or step that fills it;
`debian/rules` and the spec do not change. An entry that names a package the
file does not declare is an error (exit 2).

## Regenerating

The spec, `control`, `changelog`, `rules` and `source/format` are meant to be
edited (a changelog entry, a build dependency), so `dbdump` keeps one that
exists and warns `kept existing PATH (pass --force to replace it)`. `--force`
replaces them. The files the build reads from the DB (`files-NAME.txt`,
`NAME.install`, `NAME.dirs`, `NAME.conffiles`, `NAME.postinst`, `NAME.postrm`,
`NAME.substvars`, `not-installed`, `fixperms`) are rewritten on every run, and
`dbdump` refuses, before writing anything, to replace a hand-written one: see
[`debian`](formats.md#debian) for the three cases (a file without the marker, a
nameless `install` or `postinst`, a `NAME.conffiles` that would lose a line).

## Where next

- [`package`](commands.md#package): the options, output and exit status of
  `pkgforge package`.
- [Dump formats](formats.md): every artifact each format writes.
- [Project file](config.md): every key, and how an entry gets its mode, owner,
  group and package.
- [`examples/`](https://github.com/jose-pr/pkgforge/tree/main/examples): the
  `hello` project, its generated `hello.spec` and `debian/`.
