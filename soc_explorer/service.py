"""The shared, agent-facing tool layer for the SoC Design Explorer.

This module is intentionally dependency-free and deliberately small.  Both the
in-process scenario adapter and the stdio MCP server call :class:`ExplorerService`,
so there is one place to understand tool behavior, input checks, and errors.

``MiniEda`` remains the owner of loaded design state and persistence.  The
service adds a safer public contract on top of that API; it does not create a
second design model or parser.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

from mini_eda import MiniEda, NotFoundError, ValidationError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESIGN = ROOT / "data" / "designs" / "orion_soc.json"


class ToolError(Exception):
    """An expected tool failure with a stable, JSON-friendly error shape."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        error_type: str = "ValidationError",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.error_type = error_type
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "type": self.error_type,
            "code": self.code,
            "message": str(self),
        }
        if self.details:
            error["details"] = copy.deepcopy(self.details)
        return error


# The catalog is data rather than framework registration.  It is returned by
# the adapter and converted directly to MCP's ``tools/list`` response.
TOOL_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "name": "list_components",
        "description": "List components, optionally filtered by their exact kind.",
        "mutates": False,
        "arity": 0,
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Optional exact component kind, for example 'router'.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_links",
        "description": "List directed links, optionally filtered by exact source and/or destination id.",
        "mutates": False,
        "arity": 0,
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "Optional exact source id."},
                "dst": {"type": "string", "description": "Optional exact destination id."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_component",
        "description": "Get one component, including all of its properties.",
        "mutates": False,
        "arity": 1,
        "inputSchema": {
            "type": "object",
            "properties": {"component_id": {"type": "string"}},
            "required": ["component_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_link",
        "description": "Get one directed link, including its endpoints and properties.",
        "mutates": False,
        "arity": 1,
        "inputSchema": {
            "type": "object",
            "properties": {"link_id": {"type": "string"}},
            "required": ["link_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search",
        "description": "Search components and links with a case-insensitive smart or substring match.",
        "mutates": False,
        "arity": 1,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "scope": {
                    "type": "string",
                    "enum": ["all", "components", "links"],
                    "default": "all",
                },
                "match_mode": {
                    "type": "string",
                    "enum": ["smart", "substring"],
                    "default": "smart",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "validate_design",
        "description": "Check link endpoints, self-loops, and MiniEda's bandwidth rules.",
        "mutates": False,
        "arity": 0,
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "update_property",
        "description": "Update one dotted property path. Defaults to a non-persistent session update.",
        "mutates": True,
        "arity": 3,
        "inputSchema": {
            "type": "object",
            "properties": {
                "element_id": {"type": "string"},
                "prop_path": {
                    "type": "string",
                    "description": "Dotted path below properties, for example 'qos' or 'limits.read'.",
                },
                "value": {
                    "description": "Any finite JSON value to store at prop_path.",
                },
                "persist": {
                    "type": "boolean",
                    "default": False,
                    "description": "Write to disk only when this service was explicitly allowed to persist.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "Return the proposed change without changing memory or disk.",
                },
            },
            "required": ["element_id", "prop_path", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "export_report",
        "description": "Create a compact report containing inventory and validation findings.",
        "mutates": False,
        "arity": 0,
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["text", "json"],
                    "default": "text",
                }
            },
            "additionalProperties": False,
        },
    },
)


_PATH_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MISSING = object()


class ExplorerService:
    """A small, predictable tool façade over one ``MiniEda`` instance.

    ``persist=False`` intentionally changes this service's in-memory session:
    a later get call sees the new value, while the JSON fixture stays untouched.
    Persistent writes are disabled by default, particularly for the canonical
    teaching fixture.  A caller must opt in with ``allow_persist=True`` and
    pass ``persist=True`` for an on-disk update.
    """

    def __init__(
        self,
        design_path: str | Path | None = None,
        *,
        allow_persist: bool = False,
    ) -> None:
        self.design_path = Path(design_path or DEFAULT_DESIGN).resolve()
        if allow_persist and self.design_path == DEFAULT_DESIGN.resolve():
            raise ValueError(
                "Refusing persistent writes to the canonical Orion fixture; "
                "copy the JSON design first."
            )
        self.allow_persist = allow_persist
        self.eda = MiniEda(self.design_path)
        self._session_revision = 0

    def list_tools(self) -> list[dict[str, Any]]:
        """Return copies so a caller cannot alter the service's schemas."""

        return copy.deepcopy(list(TOOL_CATALOG))

    def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a named tool and normalize every expected failure.

        This is the contract used by *both* the direct adapter and the MCP
        transport.  Tool-level errors are data, not uncaught exceptions, which
        makes them straightforward for an agent to inspect.
        """

        try:
            if not isinstance(tool_name, str) or not tool_name:
                raise ToolError("INVALID_TOOL_NAME", "tool_name must be a non-empty string")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ToolError("INVALID_ARGUMENTS", "arguments must be a JSON object")
            result = self._dispatch(tool_name, arguments)
            return {"ok": True, "result": _json_copy(result)}
        except ToolError as exc:
            return {"ok": False, "error": exc.to_dict()}
        except NotFoundError as exc:
            return {
                "ok": False,
                "error": ToolError(
                    "NOT_FOUND", str(exc), error_type="NotFoundError"
                ).to_dict(),
            }
        except ValidationError as exc:
            return {
                "ok": False,
                "error": ToolError(
                    "EDA_VALIDATION_ERROR", str(exc), error_type="ValidationError"
                ).to_dict(),
            }
        except Exception:  # noqa: BLE001 - boundary must not crash an MCP request
            # Do not leak tracebacks or filesystem details through an agent tool.
            return {
                "ok": False,
                "error": ToolError(
                    "INTERNAL_ERROR",
                    "The explorer could not complete this request.",
                    error_type="InternalError",
                ).to_dict(),
            }

    def _dispatch(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "list_components": self._list_components,
            "list_links": self._list_links,
            "get_component": self._get_component,
            "get_link": self._get_link,
            "search": self._search,
            "validate_design": self._validate_design,
            "update_property": self._update_property,
            "export_report": self._export_report,
        }
        try:
            handler = handlers[tool_name]
        except KeyError as exc:
            raise ToolError(
                "UNKNOWN_TOOL",
                f"Unknown tool: {tool_name}",
                error_type="UnknownToolError",
                details={"tool_name": tool_name},
            ) from exc
        return handler(arguments)

    def _list_components(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        _check_keys(arguments, {"kind"})
        kind = _optional_nonempty_string(arguments, "kind")
        return [_copy_element(component) for component in self.eda.list_components(kind=kind)]

    def _list_links(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        _check_keys(arguments, {"src", "dst"})
        src = _optional_nonempty_string(arguments, "src")
        dst = _optional_nonempty_string(arguments, "dst")
        return [_copy_element(link) for link in self.eda.list_links(src=src, dst=dst)]

    def _get_component(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _check_keys(arguments, {"component_id"})
        component_id = _required_nonempty_string(arguments, "component_id")
        try:
            return _copy_element(self.eda.get_component(component_id))
        except NotFoundError as exc:
            raise ToolError(
                "NOT_FOUND",
                f"No component exists with id '{component_id}'.",
                error_type="NotFoundError",
                details={"element_type": "component", "element_id": component_id},
            ) from exc

    def _get_link(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _check_keys(arguments, {"link_id"})
        link_id = _required_nonempty_string(arguments, "link_id")
        try:
            return _copy_element(self.eda.get_link(link_id))
        except NotFoundError as exc:
            raise ToolError(
                "NOT_FOUND",
                f"No link exists with id '{link_id}'.",
                error_type="NotFoundError",
                details={"element_type": "link", "element_id": link_id},
            ) from exc

    def _search(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        _check_keys(arguments, {"query", "scope", "match_mode"})
        query = _required_nonempty_string(arguments, "query")
        scope = _enum_argument(arguments, "scope", {"all", "components", "links"}, "all")
        match_mode = _enum_argument(arguments, "match_mode", {"smart", "substring"}, "smart")
        query_folded = query.casefold()
        hits: list[dict[str, Any]] = []

        if scope in {"all", "components"}:
            for component in self.eda.list_components():
                matched_fields = _component_matches(component, query_folded, match_mode)
                if matched_fields:
                    hits.append(
                        {
                            "type": "component",
                            "matched_fields": matched_fields,
                            **_copy_element(component),
                        }
                    )

        if scope in {"all", "links"}:
            for link in self.eda.list_links():
                matched_fields = _link_matches(link, query_folded, match_mode)
                if matched_fields:
                    hits.append(
                        {
                            "type": "link",
                            "matched_fields": matched_fields,
                            **_copy_element(link),
                        }
                    )

        # A fixed order makes transcripts and automated results easy to compare.
        return sorted(hits, key=lambda hit: (hit["type"], hit["id"]))

    def _validate_design(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _check_keys(arguments, set())
        issues: list[dict[str, Any]] = []

        # Preserve MiniEda's existing numeric-bandwidth checks rather than
        # duplicating or replacing them in the explorer layer.
        for issue in self.eda.validate():
            issues.append({**issue.to_dict(), "details": {}})

        component_ids = set(self.eda.design.components)
        for link in self.eda.list_links():
            if link.src not in component_ids:
                issues.append(
                    _issue(
                        "DANGLING_LINK_SOURCE",
                        "error",
                        link.id,
                        f"Link {link.id} references missing source component '{link.src}'.",
                        endpoint="src",
                        component_id=link.src,
                    )
                )
            if link.dst not in component_ids:
                issues.append(
                    _issue(
                        "DANGLING_LINK_DESTINATION",
                        "error",
                        link.id,
                        f"Link {link.id} references missing destination component '{link.dst}'.",
                        endpoint="dst",
                        component_id=link.dst,
                    )
                )
            if link.src == link.dst:
                issues.append(
                    _issue(
                        "SELF_LOOP",
                        "warning",
                        link.id,
                        f"Link {link.id} starts and ends at '{link.src}'; review whether this loop is intentional.",
                        component_id=link.src,
                    )
                )

        issues.sort(key=lambda issue: (issue["code"], issue["element_id"] or ""))
        error_count = sum(issue["severity"] == "error" for issue in issues)
        warning_count = sum(issue["severity"] == "warning" for issue in issues)
        return {
            "valid": error_count == 0,
            "summary": {
                "issue_count": len(issues),
                "error_count": error_count,
                "warning_count": warning_count,
            },
            "issues": issues,
        }

    def _update_property(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _check_keys(arguments, {"element_id", "prop_path", "value", "persist", "dry_run"})
        element_id = _required_nonempty_string(arguments, "element_id")
        prop_path = _valid_property_path(arguments)
        if "value" not in arguments:
            raise ToolError("MISSING_ARGUMENT", "Missing required argument: value")
        value = copy.deepcopy(arguments["value"])
        if not _is_json_value(value):
            raise ToolError(
                "INVALID_VALUE",
                "value must be a finite JSON value (objects need string keys).",
            )
        persist = _boolean_argument(arguments, "persist", False)
        dry_run = _boolean_argument(arguments, "dry_run", False)
        if persist and dry_run:
            raise ToolError(
                "INVALID_ARGUMENT_COMBINATION",
                "persist and dry_run cannot both be true.",
            )
        element, element_type = self._find_element(element_id)
        _ensure_property_path_does_not_replace_a_scalar(element.properties, prop_path)
        previous_value = _read_property_path(element.properties, prop_path)
        before = None if previous_value is _MISSING else copy.deepcopy(previous_value)
        changed = previous_value is _MISSING or previous_value != value

        if dry_run:
            return {
                "element_id": element_id,
                "element_type": element_type,
                "property_path": prop_path,
                "property_existed": previous_value is not _MISSING,
                "before": before,
                "after": value,
                "changed": changed,
                "applied": False,
                "persisted": False,
                "mode": "dry_run",
                "session_revision": self._session_revision,
            }

        if persist and not self.allow_persist:
            raise ToolError(
                "PERSISTENCE_DISABLED",
                "Persistent writes are disabled for this explorer instance. "
                "Use a copied design with explicit allow_persist=True.",
                error_type="PermissionError",
            )

        before_file_sha256 = _file_sha256(self.design_path)
        try:
            updated = self.eda.update_property(
                element_id, prop_path, value, persist=persist
            )
        except NotFoundError as exc:
            # The element was resolved above, but preserve a stable answer if
            # an underlying implementation changes between the two calls.
            raise ToolError(
                "NOT_FOUND", str(exc), error_type="NotFoundError"
            ) from exc
        except ValidationError as exc:
            raise ToolError("INVALID_PROPERTY_PATH", str(exc)) from exc

        self._session_revision += 1
        after_file_sha256 = _file_sha256(self.design_path)
        return {
            "element_id": element_id,
            "element_type": element_type,
            "property_path": prop_path,
            "property_existed": previous_value is not _MISSING,
            "before": before,
            "after": value,
            "changed": changed,
            "applied": True,
            "persisted": persist,
            "mode": "persistent" if persist else "session",
            "session_revision": self._session_revision,
            "file_sha256_before": before_file_sha256,
            "file_sha256_after": after_file_sha256,
            "element": copy.deepcopy(updated),
        }

    def _export_report(self, arguments: dict[str, Any]) -> str | dict[str, Any]:
        _check_keys(arguments, {"format"})
        fmt = _enum_argument(arguments, "format", {"text", "json"}, "text")
        validation = self._validate_design({})
        components_by_kind: dict[str, int] = {}
        for component in self.eda.list_components():
            components_by_kind[component.kind] = components_by_kind.get(component.kind, 0) + 1

        fully_connected_links = sum(
            link.src in self.eda.design.components and link.dst in self.eda.design.components
            for link in self.eda.list_links()
        )
        report = {
            "design": {
                "name": self.eda.design.name,
                "version": self.eda.design.version,
                "description": self.eda.design.description,
            },
            "inventory": {
                "component_count": len(self.eda.design.components),
                "link_count": len(self.eda.design.links),
                "components_by_kind": dict(sorted(components_by_kind.items())),
                "fully_connected_link_count": fully_connected_links,
            },
            "validation": validation,
        }
        if fmt == "json":
            return report

        issue_lines = [
            f"- {issue['severity'].upper()} {issue['code']} ({issue['element_id']}): "
            f"{issue['message']}"
            for issue in validation["issues"]
        ]
        return "\n".join(
            [
                f"Design report: {report['design']['name']} (v{report['design']['version']})",
                report["design"]["description"],
                f"Components: {report['inventory']['component_count']} "
                + "(" + ", ".join(
                    f"{kind}={count}"
                    for kind, count in report["inventory"]["components_by_kind"].items()
                ) + ")",
                f"Links: {report['inventory']['link_count']} "
                f"({report['inventory']['fully_connected_link_count']} with existing endpoints)",
                "Validation: "
                f"{validation['summary']['error_count']} error(s), "
                f"{validation['summary']['warning_count']} warning(s).",
                "Findings:",
                *(issue_lines or ["- None"]),
            ]
        )

    def _find_element(self, element_id: str) -> tuple[Any, str]:
        if element_id in self.eda.design.components:
            return self.eda.get_component(element_id), "component"
        if element_id in self.eda.design.links:
            return self.eda.get_link(element_id), "link"
        raise ToolError(
            "NOT_FOUND",
            f"No component or link exists with id '{element_id}'.",
            error_type="NotFoundError",
            details={"element_id": element_id},
        )


def _check_keys(arguments: dict[str, Any], allowed: set[str]) -> None:
    unexpected = sorted(set(arguments) - allowed)
    if unexpected:
        raise ToolError(
            "UNEXPECTED_ARGUMENT",
            "Unexpected argument(s): " + ", ".join(unexpected),
            details={"unexpected": unexpected, "allowed": sorted(allowed)},
        )


def _required_nonempty_string(arguments: dict[str, Any], name: str) -> str:
    if name not in arguments:
        raise ToolError("MISSING_ARGUMENT", f"Missing required argument: {name}")
    value = arguments[name]
    if not isinstance(value, str) or not value.strip():
        raise ToolError("INVALID_ARGUMENT", f"{name} must be a non-empty string")
    return value.strip()


def _optional_nonempty_string(arguments: dict[str, Any], name: str) -> str | None:
    if name not in arguments:
        return None
    return _required_nonempty_string(arguments, name)


def _enum_argument(
    arguments: dict[str, Any], name: str, allowed: set[str], default: str
) -> str:
    if name not in arguments:
        return default
    value = arguments[name]
    if not isinstance(value, str) or value not in allowed:
        raise ToolError(
            "INVALID_ARGUMENT",
            f"{name} must be one of: " + ", ".join(sorted(allowed)),
        )
    return value


def _boolean_argument(arguments: dict[str, Any], name: str, default: bool) -> bool:
    if name not in arguments:
        return default
    value = arguments[name]
    # ``bool('false')`` is true in Python, so type checking is significant here.
    if not isinstance(value, bool):
        raise ToolError("INVALID_ARGUMENT", f"{name} must be a boolean")
    return value


def _valid_property_path(arguments: dict[str, Any]) -> str:
    prop_path = _required_nonempty_string(arguments, "prop_path")
    parts = prop_path.split(".")
    if not all(_PATH_SEGMENT.fullmatch(part) for part in parts):
        raise ToolError(
            "INVALID_PROPERTY_PATH",
            "prop_path must be a dotted path made of letters, digits, and underscores; "
            "each segment must start with a letter or underscore.",
        )
    return prop_path


def _copy_element(element: Any) -> dict[str, Any]:
    """``to_dict`` only shallow-copies properties; tool replies need deep copies."""

    return copy.deepcopy(element.to_dict())


def _json_copy(value: Any) -> Any:
    """Return an independent JSON-compatible copy and assert our contract."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ToolError("INTERNAL_ERROR", "Tool returned a non-JSON result.") from exc


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _read_property_path(properties: dict[str, Any], path: str) -> Any:
    current: Any = properties
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _ensure_property_path_does_not_replace_a_scalar(
    properties: dict[str, Any], path: str
) -> None:
    """Reject ``qos.level`` when ``qos`` is already a string.

    The supplied MiniEda helper creates dictionaries for missing intermediate
    paths. That is useful for a new path, but replacing an existing scalar
    would silently discard data, so the explorer treats it as a controlled
    update conflict instead.
    """

    current: Any = properties
    for part in path.split(".")[:-1]:
        if part not in current:
            return
        current = current[part]
        if not isinstance(current, dict):
            raise ToolError(
                "PROPERTY_PATH_CONFLICT",
                f"Cannot create '{path}' because '{part}' is already a non-object property.",
                details={"property_path": path, "conflicting_segment": part},
            )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _issue(
    code: str,
    severity: str,
    element_id: str,
    message: str,
    **details: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "element_id": element_id,
        "message": message,
        "details": details,
    }


def _component_matches(component: Any, query: str, mode: str) -> list[str]:
    fields: list[tuple[str, Any]] = [
        ("id", component.id),
        ("name", component.name),
        ("kind", component.kind),
        *[(f"properties.{key}", value) for key, value in component.properties.items()],
    ]
    return _matched_fields(fields, query, mode, id_field="id")


def _link_matches(link: Any, query: str, mode: str) -> list[str]:
    fields: list[tuple[str, Any]] = [
        ("id", link.id),
        ("src", link.src),
        ("dst", link.dst),
        *[(f"properties.{key}", value) for key, value in link.properties.items()],
    ]
    return _matched_fields(fields, query, mode, id_field="id", endpoint_fields={"src", "dst"})


def _matched_fields(
    fields: list[tuple[str, Any]],
    query: str,
    mode: str,
    *,
    id_field: str,
    endpoint_fields: set[str] | None = None,
) -> list[str]:
    """Implement the documented search policy and explain each hit's reason."""

    matches: list[str] = []
    for field_name, raw_value in fields:
        value = str(raw_value).casefold()
        if mode == "substring":
            if query in value:
                matches.append(field_name)
            continue

        # Smart mode avoids the surprising ``r0`` -> ``ddr0`` substring hit.
        # IDs match exactly or as prefixes; link endpoints also match exactly.
        if field_name == id_field and (value == query or value.startswith(query)):
            matches.append(field_name)
        elif endpoint_fields and field_name in endpoint_fields and value == query:
            matches.append(field_name)
        elif field_name not in {id_field, *(endpoint_fields or set())} and query in value:
            matches.append(field_name)
    return matches
