"""Wire-level tests for the dependency-free JSON-RPC stdio server."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from soc_explorer.mcp_server import StdioMcpServer
from soc_explorer.service import ExplorerService


ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "data" / "designs" / "orion_soc.json"


class McpServerTests(unittest.TestCase):
    def test_in_process_protocol_errors_are_json_rpc_errors(self) -> None:
        server = StdioMcpServer(ExplorerService(DESIGN))
        parse_like_error = server.handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        self.assertEqual(parse_like_error["error"]["code"], -32600)
        unknown_method = server.handle({"jsonrpc": "2.0", "id": 2, "method": "no/such/method"})
        self.assertEqual(unknown_method["error"]["code"], -32601)
        self.assertIsNone(
            server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_stdio_server_discovers_and_calls_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            copied_design = Path(temp_dir) / "orion_soc.json"
            shutil.copy2(DESIGN, copied_design)
            messages: list[dict[str, Any]] = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "validate_design", "arguments": {}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "get_component",
                        "arguments": {"component_id": "does_not_exist"},
                    },
                },
            ]
            completed = _run_server(messages, copied_design)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = {
            response["id"]: response
            for response in (json.loads(line) for line in completed.stdout.splitlines())
        }
        self.assertEqual(set(responses), {1, 2, 3, 4})
        self.assertEqual(responses[1]["result"]["serverInfo"]["name"], "orion-soc-explorer-easy")
        tool_names = {tool["name"] for tool in responses[2]["result"]["tools"]}
        self.assertIn("validate_design", tool_names)

        validation = _decode_tool_content(responses[3])
        self.assertIn(
            "DANGLING_LINK_DESTINATION", {issue["code"] for issue in validation["issues"]}
        )
        self.assertTrue(responses[4]["result"]["isError"])
        missing = _decode_tool_content(responses[4])
        self.assertEqual(missing["code"], "NOT_FOUND")

    def test_server_refuses_to_enable_persistence_for_canonical_file(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "soc_explorer.mcp_server",
                "--allow-persist",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=_environment(),
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Refusing --allow-persist", completed.stderr)


def _run_server(messages: list[dict[str, Any]], design_path: Path) -> subprocess.CompletedProcess[str]:
    protocol_input = "".join(json.dumps(message) + "\n" for message in messages)
    return subprocess.run(
        [
            sys.executable,
            "-u",
            "-m",
            "soc_explorer.mcp_server",
            "--design",
            str(design_path),
        ],
        input=protocol_input,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        env=_environment(),
        timeout=10,
        check=False,
    )


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    return environment


def _decode_tool_content(response: dict[str, Any]) -> Any:
    text = response["result"]["content"][0]["text"]
    return json.loads(text)
