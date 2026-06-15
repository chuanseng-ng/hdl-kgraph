"""SQLite persistence (M1; schema v3 since M5).

Single-file, local-first storage of the knowledge graph:

* ``meta``  — schema_version, build root, built_at, tool_version,
  options_hash (fingerprint of the effective build inputs; an options change
  invalidates incremental updates)
* ``files`` — path, language, content_hash (drives M4 incremental rebuilds),
  size_bytes, parse_error_count, skipped_reason, warnings (JSON array of
  preprocessor diagnostics, so ``status --errors`` can report them after
  the fact)
* ``nodes`` — id, kind, name, qualified_name, file, line span, language,
  attrs (JSON)
* ``edges`` — src, dst, kind, confidence, attrs (JSON)
* ``file_irs`` — per-unit pass-1 IR, macro-event log, and spliced-header
  list (JSON via :mod:`hdl_kgraph.storage.ir_codec`); only units parsed
  standalone get rows. ``update`` re-links unchanged units from here.
* ``discrepancies`` — M7 enrichment findings: where native-frontend
  elaboration disagreed with the heuristic graph (only populated by
  ``build --enrich``). Surfaced by ``hdl-kgraph discrepancies``.

``save()`` is a full rewrite, written to a temp file (in WAL mode) and
atomically swapped into place with ``os.replace`` — concurrent readers (CLI
queries, the MCP server) never block on or observe a half-written database,
and a crashed build leaves the previous database intact. ``update`` reuses
stored IRs for unchanged files and persists the re-linked result through
``save_incremental()``, which diffs the new graph against the stored rows and
writes only the delta (UPSERT changed nodes/edges/file rows, delete vanished
ones) under a single WAL transaction — so a one-file edit pays a write cost
proportional to the change, not the whole design. Migration policy: the
database is a derived cache, so there is no in-place migration — ``load()``
raises :class:`SchemaVersionError` on a version mismatch and
``update``/``watch`` respond by falling back to a full rebuild.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from hdl_kgraph import __version__
from hdl_kgraph.enrich.base import Discrepancy
from hdl_kgraph.graph.builder import RefRecord
from hdl_kgraph.schema import EdgeKind, Language, NodeKind

SCHEMA_VERSION = "8"  # v8: summaries table (precomputed whole-design reports)

# How long a reader waits on a residual write lock before giving up.
_BUSY_TIMEOUT_MS = 5_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  path              TEXT PRIMARY KEY,
  language          TEXT NOT NULL,
  content_hash      TEXT NOT NULL,
  size_bytes        INTEGER NOT NULL,
  parse_error_count INTEGER NOT NULL DEFAULT 0,
  skipped_reason    TEXT,
  warnings          TEXT NOT NULL DEFAULT '[]',
  parse_errors      TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS nodes (
  id             TEXT PRIMARY KEY,
  kind           TEXT NOT NULL,
  name           TEXT NOT NULL,
  qualified_name TEXT NOT NULL DEFAULT '',
  file           TEXT NOT NULL DEFAULT '',
  line_start     INTEGER NOT NULL DEFAULT 0,
  line_end       INTEGER NOT NULL DEFAULT 0,
  language       TEXT NOT NULL DEFAULT 'unknown',
  attrs          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind_name ON nodes(kind, name);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file);
CREATE TABLE IF NOT EXISTS edges (
  src        TEXT NOT NULL,
  dst        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  attrs      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS file_irs (
  path         TEXT PRIMARY KEY,
  ir           TEXT NOT NULL,
  macro_events TEXT NOT NULL DEFAULT '[]',
  included     TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS discrepancies (
  kind       TEXT NOT NULL,
  backend    TEXT NOT NULL,
  detail     TEXT NOT NULL DEFAULT '',
  node_id    TEXT NOT NULL DEFAULT '',
  src        TEXT NOT NULL DEFAULT '',
  dst        TEXT NOT NULL DEFAULT '',
  heuristic  TEXT NOT NULL DEFAULT '',
  elaborated TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ref_index (
  file        TEXT NOT NULL,
  src_id      TEXT NOT NULL,
  edge_kind   TEXT NOT NULL,
  target_name TEXT NOT NULL,
  scoped      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ref_index_target ON ref_index(target_name);
CREATE INDEX IF NOT EXISTS idx_ref_index_file ON ref_index(file);
CREATE TABLE IF NOT EXISTS summaries (
  name    TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
"""


