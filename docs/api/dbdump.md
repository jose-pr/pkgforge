# Dump formats (`dbdump`)

The packaging-manifest format registry and its built-in formats. A
third-party format subclasses `PerEntryFormat` or `MultiArtifactFormat` with
its own `NAME` -- no registration call needed.

::: pkgforge.exceptions.DumpError
    options:
      show_root_heading: true

::: pkgforge.exceptions.InvalidEntryError
    options:
      show_root_heading: true

::: pkgforge.exceptions.UnsupportedOutputError
    options:
      show_root_heading: true

::: pkgforge.dbdump.dump
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

## Built-in formats

::: pkgforge.dbdump.RPMSpecFiles
    options:
      show_root_heading: true

::: pkgforge.dbdump.RPMSpecFilesPre419
    options:
      show_root_heading: true

::: pkgforge.dbdump.DebianFiles
    options:
      show_root_heading: true
