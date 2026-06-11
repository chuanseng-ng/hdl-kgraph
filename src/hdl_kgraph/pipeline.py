"""Build pipeline: discover -> pass 1 (parse) -> pass 2 (link) -> persist.

Keeps the CLI thin and gives later milestones (M4 incremental rebuilds, M6
MCP server) a reusable entry point. Pass 1 runs serially in M1; the per-file
IR design keeps it embarrassingly parallel for when it matters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hdl_kgraph.discovery import DEFAULT_MAX_FILE_SIZE_KB, DiscoveredFile, discover
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.storage.sqlite_store import FileMeta, SqliteStore

DB_DIRNAME = ".hdl-kgraph"
DB_FILENAME = "graph.db"


@dataclass
class BuildReport:
    """Summary of one ``build`` run."""

    root: Path
    db_path: Path
    parsed_files: int = 0
    error_files: int = 0
    parse_error_count: int = 0
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    node_count: int = 0
    edge_count: int = 0
    unresolved_count: int = 0


def default_db_path(root: Path) -> Path:
    return root / DB_DIRNAME / DB_FILENAME


def find_db(start: Path) -> Path | None:
    """Locate the nearest database from *start* upward (git-style)."""
    for directory in [start.resolve(), *start.resolve().parents]:
        candidate = directory / DB_DIRNAME / DB_FILENAME
        if candidate.is_file():
            return candidate
    return None


def run_build(
    root: Path,
    db_path: Path | None = None,
    exclude: tuple[str, ...] = (),
    max_file_size_kb: int = DEFAULT_MAX_FILE_SIZE_KB,
) -> BuildReport:
    root = root.resolve()
    base = root.parent if root.is_file() else root
    db_path = db_path if db_path is not None else default_db_path(base)
    report = BuildReport(root=root, db_path=db_path)

    discovered: list[DiscoveredFile] = discover(
        root, exclude=exclude, max_file_size_kb=max_file_size_kb
    )

    parser = SystemVerilogParser()
    irs = []
    files_meta: list[FileMeta] = []
    for found in discovered:
        if found.skipped_reason is not None:
            report.skipped[found.skipped_reason] = report.skipped.get(found.skipped_reason, 0) + 1
            files_meta.append(
                FileMeta(
                    path=found.relpath,
                    language=found.language,
                    content_hash=found.content_hash,
                    size_bytes=found.size_bytes,
                    skipped_reason=found.skipped_reason,
                )
            )
            continue
        ir = parser.parse(Path(found.relpath), found.path.read_text(errors="replace"))
        irs.append(ir)
        report.parsed_files += 1
        if ir.parse_error_count:
            report.error_files += 1
            report.parse_error_count += ir.parse_error_count
        files_meta.append(
            FileMeta(
                path=found.relpath,
                language=found.language,
                content_hash=found.content_hash,
                size_bytes=found.size_bytes,
                parse_error_count=ir.parse_error_count,
            )
        )

    # Nothing parseable: the CLI treats this as an error, so leave any
    # previously built database untouched instead of overwriting it with an
    # empty graph.
    if report.parsed_files == 0:
        return report

    graph = build_graph(irs)
    report.node_count = graph.number_of_nodes()
    report.edge_count = graph.number_of_edges()
    report.unresolved_count = sum(
        1 for _, data in graph.nodes(data=True) if data["attrs"].get("unresolved")
    )

    SqliteStore(db_path).save(graph, files_meta, root=base)
    return report
