"""One way to run an external tool for ``install``: a failure becomes a
:class:`~pkgforge.exceptions.PkgForgeError` carrying the tool's last stderr line."""

from __future__ import annotations

import subprocess
import typing

from ..exceptions import PkgForgeError


def _run_tool(
    argv: typing.Sequence[str],
    *,
    what: str,
    stdin: typing.Optional[typing.IO] = None,
    stdout: typing.Optional[typing.IO] = None,
) -> None:
    """Run ``argv`` to completion, or raise :class:`PkgForgeError` starting
    with ``what`` (e.g. ``cannot extract app.tgz``).

    ``stdin`` is ``None`` (the tool reads nothing) or the stream a ``-``
    source feeds it; ``stdout`` is ``None`` (discarded) or the file the tool's
    output goes to. stderr is captured and its last line names the failure.
    No timeout: decompressing or extracting a large archive can legitimately
    take minutes.
    """
    proc = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL if stdin is None else stdin,
        stdout=subprocess.DEVNULL if stdout is None else stdout,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        # bsdtar ends every failure with this summary; the line before it
        # names the cause.
        if len(lines) > 1 and "Error exit delayed" in lines[-1]:
            lines.pop()
        detail = f": {lines[-1]}" if lines else ""
        raise PkgForgeError(
            f"{what}: {argv[0]} failed (exit {proc.returncode}){detail}"
        )
