"""fastmcp server over the knowledge graph (M6).

Design notes:

* **Read-only by construction** — only :class:`SqliteStore.load` is ever
  called; the build/update pipeline is never imported, so neither stdio nor
  HTTP mode can mutate the database.
* **Staleness** — the database may be rewritten by ``update``/``watch``
  while the server runs. Every tool call stats the file and reloads only
  when ``(mtime_ns, size)`` changed; a stat is cheap, a reload is not.
* **LLM-sized responses** — list-returning tools wrap results in a
  ``{total, offset, count, truncated, items}`` envelope with a clamped
  ``limit``; the hierarchy tool caps depth and node count and reports what
  it omitted.
* The ``_impl`` functions hold all logic and are testable without fastmcp;
  :func:`create_server` only wraps them in typed closures.
"""

from __future__ import annotations

import dataclasses
import enum
from collections import Counter, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import networkx as nx

from hdl_kgraph.graph import analysis, clocks, uvm
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import FileMeta, SchemaVersionError, SqliteStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastmcp import FastMCP

MAX_LIMIT = 500


class McpUnavailableError(RuntimeError):
    """fastmcp is not installed (the ``[mcp]`` extra)."""


class GraphContext:
    """Lazily loads the graph, reloading when the database file changes."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._signature: tuple[int, int] | None = None
        self._loaded: tuple[nx.MultiDiGraph, list[FileMeta], dict[str, str]] | None = None

    def graph(self) -> tuple[nx.MultiDiGraph, list[FileMeta], dict[str, str]]:
        try:
            stat = self.db_path.stat()
        except FileNotFoundError:
            raise RuntimeError(
                f"graph database not found: {self.db_path}; run `hdl-kgraph build` first"
            ) from None
        signature = (stat.st_mtime_ns, stat.st_size)
        if self._loaded is None or signature != self._signature:
            try:
                self._loaded = SqliteStore(self.db_path).load()
            except SchemaVersionError as exc:
                raise RuntimeError(str(exc)) from exc
            self._signature = signature
        return self._loaded


def _jsonable(value: Any) -> Any:
    """Recursively convert enums/dataclasses/tuples to JSON-safe values."""
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _page(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
    """The pagination envelope every list-returning tool uses."""
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    page = items[offset : offset + limit]
    return {
        "total": len(items),
        "offset": offset,
        "count": len(page),
        "truncated": offset + len(page) < len(items),
        "items": _jsonable(page),
    }


def _resolve_unit(g: nx.MultiDiGraph, name: str) -> list[str]:
    """Non-stub instantiable units named *name* (VHDL case-insensitive)."""
    return [
        node_id
        for node_id, data in g.nodes(data=True)
        if data["kind"] in analysis.INSTANTIABLE_KINDS
        and not data["attrs"].get("unresolved")
        and data["name"] == (name.lower() if data["language"] is Language.VHDL else name)
    ]


def _find_module_impl(g: nx.MultiDiGraph, name: str, limit: int) -> dict[str, Any]:
    matches = analysis.search_nodes(g, name=name, kinds=list(analysis.INSTANTIABLE_KINDS))
    for record in matches:
        unit_id = record["id"]
        counts = Counter(
            g.nodes[child]["kind"]
            for _, child, d in g.out_edges(unit_id, data=True)
            if d["kind"] is EdgeKind.DECLARES
        )
        record["port_count"] = counts[NodeKind.PORT]
        record["parameter_count"] = counts[NodeKind.PARAMETER]
        record["instantiation_count"] = sum(
            1 for _, _, d in g.in_edges(unit_id, data=True) if d["kind"] is EdgeKind.INSTANTIATES
        )
    return _page(matches, limit, 0)


def _count_tree(node: dict[str, Any]) -> int:
    return 1 + sum(_count_tree(child) for child in node["children"])


def _prune_tree(root: dict[str, Any], max_nodes: int) -> int:
    """Keep the first *max_nodes* nodes in BFS order; return how many were cut."""
    budget = max_nodes - 1
    omitted = 0
    queue = deque([root])
    while queue:
        node = queue.popleft()
        kept: list[dict[str, Any]] = []
        for child in node["children"]:
            if budget > 0:
                budget -= 1
                kept.append(child)
                queue.append(child)
            else:
                omitted += _count_tree(child)
        if len(kept) < len(node["children"]):
            node["truncated"] = True
            node["children"] = kept
    return omitted


def _get_hierarchy_impl(
    g: nx.MultiDiGraph, top: str | None, depth: int, max_nodes: int
) -> dict[str, Any]:
    if top is None:
        tops = [
            {
                "name": g.nodes[node_id]["name"],
                "file": g.nodes[node_id]["file"],
                "kind": g.nodes[node_id]["kind"].value,
            }
            for node_id in analysis.find_top_modules(g)
        ]
        return {"tops": tops, "hint": "call again with top=<name> for the tree"}
    roots = _resolve_unit(g, top)
    if not roots:
        raise ValueError(f"no module or entity named {top!r} in the graph")
    tree = _jsonable(analysis.hierarchy_tree(g, roots[0], max_depth=max(1, depth)))
    omitted = _prune_tree(tree, max(1, max_nodes))
    return {"root": tree, "nodes_omitted": omitted}


def _impact_impl(
    g: nx.MultiDiGraph,
    files: list[FileMeta],
    target: str,
    max_depth: int,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    seeds = analysis.impact_seeds(g, files, target)
    if not seeds:
        raise ValueError(
            f"{target!r} matches no file or design unit in the graph; "
            "try search_nodes to find the right name"
        )
    records = analysis.impact_radius(g, seeds, max_depth=max_depth)
    summary = {
        "affected_units": len(records),
        "affected_files": len({r.file for r in records if r.file}),
        "by_kind": dict(Counter(r.kind.value for r in records)),
        "max_depth_seen": max((r.depth for r in records), default=0),
    }
    return {"target": target, "seed_count": len(seeds), "summary": summary} | _page(
        records, limit, offset
    )


def _clock_domains_impl(g: nx.MultiDiGraph) -> dict[str, Any]:
    domains = [
        {
            "clock": d.clock_names[0] if d.clock_names else d.clock_id,
            "aliases": d.clock_names,
            "process_count": len(d.process_ids),
            "signal_count": len(d.signal_ids),
            "min_confidence": d.min_confidence,
        }
        for d in clocks.clock_domains(g)
    ]
    suspects = clocks.cdc_suspects(g)
    return {
        "domains": domains,
        "cdc_suspect_count": len(suspects),
        "cdc_suspects": _jsonable(suspects[:50]),
    }


def _uvm_impl(g: nx.MultiDiGraph) -> dict[str, Any]:
    return {
        "components": _jsonable(uvm.uvm_topology(g)),
        "test_covers": _jsonable(uvm.test_covers(g)),
    }


def _search_impl(
    g: nx.MultiDiGraph,
    name: str,
    kinds: list[str] | None,
    file: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    kind_enums: list[NodeKind] | None = None
    if kinds:
        try:
            kind_enums = [NodeKind(k) for k in kinds]
        except ValueError:
            valid = ", ".join(sorted(k.value for k in NodeKind))
            raise ValueError(f"unknown node kind in {kinds!r}; valid kinds: {valid}") from None
    return _page(analysis.search_nodes(g, name=name, kinds=kind_enums, file=file), limit, offset)


def _port_map_impl(g: nx.MultiDiGraph, module: str, instance: str | None) -> dict[str, Any]:
    units = analysis.port_map(g, module, instance=instance)
    if not units:
        raise ValueError(f"no module or entity named {module!r} in the graph")
    for unit in units:
        instances = unit.get("instances")
        if instances is not None and len(instances) > 20:
            unit["instances"] = instances[:20]
            unit["instances_truncated"] = len(instances) - 20
    return {"units": _jsonable(units)}


_INSTRUCTIONS = """\
Knowledge graph of an HDL design (SystemVerilog/Verilog/VHDL), extracted by
hdl-kgraph. Edges carry a confidence score: 1.0 = syntactically resolved,
0.8 = unique cross-file name match, 0.6 = ambiguous match, 0.4 = naming
heuristic. VHDL names are stored lowercase and match case-insensitively.
Unresolved references appear as stub nodes flagged `unresolved`. The server
is read-only; rebuild the database with `hdl-kgraph build`/`update`.
Start with get_hierarchy() or find_module() to orient yourself.
"""


def create_server(db_path: Path) -> FastMCP:
    """Build the fastmcp server with all nine tools over *db_path*."""
    try:
        from fastmcp import FastMCP
    except ImportError:
        raise McpUnavailableError(
            "fastmcp is not installed; install the MCP extra: pip install 'hdl-kgraph[mcp]'"
        ) from None

    ctx = GraphContext(db_path)
    mcp: FastMCP = FastMCP(name="hdl-kgraph", instructions=_INSTRUCTIONS)

    @mcp.tool
    def find_module(name: str, limit: int = 20) -> dict[str, Any]:
        """Find design units (modules/entities/interfaces) by exact name or glob
        pattern, with port/parameter/instantiation counts."""
        g, _, _ = ctx.graph()
        return _find_module_impl(g, name, limit)

    @mcp.tool
    def get_hierarchy(
        top: str | None = None, depth: int = 3, max_nodes: int = 500
    ) -> dict[str, Any]:
        """Design hierarchy. Without `top`, lists the top-level (never
        instantiated) units; with `top`, returns the instance tree below it,
        capped at `depth` levels and `max_nodes` nodes."""
        g, _, _ = ctx.graph()
        return _get_hierarchy_impl(g, top, depth, max_nodes)

    @mcp.tool
    def who_instantiates(name: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """All instantiation sites of the design unit named `name`."""
        g, _, _ = ctx.graph()
        return _page(analysis.instances_of(g, name), limit, offset)

    @mcp.tool
    def port_map(module: str, instance: str | None = None) -> dict[str, Any]:
        """Ports and parameters of `module` in declaration order; with
        `instance`, also that instance's port connection bindings."""
        g, _, _ = ctx.graph()
        return _port_map_impl(g, module, instance)

    @mcp.tool
    def impact_of_change(
        target: str, max_depth: int = 0, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """What breaks if `target` (a file path or design-unit name) changes:
        summary plus the transitively affected units, nearest first.
        `max_depth` 0 means unlimited."""
        g, files, _ = ctx.graph()
        return _impact_impl(g, files, target, max_depth, limit, offset)

    @mcp.tool
    def clock_domains() -> dict[str, Any]:
        """Clock domains (with alias nets and process/signal counts) and
        clock-domain-crossing suspects."""
        g, _, _ = ctx.graph()
        return _clock_domains_impl(g)

    @mcp.tool
    def find_signal_drivers(
        signal: str,
        module: str | None = None,
        readers: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """What drives (or, with readers=true, reads) signals named `signal`,
        optionally only inside design unit `module`."""
        g, _, _ = ctx.graph()
        return _page(
            analysis.signal_drivers(g, signal, module=module, readers=readers), limit, offset
        )

    @mcp.tool
    def uvm_topology() -> dict[str, Any]:
        """UVM components by role (via EXTENDS chains to uvm_* bases) and
        testbench-to-DUT TEST_COVERS links."""
        g, _, _ = ctx.graph()
        return _uvm_impl(g)

    @mcp.tool
    def search_nodes(
        name: str = "*",
        kinds: list[str] | None = None,
        file: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search graph nodes by name glob, node kind (e.g. module, signal,
        class), and/or file glob."""
        g, _, _ = ctx.graph()
        return _search_impl(g, name, kinds, file, limit, offset)

    return mcp
