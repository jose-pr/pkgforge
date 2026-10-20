# Contract suite (`testing`)

The checks every DB backend and dump format must pass. A third-party package
runs them from its own tests, so its class is held to what the built-in ones
are:

```python
from pkgforge.testing import check_db_provider, check_dump_format

def test_my_backend(tmp_path):
    check_db_provider(MyDb, tmp_path)

def test_my_format(tmp_path):
    check_dump_format(MyFormat, tmp_path)
```

`pkgforge.testing.DB_PROVIDER_CHECKS` and `DUMP_FORMAT_CHECKS` are tuples of
the individual checks, `(cls, directory) -> None`, for a runner that
parametrises one test per check. A failure is an `AssertionError` naming the
class and the check. The module does not import pytest.

::: pkgforge.testing.check_db_provider
    options:
      show_root_heading: true

::: pkgforge.testing.check_dump_format
    options:
      show_root_heading: true
