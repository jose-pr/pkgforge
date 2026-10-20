# Project (`project`)

The Python API for build scripts: stage files into a build root, record their
install metadata in a file DB and dump it into packaging manifests. The
`pkgforge` command line is a wrapper over this class.

```python
from pkgforge import Project

project = Project("build/root", "build/files.jsonl")
project.init()
project.install(["dist/app"], "/usr/bin", mode="755", parents=True)
project.scan("/etc")
project.dump("rpm", "build/files.txt")
```

::: pkgforge.project.Project
    options:
      show_root_heading: true
