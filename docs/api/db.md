# DB backends (`db`)

The file-DB provider registry and its built-in backends. A third-party
backend subclasses `DbProvider` with its own `NAME` -- no registration call
needed.

::: pkgforge.exceptions.DbError
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

::: pkgforge.db.jsonl_line
    options:
      show_root_heading: true

::: pkgforge.db.parse_jsonl
    options:
      show_root_heading: true

## Built-in backends

::: pkgforge.db.JSONLinesDb
    options:
      show_root_heading: true

::: pkgforge.db.YAMLDb
    options:
      show_root_heading: true

::: pkgforge.db.SQLiteDb
    options:
      show_root_heading: true
