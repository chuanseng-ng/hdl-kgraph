"""hdl-kgraph CLI.

M1 surface: ``build``, ``status``, ``query`` (``instances-of`` / ``modules``
/ ``unresolved``), and ``tree``. M2 adds real-world build inputs to
``build``: ``-f`` filelists, ``-D`` defines, ``-I`` include dirs, and
``hdl-kgraph.toml`` config discovery (CLI flags win over config values).
M4 adds ``update`` (incremental rebuild), ``detect-changes``, ``impact``,
and ``watch``. ``visualize`` arrives in M5, ``serve`` in M6.

The database lives at ``<root>/.hdl-kgraph/graph.db``; read commands locate
it by walking up from the current directory (git-style) unless ``--db`` is
given.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import networkx as nx

from hdl_kgraph import __version__
from hdl_kgraph.config import (
    BuildConfig,
    BuildOptions,
    ConfigError,
    find_config,
    resolve_build_options,
)
from hdl_kgraph.discovery import DEFAULT_MAX_FILE_SIZE_KB, SUFFIXES
from hdl_kgraph.graph import analysis, clocks, lint
from hdl_kgraph.incremental import detect_git_changes, dirty_closure
from hdl_kgraph.pipeline import (
    BuildReport,
    UpdateReport,
    default_db_path,
    find_db,
    run_build,
    run_update,
    scan_changes,
)
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import SchemaVersionError, SqliteStore

_db_option = click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the graph database (default: nearest .hdl-kgraph/graph.db).",
)

_json_option = click.option(
    "--json", "as_json", is_flag=True, help="Emit JSON instead of the text report."
)


def _json_default(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return str(value)


def _emit_json(payload: Any) -> None:
    click.echo(json.dumps(payload, indent=2, default=_json_default))


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


def _input_options(f: Callable) -> Callable:
    """The build-input flags shared by ``build``, ``update``, and ``watch``."""
    decorators = [
        click.argument(
            "source", type=click.Path(exists=True, path_type=Path), default=".", required=False
        ),
        _db_option,
        click.option(
            "-f",
            "--filelist",
            "filelists",
            multiple=True,
            type=click.Path(exists=True, path_type=Path),
            help="Compile the sources listed in this .f/.vc filelist (repeatable); "
            "SOURCE then only sets the build root.",
        ),
        click.option(
            "-D",
            "--define",
            "defines",
            multiple=True,
            metavar="NAME[=VALUE]",
            help="Preprocessor define (repeatable; overrides config and filelist defines).",
        ),
        click.option(
            "-I",
            "--incdir",
            "incdirs",
            multiple=True,
            type=click.Path(path_type=Path),
            help="`include search directory (repeatable).",
        ),
        click.option(
            "--lib",
            "libs",
            multiple=True,
            metavar="NAME=PATH",
            help="Map a VHDL library name to a source directory (repeatable; "
            "overrides [vhdl.libraries] config entries; default library is 'work').",
        ),
        click.option(
            "--config",
            "config_path",
            type=click.Path(exists=True, path_type=Path),
            default=None,
            help="Path to hdl-kgraph.toml (default: nearest one from SOURCE upward).",
        ),
        click.option("--no-config", is_flag=True, help="Ignore any hdl-kgraph.toml."),
        click.option(
            "--exclude",
            "excludes",
            multiple=True,
            metavar="GLOB",
            help="Skip files whose root-relative path matches GLOB (repeatable).",
        ),
        click.option(
            "--max-file-size",
            type=int,
            default=None,
            metavar="KB",
            help="Skip files larger than this many kilobytes. "
            f"[default: {DEFAULT_MAX_FILE_SIZE_KB}]",
        ),
    ]
    for decorator in reversed(decorators):
        f = decorator(f)
    return f


def _resolve_options(
    source: Path,
    filelists: tuple[Path, ...],
    defines: tuple[str, ...],
    incdirs: tuple[Path, ...],
    libs: tuple[str, ...],
    config_path: Path | None,
    no_config: bool,
    excludes: tuple[str, ...],
    max_file_size: int | None,
) -> BuildOptions:
    """Merge config-file and CLI build inputs (the ``build`` precedence rules)."""
    if no_config:
        config = BuildConfig()
    else:
        if config_path is None:
            config_path = find_config(source)
        try:
            config = BuildConfig.load(config_path) if config_path is not None else BuildConfig()
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
    try:
        return resolve_build_options(
            config,
            cli_filelists=[p.resolve() for p in filelists],
            cli_defines=defines,
            cli_incdirs=[p.resolve() for p in incdirs],
            cli_exclude=excludes,
            cli_max_file_size_kb=max_file_size,
            cli_libs=libs,
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _echo_build_report(report: BuildReport) -> None:
    for warning in report.warnings:
        click.echo(f"warning: {warning}", err=True)
    if report.parsed_files == 0:
        raise click.ClickException(f"no parseable HDL files found under {report.root}")
    click.echo(f"built {report.db_path}")
    if report.filelists_read:
        click.echo(f"  filelists:      {report.filelists_read}")
    click.echo(f"  files parsed:   {report.parsed_files}")
    if report.reused_files:
        click.echo(f"  files reused:   {report.reused_files} (re-linked without re-parsing)")
    if report.vhdl_files:
        click.echo(f"  vhdl files:     {report.vhdl_files}")
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
@_input_options
def build(
    source: Path,
    db_path: Path | None,
    filelists: tuple[Path, ...],
    defines: tuple[str, ...],
    incdirs: tuple[Path, ...],
    libs: tuple[str, ...],
    config_path: Path | None,
    no_config: bool,
    excludes: tuple[str, ...],
    max_file_size: int | None,
) -> None:
    """Build the knowledge graph from HDL sources under SOURCE."""
    options = _resolve_options(
        source, filelists, defines, incdirs, libs, config_path, no_config, excludes, max_file_size
    )
    report = run_build(source, db_path=db_path, options=options)
    _echo_build_report(report)


def _echo_update_report(report: UpdateReport) -> None:
    if report.up_to_date:
        click.echo(f"up to date ({report.elapsed_s:.2f}s)")
        return
    if report.full_rebuild_reason is not None:
        click.echo(f"full rebuild: {report.full_rebuild_reason}")
    else:
        for path in report.removed:
            click.echo(f"  removed:  {path}")
        for path, why in sorted(report.reparsed.items()):
            click.echo(f"  re-parsed: {path} ({why})")
        if not report.reparsed:
            click.echo("  no files re-parsed (re-linked only)")
    assert report.build is not None
    _echo_build_report(report.build)
    click.echo(f"  updated in {report.elapsed_s:.2f}s")


@main.command()
@_input_options
@click.option("--full", is_flag=True, help="Force a full rebuild.")
def update(
    source: Path,
    db_path: Path | None,
    filelists: tuple[Path, ...],
    defines: tuple[str, ...],
    incdirs: tuple[Path, ...],
    libs: tuple[str, ...],
    config_path: Path | None,
    no_config: bool,
    excludes: tuple[str, ...],
    max_file_size: int | None,
    full: bool,
) -> None:
    """Incrementally update the graph: re-parse only changed files.

    Changed/added/removed files and their include/macro dependents are
    re-parsed; everything else is re-linked from stored parse results.
    Falls back to a full rebuild when the database is missing or the build
    options changed.
    """
    options = _resolve_options(
        source, filelists, defines, incdirs, libs, config_path, no_config, excludes, max_file_size
    )
    report = run_update(source, db_path=db_path, options=options, full=full)
    _echo_update_report(report)


@main.command("detect-changes")
@_input_options
@click.option(
    "--git",
    "git_ref",
    is_flag=False,
    flag_value="HEAD",
    default=None,
    metavar="[REF]",
    help="Diff the working tree against a git ref (default HEAD) instead of "
    "the last build's content hashes.",
)
@click.option(
    "--closure",
    is_flag=True,
    help="Also list unchanged files dirtied through include/macro dependencies.",
)
def detect_changes(
    source: Path,
    db_path: Path | None,
    filelists: tuple[Path, ...],
    defines: tuple[str, ...],
    incdirs: tuple[Path, ...],
    libs: tuple[str, ...],
    config_path: Path | None,
    no_config: bool,
    excludes: tuple[str, ...],
    max_file_size: int | None,
    git_ref: str | None,
    closure: bool,
) -> None:
    """List build inputs that changed since the last build (or a git ref).

    Prints one ``M``/``A``/``D`` line per modified/added/deleted file
    (``~`` for closure-dirtied files with --closure) and exits 1 when
    anything changed, 0 otherwise — script- and CI-friendly.
    """
    options = _resolve_options(
        source, filelists, defines, incdirs, libs, config_path, no_config, excludes, max_file_size
    )
    base = source.resolve()
    base = base.parent if base.is_file() else base
    if git_ref is not None:
        try:
            changes = detect_git_changes(base, git_ref, SUFFIXES)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        if db_path is None:
            db_path = default_db_path(base)
        if not db_path.is_file():
            raise click.ClickException(
                f"no database at {db_path}; run `hdl-kgraph build` first, or use --git"
            )
        try:
            changes = scan_changes(source, db_path, options)
        except SchemaVersionError as exc:
            raise click.ClickException(str(exc)) from exc
    for path in changes.changed:
        click.echo(f"M {path}")
    for path in changes.added:
        click.echo(f"A {path}")
    for path in changes.removed:
        click.echo(f"D {path}")
    if closure and (changes.changed or changes.removed):
        graph, _, _ = _load(db_path if db_path is not None else find_db(base))
        seeds = {path: "" for path in (*changes.changed, *changes.removed)}
        for path, why in sorted(dirty_closure(graph, seeds).items()):
            if path not in seeds:
                click.echo(f"~ {path} ({why})")
    if changes:
        sys.exit(1)


@main.command()
@click.argument("target")
@_db_option
@click.option(
    "--max-depth",
    type=int,
    default=0,
    help="Limit the dependency distance reported (0 = unlimited).",
)
@click.option(
    "--files",
    "show_files",
    is_flag=True,
    help="List the affected files instead of design units.",
)
def impact(target: str, db_path: Path | None, max_depth: int, show_files: bool) -> None:
    """Show what a change to TARGET (a file path or design unit) affects.

    Walks reverse INSTANTIATES/IMPORTS/INCLUDES/EXTENDS (plus VHDL
    USES_PACKAGE/IMPLEMENTS/BINDS and macro use) edges transitively: the
    instantiating parents, importers, includers, and subclasses that a
    change to TARGET can break.
    """
    graph, files, _ = _load(db_path)
    seeds = _impact_seeds(graph, files, target)
    if not seeds:
        raise click.ClickException(f"{target!r} matches no file or design unit in the graph")
    records = analysis.impact_radius(graph, seeds, max_depth=max_depth)
    if not records:
        click.echo("no dependents found")
        return
    if show_files:
        for path in sorted({r.file for r in records if r.file}):
            click.echo(path)
        return
    for r in records:
        location = f"{r.file}:{r.line}" if r.file and r.line else r.file
        click.echo(
            f"{r.kind.value:13} {r.name:30} {location:30} <- {r.via.value} (depth {r.depth})"
        )


def _impact_seeds(graph: nx.MultiDiGraph, files: list, target: str) -> list[str]:
    """Resolve an ``impact`` TARGET to seed node ids (file path first)."""
    known_paths = {f.path for f in files}
    candidate = target.replace("\\", "/").lstrip("./")
    if candidate in known_paths or "/" in candidate or Path(candidate).suffix in SUFFIXES:
        matches = [p for p in known_paths if p == candidate or p.endswith("/" + candidate)]
        return [f"file:{p}" for p in matches if f"file:{p}" in graph]
    return [
        node_id
        for node_id, data in graph.nodes(data=True)
        if data["kind"] in analysis.IMPACT_UNIT_KINDS
        and data["name"] == (target.lower() if data["language"] is Language.VHDL else target)
        and not data["attrs"].get("unresolved")
    ]


@main.command()
@_input_options
@click.option(
    "--debounce",
    type=int,
    default=300,
    show_default=True,
    metavar="MS",
    help="Quiet period after the last filesystem event before updating.",
)
def watch(
    source: Path,
    db_path: Path | None,
    filelists: tuple[Path, ...],
    defines: tuple[str, ...],
    incdirs: tuple[Path, ...],
    libs: tuple[str, ...],
    config_path: Path | None,
    no_config: bool,
    excludes: tuple[str, ...],
    max_file_size: int | None,
    debounce: int,
) -> None:
    """Watch SOURCE and incrementally update the graph on every save burst."""
    from hdl_kgraph.watch import WatchUnavailableError, run_watch

    options = _resolve_options(
        source, filelists, defines, incdirs, libs, config_path, no_config, excludes, max_file_size
    )

    def on_report(report: UpdateReport) -> None:
        if report.up_to_date or report.build is None:
            click.echo(f"up to date ({report.elapsed_s:.2f}s)")
        elif report.full_rebuild_reason is not None:
            click.echo(
                f"full rebuild ({report.full_rebuild_reason}): "
                f"{report.build.parsed_files} file(s), {report.build.node_count} nodes "
                f"({report.elapsed_s:.2f}s)"
            )
        else:
            click.echo(
                f"updated: re-parsed {len(report.reparsed)} file(s), "
                f"{report.build.node_count} nodes, {report.build.edge_count} edges "
                f"({report.elapsed_s:.2f}s)"
            )

    click.echo(f"watching {source.resolve()} (Ctrl-C to stop)")
    try:
        run_watch(
            source, db_path=db_path, options=options, quiet_s=debounce / 1000, on_report=on_report
        )
    except WatchUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        click.echo("stopped")


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


@main.command("lint")
@_db_option
@_json_option
@click.option(
    "--check",
    "checks",
    multiple=True,
    metavar="NAME",
    help="Run only this check (repeatable). "
    f"Available: {', '.join(sorted(lint.CHECKS))}.",
)
@click.option(
    "--top",
    "tops",
    multiple=True,
    metavar="NAME",
    help="Treat NAME as an intended top module (repeatable; exempts it from dead-module).",
)
def lint_cmd(
    db_path: Path | None, as_json: bool, checks: tuple[str, ...], tops: tuple[str, ...]
) -> None:
    """Report graph-level lint findings (always exits 0 — a report, not a gate).

    Signal-level checks skip files with parse errors and implicit-net stubs
    so a finding is worth reading; confidences below 1.0 mark heuristics.
    """
    graph, files, _ = _load(db_path)
    error_files = frozenset(f.path for f in files if f.parse_error_count)
    try:
        findings = lint.run_checks(
            graph,
            names=checks or None,
            tops=frozenset(tops),
            error_files=error_files,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit_json(findings)
        return
    if not findings:
        click.echo("no findings")
        return
    for f in findings:
        location = f"{f.file}:{f.line}" if f.file else "?"
        marker = "" if f.confidence >= 0.8 else f"  [~{f.confidence:.1f}]"
        click.echo(f"{f.check:18} {f.name:32} {location:28} {f.message}{marker}")


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
    """List all modules and entities with their instantiation counts."""
    graph, _, _ = _load(db_path)
    rows = []
    for node_id, data in sorted(graph.nodes(data=True), key=lambda kv: kv[1]["name"]):
        if data["kind"] not in (NodeKind.MODULE, NodeKind.ENTITY) or data["attrs"].get(
            "unresolved"
        ):
            continue
        count = sum(
            1
            for _, _, edge in graph.in_edges(node_id, data=True)
            if edge["kind"] is EdgeKind.INSTANTIATES
        )
        marker = " [vhdl]" if data["kind"] is NodeKind.ENTITY else ""
        rows.append((data["name"], marker, data["file"], data["line_span"][0], count))
    for name, marker, file, line, count in rows:
        click.echo(f"{name + marker:30} {file}:{line}  instances={count}")


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
def drivers_cmd(signal: str, db_path: Path | None, as_json: bool, readers: bool) -> None:
    """List what drives (or reads) signals named SIGNAL."""
    graph, _, _ = _load(db_path)
    kind = EdgeKind.READS if readers else EdgeKind.DRIVES
    records: list[dict[str, Any]] = []
    for node_id, data in graph.nodes(data=True):
        if data["kind"] not in (NodeKind.SIGNAL, NodeKind.PORT):
            continue
        wanted = signal.lower() if data["language"] is Language.VHDL else signal
        if data["name"] != wanted:
            continue
        for src, _, edge in graph.in_edges(node_id, data=True):
            if edge["kind"] is not kind:
                continue
            site = graph.nodes[src]
            span = edge["attrs"].get("line_span") or site["line_span"]
            records.append(
                {
                    "signal_id": node_id,
                    "signal": data["qualified_name"],
                    "site_id": src,
                    "site": site["qualified_name"],
                    "site_kind": site["kind"].value,
                    "file": site["file"],
                    "line": span[0] if span else 0,
                    "confidence": edge["confidence"],
                }
            )
    records.sort(key=lambda r: (r["signal"], r["file"], r["line"], r["site"]))
    if as_json:
        _emit_json(records)
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
            if data["kind"] in (NodeKind.MODULE, NodeKind.ENTITY)
            # VHDL entity names are stored lowercase (case-insensitive).
            and data["name"] == (top.lower() if data["language"] is Language.VHDL else top)
            and not data["attrs"].get("unresolved")
        ]
        if not roots:
            raise click.ClickException(f"module or entity {top!r} not found in the graph")
    else:
        roots = analysis.find_top_modules(graph)
        if not roots:
            raise click.ClickException("no top modules found")

    for root in roots:
        _print_tree(analysis.hierarchy_tree(graph, root, max_depth=depth), prefix="", is_last=True)


def _print_tree(node: analysis.HierarchyNode, prefix: str, is_last: bool) -> None:
    # A VHDL entity shows the architecture its children came from: alu(rtl).
    unit = node.module_name + (f"({node.architecture})" if node.architecture else "")
    if node.instance_name is None:
        label = unit
    else:
        connector = "`-- " if is_last else "|-- "
        label = f"{prefix}{connector}{node.instance_name}: {unit}"
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
