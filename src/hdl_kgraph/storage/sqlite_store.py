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

``save()`` is a full rewrite, written to a temp file and atomically swapped
into place with ``os.replace`` — concurrent readers (CLI queries, the MCP
server) never block on or observe a half-written database, and a crashed
build leaves the previous database intact. ``update`` reuses stored IRs
for unchanged files and rewrites the (re-linked) result. Migration policy:
the database is a derived cache, so there is no in-place migration —
``load()`` raises :class:`SchemaVersionError` on a version mismatch and
``update``/``watch`` respond by falling back to a full rebuild.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx

from hdl_kgraph import __version__
from hdl_kgraph.schema import EdgeKind, Language, NodeKind

SCHEMA_VERSION = "5"  # v5: files table gained per-file parse error details

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
        """Write the full graph to a sibling temp file, then swap it into place.

        The live database is never written directly: readers see either the
        old or the new complete database, and a crashed build leaves the
        previous one intact (plus a harmless ``.tmp`` leftover).
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.db_path.with_name(self.db_path.name + ".tmp")
        tmp_path.unlink(missing_ok=True)  # leftover from a crashed build
        conn = sqlite3.connect(tmp_path)
        try:
            conn.executescript(_SCHEMA)
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
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        f.path,
                        f.language.value,
                        f.content_hash,
                        f.size_bytes,
                        f.parse_error_count,
                        f.skipped_reason,
                        json.dumps(f.warnings),
                        json.dumps(f.parse_errors),
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
            conn.commit()
        finally:
            conn.close()
        _replace_into_place(tmp_path, self.db_path)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A read connection that waits out a concurrent writer and is closed
        deterministically (an open handle blocks the ``save()`` swap on
        Windows)."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            yield conn
        finally:
            conn.close()

    def _check_version(self, conn: sqlite3.Connection) -> dict[str, str]:
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise  # e.g. 'database is locked' — not a schema problem
            # pre-schema or foreign database
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
