"""hdl-kgraph CLI: the ``bench-link`` incremental-link locality command."""

from __future__ import annotations

from pathlib import Path

import click

from hdl_kgraph.cli._common import CliError, _resolve_db
from hdl_kgraph.cli._options import _db_option, _json_option
from hdl_kgraph.cli.render import emit_json as _emit_json
from hdl_kgraph.linkbench import link_locality
from hdl_kgraph.storage.sqlite_store import SchemaVersionError


@click.command("bench-link")
@_db_option
@_json_option
@click.option(
    "--sample",
    type=int,
    default=None,
    help="Evaluate only N files (evenly strided) for a quick estimate on large designs.",
)
def bench_link(db_path: Path | None, as_json: bool, sample: int | None) -> None:
    """Report incremental-link locality — how much of the design a single-file
    edit re-resolves vs a full re-link.

    Content-free (counts and ratios only). A low ``locality_ratio`` means a
    bounded incremental linker (#119) would re-resolve only a small fraction of
    the refs per edit; a ratio near 1 means edits ripple design-wide. Computed
    from the persisted ``ref_index`` + include/macro dependency graph, so it runs
    on a built ``graph.db`` with no source tree.
    """
    db = _resolve_db(db_path)
    try:
        report = link_locality(db, sample=sample)
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    if as_json:
        _emit_json(report)
        return
    t = report["totals"]
    rr = report["reresolved_refs"]
    lr = report["locality_ratio"]
    click.echo(f"hdl-kgraph bench-link (schema {report['schema']}, content-free)")
    click.echo(f"  files {t['files']}  refs {t['refs']}  nodes {t['nodes']}  edges {t['edges']}")
    click.echo(
        f"  refs re-resolved per single-file edit: "
        f"p50 {rr['p50']:.0f}  p90 {rr['p90']:.0f}  max {rr['max']:.0f}  mean {rr['mean']:.1f}"
    )
    click.echo(
        f"  locality ratio (re-resolved / full re-link): "
        f"p50 {lr['p50']:.2%}  p90 {lr['p90']:.2%}  max {lr['max']:.2%}"
    )
    click.echo("  (use --json for the full content-free report)")