class SchemaVersionError(RuntimeError):
    """The database was written by an incompatible hdl-kgraph version."""


@dataclass
class StoredUnit:
    """One compilation unit's persisted pass-1 results (the ``file_irs`` row).

    All three fields are JSON text — kept opaque here so unchanged units
    round-trip through ``update`` byte-for-byte without re-serialization.
    """

    ir: str  # FileIR (ir_codec.ir_to_json)
    macro_events: str  # `define/`undef log (ir_codec.macro_events_to_json)
    included: str  # JSON array of header relpaths spliced into this unit


@dataclass
class FileMeta:
    """Per-file record persisted in the ``files`` table."""

    path: str
    language: Language
    content_hash: str
    size_bytes: int
    parse_error_count: int = 0
    skipped_reason: str | None = None  # None | 'size' | 'pragma_protect' | 'exclude'
    warnings: list[str] = field(default_factory=list)  # preprocessor diagnostics
    # ``file:line: message`` details (capped at parser.base.MAX_PARSE_ERRORS;
    # parse_error_count stays exact beyond the cap).
    parse_errors: list[str] = field(default_factory=list)


# -- row serialization (single source of truth) -------------------------------
# Both the full ``save()`` and the incremental ``save_incremental()`` build
# their rows here, so the two write paths can never drift on column order or
# JSON encoding (a drift would make the incremental row-diff see spurious or,
# worse, missed changes).


def _node_row(node_id: str, data: dict[str, object]) -> tuple[object, ...]:
    line_span = data["line_span"]
    return (
        node_id,
        data["kind"].value,  # type: ignore[attr-defined]
        data["name"],
        data["qualified_name"],
        data["file"],
        line_span[0],  # type: ignore[index]
        line_span[1],  # type: ignore[index]
        data["language"].value,  # type: ignore[attr-defined]
        json.dumps(data["attrs"], sort_keys=True),
    )


def _edge_row(src: str, dst: str, data: dict[str, object]) -> tuple[object, ...]:
    return (
        src,
        dst,
        data["kind"].value,  # type: ignore[attr-defined]
        data["confidence"],
        json.dumps(data["attrs"], sort_keys=True, default=list),
    )


def _file_row(f: FileMeta) -> tuple[object, ...]:
    return (
        f.path,
        f.language.value,
        f.content_hash,
        f.size_bytes,
        f.parse_error_count,
        f.skipped_reason,
        json.dumps(f.warnings),
        json.dumps(f.parse_errors),
    )


def _file_ir_row(path: str, unit: StoredUnit) -> tuple[object, ...]:
    return (path, unit.ir, unit.macro_events, unit.included)


def _discrepancy_row(d: Discrepancy) -> tuple[object, ...]:
    return (d.kind, d.backend, d.detail, d.node_id, d.src, d.dst, d.heuristic, d.elaborated)


def _ref_index_row(rec: RefRecord) -> tuple[object, ...]:
    return (rec.file, rec.src_id, rec.edge_kind.value, rec.target_name, int(rec.scoped))


# -- row deserialization (single source of truth) -----------------------------
# Both the full-graph ``load()`` and the bounded ``storage.query`` reader build
# their in-memory nodes/edges here, so the read paths can never drift on how a
# stored row maps back to a graph node/edge.

#: The ``nodes`` columns in table order — every node read does ``SELECT`` of
#: exactly these so :func:`add_node_row` can unpack positionally.
NODE_COLUMNS = "id, kind, name, qualified_name, file, line_start, line_end, language, attrs"
#: The ``edges`` columns in table order, for :func:`add_edge_row`.
EDGE_COLUMNS = "src, dst, kind, confidence, attrs"


def add_node_row(graph: nx.MultiDiGraph, row: tuple[Any, ...]) -> str:
    """Add one ``nodes`` row (in :data:`NODE_COLUMNS` order) to *graph*."""
    node_id, kind, name, qualified_name, file, line_start, line_end, language, attrs = row
    graph.add_node(
        node_id,
        kind=NodeKind(kind),
        name=name,
        qualified_name=qualified_name,
        file=file,
        line_span=(line_start, line_end),
        language=Language(language),
        attrs=json.loads(attrs),
    )
    return str(node_id)


def add_edge_row(graph: nx.MultiDiGraph, row: tuple[Any, ...]) -> None:
    """Add one ``edges`` row (in :data:`EDGE_COLUMNS` order) to *graph*."""
    src, dst, kind, confidence, attrs = row
    graph.add_edge(src, dst, kind=EdgeKind(kind), confidence=confidence, attrs=json.loads(attrs))


