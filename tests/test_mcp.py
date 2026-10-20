"""``pkgforge`` served over MCP: standard input is the protocol channel, so a
command that would read it must fail with an error result and leave its
output untouched."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


class _Server:
    def __init__(self, cwd):
        env = dict(os.environ, PKGFORGE_MCP="stdio")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "pkgforge"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            text=True,
        )
        self._id = 0
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        self.rpc("notifications/initialized", notify=True)

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        return json.loads(self.proc.stdout.readline())

    def call(self, tool, arguments):
        reply = self.rpc(
            "tools/call", {"name": "pkgforge." + tool, "arguments": arguments}
        )
        result = reply["result"]
        text = " ".join(part.get("text", "") for part in result.get("content", []))
        return bool(result.get("isError")), text

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()


@pytest.fixture
def server(tmp_path):
    srv = _Server(tmp_path)
    try:
        yield srv
    finally:
        srv.close()


def test_dbdump_db_dash_is_refused_and_output_kept(server, tmp_path):
    output = tmp_path / "files.txt"
    output.write_text("keep me\n")
    is_error, text = server.call(
        "dbdump", {"format": "rpmspecfiles", "db": "-", "output": "files.txt"}
    )
    assert is_error
    assert "standard input is not available in an MCP tool call" in text
    assert output.read_text() == "keep me\n"


def test_install_stdin_source_is_refused(server, tmp_path):
    (tmp_path / "root").mkdir()
    is_error, text = server.call(
        "install",
        {
            "db": "d.jsonl",
            "buildroot": "root",
            "source": ["-"],
            "destination": "/z",
            "parents": True,
            "no_target_directory": True,
        },
    )
    assert is_error
    assert "standard input is not available in an MCP tool call" in text
    assert "AttributeError" not in text
    assert not (tmp_path / "root" / "z").exists()


def test_every_command_is_served_as_a_tool(server):
    reply = server.rpc("tools/list")
    names = {tool["name"] for tool in reply["result"]["tools"]}
    wanted = {
        f"pkgforge.{name}"
        for name in (
            "install",
            "scan",
            "initdb",
            "compact",
            "dbdump",
            "mkdir",
            "symlink",
        )
    }
    assert wanted <= names
