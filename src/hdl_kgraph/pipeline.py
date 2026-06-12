"""Build pipeline: discover -> pass 0 (preprocess) -> pass 1 (parse) -> pass 2
(link) -> persist.

Keeps the CLI thin and gives later milestones (M6 MCP server) a reusable
entry point.

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

M4 — incremental updates: every standalone unit's pass-1 IR (plus its
macro-event log and spliced-header list) is persisted at build time, and
:func:`run_update` re-parses only changed/added/removed files and their
include/macro dependents (:mod:`hdl_kgraph.incremental`). Unchanged units
are re-linked from their stored IR, replaying their macro events into the
shared table at their position in compile order. Each unit gets its own
:class:`PreprocEmitter`, so stored IRs are self-contained — the linker
dedupes the resulting cross-IR repeats by first occurrence. A change to the
effective build inputs (defines, incdirs, filelist sets, library map — the
``options_hash``) falls back to a full rebuild, which also covers "a changed
``.f`` define or incdir dirties all files in that filelist".
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.discovery import (
    DEFAULT_MAX_FILE_SIZE_KB,
    DiscoveredFile,
    discover,
    discover_from_paths,
    glob_sources,
)
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.ids import file_node_id, library_node_id
from hdl_kgraph.incremental import (
    ChangeSet,
    diff_hashes,
    dirty_closure,
    newly_resolvable_includes,
)
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
from hdl_kgraph.storage import ir_codec
from hdl_kgraph.storage.sqlite_store import (
    FileMeta,
    SchemaVersionError,
    SqliteStore,
    StoredUnit,
)

DB_DIRNAME = ".hdl-kgraph"
DB_FILENAME = "graph.db"

#: Stage-progress callback: called with one human-readable line per
#: pipeline stage as it starts.
ProgressFn = Callable[[str], None]

#: Per-file progress callback for the pass 0+1 loop: ``tick(done, total)``.
TickFn = Callable[[int, int], None]


@dataclass
class BuildReport:
    """Summary of one ``build`` run."""

    root: Path
    db_path: Path
    parsed_files: int = 0  # units contributing an IR (freshly parsed + reused)
    reused_files: int = 0  # units re-linked from their stored IR (update only)
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
    # Diagnostics surfaced by `build -v` and persisted for `status --errors`:
    file_errors: dict[str, int] = field(default_factory=dict)  # relpath -> error count
    preproc_warnings: list[str] = field(default_factory=list)  # full warning text
    incdirs: list[str] = field(default_factory=list)  # effective `include search path


@dataclass
class UpdateReport:
    """Summary of one ``update`` run."""

    root: Path
    db_path: Path
    up_to_date: bool = False
    full_rebuild_reason: str | None = None  # incremental path not taken
    reparsed: dict[str, str] = field(default_factory=dict)  # relpath -> why
    removed: list[str] = field(default_factory=list)
    build: BuildReport | None = None  # None only when up_to_date
    elapsed_s: float = 0.0


def default_db_path(root: Path) -> Path:
    return root / DB_DIRNAME / DB_FILENAME


def find_db(start: Path) -> Path | None:
    """Locate the nearest database from *start* upward (git-style)."""
    for directory in [start.resolve(), *start.resolve().parents]:
        candidate = directory / DB_DIRNAME / DB_FILENAME
        if candidate.is_file():
            return candidate
    return None


@dataclass
class _Inputs:
    """Filelist/define/incdir inputs resolved once per run."""

    filelists: list[Filelist] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)
    incdirs: list[Path] = field(default_factory=list)
    list_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    filelists_read: int = 0


def _resolve_inputs(options: BuildOptions) -> _Inputs:
    inputs = _Inputs(incdirs=list(options.incdirs))
    inputs.filelists = [parse_filelist(path) for path in options.filelists]
    for fl in inputs.filelists:
        inputs.defines.update(flattened_defines(fl))
        inputs.incdirs.extend(flattened_incdirs(fl))
        inputs.list_files.extend(flattened_files(fl))
        inputs.warnings.extend(flattened_warnings(fl))
    inputs.defines.update(options.defines)  # config/CLI defines override filelist ones
    seen: set[Path] = set()
    inputs.filelists_read = sum(len(_walk_filelists(fl, seen)) for fl in inputs.filelists)
    return inputs


def _discover(
    root: Path, base: Path, options: BuildOptions, inputs: _Inputs, max_kb: int
) -> list[DiscoveredFile]:
    if inputs.filelists or options.sources:
        paths = list(inputs.list_files)
        for pattern in options.sources:
            paths.extend(glob_sources(base, pattern))
        return discover_from_paths(paths, base, exclude=options.exclude, max_file_size_kb=max_kb)
    return discover(root, exclude=options.exclude, max_file_size_kb=max_kb)


def options_hash(base: Path, options: BuildOptions, inputs: _Inputs) -> str:
    """Fingerprint of the effective build inputs.

    A mismatch invalidates incremental updates: defines and incdirs feed the
    preprocessor globally (filelist ``+define+``/``+incdir+`` included), and
    sources/exclude/size/library settings change the file set or routing.
    Pure filelist *membership* changes are not part of the hash — they show
    up as added/removed files in the ordinary hash diff.
    """

    def rel(path: Path) -> str:
        return Path(os.path.relpath(Path(path).resolve(), base)).as_posix()

    payload = {
        "defines": sorted((k, v) for k, v in inputs.defines.items()),
        "incdirs": [rel(d) for d in inputs.incdirs],
        "sources": sorted(options.sources),
        "exclude": sorted(options.exclude),
        "max_file_size_kb": options.max_file_size_kb,
        "vhdl_libraries": sorted((name, rel(p)) for name, p in options.vhdl_libraries.items()),
        "filelists": [rel(p) for p in options.filelists],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def run_build(
    root: Path,
    db_path: Path | None = None,
    options: BuildOptions | None = None,
    progress: ProgressFn | None = None,
    tick: TickFn | None = None,
) -> BuildReport:
    return _execute(
        root,
        db_path,
        options if options is not None else BuildOptions(),
        progress=progress,
        tick=tick,
    )


def _execute(
    root: Path,
    db_path: Path | None,
    options: BuildOptions,
    inputs: _Inputs | None = None,
    reuse: dict[str, StoredUnit] | None = None,
    discovered: list[DiscoveredFile] | None = None,
    prior_warnings: dict[str, list[str]] | None = None,
    progress: ProgressFn | None = None,
    tick: TickFn | None = None,
) -> BuildReport:
    """One pipeline run; units named in *reuse* re-link from their stored IR.

    *prior_warnings* carries the previous build's per-file preprocessor
    warnings for reused units (their preprocessor never re-runs).
    """
    progress = progress if progress is not None else lambda _line: None
    tick = tick if tick is not None else lambda _done, _total: None
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
    reuse = reuse or {}

    # -- inputs: filelists, defines, include dirs -----------------------------
    if inputs is None:
        progress("resolving build inputs (filelists, defines, incdirs)")
        inputs = _resolve_inputs(options)
    report.warnings.extend(inputs.warnings)
    report.filelists_read = inputs.filelists_read
    report.incdirs = [str(d) for d in inputs.incdirs]

    # -- file set --------------------------------------------------------------
    if discovered is None:
        progress(f"discovering source files under {root}")
        discovered = _discover(root, base, options, inputs, max_kb)

    # -- pass 0 + pass 1, in compile order --------------------------------------
    report.both_branches = not inputs.defines
    preprocessor = Preprocessor(
        base=base,
        incdirs=inputs.incdirs,
        macros=MacroTable(inputs.defines),
        branch_mode="both" if report.both_branches else "select",
    )
    parser = SystemVerilogParser()
    vhdl_parser = VhdlParser()
    irs: list[FileIR] = []
    units: dict[str, StoredUnit] = {}
    files_meta: list[FileMeta] = []
    macro_keys: set[tuple[str, str, int]] = set()
    processed: set[str] = set()  # units preprocessed standalone so far
    consumed: set[str] = set()  # spliced into an earlier unit -> skip standalone
    vhdl_file_libs: dict[str, str] = {}  # relpath -> library name

    progress(f"pass 0+1: preprocessing and parsing {len(discovered)} file(s)")
    for index, found in enumerate(discovered, start=1):
        # Skipped/reused files advance the counter too: it always reaches total.
        tick(index, len(discovered))
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
        file_warnings: list[str] = []
        ir = _reuse_unit(found, reuse, preprocessor, processed, consumed)
        if ir is not None:
            report.reused_files += 1
            # The preprocessor does not re-run for reused units; carry their
            # previous build's warnings forward.
            file_warnings = list((prior_warnings or {}).get(found.relpath, []))
            if found.language is Language.VHDL:
                vhdl_file_libs[found.relpath] = _library_for(found.path, options.vhdl_libraries)
                report.vhdl_files += 1
            units[found.relpath] = reuse[found.relpath]
        elif found.language is Language.VHDL:
            # VHDL has no SV preprocessor pass; route by configured library.
            library = _library_for(found.path, options.vhdl_libraries)
            vhdl_file_libs[found.relpath] = library
            text = found.path.read_text(errors="replace")
            ir = vhdl_parser.parse(Path(found.relpath), text, library=library)
            report.vhdl_files += 1
            units[found.relpath] = StoredUnit(
                ir=ir_codec.ir_to_json(ir), macro_events="[]", included="[]"
            )
        else:
            pp = preprocessor.preprocess(found.path)
            processed.add(found.relpath)
            consumed |= pp.included_relpaths - processed
            ir = parser.parse(Path(found.relpath), pp.text, line_map=pp.line_map)
            # One emitter per unit keeps each stored IR self-contained; the
            # linker dedupes repeats across units by first occurrence.
            PreprocEmitter().emit(pp, ir)
            report.includes_resolved += sum(1 for ev in pp.includes if ev.resolved is not None)
            report.includes_unresolved += sum(1 for ev in pp.includes if ev.resolved is None)
            file_warnings = list(pp.warnings)
            macro_keys |= {(d.file, d.name, d.line) for d in pp.macro_defs}
            units[found.relpath] = StoredUnit(
                ir=ir_codec.ir_to_json(ir),
                macro_events=ir_codec.macro_events_to_json(pp.macro_events),
                included=json.dumps(sorted(pp.included_relpaths)),
            )
        irs.append(ir)
        report.parsed_files += 1
        if ir.parse_error_count:
            report.error_files += 1
            report.parse_error_count += ir.parse_error_count
            report.file_errors[found.relpath] = ir.parse_error_count
        report.preproc_warnings.extend(file_warnings)
        report.preproc_warning_count += len(file_warnings)
        files_meta.append(
            FileMeta(
                path=found.relpath,
                language=found.language,
                content_hash=found.content_hash,
                size_bytes=found.size_bytes,
                parse_error_count=ir.parse_error_count,
                warnings=file_warnings,
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
    for fl in inputs.filelists:
        irs.extend(filelist_irs(fl, base))
        files_meta.extend(_filelist_meta(fl, base, seen_meta))
    if vhdl_file_libs:
        irs.append(_library_ir(vhdl_file_libs, options.vhdl_libraries))

    progress(f"pass 2: linking {len(irs)} unit(s) into the graph")
    graph = build_graph(irs, warnings=report.warnings)
    report.node_count = graph.number_of_nodes()
    report.edge_count = graph.number_of_edges()
    report.unresolved_count = sum(
        1 for _, data in graph.nodes(data=True) if data["attrs"].get("unresolved")
    )

    progress(f"writing {db_path}")
    SqliteStore(db_path).save(
        graph,
        files_meta,
        root=base,
        units=units,
        options_hash=options_hash(base, options, inputs),
    )
    return report


def _reuse_unit(
    found: DiscoveredFile,
    reuse: dict[str, StoredUnit],
    preprocessor: Preprocessor,
    processed: set[str],
    consumed: set[str],
) -> FileIR | None:
    """Decode *found*'s stored IR (replaying its macro events), or None.

    A corrupt stored row falls back to a fresh parse rather than failing the
    update.
    """
    stored = reuse.get(found.relpath)
    if stored is None:
        return None
    try:
        ir = ir_codec.ir_from_json(stored.ir)
        events = ir_codec.macro_events_from_json(stored.macro_events)
        included: set[str] = set(json.loads(stored.included))
    except (KeyError, TypeError, ValueError):
        return None
    if found.language is not Language.VHDL:
        for event in events:
            preprocessor.macros.apply(event)
        processed.add(found.relpath)
        consumed |= included - processed
    return ir


def run_update(
    root: Path,
    db_path: Path | None = None,
    options: BuildOptions | None = None,
    full: bool = False,
    progress: ProgressFn | None = None,
    tick: TickFn | None = None,
) -> UpdateReport:
    """Incrementally refresh the database after source edits.

    Re-parses changed/added files plus their include/macro dependents
    (removed files seed the closure too), re-links everything from stored
    pass-1 IRs, and rewrites the database. Falls back to a full rebuild when
    the database is missing/incompatible or the effective build inputs
    changed.
    """
    started = time.perf_counter()
    options = options if options is not None else BuildOptions()
    root = root.resolve()
    base = root.parent if root.is_file() else root
    db_path = db_path if db_path is not None else default_db_path(base)
    report = UpdateReport(root=root, db_path=db_path)
    max_kb = (
        options.max_file_size_kb
        if options.max_file_size_kb is not None
        else DEFAULT_MAX_FILE_SIZE_KB
    )

    def full_rebuild(reason: str) -> UpdateReport:
        report.full_rebuild_reason = reason
        report.build = run_build(root, db_path, options, progress=progress, tick=tick)
        report.elapsed_s = time.perf_counter() - started
        return report

    if full:
        return full_rebuild("forced with --full")
    if not db_path.is_file():
        return full_rebuild("no existing database")
    store = SqliteStore(db_path)
    try:
        meta = store.load_meta()
        stored_hashes = store.load_file_hashes()
    except SchemaVersionError as exc:
        return full_rebuild(str(exc))
    if meta.get("root") != str(base):
        return full_rebuild(f"build root changed (was {meta.get('root')})")

    inputs = _resolve_inputs(options)
    if meta.get("options_hash") != options_hash(base, options, inputs):
        return full_rebuild("build options changed (defines/incdirs/sources/libraries)")

    if progress is not None:
        progress("scanning for changed files")
    discovered = _discover(root, base, options, inputs, max_kb)
    current = _current_hashes(base, inputs, discovered)
    changes = diff_hashes(stored_hashes, current)
    if not changes:
        report.up_to_date = True
        report.elapsed_s = time.perf_counter() - started
        return report
    stored_units = store.load_units()
    if not stored_units:
        return full_rebuild("no stored parse results")

    dependencies = store.load_dependency_graph()
    seeds = {path: "changed" for path in changes.changed}
    seeds.update({path: "removed" for path in changes.removed})
    dirty = dirty_closure(dependencies, seeds)
    dirty.update(newly_resolvable_includes(dependencies, changes.added))
    dirty.update({path: "added" for path in changes.added})
    discovered_relpaths = {found.relpath for found in discovered}
    report.reparsed = {path: why for path, why in dirty.items() if path in discovered_relpaths}
    report.removed = changes.removed

    reuse = {path: unit for path, unit in stored_units.items() if path not in dirty}
    report.build = _execute(
        root,
        db_path,
        options,
        inputs=inputs,
        reuse=reuse,
        discovered=discovered,
        prior_warnings=store.load_file_warnings(),
        progress=progress,
        tick=tick,
    )
    report.elapsed_s = time.perf_counter() - started
    return report


def scan_changes(root: Path, db_path: Path, options: BuildOptions | None = None) -> ChangeSet:
    """Hash-diff the working tree against the stored build (``detect-changes``).

    Raises :class:`SchemaVersionError` for an incompatible database.
    """
    options = options if options is not None else BuildOptions()
    root = root.resolve()
    base = root.parent if root.is_file() else root
    max_kb = (
        options.max_file_size_kb
        if options.max_file_size_kb is not None
        else DEFAULT_MAX_FILE_SIZE_KB
    )
    _, stored_files, _ = SqliteStore(db_path).load()
    inputs = _resolve_inputs(options)
    discovered = _discover(root, base, options, inputs, max_kb)
    current = _current_hashes(base, inputs, discovered)
    return diff_hashes({f.path: f.content_hash for f in stored_files}, current)


def _current_hashes(
    base: Path, inputs: _Inputs, discovered: list[DiscoveredFile]
) -> dict[str, str]:
    """Content hashes of the present tree: sources plus filelists."""
    current = {found.relpath: found.content_hash for found in discovered}
    seen: set[Path] = set()
    for fl in inputs.filelists:
        for filelist_meta in _filelist_meta(fl, base, seen):
            current.setdefault(filelist_meta.path, filelist_meta.content_hash)
    return current


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
