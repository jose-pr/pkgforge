#!/bin/sh
# Stages the files of the "hello" example package and records their install
# metadata. Both hello.spec and debian/rules run it with PKGFORGE_ROOT (the
# build root) and PKGFORGE_DB (the file DB) exported.
set -eu
S="$(dirname "$0")/src"
pf() { pkgforge -q "$@"; }

pf initdb

# Directories the package owns. An empty directory has no source, so give
# install an empty one to record it from.
E="$(mktemp -d)"
for d in /usr/libexec/hello /etc/hello /usr/share/licenses/hello /usr/share/doc/hello; do
  pf install -D -d -m 755 -o root -g root "$E" "$d"
done
pf install -D -d -m 750 -o root -g adm "$E" /var/lib/hello
rmdir "$E"

pf install -p -m 755 -o root -g root "$S/hello" /usr/bin
pf install -D -t symlink -O target=hello - /usr/bin/hi
pf install -p -m 4755 -o root -g root "$S/helper" /usr/libexec/hello
pf install -p -m 640 -o root -g adm -O 'rpmprefix=%config(noreplace)' "$S/hello.conf" /etc/hello
pf install -p -m 644 -o root -g root "$S/hello.service" /usr/lib/systemd/system
pf install -p -m 644 -o root -g root -O rpmprefix=%license "$S/LICENSE" /usr/share/licenses/hello
pf install -p -m 644 -o root -g root -O rpmprefix=%doc "$S/README" /usr/share/doc/hello
pf install -D -m 640 -o root -g adm -O rpmprefix=%ghost - /var/log/hello.log < /dev/null
