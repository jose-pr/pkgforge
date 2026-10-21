# From pkgforge.cfg to .rpm and .deb

One `pkgforge.cfg` describes a package: the files, their modes and owners, and
who the package is. From it pkgforge stages the files, checks them, writes the
RPM spec and the `debian/` directory, and `rpmbuild` and `dpkg-buildpackage`
build the packages. This page walks that path with the `hello` example, which
builds two packages: `hello` and its documentation, `hello-doc`.

You need `pkgforge` on `PATH` (the spec and `debian/rules` call it), `rpmbuild`
for the `.rpm`, and `dpkg-buildpackage`, `debhelper` and `fakeroot` for the
`.deb`. Each build is independent; do the one you need.

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

## What the project file must say

Each generator checks, when it runs, that the keys it needs are set, and exits 2
naming what is missing.

| Key | `.spec` | `debian/` | Default |
| --- | --- | --- | --- |
| `name` | `Name` | `Source`, `Package` | required |
| `version` | `Version` | changelog version | required |
| `release` | `Release` | changelog `version-release`, `3.0 (quilt)` | rpm `1`; Debian none, `3.0 (native)` |
| `arch` | `BuildArch: noarch` for `all` | `Architecture: all` or `any` | arch-specific |
| `summary` | `Summary` | first `Description` line | required |
| `license` | `License` | not used | required for the spec |
| `maintainer` | not used | `Maintainer`, changelog trailer | required for Debian |
| `description` | `%description` | rest of `Description` | the summary |
| `homepage` | `URL` | `Homepage` | none |
| `[package.rpm] requires` | one `Requires:` each | | none |
| `[package.debian] depends` | | added to `Depends` | none |
| `[package.debian] section`, `priority` | | `Section`, `Priority` | `misc`, `optional` |

The same keys under `[[packages]]` describe a subpackage, which needs its own
`summary`. Its `arch` defaults to the main package's.

## Subpackages

Each `[[packages]]` element becomes a `%package -n NAME` in the spec and a
`Package:` stanza in `control`, with its own file list (`files-NAME.txt`) and
its own `NAME.install`, `NAME.dirs`, `NAME.postinst` and so on. An entry ships
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

- [Dump formats](formats.md): every artifact each format writes.
- [Project file](config.md): every key, and how an entry gets its mode, owner,
  group and package.
- [`examples/`](https://github.com/jose-pr/pkgforge/tree/main/examples): the
  `hello` project, its generated `hello.spec` and `debian/`.
