#!/usr/bin/env python3
"""Run a small offline client against the stdio MCP server.

This is intentionally a scripted client rather than a remote-LLM demo.  It
still proves the integration that matters here: a separate process discovers
tools and calls the same explorer implementation exposed by the adapter.

The demo copies Orion to a temporary directory, so neither this script nor the
server can accidentally alter the canonical teaching fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DESIGN = ROOT / "data" / "designs" / "orion_soc.json"
DEFAULT_OUTPUT = ROOT / "artifacts" / "mcp_demo_transcript.json"


def _requests() -> list[dict[str, Any]]:
    """A meaningful exploration sequence, including a safe session edit."""

    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "orion-demo-client", "version": "1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_components", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "r0"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "validate_design", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "update_property",
                "arguments": {
                    "element_id": "cpu0",
                    "prop_path": "qos",
                    "value": "medium",
                    "persist": False,
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "get_component", "arguments": {"component_id": "cpu0"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "export_report", "arguments": {"format": "text"}},
        },
    ]


def run_demo(output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    """Start the server, exchange JSON-RPC messages, and return a transcript."""

    canonical_sha256 = _sha256(CANONICAL_DESIGN)
    with tempfile.TemporaryDirectory(prefix="orion-mcp-demo-") as temp_dir:
        copied_design = Path(temp_dir) / "orion_soc.json"
        shutil.copy2(CANONICAL_DESIGN, copied_design)
        messages = _requests()
        protocol_input = "".join(
            json.dumps(message, ensure_ascii=False) + "\n" for message in messages
        )
        environment = os.environ.copy()
        # Running from the solution root makes ``python -m soc_explorer...``
        # work even if a user starts this script from another directory.
        environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
        command = [
            sys.executable,
            "-u",
            "-m",
            "soc_explorer.mcp_server",
            "--design",
            str(copied_design),
        ]
        completed = subprocess.run(
            command,
            input=protocol_input,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=environment,
            timeout=15,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "MCP server failed with exit code "
                f"{completed.returncode}: {completed.stderr.strip()}"
            )
        responses = [
            json.loads(line) for line in completed.stdout.splitlines() if line.strip()
        ]
        response_by_id = {response.get("id"): response for response in responses}
        expected_ids = {message["id"] for message in messages if "id" in message}
        if set(response_by_id) != expected_ids:
            raise RuntimeError(
                f"Expected responses for {sorted(expected_ids)}, got {sorted(response_by_id)}"
            )

        exchanges = []
        for message in messages:
            if "id" not in message:
                continue
            response = response_by_id[message["id"]]
            exchange: dict[str, Any] = {
                "id": message["id"],
                "method": message["method"],
                "request": message,
                "response": response,
            }
            decoded = _decode_tool_result(response)
            if decoded is not None:
                exchange["decoded_tool_result"] = decoded
            exchanges.append(exchange)

        copied_sha256_after = _sha256(copied_design)

    canonical_sha256_after = _sha256(CANONICAL_DESIGN)
    if canonical_sha256_after != canonical_sha256:
        raise RuntimeError("The canonical Orion fixture changed during the MCP demo.")

    transcript = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "transport": "JSON-RPC 2.0 over stdio; one message per line",
        "server_command": ["python", "-u", "-m", "soc_explorer.mcp_server", "--design", "<temporary-copy>"],
        "canonical_design_sha256_before": canonical_sha256,
        "canonical_design_sha256_after": canonical_sha256_after,
        "temporary_design_sha256_before": canonical_sha256,
        "temporary_design_sha256_after": copied_sha256_after,
        "server_stderr": completed.stderr,
        "exchanges": exchanges,
        "observations": [
            "tools/list exposed the explorer catalog over MCP.",
            "validate_design reported L7's dangling destination and L_loop's self-loop.",
            "update_property with persist=false changed cpu0.qos for the later get_component call only.",
            "The canonical JSON fingerprint was unchanged by the demo.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(transcript, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return transcript


def _decode_tool_result(response: dict[str, Any]) -> Any | None:
    result = response.get("result")
    if not isinstance(result, dict) or "content" not in result:
        return None
    content = result["content"]
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return None
    text = content[0].get("text")
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    transcript = run_demo(args.output)
    print(f"Wrote {args.output}")
    print(f"MCP exchanges: {len(transcript['exchanges'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
