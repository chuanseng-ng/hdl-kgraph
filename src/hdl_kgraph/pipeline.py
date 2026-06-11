"""Build pipeline: discover -> pass 0 (preprocess) -> pass 1 (parse) -> pass 2
(link) -> persist.

Keeps the CLI thin and gives later milestones (M4 incremental rebuilds, M6
MCP server) a reusable entry point.

Pass 0 (M2) runs *serially in compile order* — filelist order, or sorted
discovery order — threading one :class:`MacroTable` through all files the
way simulators carry ``+define+`` and earlier-file defines forward. Pass 1
parses each already-expanded unit independently, so it stays embarrassingly
parallel for when it matters.

A compilation unit whose content was already spliced into an earlier unit
via ``\\`include`` is skipped (``skipped_reason="included"``) instead of
being parsed a second time without its including context; a header that
appears *before* its includer in compile order still parses standalone,
exactly like a simulator compiling it as its own unit.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.discovery import (
    DEFAULT_MAX_FILE_SIZE_KB,
    DiscoveredFile,
    discover,
    discover_from_paths,
)
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.ids import file_node_id, library_node_id
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.parser.filelist import (
    Filelist,
    filelist_irs,
    flattened_defines,
    flattened_files,
    flattened_incdirs,
    flattened_warnings,
    parse_filelist,
)
from hdl_kgraph.parser.preprocessor import MacroTable, PreprocEmitter, Preprocessor
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.vhdl import DEFAULT_LIBRARY, VhdlParser
from hdl_kgraph.schema import Edge, EdgeKind, Language, Node, NodeKind
from hdl_kgraph.storage.sqlite_store import FileMeta, SqliteStore

DB_DIRNAME = ".hdl-kgraph"
DB_FILENAME = "graph.db"


@dataclass
class BuildReport:
    """Summary of one ``build`` run."""

    root: Path
    db_path: Path
    parsed_files: int = 0
    vhdl_files: int = 0
    error_files: int = 0
    parse_error_count: int = 0
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    node_count: int = 0
    edge_count: int = 0
    unresolved_count: int = 0
    filelists_read: int = 0
    macros_defined: int = 0
    includes_resolved: int = 0
    includes_unresolved: int = 0
    both_branches: bool = False  # no defines given: ifdef alternatives at 0.6
    preproc_warning_count: int = 0
    warnings: list[str] = field(default_factory=list)  # config + filelist warnings


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
    options: BuildOptions | None = None,
) -> BuildReport:
    options = options if options is not None else BuildOptions()
    root = root.resolve()
    base = root.parent if root.is_file() else root
    db_path = db_path if db_path is not None else default_db_path(base)
    report = BuildReport(root=root, db_path=db_path)
    report.warnings.extend(options.warnings)
    max_kb = (
        options.max_file_size_kb
        if options.max_file_size_kb is not None
        else DEFAULT_MAX_FILE_SIZE_KB
    )

    # -- inputs: filelists, defines, include dirs -----------------------------
    filelists = [parse_filelist(path) for path in options.filelists]
    defines: dict[str, str | None] = {}
    incdirs: list[Path] = list(options.incdirs)
    list_files: list[Path] = []
    for fl in filelists:
        defines.update(flattened_defines(fl))
        incdirs.extend(flattened_incdirs(fl))
        list_files.extend(flattened_files(fl))
        report.warnings.extend(flattened_warnings(fl))
    defines.update(options.defines)  # config/CLI defines override filelist ones
    seen_filelists: set[Path] = set()
    report.filelists_read = sum(len(_walk_filelists(fl, seen_filelists)) for fl in filelists)

    # -- file set --------------------------------------------------------------
    if filelists or options.sources:
        paths = list(list_files)
        for pattern in options.sources:
            paths.extend(sorted(p for p in base.glob(pattern) if p.is_file()))
        discovered: list[DiscoveredFile] = discover_from_paths(
            paths, base, exclude=options.exclude, max_file_size_kb=max_kb
        )
    else:
        discovered = discover(root, exclude=options.exclude, max_file_size_kb=max_kb)

    # -- pass 0 + pass 1, in compile order --------------------------------------
    report.both_branches = not defines
    preprocessor = Preprocessor(
        base=base,
        incdirs=incdirs,
        macros=MacroTable(defines),
        branch_mode="both" if report.both_branches else "select",
    )
    parser = SystemVerilogParser()
    vhdl_parser = VhdlParser()
    emitter = PreprocEmitter()
    irs: list[FileIR] = []
    files_meta: list[FileMeta] = []
    macro_keys: set[tuple[str, str, int]] = set()
    processed: set[str] = set()  # units preprocessed standalone so far
    consumed: set[str] = set()  # spliced into an earlier unit -> skip standalone
    vhdl_file_libs: dict[str, str] = {}  # relpath -> library name

    for found in discovered:
        skipped_reason = found.skipped_reason
        if skipped_reason is None and found.relpath in consumed:
            skipped_reason = "included"
        if skipped_reason is not None:
            report.skipped[skipped_reason] = report.skipped.get(skipped_reason, 0) + 1
            files_meta.append(
                FileMeta(
                    path=found.relpath,
                    language=found.language,
                    content_hash=found.content_hash,
                    size_bytes=found.size_bytes,
                    skipped_reason=skipped_reason,
                )
            )
            continue
        if found.language is Language.VHDL:
            # VHDL has no SV preprocessor pass; route by configured library.
            library = _library_for(found.path, options.vhdl_libraries)
            vhdl_file_libs[found.relpath] = library
            text = found.path.read_text(errors="replace")
            ir = vhdl_parser.parse(Path(found.relpath), text, library=library)
            report.vhdl_files += 1
        else:
            pp = preprocessor.preprocess(found.path)
            processed.add(found.relpath)
            consumed |= pp.included_relpaths - processed
            ir = parser.parse(Path(found.relpath), pp.text, line_map=pp.line_map)
            emitter.emit(pp, ir)
            report.includes_resolved += sum(1 for ev in pp.includes if ev.resolved is not None)
            report.includes_unresolved += sum(1 for ev in pp.includes if ev.resolved is None)
            report.preproc_warning_count += len(pp.warnings)
            macro_keys |= {(d.file, d.name, d.line) for d in pp.macro_defs}
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
    report.macros_defined = len(macro_keys)

    # Nothing parseable: the CLI treats this as an error, so leave any
    # previously built database untouched instead of overwriting it with an
    # empty graph.
    if report.parsed_files == 0:
        return report

    # FILELIST nodes/edges last, so parser-emitted FILE nodes win the
    # linker's first-occurrence dedupe over the filelist's minimal stubs.
    seen_meta: set[Path] = set()
    for fl in filelists:
        irs.extend(filelist_irs(fl, base))
        files_meta.extend(_filelist_meta(fl, base, seen_meta))
    if vhdl_file_libs:
        irs.append(_library_ir(vhdl_file_libs, options.vhdl_libraries))

    graph = build_graph(irs)
    report.node_count = graph.number_of_nodes()
    report.edge_count = graph.number_of_edges()
    report.unresolved_count = sum(
        1 for _, data in graph.nodes(data=True) if data["attrs"].get("unresolved")
    )

    SqliteStore(db_path).save(graph, files_meta, root=base)
    return report


def _library_for(path: Path, libraries: dict[str, Path]) -> str:
    """The VHDL library *path* compiles into: longest matching mapped prefix."""
    best = DEFAULT_LIBRARY
    best_len = -1
    for name, lib_path in libraries.items():
        lib_path = lib_path.resolve()
        if path == lib_path or lib_path in path.parents:
            depth = len(lib_path.parts)
            if depth > best_len:
                best, best_len = name, depth
    return best


def _library_ir(file_libs: dict[str, str], libraries: dict[str, Path]) -> FileIR:
    """LIBRARY nodes + LIBRARY->FILE DECLARES edges for the VHDL files seen.

    Emitted as an adapter IR (the FILELIST pattern); parser-emitted FILE
    nodes win the linker's first-occurrence dedupe, so the minimal FILE
    stubs here only matter if a file somehow failed to parse.
    """
    ir = FileIR(path="<vhdl-libraries>")
    seen: set[str] = set()
    for relpath, library in file_libs.items():
        lib_id = library_node_id(library)
        if library not in seen:
            seen.add(library)
            attrs: dict[str, object] = {}
            mapped = libraries.get(library)
            if mapped is not None:
                attrs["path"] = str(mapped)
            ir.nodes.append(
                Node(
                    id=lib_id,
                    kind=NodeKind.LIBRARY,
                    name=library,
                    qualified_name=library,
                    language=Language.VHDL,
                    attrs=attrs,
                )
            )
        ir.nodes.append(
            Node(
                id=file_node_id(relpath),
                kind=NodeKind.FILE,
                name=Path(relpath).name,
                qualified_name=relpath,
                file=relpath,
                language=Language.VHDL,
            )
        )
        ir.local_edges.append(Edge(src=lib_id, dst=file_node_id(relpath), kind=EdgeKind.DECLARES))
    return ir


def _walk_filelists(fl: Filelist, seen: set[Path]) -> list[Filelist]:
    if fl.path in seen:
        return []
    seen.add(fl.path)
    result = [fl]
    for nested in fl.nested:
        result.extend(_walk_filelists(nested, seen))
    return result


def _filelist_meta(fl: Filelist, base: Path, seen: set[Path]) -> list[FileMeta]:
    """files-table records for filelists (feeds the M4 incremental rebuild)."""
    metas: list[FileMeta] = []
    for each in _walk_filelists(fl, seen):
        relpath = Path(os.path.relpath(each.path, base)).as_posix()
        try:
            data = each.path.read_bytes()
        except OSError:
            continue
        metas.append(
            FileMeta(
                path=relpath,
                language=Language.UNKNOWN,
                content_hash=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        )
    return metas
