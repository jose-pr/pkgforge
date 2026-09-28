# CLI subcommands

Each leaf command is a `duho.Cmd` subclass self-registered onto `PkgForge`;
use them via the CLI (`pkgforge <name> ...`) rather than instantiating
directly unless you're embedding the CLI layer itself.

::: pkgforge.install.Install
    options:
      show_root_heading: true

::: pkgforge.scan.ScanCmd
    options:
      show_root_heading: true

::: pkgforge.initdb.InitDb
    options:
      show_root_heading: true

::: pkgforge.compact.Compact
    options:
      show_root_heading: true

::: pkgforge.dbdump.DbDump
    options:
      show_root_heading: true
