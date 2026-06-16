"""hdl-kgraph CLI: the ``tools`` subcommand group (MCP tools without MCP).

This group exposes the same nine read-only queries as the MCP server, but as
plain subprocess-friendly CLI commands that print JSON to stdout. It exists for
environments where the MCP server cannot be configured: an agent can shell out
to ``hdl-kgraph tools <name> ...`` and get the **identical** response envelope
the MCP tools return.

Crucially these are the *fast* reads. Each command goes through
:class:`~hdl_kgraph.storage.query.GraphQuery`, which hydrates only the bounded
subgraph a query touches via the SQLite indices — not
:meth:`SqliteStore.load`, which rebuilds the whole graph in memory (what the
``query``/``tree``/``impact`` commands and a naive hand-written SQL read pay
for). ``GraphQuery`` already shapes its results into the MCP envelope and its
imports never pull in ``fastmcp``, so this surface works on a base install.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from hdl_kgraph.cli._common import CliError, _resolve_db
from hdl_kgraph.cli._options import _db_option
from hdl_kgraph.cli.render import emit_json
from hdl_kgraph.storage.query import GraphQuery
from hdl_kgraph.storage.sqlite_store import SchemaVersionError


def _query(db_path: Path | None) -> GraphQuery:
    """A ``GraphQuery`` over the resolved database (nearest one by default)."""
    return GraphQuery(_resolve_db(db_path))


def _run(action: Callable[[], Any]) -> None:
    """Call a ``GraphQuery`` method, print its JSON, and turn the documented
    failure modes (bad name/target, foreign schema) into a clean exit-2 error
    instead of a traceback — matching the rest of the CLI."""
    try:
        result = action()
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    except ValueError as exc:
        # hierarchy()/impact_of_change()/search-nodes kind validation raise this
        # for a not-found target or a bad filter — a usage error, not a crash.
        raise CliError(str(exc)) from exc
    emit_json(result)


@click.group()
def tools() -> None:
    """Run the MCP query tools as plain commands (JSON to stdout).

    Same nine read-only tools the MCP server exposes — for environments where
    MCP cannot be configured. Every command uses the bounded, index-backed
    reader (not a full-graph load), so it stays fast on large designs. Output is
    the same envelope MCP returns; list tools wrap items in
    ``{total, offset, count, truncated, items}``.
    """


@tools.command("find-module")
@click.argument("name")
@_db_option
@click.option("--limit", type=int, default=20, help="Max units to return [default: 20].")
def find_module_cmd(name: str, db_path: Path | None, limit: int) -> None:
    """Find design units by exact NAME or glob, with port/param/instance counts."""
    q = _query(db_path)
    _run(lambda: q.find_module(name, limit))


@tools.command("hierarchy")
@click.argument("top", required=False)
@_db_option
@click.option("--depth", type=int, default=3, help="Levels below TOP [default: 3].")
@click.option("--max-nodes", type=int, default=500, help="Node cap on the tree [default: 500].")
def hierarchy_cmd(top: str | None, db_path: Path | None, depth: int, max_nodes: int) -> None:
    """Design hierarchy: without TOP, the top-level units; with TOP, its tree."""
    q = _query(db_path)
    if top is None:
        _run(lambda: {"tops": q.top_modules(), "hint": "call again with a TOP name for the tree"})
        return
    _run(lambda: q.hierarchy(top, depth, max_nodes))


@tools.command("who-instantiates")
@click.argument("name")
@_db_option
@click.option("--limit", type=int, default=50, help="Max sites to return [default: 50].")
@click.option("--offset", type=int, default=0, help="Pagination offset [default: 0].")
def who_instantiates_cmd(name: str, db_path: Path | None, limit: int, offset: int) -> None:
    """All instantiation sites of the design unit named NAME."""
    q = _query(db_path)
    _run(lambda: q.who_instantiates(name, limit, offset))


@tools.command("port-map")
@click.argument("module")
@_db_option
@click.option("--instance", default=None, help="Also report this instance's port bindings.")
def port_map_cmd(module: str, db_path: Path | None, instance: str | None) -> None:
    """Ports/parameters of MODULE in declaration order (plus instance bindings)."""
    q = _query(db_path)
    _run(lambda: q.port_map(module, instance))


@tools.command("impact")
@click.argument("target")
@_db_option
@click.option(
    "--max-depth", type=int, default=0, help="Hops to follow; 0 = unlimited [default: 0]."
)
@click.option("--limit", type=int, default=100, help="Max affected units [default: 100].")
@click.option("--offset", type=int, default=0, help="Pagination offset [default: 0].")
def impact_cmd(target: str, db_path: Path | None, max_depth: int, limit: int, offset: int) -> None:
    """What breaks if TARGET (a file path or design-unit name) changes."""
    q = _query(db_path)
    _run(lambda: q.impact_of_change(target, max_depth, limit, offset))


@tools.command("clock-domains")
@_db_option
def clock_domains_cmd(db_path: Path | None) -> None:
    """Clock domains (alias nets, process/signal counts) and CDC suspects."""
    q = _query(db_path)
    _run(q.clock_domains)


@tools.command("find-signal-drivers")
@click.argument("signal")
@_db_option
@click.option("--module", default=None, help="Only signals inside this design unit.")
@click.option("--readers", is_flag=True, help="List readers instead of drivers.")
@click.option("--limit", type=int, default=50, help="Max sites to return [default: 50].")
@click.option("--offset", type=int, default=0, help="Pagination offset [default: 0].")
def find_signal_drivers_cmd(
    signal: str,
    db_path: Path | None,
    module: str | None,
    readers: bool,
    limit: int,
    offset: int,
) -> None:
    """What drives (or, with --readers, reads) signals named SIGNAL."""
    q = _query(db_path)
    _run(lambda: q.find_signal_drivers(signal, module, readers, limit, offset))


@tools.command("uvm-topology")
@_db_option
def uvm_topology_cmd(db_path: Path | None) -> None:
    """UVM components by role and testbench-to-DUT TEST_COVERS links."""
    q = _query(db_path)
    _run(q.uvm_topology)


@tools.command("search-nodes")
@click.argument("name", default="*")
@_db_option
@click.option(
    "--kind",
    "kinds",
    multiple=True,
    help="Restrict to this node kind, e.g. module, signal, class (repeatable).",
)
@click.option("--file", "file", default=None, help="Restrict to files matching this glob.")
@click.option("--limit", type=int, default=50, help="Max nodes to return [default: 50].")
@click.option("--offset", type=int, default=0, help="Pagination offset [default: 0].")
def search_nodes_cmd(
    name: str,
    db_path: Path | None,
    kinds: tuple[str, ...],
    file: str | None,
    limit: int,
    offset: int,
) -> None:
    """Search nodes by NAME glob, --kind, and/or --file glob."""
    from hdl_kgraph.mcp.server import _validate_kinds

    q = _query(db_path)
    try:
        kind_enums = _validate_kinds(list(kinds) or None)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    _run(lambda: q.search_nodes(name, kind_enums, file, limit, offset))
