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

%install
export PKGFORGE_ROOT=%{buildroot} PKGFORGE_DB=%{_builddir}/files.jsonl
pkgforge -q --config %{proj}/pkgforge.cfg stage --init
pkgforge -q dbdump -f rpmspecfiles %{_builddir}/files.txt

%files -f %{_builddir}/files.txt
