"""In-process entry point used by the required-scenarios runner.

The runner intentionally does not start an MCP process.  This adapter therefore
does almost no work: it forwards calls to the exact same ``ExplorerService``
that the MCP server uses.  Keeping it thin prevents two subtly different tool
contracts from emerging.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .service import DEFAULT_DESIGN, ExplorerService


class ExplorerAdapter:
    """Small compatibility wrapper around the shared explorer service."""

    def __init__(
        self,
        design_path: str | Path | None = None,
        *,
        allow_persist: bool = False,
    ) -> None:
        self.service = ExplorerService(
            design_path or DEFAULT_DESIGN, allow_persist=allow_persist
        )
        # Compatibility convenience for a reader migrating from the starter.
        # Tool calls still go through ``service`` so this is not a second path.
        self.eda = self.service.eda

    def list_tools(self) -> list[dict[str, Any]]:
        return self.service.list_tools()

    def call(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.service.call(tool_name, arguments)


# Keeping the old name as an alias is harmless for anyone who imported the
# starter template, while ``get_adapter`` now returns the real implementation.
TemplateAdapter = ExplorerAdapter


def get_adapter() -> ExplorerAdapter:
    """Factory imported by ``scripts/run_required_scenarios.py``."""

    # Persistent writes remain off for the canonical Orion fixture.  Scenario
    # S6 uses persist=false and proves the separate in-memory session behavior.
    return ExplorerAdapter()
