"""SQLite persistence (M1; schema v2 since M4).

Single-file, local-first storage of the knowledge graph:

* ``meta``  — schema_version, build root, built_at, tool_version,
  options_hash (fingerprint of the effective build inputs; an options change
  invalidates incremental updates)
* ``files`` — path, language, content_hash (drives M4 incremental rebuilds),
  size_bytes, parse_error_count, skipped_reason
* ``nodes`` — id, kind, name, qualified_name, file, line span, language,
  attrs (JSON)
* ``edges`` — src, dst, kind, confidence, attrs (JSON)
* ``file_irs`` — per-unit pass-1 IR, macro-event log, and spliced-header
  list (JSON via :mod:`hdl_kgraph.storage.ir_codec`); only units parsed
  standalone get rows. ``update`` re-links unchanged units from here.

``save()`` is a transactional full rewrite; ``update`` reuses stored IRs
for unchanged files and rewrites the (re-linked) result. Migration policy:
the database is a derived cache, so there is no in-place migration —
``load()`` raises :class:`SchemaVersionError` on a version mismatch and
``update``/``watch`` respond by falling back to a full rebuild.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx

from hdl_kgraph import __version__
from hdl_kgraph.schema import EdgeKind, Language, NodeKind

SCHEMA_VERSION = "2"

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
  skipped_reason    TEXT
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


class SqliteStore:
    """Saves and loads the knowledge graph at a fixed database path."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def save(
        self,
        graph: nx.MultiDiGraph,
        files: list[FileMeta],
        root: Path,
        units: dict[str, StoredUnit] | None = None,
        options_hash: str = "",
    ) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)
            conn.execute("DELETE FROM meta")
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM nodes")
            conn.execute("DELETE FROM edges")
            conn.execute("DELETE FROM file_irs")
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("root", str(root.resolve())),
                    ("built_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    ("tool_version", __version__),
                    ("options_hash", options_hash),
                ],
            )
            conn.executemany(
                "INSERT INTO file_irs VALUES (?, ?, ?, ?)",
                [
                    (path, unit.ir, unit.macro_events, unit.included)
                    for path, unit in (units or {}).items()
                ],
            )
            conn.executemany(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        f.path,
                        f.language.value,
                        f.content_hash,
                        f.size_bytes,
                        f.parse_error_count,
                        f.skipped_reason,
                    )
                    for f in files
                ],
            )
            conn.executemany(
                "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        node_id,
                        data["kind"].value,
                        data["name"],
                        data["qualified_name"],
                        data["file"],
                        data["line_span"][0],
                        data["line_span"][1],
                        data["language"].value,
                        json.dumps(data["attrs"], sort_keys=True),
                    )
                    for node_id, data in graph.nodes(data=True)
                ],
            )
            conn.executemany(
                "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        src,
                        dst,
                        data["kind"].value,
                        data["confidence"],
                        json.dumps(data["attrs"], sort_keys=True, default=list),
                    )
                    for src, dst, data in graph.edges(data=True)
                ],
            )

    def _check_version(self, conn: sqlite3.Connection) -> dict[str, str]:
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
        except sqlite3.OperationalError as exc:  # pre-schema or foreign database
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
        with sqlite3.connect(self.db_path) as conn:
            return self._check_version(conn)

    def load_file_hashes(self) -> dict[str, str]:
        """path -> content_hash for every stored file record."""
        with sqlite3.connect(self.db_path) as conn:
            self._check_version(conn)
            return dict(conn.execute("SELECT path, content_hash FROM files"))

    def load_dependency_graph(self) -> nx.MultiDiGraph:
        """The preprocessor-dependency subgraph (M4 dirty closure).

        Only INCLUDES / DEFINES_MACRO / USES_MACRO edges and FILE / MACRO /
        INCLUDE_FILE nodes — a fraction of the full graph, so ``update``
        skips rehydrating everything else.
        """
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
            self._check_version(conn)
            return {
                path: StoredUnit(ir=ir, macro_events=events, included=included)
                for path, ir, events, included in conn.execute("SELECT * FROM file_irs")
            }

    def load(self) -> tuple[nx.MultiDiGraph, list[FileMeta], dict[str, str]]:
        """Load (graph, file metadata, meta key/values) from the database."""
        with sqlite3.connect(self.db_path) as conn:
            meta = self._check_version(conn)
            files = [
                FileMeta(
                    path=row[0],
                    language=Language(row[1]),
                    content_hash=row[2],
                    size_bytes=row[3],
                    parse_error_count=row[4],
                    skipped_reason=row[5],
                )
                for row in conn.execute("SELECT * FROM files")
            ]
            graph = nx.MultiDiGraph()
            for row in conn.execute("SELECT * FROM nodes"):
                node_id, kind, name, qualified_name, file, line_start, line_end, language, attrs = (
                    row
                )
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
            for src, dst, kind, confidence, attrs in conn.execute("SELECT * FROM edges"):
                graph.add_edge(
                    src, dst, kind=EdgeKind(kind), confidence=confidence, attrs=json.loads(attrs)
                )
            return graph, files, meta
