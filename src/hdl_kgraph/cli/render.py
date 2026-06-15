"""Shared rendering helpers for the CLI and the MCP server (issue #70).

Both surfaces turn graph records (dicts with enum/dataclass/tuple values) into
JSON. They used to do it independently — ``_json_default``/``_emit_json`` in
``cli/main.py`` and ``_jsonable``/``_page`` in ``mcp/server.py`` — so this
module holds the one implementation both import.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from typing import Any

import click

from hdl_kgraph.graph import summary

#: Recursive JSON-safe conversion (enums → value, dataclasses → dict, tuples and
#: sets → lists). This is the build-time summary writer's converter, so a
#: precomputed summary and a live MCP response are byte-identical.
jsonable = summary.jsonable


def json_default(value: Any) -> Any:
    """``json.dumps`` ``default`` hook for the CLI's text/JSON commands."""
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return str(value)


def emit_json(payload: Any) -> None:
    """Print *payload* as indented JSON to stdout (the CLI ``--json`` convention)."""
    click.echo(json.dumps(payload, indent=2, default=json_default))


def page(items: list[Any], limit: int, offset: int, max_limit: int) -> dict[str, Any]:
    """The pagination envelope every list-returning MCP tool uses."""
    limit = max(1, min(limit, max_limit))
    offset = max(0, offset)
    chunk = items[offset : offset + limit]
    return {
        "total": len(items),
        "offset": offset,
        "count": len(chunk),
        "truncated": offset + len(chunk) < len(items),
        "items": jsonable(chunk),
    }
