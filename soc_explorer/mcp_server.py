"""A minimal, dependency-free MCP-compatible server over JSON-RPC stdio.

It implements the useful MCP subset for this exercise:

* ``initialize`` and ``notifications/initialized``
* ``ping``
* ``tools/list``
* ``tools/call``

One JSON-RPC message is read from each stdin line and one response (when a
request has an id) is written to stdout.  Diagnostic messages must go to stderr
so they cannot corrupt the protocol stream.  The service behind this transport
is the same one used by ``soc_explorer.adapter``.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from .service import DEFAULT_DESIGN, ExplorerService


SERVER_NAME = "orion-soc-explorer-easy"
SERVER_VERSION = "1.0.0"
# These versions cover common MCP hosts while keeping the implementation small.
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]


class StdioMcpServer:
    """Translate a JSON-RPC request into the shared explorer service contract."""

    def __init__(self, service: ExplorerService) -> None:
        self.service = service
        self.initialized = False

    def handle(self, request: Any) -> dict[str, Any] | None:
        """Handle one decoded JSON message.

        A return value of ``None`` means this was a valid notification and
        JSON-RPC requires no reply.
        """

        if not isinstance(request, dict):
            return _rpc_error(None, -32600, "Invalid Request: expected an object.")
        if request.get("jsonrpc") != "2.0":
            return _rpc_error(request.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'.")

        is_notification = "id" not in request
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str) or not method:
            return _rpc_error(request_id, -32600, "Invalid Request: method must be a non-empty string.")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "Invalid params: params must be an object.")

        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method.startswith("notifications/"):
            # Unknown notifications are safely ignored, as a small server has
            # no progress, logging, or cancellation implementation.
            return None

        try:
            result = self._dispatch(method, params)
        except RpcRequestError as exc:
            response = _rpc_error(request_id, exc.code, exc.message, exc.data)
        except Exception:  # noqa: BLE001 - never take down the protocol loop
            response = _rpc_error(request_id, -32603, "Internal server error.")
        else:
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}

        # A malformed *request* still receives an error, while ordinary
        # notifications above deliberately receive no response.
        return None if is_notification else response

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested_version = params.get("protocolVersion")
            protocol_version = (
                requested_version
                if isinstance(requested_version, str)
                and requested_version in SUPPORTED_PROTOCOL_VERSIONS
                else DEFAULT_PROTOCOL_VERSION
            )
            return {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Explore the local Orion SoC. Start with validate_design; "
                    "use persist=false (the default) for safe session-only edits."
                ),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            # MCP wants exactly this schema field spelling.  ``arity`` and
            # ``mutates`` are useful harmless extensions for simple clients.
            return {"tools": self.service.list_tools()}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not name:
                raise RpcRequestError(-32602, "Invalid params: name must be a non-empty string.")
            if not isinstance(arguments, dict):
                raise RpcRequestError(-32602, "Invalid params: arguments must be an object.")
            tool_response = self.service.call(name, arguments)
            return _mcp_tool_result(tool_response)
        raise RpcRequestError(-32601, f"Method not found: {method}")


class RpcRequestError(Exception):
    """A protocol-level invalid request, distinct from a tool-level failure."""

    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        self.code = code
        self.message = message
        self.data = data


def _mcp_tool_result(tool_response: dict[str, Any]) -> dict[str, Any]:
    """Represent a normal explorer error as an MCP tool result, not RPC failure."""

    if tool_response["ok"]:
        payload = copy.deepcopy(tool_response["result"])
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                }
            ],
            # ``structuredContent`` is optional in MCP, but lets modern hosts
            # consume data without reparsing the text block.
            "structuredContent": {"result": payload},
        }

    error = copy.deepcopy(tool_response["error"])
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(error, ensure_ascii=False, sort_keys=True),
            }
        ],
        "isError": True,
    }


def _rpc_error(
    request_id: Any, code: int, message: str, data: Any | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def serve(service: ExplorerService) -> int:
    """Run the newline-delimited JSON-RPC loop until stdin is closed."""

    server = StdioMcpServer(service)
    for line_number, raw_line in enumerate(sys.stdin, start=1):
        if not raw_line.strip():
            continue
        try:
            request = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            response = _rpc_error(None, -32700, "Parse error.", {"line": line_number, "detail": exc.msg})
        else:
            response = server.handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Orion SoC Explorer MCP server over stdio.")
    parser.add_argument(
        "--design",
        type=Path,
        default=DEFAULT_DESIGN,
        help="Path to a JSON design (defaults to the protected Orion fixture).",
    )
    parser.add_argument(
        "--allow-persist",
        action="store_true",
        help="Allow persist=true only for a non-canonical copied design.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    design_path = args.design.resolve()
    if args.allow_persist and design_path == DEFAULT_DESIGN.resolve():
        print(
            "Refusing --allow-persist for the canonical Orion fixture. "
            "Copy it first and pass --design /path/to/copy.json.",
            file=sys.stderr,
        )
        return 2
    try:
        service = ExplorerService(design_path, allow_persist=args.allow_persist)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Could not load design {design_path}: {exc}", file=sys.stderr)
        return 2
    return serve(service)


if __name__ == "__main__":
    raise SystemExit(main())
