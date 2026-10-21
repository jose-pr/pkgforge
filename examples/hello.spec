Name:           hello
Version:        1.0
Release:        1
Summary:        pkgforge example package
License:        MIT
BuildArch:      noarch

%description
A sample package whose file list, modes and owners come from a pkgforge file
DB. Build it from this directory with
rpmbuild -bb --define "proj $PWD" hello.spec
The hello user comes from the sysusers.d file in the package. rpm 4.19 and
later creates it before unpacking; the pre-install script below does the same
on older rpm.

%install
export PKGFORGE_ROOT=%{buildroot} PKGFORGE_DB=%{_builddir}/files.jsonl
pkgforge -q --config %{proj}/pkgforge.cfg stage --init
pkgforge -q dbdump -f rpmspecfiles %{_builddir}/files.txt

# rpm below 4.19 reads a pre-install script file when it parses the spec,
# before the install step has written anything, so the sysusers text is
# expanded into the script here instead.
%pre
systemd-sysusers - <<'PKGFORGE_SYSUSERS' || :
%(d=$(mktemp -d) && PKGFORGE_ROOT=$d PKGFORGE_DB=- pkgforge -q --config %{proj}/pkgforge.cfg sysfiles >/dev/null && cat $d/usr/lib/sysusers.d/hello.conf; rm -rf $d)
PKGFORGE_SYSUSERS

%post
systemd-tmpfiles --create /usr/lib/tmpfiles.d/hello.conf >/dev/null 2>&1 || :

%files -f %{_builddir}/files.txt
