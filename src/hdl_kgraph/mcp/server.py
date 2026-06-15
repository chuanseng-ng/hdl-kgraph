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

import sqlite3
from collections import Counter, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import networkx as nx

from hdl_kgraph.graph import analysis, summary
from hdl_kgraph.schema import EdgeKind, NodeKind
from hdl_kgraph.storage.query import GraphQuery
from hdl_kgraph.storage.sqlite_store import FileMeta, SchemaVersionError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastmcp import FastMCP

MAX_LIMIT = 500


class McpUnavailableError(RuntimeError):
    """fastmcp is not installed (the ``[mcp]`` extra)."""


class GraphContext:
    """Runs bounded, index-backed queries without ever loading the whole graph.

    Each tool call goes straight to :class:`~hdl_kgraph.storage.query.GraphQuery`,
    which hydrates only the subgraph the query touches (so a 10–100+ GB design no
    longer pays a full in-memory load per call). A fresh read connection per call
    means a concurrent ``update``/``watch`` swap is always observed — no caching
    or staleness check needed. :meth:`run` reproduces the previous full-load
    error contract (missing/busy/incompatible database).
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.query = GraphQuery(db_path)

    def run(self, fn: Any) -> Any:
        """Invoke ``fn(self.query)``, translating storage errors for the client."""
        try:
            self.db_path.stat()
        except FileNotFoundError:
            raise RuntimeError(
                f"graph database not found: {self.db_path}; run `hdl-kgraph build` first"
            ) from None
        try:
            return fn(self.query)
        except SchemaVersionError as exc:
            raise RuntimeError(str(exc)) from exc
        except sqlite3.OperationalError as exc:
            # A concurrent `update`/`watch` may be rebuilding the database.
            raise RuntimeError(
                f"graph database is busy ({exc}); a rebuild may be in progress — retry shortly"
            ) from exc


#: JSON-safe conversion (enums/dataclasses/tuples), shared with the build-time
#: summary writer so a precomputed summary and a live one are byte-identical.
_jsonable = summary.jsonable


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
    return summary.clock_summary(g)


def _uvm_impl(g: nx.MultiDiGraph) -> dict[str, Any]:
    return summary.uvm_summary(g)


def _validate_kinds(kinds: list[str] | None) -> list[NodeKind] | None:
    """Parse the ``search_nodes`` kind filter, with a helpful error on a typo."""
    if not kinds:
        return None
    try:
        return [NodeKind(k) for k in kinds]
    except ValueError:
        valid = ", ".join(sorted(k.value for k in NodeKind))
        raise ValueError(f"unknown node kind in {kinds!r}; valid kinds: {valid}") from None


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


def create_server(db_path: Path, *, token: str | None = None) -> FastMCP:
    """Build the fastmcp server with all nine tools over *db_path*.

    When *token* is given, the HTTP transport requires it as a bearer token
    (clients send ``Authorization: Bearer <token>``); requests without it are
    rejected. stdio is a local pipe with no network surface, so it needs none.
    See issue #69 and docs/mcp.md.
    """
    try:
        from fastmcp import FastMCP
    except ImportError:
        raise McpUnavailableError(
            "fastmcp is not installed; install the MCP extra: pip install 'hdl-kgraph[mcp]'"
        ) from None

    auth = None
    if token is not None:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

        # One accepted opaque token mapped to a fixed identity — enough to gate
        # the read-only HTTP surface without standing up an OAuth/JWT provider.
        auth = StaticTokenVerifier({token: {"client_id": "hdl-kgraph", "scopes": []}})

    ctx = GraphContext(db_path)
    mcp: FastMCP = FastMCP(name="hdl-kgraph", instructions=_INSTRUCTIONS, auth=auth)

    @mcp.tool
    def find_module(name: str, limit: int = 20) -> dict[str, Any]:
        """Find design units (modules/entities/interfaces) by exact name or glob
        pattern, with port/parameter/instantiation counts."""
        return ctx.run(lambda q: q.find_module(name, limit))

    @mcp.tool
    def get_hierarchy(
        top: str | None = None, depth: int = 3, max_nodes: int = 500
    ) -> dict[str, Any]:
        """Design hierarchy. Without `top`, lists the top-level (never
        instantiated) units; with `top`, returns the instance tree below it,
        capped at `depth` levels and `max_nodes` nodes."""
        if top is None:
            return ctx.run(
                lambda q: {
                    "tops": q.top_modules(),
                    "hint": "call again with top=<name> for the tree",
                }
            )
        return ctx.run(lambda q: q.hierarchy(top, depth, max_nodes))

    @mcp.tool
    def who_instantiates(name: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """All instantiation sites of the design unit named `name`."""
        return ctx.run(lambda q: q.who_instantiates(name, limit, offset))

    @mcp.tool
    def port_map(module: str, instance: str | None = None) -> dict[str, Any]:
        """Ports and parameters of `module` in declaration order; with
        `instance`, also that instance's port connection bindings."""
        return ctx.run(lambda q: q.port_map(module, instance))

    @mcp.tool
    def impact_of_change(
        target: str, max_depth: int = 0, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """What breaks if `target` (a file path or design-unit name) changes:
        summary plus the transitively affected units, nearest first.
        `max_depth` 0 means unlimited."""
        return ctx.run(lambda q: q.impact_of_change(target, max_depth, limit, offset))

    @mcp.tool
    def clock_domains() -> dict[str, Any]:
        """Clock domains (with alias nets and process/signal counts) and
        clock-domain-crossing suspects."""
        return ctx.run(lambda q: q.clock_domains())

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
        return ctx.run(
            lambda q: q.find_signal_drivers(signal, module, readers, limit, offset)
        )

    @mcp.tool
    def uvm_topology() -> dict[str, Any]:
        """UVM components by role (via EXTENDS chains to uvm_* bases) and
        testbench-to-DUT TEST_COVERS links."""
        return ctx.run(lambda q: q.uvm_topology())

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
        kind_enums = _validate_kinds(kinds)
        return ctx.run(lambda q: q.search_nodes(name, kind_enums, file, limit, offset))

    return mcp
