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
export PKGFORGE_DB="$work/files.yaml"
mkdir -p "$PKGFORGE_ROOT" "$work/src/share"

# A tiny source tree to stage.
printf '#!/bin/sh\necho hello\n' > "$work/src/tool"
printf 'key = value\n'           > "$work/src/tool.conf"
printf 'data\n'                  > "$work/src/share/data.txt"

echo "== initdb =="
pkgforge initdb

echo "== install binary (0755 root:root) =="
pkgforge install -p -m 755 -o root -g root "$work/src/tool" /usr/bin

echo "== install config (0640 root:adm, %config on rpm) =="
pkgforge install -p -m 640 -o root -g adm -O rpmprefix=%config "$work/src/tool.conf" /etc

echo "== install a directory tree =="
pkgforge install -p -d -m 755 "$work/src/share" /usr/share/tool

echo
echo "== file DB =="
cat "$PKGFORGE_DB"

echo
echo "== rpm %files =="
pkgforge dbdump -f rpmspecfiles -

echo
echo "== debian artifacts =="
pkgforge dbdump -f debian "$work/debian"
echo "-- debian/install --";     cat "$work/debian/install"
echo "-- debian/permissions --"; cat "$work/debian/permissions"
