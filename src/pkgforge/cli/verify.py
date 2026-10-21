"""``verify``: check the build root against the file DB."""

from __future__ import annotations

import json

import duho

from ._common import ExcludeArgs, ProjectCmd, _open_stdout

__all__ = ["Verify"]

#: The problems that carry both sides, printed as ``recorded R, found F``.
_CHANGED = ("type", "target")


class Verify(ExcludeArgs, ProjectCmd):
    """Check the build root against the file DB: unrecorded, missing and changed paths"""

    _parsername_ = "verify"
    _logger_name_ = "pkgforge.verify"

    format: duho.Arg[str, duho.Meta(choices=("text", "json"))] = "text"
    "output: text (one line per issue) or json"
    ("--format", "-f")

    def __call__(self) -> int:
        issues = self._project().verify(exclude=self.exclude)
        if self.format == "json":
            text = json.dumps({"issues": [issue._asdict() for issue in issues]}) + "\n"
        else:
            text = "".join(
                (
                    f"{i.problem} {i.path}: recorded {i.recorded}, found {i.found}\n"
                    if i.problem in _CHANGED
                    else f"{i.problem} {i.path}\n"
                )
                for i in issues
            )
        if text:
            with _open_stdout() as out:
                out.write(text.encode("utf-8", "surrogateescape"))
        return 1 if issues else 0
