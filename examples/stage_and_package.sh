#!/usr/bin/env bash
# End-to-end pkgforge example: stage a small "app" into a build root, record
# its install metadata, and emit both RPM and Debian packaging manifests.
#
#   ./examples/stage_and_package.sh
#
# Requires: pkgforge on PATH (pip install -e . from the repo root), a POSIX
# filesystem. No packaging tools are needed -- this only produces the manifests.
set -euo pipefail

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

export PKGFORGE_ROOT="$work/stage"
export PKGFORGE_DB="$work/files.jsonl"
mkdir -p "$PKGFORGE_ROOT" "$work/src/share"

# A tiny source tree to stage.
printf '#!/bin/sh\necho hello\n' > "$work/src/tool"
printf 'key = value\n'           > "$work/src/tool.conf"
printf 'data\n'                  > "$work/src/share/data.txt"

echo "== initdb =="
pkgforge initdb

echo "== install binary (0755 root:root) =="
pkgforge install -p -m 755 -o root -g root "$work/src/tool" /usr/bin

echo "== install config (0640 root:adm, %config(noreplace) on rpm) =="
pkgforge install -p -m 640 -o root -g adm -k config-noreplace "$work/src/tool.conf" /etc

echo "== install a directory tree, recording its contents too =="
# A tree install stages every file but records only its own entry by
# default; --record-tree also records everything below it the DB doesn't
# already hold (owner/group here, mode/type read from disk). Skipping this
# flag leaves data.txt out of both manifests below.
pkgforge install -p -D -d -m 755 -o root -g root --record-tree "$work/src/share" /usr/share/tool

echo
echo "== file DB =="
cat "$PKGFORGE_DB"

echo
echo "== rpm %files =="
pkgforge dbdump -f rpmspecfiles -

echo
echo "== debian artifacts =="
pkgforge dbdump -f debian "$work/debian"
echo "-- debian/install --";  cat "$work/debian/install"
echo "-- debian/dirs --";     cat "$work/debian/dirs"
echo "-- debian/fixperms --"; cat "$work/debian/fixperms"
echo "-- debian/postinst --"; cat "$work/debian/postinst"

echo
echo "== build real packages from this directory =="
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cat <<MSG
RPM (hello.spec runs "pkgforge stage" from pkgforge.cfg inside %install):
  cd $here && rpmbuild -bb --define "proj \$PWD" hello.spec

Debian (debian/rules runs "pkgforge stage" from pkgforge.cfg into debian/tmp;
build in a copy, since dpkg-buildpackage writes next to the source tree):
  cp -r $here /tmp/hello && cd /tmp/hello && dpkg-buildpackage -b -us -uc -d

Both need pkgforge on PATH; the .deb build needs debhelper and fakeroot.
MSG
