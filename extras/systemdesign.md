# Orion SoC Explorer — System Design

## 1. Architecture

```text
Scenario Runner → Adapter ─────┐
                               ├→ ExplorerService → MiniEda → Orion JSON
MCP Client → MCP stdio Server ─┘
```

`ExplorerService` contains the shared tool logic, validation, errors, and update policy. Both the scenario runner and MCP server therefore expose the same behavior.

## 2. Responsibilities

| Component         | Responsibility                             |
| ----------------- | ------------------------------------------ |
| `MiniEda`         | Source of truth for design data            |
| `ExplorerService` | Tools, validation, search, updates, errors |
| `ExplorerAdapter` | In-process interface for scenarios         |
| `MCP Server`      | JSON-RPC/MCP interface over stdio          |
| `MCP Client Demo` | End-to-end subprocess integration proof    |

## 3. Tool Flow

```text
Client
  ↓
Adapter / MCP Server
  ↓
ExplorerService.call(name, arguments)
  ↓
Validate arguments
  ↓
MiniEda list / get / update
  ↓
{ok, result} or {ok:false, error}
```

The service normalizes expected failures into stable JSON rather than exposing Python exceptions.

## 4. Validation

`validate_design` combines existing `MiniEda` validation with graph checks.

The Orion fixture contains:

* `L7` → dangling destination `sram_l2` (**error**)
* `L_loop` → `r0 → r0` self-loop (**warning**)

## 5. Update Safety

| Mode             | Memory | Disk |
| ---------------- | -----: | ---: |
| `dry_run: true`  |     No |   No |
| `persist: false` |    Yes |   No |
| `persist: true`  |    Yes |  Yes |

`persist: false` changes the current service session, so later reads see the update.

Persistent writes require explicit permission and are blocked for the canonical Orion JSON.

## 6. MCP Boundary

```text
stdin JSON-RPC
      ↓
StdioMcpServer
      ↓
ExplorerService
      ↓
MiniEda
      ↓
stdout JSON-RPC
```

Supported operations include `initialize`, `ping`, `tools/list`, `tools/call`, and `notifications/initialized`.

Protocol errors use standard JSON-RPC errors. Tool-level failures are returned as MCP tool results with `isError: true`.

## 7. Testing

Tests cover:

* list/get/search
* graph validation
* argument and error handling
* dry-run/session/persistent updates
* canonical-file protection
* reports
* adapter/service integration
* JSON-RPC protocol errors
* real MCP subprocess discovery and tool calls

Generated artifacts provide scenario results, tool manifests, test output, and the MCP demo transcript.

## 8. Scope

This is intentionally a local teaching prototype. Production evolution could add authentication, authorization, concurrency control, transactions, audit logging, larger graph analysis, and additional MCP capabilities.
