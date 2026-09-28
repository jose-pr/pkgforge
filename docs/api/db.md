# DB backends (`db`)

The file-DB provider registry and its built-in backends. A third-party
backend subclasses `DbProvider` with its own `NAME` -- no registration call
needed.

::: pkgforge.db.DbError
    options:
      show_root_heading: true

::: pkgforge.db.Db
    options:
      show_root_heading: true

::: pkgforge.db.DbProvider
    options:
      show_root_heading: true

::: pkgforge.db.open_db
    options:
      show_root_heading: true

::: pkgforge.db.format_for_suffix
    options:
      show_root_heading: true

::: pkgforge.db.sniff_format
    options:
      show_root_heading: true

## Built-in backends

::: pkgforge.db.jsonl.JsonlDb
    options:
      show_root_heading: true

::: pkgforge.db.yaml.YamlDb
    options:
      show_root_heading: true

::: pkgforge.db.sqlite.SqliteDb
    options:
      show_root_heading: true
