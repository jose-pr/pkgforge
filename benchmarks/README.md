# Benchmarks

`run.py` times the operations pkgforge users spend the most wall-clock time on
-- loading a file DB, rendering a packaging manifest, and scanning a build
root -- and prints (and optionally records) a comparable JSON result.

## Reproduce

```sh
pip install -e ".[dev]"
python benchmarks/run.py            # print a summary only
python benchmarks/run.py --save     # also write benchmarks/results/<name>.json
python benchmarks/run.py --name foo # custom result name
```

## Metrics

Each id below is one entry under the result's `metrics` map. `db.*` and
`dump.*` measure an isolated function call; `fs.walk_baseline` and `scan.cmd_*`
measure against a real, on-disk build-root tree.

| id | what it times |
| --- | --- |
| `db.load_jsonl` | Loading a `DB_SIZE`-entry DB through the JSON Lines backend (`JsonlDb.load`) |
| `db.load_yaml` | Loading the same DB through the YAML backend (`YamlDb.load`) |
| `db.load_sqlite` | Loading the same DB through the SQLite backend (`SqliteDb.load`) |
| `dump.rpmspecfiles` | Rendering an RPM `%files` spec fragment for the DB entries (`RpmSpecFiles.render(entries)`, the same batch call `PerEntryFormat.dump` runs) |
| `dump.debian` | Rendering the Debian `install`/`permissions` manifests for the DB entries |
| `fs.walk_baseline` | A bare `os.walk` over the scan tree -- the filesystem floor `scan.cmd_*` is read against |
| `scan.cmd_jsonl` | `ScanCmd.__call__` end to end against a jsonl DB: walk the tree, resolve each entry, append it |
| `scan.cmd_yaml` | The same scan against the yaml DB backend |
| `scan.cmd_sqlite` | The same scan against the sqlite DB backend (one fsync-backed commit per row today) |
| `scan.cmd_auto_owner` | The same jsonl scan with `--owner=-- --group=--`, so every entry also resolves owner/group from disk (`pwd`/`grp` lookups) |
| `install.tree_copy` | `Install.__call__` staging a synthetic directory tree with `--method copy` (the default) |
| `install.tree_link` | The same tree staged with `--method link` (hardlink, falling back to a copy per file it can't span) |
| `install.tree_move` | The same tree staged with `--method move` (whole-tree rename fast path) |
| `install.tree_record` | The same tree staged with the plain `copy` method and `--record-tree` set, so every child is also walked and recorded -- compare against `install.tree_copy` (same run) for the walk's own added cost |

Compare two results on **median**, not mean or min -- a single `timeit`
average hides real run-to-run noise, median does not.

## JSON schema

Top level:

```
name, pkgforge_version, python, platform, processor, timestamp,
db_size, scan_tree_size, iterations, metrics
```

`iterations`: `load_inner, render_inner, scan_inner, scan_cmd_inner, repeat` --
the sample counts `run.py` used for this result (see `sample()` in `run.py`).

`metrics.<id>`: `{median_ms, min_ms, max_ms}` -- ms per call, over `repeat`
samples.

## Caveats

- **CI is the source of truth.** A local run is a sanity check: laptop noise,
  thermal throttling and background load make it unsuitable for a real
  before/after comparison. Only a CI-taken result is used as a committed
  baseline (see Naming and Recording below).
- **A tmpfs `TMPDIR` hides the sqlite commit cost.** `scan.cmd_sqlite` commits
  once per scanned entry; that cost is dominated by `fsync`, which is visible
  on a real disk and close to free on tmpfs. Compare `scan.cmd_sqlite` only
  between runs with the same kind of backing storage.
- **Scan cost depends on the host NSS stack.** `scan.cmd_auto_owner` resolves
  every entry's owner/group via `pwd`/`grp`; that lookup's cost varies with
  the host's `nsswitch.conf` (e.g. `group: files [SUCCESS=merge] systemd`) and
  may be near-invisible on a CI runner's minimal configuration.

## Naming

A committed result's file stem always equals its JSON `name` field.

- `pkgforge-<version>-ci-py<X.Y>` -- an ordinary CI-taken result.
- `pkgforge-<version>-baseline-ci-py<X.Y>` -- a CI-taken result committed as
  the before/after baseline for a specific piece of work.
- `pkgforge-<version>-baseline-local-py<X.Y>` -- a **local** sanity run,
  recorded because no CI-taken baseline was available yet. Never used as
  before/after evidence for a performance claim (see Caveats); superseded by
  the matching `-baseline-ci-` file once one is recorded.

## Recording a CI result

1. Dispatch `test.yml` with the `benchmark` input set to `true` (optionally
   against a specific `ref`): `gh workflow run test.yml --ref <ref> -f benchmark=true`.
2. Wait for the run, then download its artifacts: the run uploads one
   `benchmark-py<ver>` artifact per matrix entry (today `3.9` and `3.14`),
   each holding `benchmarks/results/*.json` -- once a baseline is committed,
   that means every already-tracked file too, so take only the freshly
   produced `pkgforge-ci-py<ver>.json`.
3. Rewrite that file's `name` field to the stem you intend to commit it as
   (see Naming) and save it under `benchmarks/results/<name>.json`.
4. Commit it, and list it below under Baselines.

## Baselines

Baselines are normally CI-taken (see Recording above) so a before/after
comparison shares one runner and interpreter with its baseline; a local run
is a sanity check only (see Caveats) and is never used as before/after
evidence for a performance claim.

- `pkgforge-0.1.2-baseline-local-py3.14.json` -- local sanity run, pkgforge
  0.1.2, a developer machine (Linux, aarch64). Not CI-comparable.
- `pkgforge-0.1.2-baseline-local-py3.9.json` -- same, py3.9.
- `pkgforge-0.1.2-baseline-ci-py3.14.json` -- CI-taken baseline, pkgforge 0.1.2
  code before the scan/DB performance fixes, `ubuntu-latest`, CPython 3.14.7.
  Use this for before/after comparisons.
- `pkgforge-0.1.2-baseline-ci-py3.9.json` -- same, CPython 3.9.25.
