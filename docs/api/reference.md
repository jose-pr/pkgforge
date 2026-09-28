# API Reference

Generated from docstrings. pkgforge is primarily a CLI, but its core types
and dump helpers are importable.

## Core types

::: pkgforge.entry.FileType
    options:
      show_root_heading: true

::: pkgforge.entry.FileEntry
    options:
      show_root_heading: true

::: pkgforge.entry.FileEntryArgs
    options:
      show_root_heading: true

::: pkgforge.command.PkgForgeCmd
    options:
      show_root_heading: true

## Storage backends

::: pkgforge.db.DbProvider
    options:
      show_root_heading: true

::: pkgforge.db.open_db

## Commands

::: pkgforge.install.Install
    options:
      show_root_heading: true

::: pkgforge.scan.ScanCmd
    options:
      show_root_heading: true

::: pkgforge.compact.Compact
    options:
      show_root_heading: true

::: pkgforge.dbdump.DbDump
    options:
      show_root_heading: true

## Dump formats

::: pkgforge.dbdump.DumpFormat
    options:
      show_root_heading: true

::: pkgforge.dbdump.PerEntryFormat

::: pkgforge.dbdump.MultiArtifactFormat

::: pkgforge.dbdump.rpm.RpmSpecFiles

::: pkgforge.dbdump.debian.Debian

::: pkgforge.dbdump.UnsupportedOutputError

## Extraction helpers

::: pkgforge.install._is_tar_source

::: pkgforge.install._extract_tar