def _meta_rows(root: Path, options_hash: str) -> list[tuple[str, str]]:
    return [
        ("schema_version", SCHEMA_VERSION),
        ("root", str(root.resolve())),
        ("built_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ("tool_version", __version__),
        ("options_hash", options_hash),
    ]


class SqliteStore:
    """Saves and loads the knowledge graph at a fixed database path."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        # Set by save_incremental(): {nodes_upserted, nodes_deleted,
        # edge_srcs_rewritten} for the last incremental write (None after a
        # full save or before any write). Used by scripts/bench_incremental.py
        # to assert write cost scales with the change, not the design size.
        self.last_write_stats: dict[str, int] | None = None

    def save(
        self,
        graph: nx.MultiDiGraph,
        files: list[FileMeta],
        root: Path,
        units: dict[str, StoredUnit] | None = None,
        options_hash: str = "",
        discrepancies: list[Discrepancy] | None = None,
        ref_records: list[RefRecord] | None = None,
        summaries: dict[str, str] | None = None,
    ) -> None:
        """Write the full graph to a sibling temp file, then swap it into place.

        The live database is never written directly: readers see either the
        old or the new complete database, and a crashed build leaves the
        previous one intact (plus a harmless ``.tmp`` leftover).
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.db_path.with_name(self.db_path.name + ".tmp")
        _unlink_db(tmp_path)  # leftover (db + sidecars) from a crashed build
        conn = sqlite3.connect(tmp_path)
        try:
            # Build in WAL mode so the persisted database is already WAL: a
            # later incremental write never has to switch the journal mode
            # (which is impossible while a reader holds the database open), so
            # it can never block on a concurrent reader.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                _meta_rows(root, options_hash),
            )
            conn.executemany(
                "INSERT INTO file_irs VALUES (?, ?, ?, ?)",
                [_file_ir_row(path, unit) for path, unit in (units or {}).items()],
            )
            conn.executemany(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [_file_row(f) for f in files],
            )
            conn.executemany(
                "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [_node_row(node_id, data) for node_id, data in graph.nodes(data=True)],
            )
            conn.executemany(
                "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
                [_edge_row(src, dst, data) for src, dst, data in graph.edges(data=True)],
            )
            conn.executemany(
                "INSERT INTO discrepancies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [_discrepancy_row(d) for d in (discrepancies or [])],
            )
            conn.executemany(
                "INSERT INTO ref_index VALUES (?, ?, ?, ?, ?)",
                [_ref_index_row(r) for r in (ref_records or [])],
            )
            conn.executemany(
                "INSERT INTO summaries (name, payload) VALUES (?, ?)",
                list((summaries or {}).items()),
            )
            conn.commit()
            # Fold the WAL back into the main file so the temp database is a
            # self-contained single file ready for the atomic swap.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()  # last connection closing also drops the WAL sidecars
        _replace_into_place(tmp_path, self.db_path)
        # A prior incremental update may have left WAL sidecars beside the live
        # database; they belong to the now-replaced inode, so a reader must not
        # apply them to the fresh file.
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(OSError):
                self.db_path.with_name(self.db_path.name + suffix).unlink(missing_ok=True)
        self.last_write_stats = None

    def save_incremental(
        self,
        graph: nx.MultiDiGraph,
        files: list[FileMeta],
        root: Path,
        units: dict[str, StoredUnit] | None = None,
        options_hash: str = "",
        discrepancies: list[Discrepancy] | None = None,
        ref_records: list[RefRecord] | None = None,
        summaries: dict[str, str] | None = None,
    ) -> None:
        """Write only the rows that changed since the stored build, in place.

        ``update`` re-links the full graph but usually touches few files. This
        diffs the new graph against the stored rows and writes just the delta,
        so the write cost scales with the change rather than the design size
        (the full ``save()`` rewrites every row every time). The write runs in
        WAL mode under a single ``BEGIN IMMEDIATE`` transaction: a concurrent
        reader never blocks the writer, and a crash mid-write rolls back to the
        previous database. Falls back to a full :meth:`save` when the database
        is missing, foreign, or on an incompatible schema.

        The loaded result is identical to a full ``save()`` of the same graph;
        this is the contract ``test_update_graph_matches_full_rebuild`` pins.
        """
        if not self.db_path.is_file():
            self.save(
                graph, files, root, units, options_hash, discrepancies, ref_records, summaries
            )
            return
        conn = sqlite3.connect(self.db_path)
        conn.isolation_level = None  # we drive BEGIN/COMMIT explicitly
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            try:
                self._check_version(conn)
            except SchemaVersionError:
                conn.close()
                self.save(
                    graph, files, root, units, options_hash, discrepancies, ref_records, summaries
                )
                return
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                # WAL could not be enabled (e.g. a filesystem without shared-
                # memory support). An in-place BEGIN IMMEDIATE here could block
                # a concurrent reader, so fall back to the full save(), whose
                # os.replace swap is non-blocking regardless of journal mode.
                conn.close()
                self.save(
                    graph, files, root, units, options_hash, discrepancies, ref_records, summaries
                )
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                stats = _apply_delta(
                    conn,
                    graph,
                    files,
                    root,
                    units,
                    options_hash,
                    discrepancies,
                    ref_records,
                    summaries,
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            # Keep the WAL sidecar near-empty so a later full save() never
            # risks applying stale frames to the swapped-in file.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.last_write_stats = stats
        finally:
            conn.close()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A read-only connection that waits out a concurrent writer and is
        closed deterministically (an open handle blocks the ``save()`` swap on
        Windows).

        Opened ``mode=ro`` via a file: URI so a read can never create or write
        the database — without it, a plain ``connect`` to a path that a
        concurrent rebuild has momentarily removed would silently materialize an
        empty database. All writers (``save``/``save_incremental``) use their own
        connections, so this only constrains the read paths.
        """
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            yield conn
        finally:
            conn.close()

    def _check_version(self, conn: sqlite3.Connection) -> dict[str, str]:
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
        except sqlite3.DatabaseError as exc:
            # OperationalError is a DatabaseError subclass: re-raise transient
            # problems (e.g. 'database is locked'), but remap a missing meta
            # table ('no such table') or a non-SQLite/foreign file ('file is
            # not a database') to the schema error so callers fall back to a
            # full rebuild.
            message = str(exc).lower()
            if "no such table" not in message and "not a database" not in message:
                raise
            raise SchemaVersionError(f"{self.db_path} is not an hdl-kgraph database") from exc
        version = meta.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"{self.db_path} has graph schema version {version!r}; this "
                f"hdl-kgraph expects {SCHEMA_VERSION!r}. Re-run `hdl-kgraph build`."
            )
        return meta

    def load_meta(self) -> dict[str, str]:
        """Load the meta key/values (checking the schema version)."""
        with self._connect() as conn:
            return self._check_version(conn)

    def load_file_hashes(self) -> dict[str, str]:
        """path -> content_hash for every stored file record."""
        with self._connect() as conn:
            self._check_version(conn)
            return dict(conn.execute("SELECT path, content_hash FROM files"))

    def load_file_warnings(self) -> dict[str, list[str]]:
        """path -> preprocessor warnings, for files that have any.

        ``update`` carries these forward for units re-linked from stored IRs
        without re-running the preprocessor.
        """
        with self._connect() as conn:
            self._check_version(conn)
            return {
                path: json.loads(warnings)
                for path, warnings in conn.execute(
                    "SELECT path, warnings FROM files WHERE warnings != '[]'"
                )
            }

    def load_dependency_graph(self) -> nx.MultiDiGraph:
        """The preprocessor-dependency subgraph (M4 dirty closure).

        Only INCLUDES / DEFINES_MACRO / USES_MACRO edges and FILE / MACRO /
        INCLUDE_FILE nodes — a fraction of the full graph, so ``update``
        skips rehydrating everything else.
        """
        with self._connect() as conn:
            self._check_version(conn)
            graph = nx.MultiDiGraph()
            for node_id, kind, name, attrs in conn.execute(
                "SELECT id, kind, name, attrs FROM nodes WHERE kind IN (?, ?, ?)",
                (NodeKind.FILE.value, NodeKind.MACRO.value, NodeKind.INCLUDE_FILE.value),
            ):
                graph.add_node(node_id, kind=NodeKind(kind), name=name, attrs=json.loads(attrs))
            for src, dst, kind in conn.execute(
                "SELECT src, dst, kind FROM edges WHERE kind IN (?, ?, ?)",
                (
                    EdgeKind.INCLUDES.value,
                    EdgeKind.DEFINES_MACRO.value,
                    EdgeKind.USES_MACRO.value,
                ),
            ):
                if src in graph and dst in graph:
                    graph.add_edge(src, dst, kind=EdgeKind(kind))
            return graph

    def load_units(self) -> dict[str, StoredUnit]:
        """Load the per-unit pass-1 IRs persisted for incremental updates."""
        with self._connect() as conn:
            self._check_version(conn)
            return {
                path: StoredUnit(ir=ir, macro_events=events, included=included)
                for path, ir, events, included in conn.execute("SELECT * FROM file_irs")
            }

    def load_discrepancies(self) -> list[Discrepancy]:
        """The M7 enrichment findings (empty for a non-enriched build)."""
        with self._connect() as conn:
            self._check_version(conn)
            return [
                Discrepancy(
                    kind=row[0],
                    backend=row[1],
                    detail=row[2],
                    node_id=row[3],
                    src=row[4],
                    dst=row[5],
                    heuristic=row[6],
                    elaborated=row[7],
                )
                for row in conn.execute(
                    "SELECT kind, backend, detail, node_id, src, dst, heuristic, elaborated "
                    "FROM discrepancies"
                )
            ]

    def load_ref_index(self) -> list[RefRecord]:
        """All persisted pass-2 reference records (incremental-link reverse index)."""
        with self._connect() as conn:
            self._check_version(conn)
            return [
                RefRecord(
                    file=row[0],
                    src_id=row[1],
                    edge_kind=EdgeKind(row[2]),
                    target_name=row[3],
                    scoped=bool(row[4]),
                )
                for row in conn.execute(
                    "SELECT file, src_id, edge_kind, target_name, scoped FROM ref_index"
                )
            ]

    def load_summary(self, name: str) -> str | None:
        """The precomputed JSON payload for whole-design summary *name*, or None.

        ``None`` means the build predates summaries (or did not compute this
        one), so the reader falls back to computing it from the full graph.
        """
        with self._connect() as conn:
            self._check_version(conn)
            row = conn.execute("SELECT payload FROM summaries WHERE name = ?", (name,)).fetchone()
            return row[0] if row else None

    def load(self) -> tuple[nx.MultiDiGraph, list[FileMeta], dict[str, str]]:
        """Load (graph, file metadata, meta key/values) from the database."""
        with self._connect() as conn:
            meta = self._check_version(conn)
            files = [
                FileMeta(
                    path=row[0],
                    language=Language(row[1]),
                    content_hash=row[2],
                    size_bytes=row[3],
                    parse_error_count=row[4],
                    skipped_reason=row[5],
                    warnings=json.loads(row[6]),
                    parse_errors=json.loads(row[7]),
                )
                for row in conn.execute("SELECT * FROM files")
            ]
            graph = nx.MultiDiGraph()
            for row in conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes"):
                add_node_row(graph, row)
            for row in conn.execute(f"SELECT {EDGE_COLUMNS} FROM edges"):
                add_edge_row(graph, row)
            return graph, files, meta


def _apply_delta(
    conn: sqlite3.Connection,
    graph: nx.MultiDiGraph,
    files: list[FileMeta],
    root: Path,
    units: dict[str, StoredUnit] | None,
    options_hash: str,
    discrepancies: list[Discrepancy] | None,
    ref_records: list[RefRecord] | None = None,
    summaries: dict[str, str] | None = None,
) -> dict[str, int]:
    """UPSERT/delete only the rows that differ from what is stored.

    Runs inside the caller's open transaction. Returns write-volume stats.
    """
    units = units or {}
    discrepancies = discrepancies or []
    ref_records = ref_records or []
    summaries = summaries or {}

    # -- nodes (PK id): UPSERT changed rows, delete vanished ids ---------------
    stored_nodes = {row[0]: row for row in conn.execute("SELECT * FROM nodes")}
    new_nodes = {node_id: _node_row(node_id, data) for node_id, data in graph.nodes(data=True)}
    node_upserts = [row for node_id, row in new_nodes.items() if stored_nodes.get(node_id) != row]
    node_deletes = [(node_id,) for node_id in stored_nodes if node_id not in new_nodes]
    conn.executemany(
        "INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", node_upserts
    )
    conn.executemany("DELETE FROM nodes WHERE id = ?", node_deletes)

    # -- edges (no PK; a multiset grouped by src) -----------------------------
    # Edges carry no key, so the unit of replacement is "all edges from one
    # src". For each src whose edge multiset differs (including a src whose
    # edges only changed because a *target* in an unchanged file was removed/
    # re-resolved) we delete and re-insert just that src's rows. Counter, not
    # set: a MultiDiGraph may hold identical parallel edges.
    stored_edges: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for row in conn.execute("SELECT src, dst, kind, confidence, attrs FROM edges"):
        stored_edges[row[0]][row] += 1
    new_edges: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for src, dst, data in graph.edges(data=True):
        new_edges[src][_edge_row(src, dst, data)] += 1
    edge_srcs_rewritten = 0
    for src in stored_edges.keys() | new_edges.keys():
        if stored_edges.get(src) == new_edges.get(src):
            continue
        edge_srcs_rewritten += 1
        conn.execute("DELETE FROM edges WHERE src = ?", (src,))
        rows = [row for row, count in new_edges.get(src, Counter()).items() for _ in range(count)]
        conn.executemany("INSERT INTO edges VALUES (?, ?, ?, ?, ?)", rows)

    # -- files / file_irs (PK path): UPSERT changed, delete removed -----------
    # A removed source file leaves no FileMeta and no unit, so its rows in both
    # tables are deleted here; a stale files row would carry a stale
    # content_hash that the next scan_changes would miss.
    _upsert_by_path(
        conn, "files", "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", {f.path: _file_row(f) for f in files}
    )
    _upsert_by_path(
        conn,
        "file_irs",
        "VALUES (?, ?, ?, ?)",
        {path: _file_ir_row(path, unit) for path, unit in units.items()},
    )

    # -- ref_index (no PK; a multiset grouped by file) ------------------------
    # Like edges, ref rows carry no key; replace per owning unit so a one-file
    # edit rewrites only that unit's ref rows. Counter handles duplicate refs
    # (a header spliced into several units).
    stored_refs: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for row in conn.execute("SELECT file, src_id, edge_kind, target_name, scoped FROM ref_index"):
        stored_refs[row[0]][row] += 1
    new_refs: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for rec in ref_records:
        new_refs[rec.file][_ref_index_row(rec)] += 1
    for file in stored_refs.keys() | new_refs.keys():
        if stored_refs.get(file) == new_refs.get(file):
            continue
        conn.execute("DELETE FROM ref_index WHERE file = ?", (file,))
        rows = [row for row, count in new_refs.get(file, Counter()).items() for _ in range(count)]
        conn.executemany("INSERT INTO ref_index VALUES (?, ?, ?, ?, ?)", rows)

    # -- discrepancies (small, enrich-only): refresh wholesale ----------------
    conn.execute("DELETE FROM discrepancies")
    conn.executemany(
        "INSERT INTO discrepancies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [_discrepancy_row(d) for d in discrepancies],
    )

    # -- summaries (a handful of whole-design JSON blobs): refresh wholesale ---
    conn.execute("DELETE FROM summaries")
    conn.executemany("INSERT INTO summaries (name, payload) VALUES (?, ?)", list(summaries.items()))

    # -- meta: refresh built_at / options_hash / tool_version -----------------
    conn.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", _meta_rows(root, options_hash)
    )

    return {
        "nodes_upserted": len(node_upserts),
        "nodes_deleted": len(node_deletes),
        "edge_srcs_rewritten": edge_srcs_rewritten,
    }


def _upsert_by_path(
    conn: sqlite3.Connection,
    table: str,
    values_clause: str,
    new_rows: dict[str, tuple[object, ...]],
) -> None:
    """UPSERT path-keyed rows that changed and delete paths no longer present."""
    stored = {row[0]: row for row in conn.execute(f"SELECT * FROM {table}")}
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} {values_clause}",
        [row for path, row in new_rows.items() if stored.get(path) != row],
    )
    conn.executemany(
        f"DELETE FROM {table} WHERE path = ?",
        [(path,) for path in stored if path not in new_rows],
    )


def _unlink_db(db_path: Path) -> None:
    """Remove a database file and its WAL sidecars, ignoring absence."""
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def _replace_into_place(tmp_path: Path, db_path: Path) -> None:
    """``os.replace`` with retries.

    Atomic and lock-free on POSIX; on Windows the swap fails with
    ``PermissionError`` while a reader briefly holds the destination open,
    so back off and retry (~6 s total) before giving up.
    """
    delay = 0.05
    for _ in range(7):
        try:
            os.replace(tmp_path, db_path)
            return
        except PermissionError:
            time.sleep(delay)
            delay *= 2
    os.replace(tmp_path, db_path)
