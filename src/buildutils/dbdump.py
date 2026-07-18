"""``dbdump`` subcommand: render the file DB into packaging manifests.

Two shapes of format are supported:

* **per-entry** formats (``rpmspecfiles``) render one line per DB entry and are
  written to a single output stream (a file, or ``-`` for stdout);
* **multi-artifact** formats (``debian``) render several related files (an
  ``install`` path list plus a ``permissions`` manifest) into an output
  *directory* -- or, when the output is ``-``, concatenated to stdout under
  ``# === <name> ===`` section headers.
"""

from __future__ import annotations

import json
import os
import sys
import typing
from pathlib import Path

import duho

from .common import BuildUtil, FileEntry
from .exclude import PathMatch, PathMatchStmt

#: An entry that survived filtering: (db-path, FileEntry).
Entries = typing.List[typing.Tuple[str, FileEntry]]


class PerEntryDumper(typing.Protocol):
    """Render a single DB entry to one line of bytes."""

    def __call__(self, path: str, entry: FileEntry) -> bytes: ...


# --------------------------------------------------------------------------
# rpm (per-entry)
# --------------------------------------------------------------------------


def rpmspecfile(path: str, entry: FileEntry) -> bytes:
    prefix = entry["meta"].get("rpmprefix") or ""
    if prefix:
        prefix += " "
    if entry["type"] == "directory":
        prefix += "%dir "

    return (
        f"{prefix}%attr({entry['mode']},{entry['owner']},{entry['group']}) "
        f"{json.dumps(path)}\n"
    ).encode()


# --------------------------------------------------------------------------
# debian (multi-artifact: install list + permissions manifest)
# --------------------------------------------------------------------------


def _debian_artifacts(entries: "Entries") -> "typing.Dict[str, bytes]":
    """Build Debian packaging artifacts from surviving DB entries.

    Returns a mapping of artifact filename -> bytes:

    * ``install`` -- ``dh_install``-style lines ``<src>  <dest-dir>`` (the source
      is the build-root-relative path, the destination is the entry's parent
      directory), one per non-directory entry;
    * ``permissions`` -- ``<path> <mode> <owner> <group>`` lines
      (``dpkg-statoverride``-friendly) for every entry that pins a non-default
      mode/owner/group.
    """
    install_lines: "typing.List[str]" = []
    perm_lines: "typing.List[str]" = []
    for path, entry in entries:
        rel = path.lstrip("/")
        if entry["type"] != "directory":
            dest_dir = os.path.dirname(rel)
            install_lines.append(f"{rel} {dest_dir}".rstrip())
        mode, owner, group = entry["mode"], entry["owner"], entry["group"]
        if mode not in ("-", "") or owner != "-" or group != "-":
            perm_lines.append(f"{path} {mode} {owner} {group}")

    def _join(lines: "typing.List[str]") -> bytes:
        return ("\n".join(lines) + "\n" if lines else "").encode()

    return {"install": _join(install_lines), "permissions": _join(perm_lines)}


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

#: Per-entry line formats: name -> dumper.
PER_ENTRY_FORMATS: "typing.Dict[str, PerEntryDumper]" = {"rpmspecfiles": rpmspecfile}

#: Multi-artifact formats: name -> (entries -> {filename: bytes}).
MULTI_ARTIFACT_FORMATS: "typing.Dict[str, typing.Callable[[Entries], typing.Dict[str, bytes]]]" = {
    "debian": _debian_artifacts,
}


def dump_formats() -> "typing.List[str]":
    """All known format names, sorted (for --help / error messages)."""
    return sorted([*PER_ENTRY_FORMATS, *MULTI_ARTIFACT_FORMATS])


class DbDump(BuildUtil):
    """Dump the file DB into a packaging manifest (rpm or debian)."""

    _parsername_ = "dbdump"

    exclude: duho.Arg[
        typing.List[PathMatchStmt],
        duho.NS(type=PathMatchStmt.parse, action="append"),
    ] = []
    ("--exclude", "-X")
    format: str
    ("--format", "-f")
    output: Path = Path("-")
    ("output",)

    def _surviving_entries(self) -> "Entries":
        db = self.loaddb()
        filter = PathMatch(self.exclude)
        entries: "Entries" = []
        for path, entry in db.items():
            if entry is None or (self.exclude and filter.match(Path(path), entry)):
                continue
            entries.append((path, entry))
        return entries

    def __call__(self):
        entries = self._surviving_entries()

        if self.format in PER_ENTRY_FORMATS:
            dumper = PER_ENTRY_FORMATS[self.format]
            out = None
            try:
                if str(self.output) == "-":
                    out = os.fdopen(sys.stdout.fileno(), "wb", closefd=False)
                else:
                    out = self.output.open("wb")
                for path, entry in entries:
                    out.write(dumper(path, entry))
            finally:
                if out:
                    out.flush()
                    if str(self.output) != "-":
                        out.close()
            return

        if self.format in MULTI_ARTIFACT_FORMATS:
            artifacts = MULTI_ARTIFACT_FORMATS[self.format](entries)
            if str(self.output) == "-":
                out = os.fdopen(sys.stdout.fileno(), "wb", closefd=False)
                try:
                    for name, data in artifacts.items():
                        out.write(f"# === {name} ===\n".encode())
                        out.write(data)
                    out.flush()
                finally:
                    pass  # do not close the shared stdout fd
            else:
                self.output.mkdir(parents=True, exist_ok=True)
                for name, data in artifacts.items():
                    (self.output / name).write_bytes(data)
                    self._logger_.info("Wrote %s", self.output / name)
            return

        raise SystemExit(
            f"unknown format {self.format!r}; choose from {', '.join(dump_formats())}"
        )


DbDump._register()
