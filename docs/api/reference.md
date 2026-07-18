# API Reference

Generated from docstrings. pkgforge is primarily a CLI, but its core types
and dump helpers are importable.

## Core types

::: pkgforge.common.FileType
    options:
      show_root_heading: true

::: pkgforge.common.FileEntry
    options:
      show_root_heading: true

::: pkgforge.common.FileEntryArgs
    options:
      show_root_heading: true

::: pkgforge.common.PkgForgeCmd
    options:
      show_root_heading: true

## Storage backends

::: pkgforge.db.DbProvider
    options:
      show_root_heading: true

::: pkgforge.db.register_provider

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

::: pkgforge.dbdump.rpmspecfile

::: pkgforge.dbdump.dump_formats

## Extraction helpers

::: pkgforge.install._is_tar_source

::: pkgforge.install._extract_tar
