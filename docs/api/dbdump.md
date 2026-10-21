# Dump formats (`dbdump`)

The packaging-manifest format registry and its built-in formats. A
third-party format subclasses `PerEntryFormat` or `MultiArtifactFormat` with
its own `NAME` and advertises it in the `pkgforge.dump_formats` entry-point
group; see [Adding a format](../guide/formats.md#adding-a-format). The checks
every format must pass are in [Contract suite](testing.md).

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

::: pkgforge.dbdump.DumpContext
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
