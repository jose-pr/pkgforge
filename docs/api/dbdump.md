# Dump formats (`dbdump`)

The packaging-manifest format registry and its built-in formats. A
third-party format subclasses `PerEntryFormat` or `MultiArtifactFormat` with
its own `NAME` -- no registration call needed.

::: pkgforge.dbdump.DumpError
    options:
      show_root_heading: true

::: pkgforge.dbdump.UnsupportedOutputError
    options:
      show_root_heading: true

::: pkgforge.dbdump.DumpFormat
    options:
      show_root_heading: true

::: pkgforge.dbdump.PerEntryFormat
    options:
      show_root_heading: true

::: pkgforge.dbdump.MultiArtifactFormat
    options:
      show_root_heading: true

::: pkgforge.dbdump.Entries
    options:
      show_root_heading: true

## Built-in formats

::: pkgforge.dbdump.rpm.RpmSpecFiles
    options:
      show_root_heading: true

::: pkgforge.dbdump.rpm.RpmSpecFilesPre419
    options:
      show_root_heading: true

::: pkgforge.dbdump.debian.Debian
    options:
      show_root_heading: true
