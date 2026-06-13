"""(De)serialization of pass-1 results (M4).

``update`` re-parses only changed files; everything else is re-linked from
the per-unit :class:`~hdl_kgraph.parser.base.FileIR` persisted at build time
(the linked graph cannot be decomposed back into IRs — resolution discards
the :class:`~hdl_kgraph.parser.base.UnresolvedRef` form). The codec is plain
JSON: enums by value, tuples as arrays (restored on decode), ``attrs``
verbatim — the same canonicalization the SQLite store applies, so a decoded
IR links to an identical graph.

Macro events (``\\`define``/``\\`undef`` in textual order, including ones
from spliced headers) are encoded alongside so an unchanged unit's effect on
the shared :class:`~hdl_kgraph.parser.preprocessor.MacroTable` can be
replayed without re-preprocessing it.
"""

from __future__ import annotations

import json
from typing import Any

from hdl_kgraph.parser.base import FileIR, UnresolvedRef
from hdl_kgraph.parser.preprocessor import MacroDef, MacroEvent
from hdl_kgraph.schema import Edge, EdgeKind, Language, Node, NodeKind


def ir_to_json(ir: FileIR) -> str:
    return json.dumps(
        {
            "path": ir.path,
            "nodes": [_encode_node(n) for n in ir.nodes],
            "local_edges": [_encode_edge(e) for e in ir.local_edges],
            "unresolved_refs": [_encode_ref(r) for r in ir.unresolved_refs],
            "parse_error_count": ir.parse_error_count,
            "parse_errors": ir.parse_errors,
        },
        sort_keys=True,
        default=list,
    )


def ir_from_json(text: str) -> FileIR:
    data = json.loads(text)
    return FileIR(
        path=data["path"],
        nodes=[_decode_node(n) for n in data["nodes"]],
        local_edges=[_decode_edge(e) for e in data["local_edges"]],
        unresolved_refs=[_decode_ref(r) for r in data["unresolved_refs"]],
        parse_error_count=data["parse_error_count"],
        parse_errors=data.get("parse_errors", []),
    )


def macro_events_to_json(events: list[MacroEvent]) -> str:
    return json.dumps(
        [
            {
                "op": ev.op,
                "name": ev.name,
                "macro": None if ev.macro is None else _encode_macro(ev.macro),
            }
            for ev in events
        ]
    )


def macro_events_from_json(text: str) -> list[MacroEvent]:
    return [
        MacroEvent(
            op=item["op"],
            name=item["name"],
            macro=None if item["macro"] is None else _decode_macro(item["macro"]),
        )
        for item in json.loads(text)
    ]


def _encode_node(node: Node) -> dict[str, Any]:
    return {
        "id": node.id,
        "kind": node.kind.value,
        "name": node.name,
        "qualified_name": node.qualified_name,
        "file": node.file,
        "line_span": list(node.line_span),
        "language": node.language.value,
        "attrs": node.attrs,
    }


def _decode_node(data: dict[str, Any]) -> Node:
    return Node(
        id=data["id"],
        kind=NodeKind(data["kind"]),
        name=data["name"],
        qualified_name=data["qualified_name"],
        file=data["file"],
        line_span=tuple(data["line_span"]),
        language=Language(data["language"]),
        attrs=data["attrs"],
    )


def _encode_edge(edge: Edge) -> dict[str, Any]:
    return {
        "src": edge.src,
        "dst": edge.dst,
        "kind": edge.kind.value,
        "confidence": edge.confidence,
        "attrs": edge.attrs,
    }


def _decode_edge(data: dict[str, Any]) -> Edge:
    return Edge(
        src=data["src"],
        dst=data["dst"],
        kind=EdgeKind(data["kind"]),
        confidence=data["confidence"],
        attrs=data["attrs"],
    )


def _encode_ref(ref: UnresolvedRef) -> dict[str, Any]:
    return {
        "edge_kind": ref.edge_kind.value,
        "src_id": ref.src_id,
        "target_name": ref.target_name,
        "line_span": list(ref.line_span),
        "attrs": ref.attrs,
        "confidence": ref.confidence,
    }


def _decode_ref(data: dict[str, Any]) -> UnresolvedRef:
    return UnresolvedRef(
        edge_kind=EdgeKind(data["edge_kind"]),
        src_id=data["src_id"],
        target_name=data["target_name"],
        line_span=tuple(data["line_span"]),
        attrs=data["attrs"],
        confidence=data["confidence"],
    )


def _encode_macro(macro: MacroDef) -> dict[str, Any]:
    return {
        "name": macro.name,
        "params": macro.params if macro.params is None else [list(p) for p in macro.params],
        "body": macro.body,
        "file": macro.file,
        "line": macro.line,
    }


def _decode_macro(data: dict[str, Any]) -> MacroDef:
    params = data["params"]
    return MacroDef(
        name=data["name"],
        params=params if params is None else [(p[0], p[1]) for p in params],
        body=data["body"],
        file=data["file"],
        line=data["line"],
    )
