"""hdl-kgraph CLI.

M1 surface: ``build``, ``status``, ``query`` (``instances-of`` / ``modules``
/ ``unresolved``), and ``tree``. M2 adds real-world build inputs to
``build``: ``-f`` filelists, ``-D`` defines, ``-I`` include dirs, and
``hdl-kgraph.toml`` config discovery (CLI flags win over config values).
M4 adds ``update`` (incremental rebuild), ``detect-changes``, ``impact``,
and ``watch``. M5 adds the analyses — ``lint``, ``metrics``, ``visualize``,
and ``query`` ``clock-domains``/``reset-tree``/``cdc``/``drivers``/``uvm``
(all with ``--json``). M6 adds ``serve --mcp`` (AI assistants query the
graph over MCP) and ``setup`` (auto-configure detected assistants).
Diagnostics: ``build``/``update``/``watch`` report pipeline stages and a
per-file parse counter on stderr as they run; ``-v/--verbose`` adds
per-file parse errors, preprocessor warnings, and unresolved includes, and
``status --errors`` lists the same per-file diagnostics after the fact.

The database lives at ``<root>/.hdl-kgraph/graph.db``; read commands locate
it by walking up from the current directory (git-style) unless ``--db`` is
given.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import sys
import time
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
    LintWaiver,
    find_config,
    load_waivers,
    resolve_build_options,
)
from hdl_kgraph.discovery import DEFAULT_MAX_FILE_SIZE_KB, SUFFIXES
from hdl_kgraph.enrich import summarize_enrichment
from hdl_kgraph.export import EXPORT_FORMATS
from hdl_kgraph.graph import analysis, clocks, lint, metrics, uvm
from hdl_kgraph.incremental import dirty_closure
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
from hdl_kgraph.vcs import detect_vcs, detect_vcs_changes

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

_verbose_option = click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Also report per-file parse errors, preprocessor warnings, and "
    "unresolved includes (stage progress is always shown).",
)

_jobs_option = click.option(
    "-j",
    "--jobs",
    type=int,
    default=None,
    metavar="N",
    help="Parse with N worker processes (default: auto — serial for small "
    "builds, otherwise one per CPU up to a cap; 1 = always serial).",
)

_enrich_option = click.option(
    "--enrich",
    is_flag=True,
    help="Run native-frontend elaboration (pyslang for SV/Verilog, ghdl for "
    "VHDL) to upgrade heuristic edges to elaboration-accurate facts and record "
    "discrepancies (M7). Slower; re-runs whole-design elaboration on every "
    "update. `hdl-kgraph enriched` reports the delta vs the default build.",
)


class _ProgressRenderer:
    """Default-on pipeline progress on stderr (so stdout reports stay clean).

    ``stage`` prints one line per pipeline stage; ``tick`` drives the
    pass 0+1 per-file counter — a single ``\\r``-rewritten line when stderr
    is a terminal, a milestone line every ``MILESTONE_EVERY`` files
    otherwise (CI logs, pipes). ``finish`` terminates a pending live line
    so later output starts on a fresh line.
    """

    MILESTONE_EVERY = 25
    MIN_INTERVAL_S = 0.1

    def __init__(self) -> None:
        # Resolve stderr at command runtime, not import time: Click's
        # CliRunner patches sys.stderr around each invocation.
        self._stream = sys.stderr
        self._isatty = bool(getattr(self._stream, "isatty", lambda: False)())
        self._live_len = 0  # width of the pending \r-rewritten line (0 = none)
        self._last_draw = 0.0
        self._last_milestone = 0

    def stage(self, line: str) -> None:
        self.finish()
        self._stream.write(line + "\n")
        self._stream.flush()
        self._last_milestone = 0

    def tick(self, done: int, total: int) -> None:
        if self._isatty:
            now = time.monotonic()
            if done != total and now - self._last_draw < self.MIN_INTERVAL_S:
                return
            text = f"pass 0+1: parsing {done}/{total} file(s)..."
            # Pad over any leftover from a longer previous draw.
            pad = " " * max(self._live_len - len(text), 0)
            self._stream.write("\r" + text + pad)
            self._stream.flush()
            self._live_len = len(text)
            self._last_draw = now
        elif done == total or done - self._last_milestone >= self.MILESTONE_EVERY:
            self._stream.write(f"pass 0+1: parsing {done}/{total} file(s)\n")
            self._stream.flush()
            self._last_milestone = done

    def finish(self) -> None:
        if self._live_len:
            self._stream.write("\n")
            self._stream.flush()
            self._live_len = 0


def _json_default(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return str(value)


def _emit_json(payload: Any) -> None:
    click.echo(json.dumps(payload, indent=2, default=_json_default))


def _resolve_db(db_path: Path | None) -> Path:
    """The database path to read, defaulting to the nearest one upward."""
    if db_path is None:
        db_path = find_db(Path.cwd())
        if db_path is None:
            raise click.ClickException(
                "no .hdl-kgraph/graph.db found here or in any parent directory; "
                "run `hdl-kgraph build` first or pass --db"
            )
    if not db_path.is_file():
        raise click.ClickException(f"database not found: {db_path}")
    return db_path


def _load(db_path: Path | None) -> tuple[nx.MultiDiGraph, list, dict[str, str]]:
    try:
        return SqliteStore(_resolve_db(db_path)).load()
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


def _echo_build_report(report: BuildReport, verbose: bool = False) -> None:
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
        if verbose:
            for path, count in sorted(report.file_errors.items()):
                click.echo(f"      {path}: {count} error(s)")
                details = report.file_error_details.get(path, [])
                for detail in details:
                    click.echo(f"        {detail}")
                if details and count > len(details):
                    click.echo(f"        ... and {count - len(details)} more")
    if report.macros_defined:
        click.echo(f"  macros defined: {report.macros_defined}")
    if report.includes_resolved or report.includes_unresolved:
        includes = f"  includes:       {report.includes_resolved} resolved"
        if report.includes_unresolved:
            includes += f", {report.includes_unresolved} unresolved"
        click.echo(includes)
        if verbose and report.includes_unresolved:
            search = ", ".join(report.incdirs) if report.incdirs else "(no incdirs configured)"
            click.echo(f"      `include search path: {search}")
    if report.preproc_warning_count:
        click.echo(f"  preprocessor warnings: {report.preproc_warning_count}")
        if verbose:
            for warning in report.preproc_warnings:
                click.echo(f"      {warning}")
    if report.both_branches:
        click.echo("  both-branches mode: no defines given; `ifdef alternatives kept at 0.6")
    click.echo(f"  nodes: {report.node_count}  edges: {report.edge_count}")
    if report.unresolved_count:
        click.echo(f"  unresolved: {report.unresolved_count}")
    if report.enriched:
        backends = ", ".join(report.enrich_backends) or "(no matching files)"
        click.echo(f"  enriched via {backends}: {report.edges_upgraded} edge(s) upgraded")
        if report.discrepancy_count:
            click.echo(
                f"  discrepancies: {report.discrepancy_count} "
                "(`hdl-kgraph discrepancies` lists them)"
            )
        if verbose:
            click.echo(f"      nodes added:       {report.enrich_nodes_added}")
            click.echo(f"      generates unrolled: {report.enrich_generates_unrolled}")
            click.echo("      (full delta: `hdl-kgraph enriched`)")
            for diag in report.enrich_diagnostics:
                click.echo(f"      {diag}")
    if not verbose and (report.error_files or report.preproc_warning_count):
        click.echo("  (per-file details: re-run with -v, or `hdl-kgraph status --errors`)")


def _echo_file_diagnostics(files: list, as_json: bool = False) -> None:
    """``status --errors``: per-file parse errors, warnings, skip reasons."""
    flagged = sorted(
        (f for f in files if f.skipped_reason is not None or f.parse_error_count or f.warnings),
        key=lambda f: f.path,
    )
    if as_json:
        _emit_json(
            [
                {
                    "path": f.path,
                    "parse_errors": f.parse_error_count,
                    "errors": f.parse_errors,
                    "skipped_reason": f.skipped_reason,
                    "warnings": f.warnings,
                }
                for f in flagged
            ]
        )
        return
    for f in flagged:
        if f.skipped_reason is not None:
            click.echo(f"{f.path}: skipped ({f.skipped_reason})")
            continue
        if f.parse_error_count:
            click.echo(f"{f.path}: {f.parse_error_count} parse error(s)")
            for detail in f.parse_errors:
                click.echo(f"  {detail}")
            if f.parse_errors and f.parse_error_count > len(f.parse_errors):
                click.echo(f"  ... and {f.parse_error_count - len(f.parse_errors)} more")
        for warning in f.warnings:
            click.echo(f"{f.path}: warning: {warning}")
    if not flagged:
        click.echo("no parse errors, preprocessor warnings, or skipped files")


@main.command()
@_input_options
@_verbose_option
@_jobs_option
@_enrich_option
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
    verbose: bool,
    jobs: int | None,
    enrich: bool,
) -> None:
    """Build the knowledge graph from HDL sources under SOURCE."""
    options = _resolve_options(
        source, filelists, defines, incdirs, libs, config_path, no_config, excludes, max_file_size
    )
    options.jobs = jobs
    options.enrich = options.enrich or enrich
    renderer = _ProgressRenderer()
    report = run_build(
        source, db_path=db_path, options=options, progress=renderer.stage, tick=renderer.tick
    )
    renderer.finish()
    _echo_build_report(report, verbose=verbose)


def _echo_update_report(report: UpdateReport, verbose: bool = False) -> None:
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
    _echo_build_report(report.build, verbose=verbose)
    click.echo(f"  updated in {report.elapsed_s:.2f}s")


@main.command()
@_input_options
@_verbose_option
@_jobs_option
@_enrich_option
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
    verbose: bool,
    jobs: int | None,
    enrich: bool,
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
    options.jobs = jobs
    options.enrich = options.enrich or enrich
    renderer = _ProgressRenderer()
    report = run_update(
        source,
        db_path=db_path,
        options=options,
        full=full,
        progress=renderer.stage,
        tick=renderer.tick,
    )
    renderer.finish()
    _echo_update_report(report, verbose=verbose)


@main.command("detect-changes")
@_input_options
@_json_option
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
    "--svn",
    "svn_rev",
    is_flag=False,
    flag_value="BASE",
    default=None,
    metavar="[REV]",
    help="Diff the svn working copy against a revision (default BASE).",
)
@click.option(
    "--p4",
    "p4_rev",
    is_flag=False,
    flag_value="have",
    default=None,
    metavar="[CL]",
    help="List Perforce workspace changes (opened + reconciled local edits).",
)
@click.option(
    "--vcs",
    "use_vcs",
    is_flag=True,
    help="Diff against the repo's auto-detected VCS (git/svn/p4) vs its "
    "default ref, instead of the last build's content hashes.",
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
    as_json: bool,
    git_ref: str | None,
    svn_rev: str | None,
    p4_rev: str | None,
    use_vcs: bool,
    closure: bool,
) -> None:
    """List build inputs that changed since the last build (or a VCS ref).

    Prints one ``M``/``A``/``D`` line per modified/added/deleted file
    (``~`` for closure-dirtied files with --closure). Diff against version
    control with --git/--svn/--p4 (each takes an optional ref), or --vcs to
    auto-detect which VCS the tree uses. Exit codes follow the
    ``git diff --exit-code`` convention so scripts can tell the cases apart:
    0 nothing changed, 1 changes detected, 2 error (missing database, bad
    config, ...).
    """
    # Errors must not exit 1 — scripts read 1 as "changed", per the docstring.
    try:
        options = _resolve_options(
            source,
            filelists,
            defines,
            incdirs,
            libs,
            config_path,
            no_config,
            excludes,
            max_file_size,
        )
        base = source.resolve()
        base = base.parent if base.is_file() else base
        explicit = [
            (vcs, ref)
            for vcs, ref in (("git", git_ref), ("svn", svn_rev), ("p4", p4_rev))
            if ref is not None
        ]
        if use_vcs and explicit:
            raise click.ClickException("--vcs cannot be combined with --git/--svn/--p4")
        if len(explicit) > 1:
            raise click.ClickException("choose only one of --git/--svn/--p4")
        if explicit or use_vcs:
            if explicit:
                vcs, ref = explicit[0]
            else:  # bare --vcs: auto-detect the tree's VCS, default ref
                detected = detect_vcs(base)
                if detected is None:
                    raise click.ClickException(
                        "could not detect a VCS (no .git/.svn, no P4 environment); "
                        "pass --git/--svn/--p4"
                    )
                vcs, ref = detected, None
            try:
                changes = detect_vcs_changes(base, vcs, ref, SUFFIXES)
            except RuntimeError as exc:
                raise click.ClickException(str(exc)) from exc
        else:
            if db_path is None:
                db_path = default_db_path(base)
            if not db_path.is_file():
                raise click.ClickException(
                    f"no database at {db_path}; run `hdl-kgraph build` first, "
                    "or use --git/--svn/--p4/--vcs"
                )
            try:
                changes = scan_changes(source, db_path, options)
            except SchemaVersionError as exc:
                raise click.ClickException(str(exc)) from exc
        dirtied: list[dict[str, str]] = []
        if closure and (changes.changed or changes.removed):
            graph, _, _ = _load(db_path if db_path is not None else find_db(base))
            seeds = {path: "" for path in (*changes.changed, *changes.removed)}
            dirtied = [
                {"path": path, "via": why}
                for path, why in sorted(dirty_closure(graph, seeds).items())
                if path not in seeds
            ]
    except click.ClickException as exc:
        exc.exit_code = 2
        raise
    if as_json:
        payload: dict[str, Any] = {
            "changed": changes.changed,
            "added": changes.added,
            "removed": changes.removed,
        }
        if closure:
            payload["closure"] = dirtied
        _emit_json(payload)
    else:
        for path in changes.changed:
            click.echo(f"M {path}")
        for path in changes.added:
            click.echo(f"A {path}")
        for path in changes.removed:
            click.echo(f"D {path}")
        for entry in dirtied:
            click.echo(f"~ {entry['path']} ({entry['via']})")
    if changes:
        sys.exit(1)


@main.command()
@click.argument("target")
@_db_option
@_json_option
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
def impact(
    target: str, db_path: Path | None, as_json: bool, max_depth: int, show_files: bool
) -> None:
    """Show what a change to TARGET (a file path or design unit) affects.

    Walks reverse INSTANTIATES/IMPORTS/INCLUDES/EXTENDS (plus VHDL
    USES_PACKAGE/IMPLEMENTS/BINDS and macro use) edges transitively: the
    instantiating parents, importers, includers, and subclasses that a
    change to TARGET can break.
    """
    graph, files, _ = _load(db_path)
    seeds = analysis.impact_seeds(graph, files, target)
    if not seeds:
        raise click.ClickException(f"{target!r} matches no file or design unit in the graph")
    records = analysis.impact_radius(graph, seeds, max_depth=max_depth)
    if show_files:
        affected = sorted({r.file for r in records if r.file})
        if as_json:
            _emit_json(affected)
            return
        for path in affected:
            click.echo(path)
        return
    if as_json:
        _emit_json(records)
        return
    if not records:
        click.echo("no dependents found")
        return
    for r in records:
        location = f"{r.file}:{r.line}" if r.file and r.line else r.file
        click.echo(
            f"{r.kind.value:13} {r.name:30} {location:30} <- {r.via.value} (depth {r.depth})"
        )


@main.command()
@_input_options
@_verbose_option
@_jobs_option
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
    verbose: bool,
    jobs: int | None,
    debounce: int,
) -> None:
    """Watch SOURCE and incrementally update the graph on every save burst."""
    from hdl_kgraph.watch import WatchUnavailableError, run_watch

    options = _resolve_options(
        source, filelists, defines, incdirs, libs, config_path, no_config, excludes, max_file_size
    )
    options.jobs = jobs

    renderer = _ProgressRenderer()

    def on_report(report: UpdateReport) -> None:
        renderer.finish()
        if report.up_to_date or report.build is None:
            click.echo(f"up to date ({report.elapsed_s:.2f}s)")
            return
        if report.full_rebuild_reason is not None:
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
        if verbose:
            for path, count in sorted(report.build.file_errors.items()):
                click.echo(f"    parse errors: {path}: {count}")
                for detail in report.build.file_error_details.get(path, []):
                    click.echo(f"      {detail}")
            for warning in report.build.preproc_warnings:
                click.echo(f"    warning: {warning}")

    def on_error(exc: BaseException) -> None:
        renderer.finish()
        click.echo(f"update failed: {exc} (still watching)", err=True)

    click.echo(f"watching {source.resolve()} (Ctrl-C to stop)")
    try:
        run_watch(
            source,
            db_path=db_path,
            options=options,
            quiet_s=debounce / 1000,
            on_report=on_report,
            progress=renderer.stage,
            tick=renderer.tick,
            on_error=on_error,
        )
    except WatchUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        click.echo("stopped")


@main.command()
@_db_option
@_json_option
@click.option(
    "--errors",
    "show_errors",
    is_flag=True,
    help="List files with parse errors, preprocessor warnings, and skipped "
    "files (with reasons) instead of the statistics.",
)
def status(db_path: Path | None, as_json: bool, show_errors: bool) -> None:
    """Show graph statistics for the current build."""
    graph, files, meta = _load(db_path)
    if show_errors:
        _echo_file_diagnostics(files, as_json)
        return
    # Filelists are recorded for M4 incremental rebuilds but are not parsed
    # HDL sources; report them on their own line.
    parsed = [f for f in files if f.skipped_reason is None and f.language is not Language.UNKNOWN]
    filelists = [f for f in files if f.skipped_reason is None and f.language is Language.UNKNOWN]
    skipped = Counter(f.skipped_reason for f in files if f.skipped_reason is not None)
    error_files = [f for f in parsed if f.parse_error_count]
    warning_count = sum(len(f.warnings) for f in files)
    total_errors = sum(f.parse_error_count for f in error_files)
    node_kinds = Counter(data["kind"].value for _, data in graph.nodes(data=True))
    edge_kinds = Counter(data["kind"].value for _, _, data in graph.edges(data=True))
    stubs = analysis.unresolved_stubs(graph)
    if as_json:
        _emit_json(
            {
                "root": meta.get("root"),
                "built_at": meta.get("built_at"),
                "tool_version": meta.get("tool_version"),
                "files": {
                    "parsed": len(parsed),
                    "filelists": len(filelists),
                    "skipped": dict(skipped),
                    "parse_errors": total_errors,
                    "error_files": len(error_files),
                    "preproc_warnings": warning_count,
                },
                "nodes": {"total": graph.number_of_nodes(), "kinds": dict(node_kinds)},
                "edges": {"total": graph.number_of_edges(), "kinds": dict(edge_kinds)},
                "unresolved": len(stubs),
            }
        )
        return
    click.echo(f"root:     {meta.get('root', '?')}")
    click.echo(f"built at: {meta.get('built_at', '?')} (hdl-kgraph {meta.get('tool_version')})")
    click.echo(f"files:    {len(parsed)} parsed")
    if filelists:
        click.echo(f"          {len(filelists)} filelist(s)")
    for reason, count in sorted(skipped.items()):
        click.echo(f"          {count} skipped ({reason})")
    if error_files:
        click.echo(f"          {total_errors} parse error(s) in {len(error_files)} file(s)")
    if warning_count:
        click.echo(f"          {warning_count} preprocessor warning(s)")
    if error_files or warning_count:
        click.echo("          (`hdl-kgraph status --errors` lists them per file)")
    click.echo(f"nodes:    {graph.number_of_nodes()}")
    for kind, count in node_kinds.most_common():
        click.echo(f"          {count:6} {kind}")
    click.echo(f"edges:    {graph.number_of_edges()}")
    for kind, count in edge_kinds.most_common():
        click.echo(f"          {count:6} {kind}")
    if stubs:
        click.echo(f"unresolved: {len(stubs)}")


@main.command()
@_db_option
@_json_option
def discrepancies(db_path: Path | None, as_json: bool) -> None:
    """List where native-frontend elaboration disagreed with the heuristic graph.

    Populated by ``build --enrich`` (M7). Each finding names the kind of
    disagreement (e.g. ``instance_count`` for a generate loop the tree-sitter
    tier counted as one instance), the design node, and the heuristic vs
    elaborated values.
    """
    try:
        items = SqliteStore(_resolve_db(db_path)).load_discrepancies()
    except SchemaVersionError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit_json([dataclasses.asdict(d) for d in items])
        return
    if not items:
        click.echo("no discrepancies (run `hdl-kgraph build --enrich` to record them)")
        return
    by_kind = Counter(d.kind for d in items)
    click.echo(f"{len(items)} discrepancy finding(s):")
    for kind, count in by_kind.most_common():
        click.echo(f"  {count:6} {kind}")
    for d in items:
        click.echo(f"[{d.kind}] {d.detail} (via {d.backend})")
        if d.heuristic or d.elaborated:
            click.echo(f"    heuristic: {d.heuristic or '-'}  elaborated: {d.elaborated or '-'}")


@main.command()
@_db_option
@_json_option
def enriched(db_path: Path | None, as_json: bool) -> None:
    """Report exactly what ``build --enrich`` added vs the default build.

    Reconstructed from the stored graph's elaboration stamps (M7): heuristic
    edges promoted to elaboration-accurate facts, elaborated nodes/edges added
    by unrolling generate loops and instance arrays, and the discrepancies where
    elaboration disagreed with the heuristic graph. Read-only — no rebuild.
    """
    try:
        store = SqliteStore(_resolve_db(db_path))
        graph, _files, _meta = store.load()
        items = store.load_discrepancies()
    except SchemaVersionError as exc:
        raise click.ClickException(str(exc)) from exc
    summary = summarize_enrichment(graph)
    if as_json:
        _emit_json(
            {
                "summary": dataclasses.asdict(summary),
                "discrepancies": [dataclasses.asdict(d) for d in items],
            }
        )
        return
    if not summary.enriched and not items:
        click.echo("not enriched (run `hdl-kgraph build --enrich`)")
        return
    backends = ", ".join(summary.backends) or "(none)"
    click.echo(f"enrichment via {backends}:")
    click.echo(f"  edges upgraded:     {summary.edges_upgraded}")
    click.echo(f"  edges added:        {summary.edges_added}")
    click.echo(f"  nodes added:        {summary.nodes_added}")
    click.echo(f"  generates unrolled: {summary.generates_unrolled}")
    click.echo(f"  discrepancies:      {len(items)}")
    if not items:
        return
    by_kind = Counter(d.kind for d in items)
    for kind, count in by_kind.most_common():
        click.echo(f"    {count:6} {kind}")
    for d in items:
        click.echo(f"[{d.kind}] {d.detail} (via {d.backend})")
        if d.heuristic or d.elaborated:
            click.echo(f"    heuristic: {d.heuristic or '-'}  elaborated: {d.elaborated or '-'}")


def _lint_config(
    db_path: Path | None, meta: dict[str, str], config_path: Path | None, no_config: bool
) -> BuildConfig:
    """The config whose ``[lint]`` section (and ``[build].top``) lint honors."""
    if no_config:
        return BuildConfig()
    if config_path is None:
        root = Path(meta.get("root") or "")
        if not root.is_dir():  # project moved since the build: the DB is ground truth
            root = db_path.parent.parent if db_path is not None else Path.cwd()
        config_path = find_config(root)
        if config_path is None:
            return BuildConfig()
    try:
        return BuildConfig.load(config_path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _waiver_desc(waiver: LintWaiver) -> str:
    parts = [f"check={waiver.check}"]
    parts += [
        f"{key}={getattr(waiver, key)}"
        for key in ("name", "module", "file", "line")
        if getattr(waiver, key) is not None
    ]
    return ", ".join(parts)


def _lint_row(f: lint.LintFinding, suffix: str = "") -> str:
    location = f"{f.file}:{f.line}" if f.file else "?"
    return f"{f.check:18} {f.name:32} {location:28} {f.message}{suffix}"


@main.command("lint")
@_db_option
@_json_option
@click.option(
    "--check",
    "checks",
    multiple=True,
    metavar="NAME",
    help=f"Run only this check (repeatable). Available: {', '.join(sorted(lint.CHECKS))}.",
)
@click.option(
    "--top",
    "tops",
    multiple=True,
    metavar="NAME",
    help="Treat NAME as an intended top module (repeatable, additive with "
    "[build].top; exempts it from dead-module).",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to hdl-kgraph.toml (default: nearest one from the build root upward).",
)
@click.option("--no-config", is_flag=True, help="Ignore any hdl-kgraph.toml.")
@click.option(
    "--waiver-file",
    "waiver_files",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="Apply the [[lint.waivers]] entries of this TOML file too "
    "(repeatable; after config waivers).",
)
@click.option("--show-waived", is_flag=True, help="List the waived findings as well.")
def lint_cmd(
    db_path: Path | None,
    as_json: bool,
    checks: tuple[str, ...],
    tops: tuple[str, ...],
    config_path: Path | None,
    no_config: bool,
    waiver_files: tuple[Path, ...],
    show_waived: bool,
) -> None:
    """Report graph-level lint findings (always exits 0 — a report, not a gate).

    Signal-level checks skip files with parse errors and implicit-net stubs
    so a finding is worth reading; confidences below 1.0 mark heuristics.
    Known-benign findings can be waived via [[lint.waivers]] in
    hdl-kgraph.toml or a --waiver-file; waivers that match nothing are
    reported stale on stderr.
    """
    graph, files, meta = _load(db_path)
    config = _lint_config(db_path, meta, config_path, no_config)
    waiver_warnings = list(config.warnings)
    waivers = list(config.lint_waivers)
    for path in waiver_files:
        try:
            waivers.extend(load_waivers(path, waiver_warnings))
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
    for warning in waiver_warnings:
        click.echo(f"warning: {warning}", err=True)
    error_files = frozenset(f.path for f in files if f.parse_error_count)
    try:
        findings = lint.run_checks(
            graph,
            names=checks or None,
            tops=frozenset(tops) | frozenset(config.top),
            error_files=error_files,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    result = lint.apply_waivers(findings, waivers, checks or None)
    for i in result.unknown:
        click.echo(
            f"warning: lint waiver #{i + 1} names unknown check '{waivers[i].check}'", err=True
        )
    for i in result.unused:
        click.echo(
            f"warning: lint waiver #{i + 1} ({_waiver_desc(waivers[i])}) matched nothing",
            err=True,
        )
    if as_json:
        _emit_json(
            {
                "findings": result.kept,
                "waived": result.waived,
                "unused_waivers": result.unused,
                "counts": {"findings": len(result.kept), "waived": len(result.waived)},
            }
        )
        return
    for f in result.kept:
        marker = "" if f.confidence >= 0.8 else f"  [~{f.confidence:.1f}]"
        click.echo(_lint_row(f, marker))
    if show_waived and result.waived:
        click.echo("waived:")
        for wf in result.waived:
            click.echo(_lint_row(wf.finding, f"  [waived: {wf.reason}]"))
    if waivers:
        if result.kept:
            click.echo(f"{len(result.kept)} finding(s), {len(result.waived)} waived")
        else:
            click.echo(f"no findings ({len(result.waived)} waived)")
    elif not result.kept:
        click.echo("no findings")


@main.command("metrics")
@_db_option
@_json_option
# --limit, not --top: --top means "top module" in lint/visualize, and this is
# a row count. Renamed before 1.0 freezes the surface (issue #22).
@click.option(
    "-n",
    "--limit",
    "top_n",
    type=int,
    default=10,
    show_default=True,
    metavar="N",
    help="How many units to list (0 = all).",
)
@click.option(
    "--communities",
    "show_communities",
    is_flag=True,
    help="Also report Louvain communities (subsystem suggestions).",
)
def metrics_cmd(db_path: Path | None, as_json: bool, top_n: int, show_communities: bool) -> None:
    """Module fan-in/fan-out, hub/bridge detection, community discovery.

    Metrics are computed on the module-level instantiation projection;
    units are listed hubs-first (descending betweenness centrality).
    """
    graph, _, _ = _load(db_path)
    result = metrics.module_metrics(graph)
    records = result.modules
    parts = metrics.communities(graph) if show_communities else []
    if as_json:
        payload: dict[str, Any] = {
            "modules": records,
            "betweenness_approximate": result.betweenness_approximate,
        }
        if show_communities:
            payload["communities"] = parts
        _emit_json(payload)
        return
    if not records:
        click.echo("no design units in the graph")
        return
    shown = records if top_n <= 0 else records[:top_n]
    click.echo(f"{'unit':30} {'fan-in':>6} {'fan-out':>7} {'betweenness':>12}")
    for m in shown:
        markers = ""
        if m.is_articulation:
            markers += " [bridge]"
        if m.unresolved:
            markers += " [?]"
        click.echo(f"{m.name:30} {m.fan_in:>6} {m.fan_out:>7} {m.betweenness:>12.4f}{markers}")
    if result.betweenness_approximate:
        click.echo(
            f"note: betweenness sampled (k={metrics.BETWEENNESS_SAMPLES}, "
            f"seed {metrics.BETWEENNESS_SEED}) — graph exceeds "
            f"{metrics.BETWEENNESS_EXACT_MAX_NODES} units"
        )
    if show_communities:
        click.echo(f"communities: {len(parts)}")
        for i, part in enumerate(parts):
            names = ", ".join(sorted(graph.nodes[n]["name"] for n in part))
            click.echo(f"    [{i}] {names}")


@main.command("visualize")
@_db_option
@click.option(
    "-o",
    "--output",
    "output",
    type=click.Path(path_type=Path),
    default=Path("graph.html"),
    show_default=True,
    help="Output HTML file.",
)
@click.option(
    "--full",
    is_flag=True,
    help="Embed every node and edge (default: the module-level projection, "
    "which stays responsive on large designs).",
)
@click.option(
    "--top",
    "top",
    default=None,
    help="Root the hierarchy and graph views at this module (constrains both to its subtree).",
)
@click.option("--title", default=None, help="Page title (default: the build root's name).")
@click.option(
    "--layout",
    type=click.Choice(["auto", "live", "static"]),
    default="auto",
    show_default=True,
    help="Layout tier: 'live' runs the in-browser force simulation; 'static' "
    "ships precomputed coordinates (needs the [layout] extra); 'auto' routes "
    "by graph size.",
)
@click.option(
    "--force-inline",
    "force_inline",
    is_flag=True,
    help="Write the HTML even if the payload exceeds the inline size limit.",
)
@click.option(
    "--collapse",
    is_flag=True,
    help="Aggregate into one supernode per community (double-click to expand in "
    "the browser). With --full it is two-level: communities of units, each "
    "expandable to its leaf nodes.",
)
@click.option("--open", "open_browser", is_flag=True, help="Open the result in a browser.")
def visualize(
    db_path: Path | None,
    output: Path,
    full: bool,
    top: str | None,
    title: str | None,
    layout: str,
    force_inline: bool,
    collapse: bool,
    open_browser: bool,
) -> None:
    """Render a self-contained interactive HTML view of the graph.

    The file embeds D3 and the graph data — no network access needed to
    open it. Two views: a collapsible hierarchy and a force-directed graph
    with node-kind / edge-kind / community filters (and colour-by-community).
    Large designs route to
    a precomputed 'static' layout so the graph view paints without a
    client-side simulation freeze; ``--collapse`` shows one supernode per
    subsystem instead of every unit (see docs/viz-scalability.md).
    """
    from hdl_kgraph.viz import render_html

    graph, _, meta = _load(db_path)
    if title is None:
        root = meta.get("root", "")
        title = f"hdl-kgraph: {Path(root).name}" if root else "hdl-kgraph"
    try:
        result = render_html(
            graph,
            output,
            full=full,
            top=top,
            title=title,
            layout=layout,
            force_inline=force_inline,
            collapse=collapse,
        )
    except ValueError as exc:  # --top names nothing: error out, like `tree`
        raise click.ClickException(str(exc)) from exc
    if result.note:
        click.echo(result.note, err=True)
    click.echo(f"wrote {result.path}")
    if open_browser:
        import webbrowser

        webbrowser.open(result.path.resolve().as_uri())


@main.command("export")
@_db_option
@click.option(
    "-o",
    "--output",
    "output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output file (default: graph.<format>).",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(EXPORT_FORMATS),
    default="graphml",
    show_default=True,
    help="Interchange format: 'graphml'/'gexf' for Gephi & Cytoscape, 'json' for node-link data.",
)
def export_cmd(db_path: Path | None, output: Path | None, fmt: str) -> None:
    """Export the graph to GraphML/GEXF/JSON for external tools.

    The escape hatch for designs too large for the inline HTML artifact:
    Gephi (OpenOrd/ForceAtlas2) and Cytoscape handle graphs the browser
    cannot (see docs/viz-scalability.md).
    """
    from hdl_kgraph.export import export_graph

    graph, _, _ = _load(db_path)
    if output is None:
        output = Path(f"graph.{fmt}")
    try:
        path = export_graph(graph, output, fmt)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"wrote {path}")


@main.group()
def query() -> None:
    """Query the knowledge graph."""


@query.command("instances-of")
@click.argument("name")
@_db_option
@_json_option
def instances_of(name: str, db_path: Path | None, as_json: bool) -> None:
    """List all instantiation sites of design units named NAME."""
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
        count = sum(
            1
            for _, _, edge in graph.in_edges(node_id, data=True)
            if edge["kind"] is EdgeKind.INSTANTIATES
        )
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
    """List what drives (or reads) signals named SIGNAL."""
    graph, _, _ = _load(db_path)
    records = analysis.signal_drivers(graph, signal, module=module, readers=readers)
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


@main.command()
@click.argument("top", required=False)
@click.option("--depth", type=int, default=64, show_default=True, help="Maximum tree depth.")
@_db_option
@_json_option
def tree(top: str | None, depth: int, db_path: Path | None, as_json: bool) -> None:
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

    trees = [analysis.hierarchy_tree(graph, root, max_depth=depth) for root in roots]
    if as_json:
        _emit_json(trees)
        return
    for hierarchy in trees:
        _print_tree(hierarchy, prefix="", is_last=True)


@main.command()
@click.option(
    "--mcp",
    "mcp_mode",
    is_flag=True,
    help="Run the MCP server (the default and only mode; kept for compatibility).",
)
@_db_option
@click.option(
    "--http",
    "http_addr",
    metavar="HOST:PORT",
    default=None,
    help="Serve over streamable HTTP instead of stdio. No authentication — "
    "the graph exposes your design's structure, so bind 127.0.0.1 unless "
    "every host on the network is trusted.",
)
def serve(mcp_mode: bool, db_path: Path | None, http_addr: str | None) -> None:
    """Serve the knowledge graph to AI assistants over MCP (read-only).

    Speaks MCP on stdio by default (the transport assistant configs use);
    ``--http`` exposes the same tools over streamable HTTP instead — with
    no authentication, so keep it bound to loopback (see docs/mcp.md). The
    server only ever reads the database — rebuild with ``build``/``update``
    (a running server picks up the new database automatically).
    """
    del mcp_mode  # MCP is the default and only mode; the flag is a no-op
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
        SqliteStore(db_path).load_meta()  # schema check before the MCP handshake
    except SchemaVersionError as exc:
        raise click.ClickException(str(exc)) from exc

    from hdl_kgraph.mcp import McpUnavailableError, create_server

    try:
        server = create_server(db_path)
    except McpUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
    if http_addr is None:
        server.run()
        return
    host, _, port_text = http_addr.rpartition(":")
    if not host or not port_text.isdigit():
        raise click.ClickException(f"--http expects HOST:PORT, got {http_addr!r}")
    if host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        click.echo(
            f"warning: serving on {host} exposes your design's structure to the "
            "network with no authentication; bind 127.0.0.1 unless every host "
            "is trusted (see docs/mcp.md)",
            err=True,
        )
    server.run(transport="http", host=host, port=int(port_text))


@main.command()
@_db_option
@click.option(
    "--assistant",
    "assistants",
    multiple=True,
    help="Only configure these assistants (repeatable; default: all detected).",
)
@click.option("--list", "list_only", is_flag=True, help="Only report what is detected.")
@click.option("--dry-run", is_flag=True, help="Show what would be written without writing.")
@click.option("--yes", "assume_yes", is_flag=True, help="Configure without confirmation prompts.")
def setup(
    db_path: Path | None,
    assistants: tuple[str, ...],
    list_only: bool,
    dry_run: bool,
    assume_yes: bool,
) -> None:
    """Detect installed AI assistants and configure them to use this graph.

    Writes (or updates) the ``hdl-kgraph`` MCP server entry in each detected
    assistant's config — project-scope files for Claude Code (``.mcp.json``),
    Cursor (``.cursor/mcp.json``), and VS Code (``.vscode/mcp.json``);
    user-level files for Claude Desktop, Codex (``~/.codex/config.toml``),
    Windsurf, and Gemini CLI. Re-running is safe: the entry is updated in
    place and everything else in the file is preserved.
    """
    from hdl_kgraph.mcp.setup import detect_targets, plan_entry, write_config

    targets = detect_targets()
    if assistants:
        known = {t.name for t in targets}
        unknown = [a for a in assistants if a not in known]
        if unknown:
            raise click.ClickException(
                f"unknown assistant(s) {', '.join(unknown)}; known: {', '.join(sorted(known))}"
            )
        targets = [t for t in targets if t.name in assistants]
    detected = [t for t in targets if t.detected]
    for target in targets:
        state = "detected" if target.detected else "not detected"
        click.echo(f"{target.name:15} {state}  ({target.config_path})")
    if list_only:
        return
    if not detected:
        raise click.ClickException(
            "no supported AI assistant detected; see docs/mcp.md for manual setup"
        )

    if db_path is None:
        db_path = find_db(Path.cwd())
        if db_path is None:
            raise click.ClickException(
                "no .hdl-kgraph/graph.db found here or in any parent directory; "
                "run `hdl-kgraph build` first or pass --db"
            )
    entry = plan_entry(db_path.resolve())

    try:
        import fastmcp  # noqa: F401
    except ImportError:
        click.echo(
            "warning: fastmcp is not installed — the configured server will not "
            "start until you `pip install 'hdl-kgraph[mcp]'`",
            err=True,
        )

    for target in detected:
        if dry_run:
            click.echo(f"would write {target.config_path}:")
            try:
                click.echo(target.preview(entry), nl=False)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            continue
        if not assume_yes and not click.confirm(f"configure {target.name}?", default=True):
            continue
        try:
            changed = write_config(target, entry)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"{target.config_path}: {'updated' if changed else 'already up to date'}")


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
