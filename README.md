# Orion SoC Explorer MCP

A lightweight Python implementation that exposes the Orion SoC design through an agent-friendly service and an MCP server.

## Architecture

```text
Scenario Runner → ExplorerAdapter ─┐
                                   ├→ ExplorerService → MiniEda → Orion JSON
MCP Client → stdio MCP Server ─────┘
```

`ExplorerService` contains the shared business logic, so both the adapter and MCP server expose the same behavior.

## Tools

| Tool              | Purpose                          |
| ----------------- | -------------------------------- |
| `list_components` | List/filter SoC components       |
| `list_links`      | List/filter links                |
| `get_component`   | Get component by ID              |
| `get_link`        | Get link by ID                   |
| `search`          | Search components and links      |
| `validate_design` | Detect design/graph issues       |
| `update_property` | Safely update element properties |
| `export_report`   | Generate text/JSON design report |

All direct tool calls return a consistent envelope:

```json
{"ok": true, "result": {}}
```

or:

```json
{"ok": false, "error": {"type": "...", "code": "...", "message": "..."}}
```

## Validation

The supplied Orion design intentionally contains two graph findings:

* `L7`: dangling destination `sram_l2`
* `L_loop`: self-loop `r0 → r0`

The service detects both in addition to the existing `MiniEda` validation rules.

## Search

Smart search is the default.

```json
{"query": "r0"}
```

matches `r0` and its connected links without incorrectly matching `ddr0`.

Use `"match_mode": "substring"` for broader substring matching.

## Safe Updates

`update_property` supports:

| Mode             | Memory | Disk |
| ---------------- | -----: | ---: |
| `dry_run: true`  |     No |   No |
| `persist: false` |    Yes |   No |
| `persist: true`  |    Yes |  Yes |

Persistent writes require explicit opt-in and are blocked for the canonical Orion JSON file.

## MCP

Run the MCP server:

```bash
python -u -m soc_explorer.mcp_server
```

Communication uses newline-delimited **JSON-RPC 2.0 over stdin/stdout**.

Supported MCP operations:

* `initialize`
* `notifications/initialized`
* `ping`
* `tools/list`
* `tools/call`

## Tests

Run:

```bash
python -m unittest discover -s tests -v
python -m pytest -q
python scripts/run_required_scenarios.py
python examples/mcp_client_demo.py
```

The tests cover service behavior, validation, search, safe updates, persistence protection, JSON-RPC errors, and real stdio MCP tool discovery/calls.

## Project Structure

```text
mini_eda/                     Existing design API
soc_explorer/service.py       Shared tool/service logic
soc_explorer/adapter.py       Scenario-runner adapter
soc_explorer/mcp_server.py    MCP JSON-RPC stdio server
examples/mcp_client_demo.py   End-to-end MCP client demo
tests/                        Service and MCP tests
artifacts/                    Generated test/demo evidence
```

## Key Design Principle

`MiniEda` and the Orion JSON remain the source of truth. The explorer adds validation, safe mutation policies, consistent tool contracts, and MCP access without duplicating the underlying design model.
