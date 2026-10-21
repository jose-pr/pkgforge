# Package formats (`package`)

The package-format registry and its built-in formats. `build` writes one package
per declared package from the file DB and the build root. A third-party format
subclasses `PackageFormat` with its own `NAME` and advertises it in the
`pkgforge.package_formats` entry-point group. The packages' user and group
names, modes and times are read from the DB and the build root the same way for
every format.

::: pkgforge.package.build
    options:
      show_root_heading: true

::: pkgforge.package.BuildContext
    options:
      show_root_heading: true

::: pkgforge.package.PackagePayload
    options:
      show_root_heading: true

::: pkgforge.package.PackageFormat
    options:
      show_root_heading: true

## Built-in formats

::: pkgforge.package.DebFormat
    options:
      show_root_heading: true

::: pkgforge.package.TarFormat
    options:
      show_root_heading: true
