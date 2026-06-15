"""hdl-kgraph CLI: The `query` subcommand group."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from hdl_kgraph.cli._common import (
    _load,
)
from hdl_kgraph.cli._options import (
    _db_option,
    _json_option,
)
from hdl_kgraph.cli.render import emit_json as _emit_json
from hdl_kgraph.graph import analysis, clocks, uvm
from hdl_kgraph.schema import NodeKind


@click.group()
def query() -> None:
    """Query the knowledge graph."""


@query.command("instances-of")
@click.argument("name")
@_db_option
@_json_option
def instances_of(name: str, db_path: Path | None, as_json: bool) -> None:
    """List all instantiation sites of design units named NAME.

    Exits 1 if NAME matches nothing (a negative result, not an error).
    """
    graph, _, _ = _load(db_path)
    records = analysis.instances_of(graph, name)
    if as_json:
        _emit_json(records)
        if not records:
            sys.exit(1)
        return
    if not records:
        click.echo(f"no instances of {name!r} found", err=True)
        sys.exit(1)
    for rec in records:
        marker = " [?]" if rec["target_unresolved"] else ""
        click.echo(
            f"{rec['qualified_name']}  {rec['file']}:{rec['line']}"
            f"  confidence={rec['confidence']:.1f}{marker}"
        )


@query.command("modules")
@_db_option
@_json_option
def modules(db_path: Path | None, as_json: bool) -> None:
    """List all modules and entities with their instantiation counts."""
    graph, _, _ = _load(db_path)
    rows = []
    for node_id, data in sorted(graph.nodes(data=True), key=lambda kv: kv[1]["name"]):
        if data["kind"] not in (NodeKind.MODULE, NodeKind.ENTITY) or data["attrs"].get(
            "unresolved"
        ):
            continue
        count = analysis.instantiation_count(graph, node_id)
        rows.append(
            {
                "name": data["name"],
                "kind": data["kind"],
                "file": data["file"],
                "line": data["line_span"][0],
                "instances": count,
            }
        )
    if as_json:
        _emit_json(rows)
        return
    for row in rows:
        marker = " [vhdl]" if row["kind"] is NodeKind.ENTITY else ""
        click.echo(
            f"{row['name'] + marker:30} {row['file']}:{row['line']}  instances={row['instances']}"
        )


@query.command("clock-domains")
@_db_option
@_json_option
def clock_domains_cmd(db_path: Path | None, as_json: bool) -> None:
    """Report clock domains: clock nets, their processes and signals.

    Domains come from CLOCKED_BY edges (sensitivity-list evidence = 1.0,
    name heuristics = 0.4) with clock nets alias-merged across the
    hierarchy through single-identifier port connections.
    """
    graph, _, _ = _load(db_path)
    domains = clocks.clock_domains(graph)
    if as_json:
        _emit_json(domains)
        return
    if not domains:
        click.echo("no clocked processes found")
        return
    for domain in domains:
        label = graph.nodes[domain.clock_id]["qualified_name"]
        aliases = [n for n in domain.clock_names if n != graph.nodes[domain.clock_id]["name"]]
        if aliases:
            label += " (= " + ", ".join(aliases) + ")"
        marker = "" if domain.min_confidence >= 0.8 else f"  [~{domain.min_confidence:.1f}]"
        click.echo(f"{label}{marker}")
        click.echo(f"    processes: {len(domain.process_ids)}")
        click.echo(f"    signals driven: {len(domain.signal_ids)}")


@query.command("reset-tree")
@_db_option
@_json_option
def reset_tree_cmd(db_path: Path | None, as_json: bool) -> None:
    """Report reset nets and the processes they reset."""
    graph, _, _ = _load(db_path)
    groups = clocks.reset_tree(graph)
    if as_json:
        _emit_json(groups)
        return
    if not groups:
        click.echo("no resets found")
        return
    for group in groups:
        label = graph.nodes[group.reset_id]["qualified_name"]
        aliases = [n for n in group.reset_names if n != graph.nodes[group.reset_id]["name"]]
        if aliases:
            label += " (= " + ", ".join(aliases) + ")"
        flavor = "async" if group.is_async else "sync (name heuristic)"
        marker = "" if group.min_confidence >= 0.8 else f"  [~{group.min_confidence:.1f}]"
        click.echo(f"{label}  {flavor}{marker}")
        for proc in group.process_ids:
            click.echo(f"    resets {graph.nodes[proc]['qualified_name']}")


@query.command("cdc")
@_db_option
@_json_option
def cdc_cmd(db_path: Path | None, as_json: bool) -> None:
    """Report clock-domain-crossing suspects.

    A suspect is a signal driven in one domain and read by a process in
    another. Synchronizers are not recognized — review each finding (this
    is a report, not a gate; the exit code is always 0).
    """
    graph, _, _ = _load(db_path)
    suspects = clocks.cdc_suspects(graph)
    if as_json:
        _emit_json(suspects)
        return
    if not suspects:
        click.echo("no CDC suspects found")
        return
    for s in suspects:
        location = f"{s.file}:{s.line}" if s.file else "?"
        click.echo(
            f"{s.signal_name:24} {s.driver_domain} -> {s.reader_domain}"
            f"  read by {graph.nodes[s.reader_id]['qualified_name']}"
            f"  {location}  confidence={s.confidence:.1f}"
        )


@query.command("drivers")
@click.argument("signal")
@_db_option
@_json_option
@click.option("--readers", is_flag=True, help="List readers instead of drivers.")
@click.option("--module", default=None, help="Only signals inside this design unit.")
def drivers_cmd(
    signal: str, db_path: Path | None, as_json: bool, readers: bool, module: str | None
) -> None:
    """List what drives (or reads) signals named SIGNAL.

    Exits 1 if SIGNAL matches nothing (a negative result, not an error).
    """
    graph, _, _ = _load(db_path)
    records = analysis.signal_drivers(graph, signal, module=module, readers=readers)
    if as_json:
        _emit_json(records)
        if not records:
            # Empty is a documented negative result (exit 1), in JSON as in text —
            # matches `instances-of`; the JSON body is still emitted ([]).
            sys.exit(1)
        return
    if not records:
        verb = "reads" if readers else "drives"
        click.echo(f"nothing {verb} a signal named {signal!r}", err=True)
        sys.exit(1)
    for rec in records:
        click.echo(
            f"{rec['signal']:30} <- {rec['site_kind']} {rec['site']}"
            f"  {rec['file']}:{rec['line']}  confidence={rec['confidence']:.1f}"
        )


@query.command("uvm")
@_db_option
@_json_option
def uvm_cmd(db_path: Path | None, as_json: bool) -> None:
    """Report UVM topology: components by role, plus TEST_COVERS links.

    Classes are classified by walking their EXTENDS chain to the first
    uvm_* base (usually an unresolved stub — UVM itself is rarely parsed).
    """
    graph, _, _ = _load(db_path)
    components = uvm.uvm_topology(graph)
    covers = uvm.test_covers(graph)
    if as_json:
        _emit_json({"components": components, "test_covers": covers})
        return
    if not components and not covers:
        click.echo("no UVM components or testbench tops found")
        return
    for role in uvm.ROLE_ORDER:
        members = [c for c in components if c.role == role]
        if not members:
            continue
        click.echo(f"{role}:")
        for c in members:
            chain = " -> ".join(c.base_chain)
            click.echo(f"    {c.name:28} {c.file}:{c.line}  ({chain})")
    if covers:
        click.echo("test coverage (name-pattern heuristic, 0.4):")
        for cover in covers:
            click.echo(f"    {cover['test']} covers {cover['dut']}")


@query.command("unresolved")
@_db_option
@_json_option
def unresolved(db_path: Path | None, as_json: bool) -> None:
    """List unresolved stub nodes and who references them."""
    graph, _, _ = _load(db_path)
    stubs = analysis.unresolved_stubs(graph)
    if as_json:
        _emit_json(stubs)
        return
    if not stubs:
        click.echo("no unresolved references")
        return
    for stub in stubs:
        click.echo(f"{stub['kind'].value}:{stub['name']}")
        for referrer in stub["referrers"]:
            click.echo(f"    <- {referrer}")
