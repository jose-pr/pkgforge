# Configuration (`config`)

The project file `pkgforge.cfg`, read into frozen dataclasses. `load` reads and
validates a file; every problem is a `UsageError` that names the file and the
key. `Project.from_config` builds a project from a file, and
`Project(..., config=)` takes a `ProjectConfig` you built yourself.

```python
from pkgforge import Project
from pkgforge.config import load

config = load("pkgforge.cfg")
project = Project.from_config("pkgforge.cfg", buildroot="build/root")
```

::: pkgforge.config.ProjectConfig
    options:
      show_root_heading: true

::: pkgforge.config.PackageInfo
    options:
      show_root_heading: true

::: pkgforge.config.SubPackageInfo
    options:
      show_root_heading: true

::: pkgforge.config.RpmInfo
    options:
      show_root_heading: true

::: pkgforge.config.DebianInfo
    options:
      show_root_heading: true

::: pkgforge.config.EntryDefaults
    options:
      show_root_heading: true

::: pkgforge.config.EntryRule
    options:
      show_root_heading: true

::: pkgforge.config.StageStep
    options:
      show_root_heading: true

::: pkgforge.config.UserAccount
    options:
      show_root_heading: true

::: pkgforge.config.GroupAccount
    options:
      show_root_heading: true

::: pkgforge.config.load
    options:
      show_root_heading: true
