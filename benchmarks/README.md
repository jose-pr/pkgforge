# Benchmarks

`run.py` times the operations pkgforge users spend the most wall-clock time on
-- loading a file DB, rendering a packaging manifest, scanning a build root,
staging a tree and building a package -- and prints (and optionally records) a
comparable JSON result.

## Reproduce

```sh
pip install -e ".[dev]"
python benchmarks/run.py            # print a summary only
python benchmarks/run.py --save     # also write benchmarks/results/<name>.json
python benchmarks/run.py --name foo # custom result name
```

Without `--name`, a result is named `pkgforge-<version>-py<major><minor>`.

## Metrics

Each id below is one entry under the result's `metrics` map. `db.*` and
`dump.*` time an isolated function call on an in-memory `DB_SIZE`-entry DB;
`fs.walk_baseline`, `scan.*`, `install.*` and `package.*` work on a real,
on-disk tree.

| id | what it times |
| --- | --- |
| `db.load_jsonl` | loading a `DB_SIZE`-entry DB through the JSON Lines backend (`JSONLinesDb.load`) |
| `db.load_yaml` | loading the same DB through the YAML backend (`YAMLDb.load`) |
| `db.load_sqlite` | loading the same DB through the SQLite backend (`SQLiteDb.load`) |
| `dump.rpmspecfiles` | rendering the RPM `%files` lines for the DB entries (`RPMSpecFiles.render`) |
| `dump.debian` | rendering the Debian `install`/`dirs`/`fixperms` and other per-package files for the DB entries (`DebianFiles.render`) |
| `fs.walk_baseline` | a bare `os.walk` over the scan tree: the filesystem floor `scan.cmd_*` is read against |
| `scan.cmd_jsonl` | `Project.scan` end to end against a jsonl DB: walk the tree, resolve each entry, append it |
| `scan.cmd_yaml` | the same scan against the yaml DB backend |
| `scan.cmd_sqlite` | the same scan against the sqlite DB backend, inside one batch that commits every 1000 rows and on exit |
| `scan.cmd_auto_owner` | the same jsonl scan with `--owner=-- --group=--`, so every entry also resolves owner and group from disk (`pwd`/`grp` lookups) |
| `install.tree_copy` | `Project.install` staging an `INSTALL_TREE_SIZE`-file directory tree with `--method copy` (the default) |
| `install.tree_link` | the same tree staged with `--method link` (hardlink, a copy for each file it cannot span) |
| `install.tree_move` | the same tree staged with `--method move` (the whole-tree rename) |
| `install.tree_record` | the same tree staged with `copy` and `--record-tree`, so every child is also walked and recorded; compare against `install.tree_copy` from the same run for the walk's cost |
| `package.tar` | `Project.package("tar", compression="gz")` over a `PACKAGE_TREE_SIZE`-file build root: read the tree, write and compress the tar, rename it into place |
| `package.deb` | the same build as a `.deb` (`control.tar` and `data.tar` in an ar archive, gzip) |

A `scan.cmd_*` call builds a fresh `Project` and re-creates its DB for every
timed call, so the DB never grows across samples. Compare two results on
**median**, not mean or min: a single `timeit` average hides run-to-run noise.

## JSON schema

Top level:

```
name, pkgforge_version, python, platform, processor, timestamp,
db_size, scan_tree_size, iterations, metrics
```

`iterations`: `load_inner, render_inner, scan_inner, scan_cmd_inner, repeat` --
the sample counts `run.py` used (see `sample()` in `run.py`). The `install.*`
and `package.*` counts (`INSTALL_TREE_INNER`, `PACKAGE_INNER`) and tree sizes
(`INSTALL_TREE_SIZE`, `PACKAGE_TREE_SIZE`, `PACKAGE_FILE_BYTES`) are constants in
`run.py`, not recorded in the result.

`metrics.<id>`: `{median_ms, min_ms, max_ms}` -- ms per call, over `repeat`
samples.

## Caveats

- **CI is the source of truth.** A local run is a sanity check: laptop noise,
  thermal throttling and background load make it unsuitable for a before/after
  comparison. Only a CI-taken result is committed under a CI name (see Naming).
- **The storage under `TMPDIR` changes the sqlite and install numbers.**
  `scan.cmd_sqlite` pays an fsync-backed commit every 1000 rows and on exit,
  and `install.*` and `package.*` write real files; tmpfs makes both nearly
  free. Compare them only between runs on the same kind of storage.
- **Scan cost depends on the host NSS stack.** `scan.cmd_auto_owner` resolves
  every entry's owner and group via `pwd`/`grp`; that cost varies with the
  host's `nsswitch.conf` (e.g. `group: files [SUCCESS=merge] systemd`) and can
  be near-invisible on a CI runner's minimal configuration.
- **Results older than a metric lack it.** The 0.1.2 baselines predate the
  `install.*` and `package.*` metrics, and their `scan.cmd_sqlite` committed
  once per row.

## Naming

A committed result's file stem always equals its JSON `name` field.

- `pkgforge-<version>-ci-py<X.Y>` -- an ordinary CI-taken result.
- `pkgforge-<version>-baseline-ci-py<X.Y>` -- a CI-taken result committed as
  the before/after baseline for a specific piece of work.
- `pkgforge-<version>-baseline-local-py<X.Y>` -- a **local** sanity run,
  recorded because no CI-taken baseline was available. Never used as
  before/after evidence for a performance claim (see Caveats); superseded by
  the matching `-baseline-ci-` file once one is recorded.

A local run is never committed under a `-ci-` name.

## Recording a CI result

1. Dispatch `test.yml` with the `benchmark` input set to `true` (optionally
   against a specific `ref`): `gh workflow run test.yml --ref <ref> -f benchmark=true`.
2. Wait for the run, then download its artifacts: the run uploads one
   `benchmark-py<ver>` artifact per matrix entry (`3.9` and `3.14`), each
   holding `benchmarks/results/*.json`, the committed files included; take only
   the freshly produced `pkgforge-ci-py<ver>.json`.
3. Rewrite that file's `name` field to the stem you intend to commit it as
   (see Naming) and save it under `benchmarks/results/<name>.json`.
4. Commit it, and list it below under Baselines.

## Baselines

Baselines are CI-taken (see Recording above), so a before/after comparison
shares one runner and interpreter with its baseline; a local run is a sanity
check only.

- `pkgforge-0.1.2-baseline-local-py3.14.json` -- local sanity run, pkgforge
  0.1.2, a developer machine (Linux, aarch64). Not CI-comparable.
- `pkgforge-0.1.2-baseline-local-py3.9.json` -- same, py3.9.
- `pkgforge-0.1.2-baseline-ci-py3.14.json` -- CI-taken baseline, pkgforge 0.1.2
  code before the scan and DB performance fixes, `ubuntu-latest`, CPython
  3.14.7. Use this for before/after comparisons.
- `pkgforge-0.1.2-baseline-ci-py3.9.json` -- same, CPython 3.9.25.
