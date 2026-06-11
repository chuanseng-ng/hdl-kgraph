"""hdl-kgraph CLI.

M1 surface: ``build``, ``status``, ``query`` (``instances-of`` / ``modules``
/ ``unresolved``), and ``tree``. M2 adds real-world build inputs to
``build``: ``-f`` filelists, ``-D`` defines, ``-I`` include dirs, and
``hdl-kgraph.toml`` config discovery (CLI flags win over config values).
``update``/``watch``/``impact`` arrive in M4, ``visualize`` in M5,
``serve`` in M6.

The database lives at ``<root>/.hdl-kgraph/graph.db``; read commands locate
it by walking up from the current directory (git-style) unless ``--db`` is
given.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import click
import networkx as nx

from hdl_kgraph import __version__
from hdl_kgraph.config import (
    BuildConfig,
    ConfigError,
    find_config,
    resolve_build_options,
)
from hdl_kgraph.discovery import DEFAULT_MAX_FILE_SIZE_KB
from hdl_kgraph.graph import analysis
from hdl_kgraph.pipeline import find_db, run_build
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import SchemaVersionError, SqliteStore

_db_option = click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the graph database (default: nearest .hdl-kgraph/graph.db).",
)


def _load(db_path: Path | None) -> tuple[nx.MultiDiGraph, list, dict[str, str]]:
    if db_path is None:
        db_path = find_db(Path.cwd())
        if db_path is None:
            raise click.ClickException(
                "no .hdl-kgraph/graph.db found here or in any parent directory; "
                "run `hdl-kgraph build` first or pass --db"
            )
    if not db_path.is_file():
        raise click.ClickException(f"database not found: {db_path}")
    try:
        return SqliteStore(db_path).load()
    except SchemaVersionError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
@click.version_option(version=__version__, prog_name="hdl-kgraph")
def main() -> None:
    """Build and query a knowledge graph of your HDL design."""


@main.command()
@click.argument("source", type=click.Path(exists=True, path_type=Path), default=".", required=False)
@_db_option
@click.option(
    "-f",
    "--filelist",
    "filelists",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="Compile the sources listed in this .f/.vc filelist (repeatable); "
    "SOURCE then only sets the build root.",
)
@click.option(
    "-D",
    "--define",
    "defines",
    multiple=True,
    metavar="NAME[=VALUE]",
    help="Preprocessor define (repeatable; overrides config and filelist defines).",
)
@click.option(
    "-I",
    "--incdir",
    "incdirs",
    multiple=True,
    type=click.Path(path_type=Path),
    help="`include search directory (repeatable).",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to hdl-kgraph.toml (default: nearest one from SOURCE upward).",
)
@click.option("--no-config", is_flag=True, help="Ignore any hdl-kgraph.toml.")
@click.option(
    "--exclude",
    "excludes",
    multiple=True,
    metavar="GLOB",
    help="Skip files whose root-relative path matches GLOB (repeatable).",
)
@click.option(
    "--max-file-size",
    type=int,
    default=None,
    metavar="KB",
    help=f"Skip files larger than this many kilobytes. [default: {DEFAULT_MAX_FILE_SIZE_KB}]",
)
def build(
    source: Path,
    db_path: Path | None,
    filelists: tuple[Path, ...],
    defines: tuple[str, ...],
    incdirs: tuple[Path, ...],
    config_path: Path | None,
    no_config: bool,
    excludes: tuple[str, ...],
    max_file_size: int | None,
) -> None:
    """Build the knowledge graph from HDL sources under SOURCE."""
    if no_config:
        config = BuildConfig()
    else:
        if config_path is None:
            config_path = find_config(source)
        try:
            config = BuildConfig.load(config_path) if config_path is not None else BuildConfig()
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
    options = resolve_build_options(
        config,
        cli_filelists=[p.resolve() for p in filelists],
        cli_defines=defines,
        cli_incdirs=[p.resolve() for p in incdirs],
        cli_exclude=excludes,
        cli_max_file_size_kb=max_file_size,
    )
    report = run_build(source, db_path=db_path, options=options)
    for warning in report.warnings:
        click.echo(f"warning: {warning}", err=True)
    if report.parsed_files == 0:
        raise click.ClickException(f"no parseable HDL files found under {report.root}")
    click.echo(f"built {report.db_path}")
    if report.filelists_read:
        click.echo(f"  filelists:      {report.filelists_read}")
    click.echo(f"  files parsed:   {report.parsed_files}")
    for reason, count in sorted(report.skipped.items()):
        click.echo(f"  skipped ({reason}): {count}")
    if report.error_files:
        click.echo(f"  parse errors:   {report.parse_error_count} in {report.error_files} file(s)")
    if report.macros_defined:
        click.echo(f"  macros defined: {report.macros_defined}")
    if report.includes_resolved or report.includes_unresolved:
        includes = f"  includes:       {report.includes_resolved} resolved"
        if report.includes_unresolved:
            includes += f", {report.includes_unresolved} unresolved"
        click.echo(includes)
    if report.preproc_warning_count:
        click.echo(f"  preprocessor warnings: {report.preproc_warning_count}")
    if report.both_branches:
        click.echo("  both-branches mode: no defines given; `ifdef alternatives kept at 0.6")
    click.echo(f"  nodes: {report.node_count}  edges: {report.edge_count}")
    if report.unresolved_count:
        click.echo(f"  unresolved: {report.unresolved_count}")


@main.command()
@_db_option
def status(db_path: Path | None) -> None:
    """Show graph statistics for the current build."""
    graph, files, meta = _load(db_path)
    click.echo(f"root:     {meta.get('root', '?')}")
    click.echo(f"built at: {meta.get('built_at', '?')} (hdl-kgraph {meta.get('tool_version')})")

    # Filelists are recorded for M4 incremental rebuilds but are not parsed
    # HDL sources; report them on their own line.
    parsed = [f for f in files if f.skipped_reason is None and f.language is not Language.UNKNOWN]
    filelists = [f for f in files if f.skipped_reason is None and f.language is Language.UNKNOWN]
    skipped = Counter(f.skipped_reason for f in files if f.skipped_reason is not None)
    error_files = [f for f in parsed if f.parse_error_count]
    click.echo(f"files:    {len(parsed)} parsed")
    if filelists:
        click.echo(f"          {len(filelists)} filelist(s)")
    for reason, count in sorted(skipped.items()):
        click.echo(f"          {count} skipped ({reason})")
    total_errors = sum(f.parse_error_count for f in error_files)
    if error_files:
        click.echo(f"          {total_errors} parse error(s) in {len(error_files)} file(s)")

    node_kinds = Counter(data["kind"].value for _, data in graph.nodes(data=True))
    edge_kinds = Counter(data["kind"].value for _, _, data in graph.edges(data=True))
    click.echo(f"nodes:    {graph.number_of_nodes()}")
    for kind, count in node_kinds.most_common():
        click.echo(f"          {count:6} {kind}")
    click.echo(f"edges:    {graph.number_of_edges()}")
    for kind, count in edge_kinds.most_common():
        click.echo(f"          {count:6} {kind}")
    stubs = analysis.unresolved_stubs(graph)
    if stubs:
        click.echo(f"unresolved: {len(stubs)}")


@main.group()
def query() -> None:
    """Query the knowledge graph."""


@query.command("instances-of")
@click.argument("name")
@_db_option
def instances_of(name: str, db_path: Path | None) -> None:
    """List all instantiation sites of design units named NAME."""
    graph, _, _ = _load(db_path)
    records = analysis.instances_of(graph, name)
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
def modules(db_path: Path | None) -> None:
    """List all modules with their instantiation counts."""
    graph, _, _ = _load(db_path)
    rows = []
    for node_id, data in sorted(graph.nodes(data=True), key=lambda kv: kv[1]["name"]):
        if data["kind"] is not NodeKind.MODULE or data["attrs"].get("unresolved"):
            continue
        count = sum(
            1
            for _, _, edge in graph.in_edges(node_id, data=True)
            if edge["kind"] is EdgeKind.INSTANTIATES
        )
        rows.append((data["name"], data["file"], data["line_span"][0], count))
    for name, file, line, count in rows:
        click.echo(f"{name:30} {file}:{line}  instances={count}")


@query.command("unresolved")
@_db_option
def unresolved(db_path: Path | None) -> None:
    """List unresolved stub nodes and who references them."""
    graph, _, _ = _load(db_path)
    stubs = analysis.unresolved_stubs(graph)
    if not stubs:
        click.echo("no unresolved references")
        return
    for stub in stubs:
        click.echo(f"{stub['kind'].value}:{stub['name']}")
        for referrer in stub["referrers"]:
            click.echo(f"    <- {referrer}")


@main.command()
@click.argument("top", required=False)
@click.option("--depth", type=int, default=64, show_default=True, help="Maximum tree depth.")
@_db_option
def tree(top: str | None, depth: int, db_path: Path | None) -> None:
    """Print the design hierarchy from TOP (default: every top module)."""
    graph, _, _ = _load(db_path)
    if top is not None:
        roots = [
            node_id
            for node_id, data in graph.nodes(data=True)
            if data["kind"] is NodeKind.MODULE
            and data["name"] == top
            and not data["attrs"].get("unresolved")
        ]
        if not roots:
            raise click.ClickException(f"module {top!r} not found in the graph")
    else:
        roots = analysis.find_top_modules(graph)
        if not roots:
            raise click.ClickException("no top modules found")

    for root in roots:
        _print_tree(analysis.hierarchy_tree(graph, root, max_depth=depth), prefix="", is_last=True)


def _print_tree(node: analysis.HierarchyNode, prefix: str, is_last: bool) -> None:
    if node.instance_name is None:
        label = node.module_name
    else:
        connector = "`-- " if is_last else "|-- "
        label = f"{prefix}{connector}{node.instance_name}: {node.module_name}"
    if node.unresolved:
        label += " [?]"
    elif node.confidence < 0.8:
        label += f" [~{node.confidence:.1f}]"
    if node.truncated:
        label += " (...)"
    click.echo(label)
    child_prefix = "" if node.instance_name is None else prefix + ("    " if is_last else "|   ")
    for i, child in enumerate(node.children):
        _print_tree(child, child_prefix, i == len(node.children) - 1)
