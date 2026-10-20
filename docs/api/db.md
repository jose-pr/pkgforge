# DB backends (`db`)

The file-DB provider registry and its built-in backends. A third-party
backend subclasses `DbProvider` with its own `NAME` and advertises it in the
`pkgforge.dbs` entry-point group; see [Adding a
backend](../guide/file-db.md#adding-a-backend). The checks every backend must
pass are in [Contract suite](testing.md).

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
