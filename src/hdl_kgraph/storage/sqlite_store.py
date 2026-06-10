"""SQLite persistence (M1).

Single-file, local-first storage of the knowledge graph:

* ``meta``  — schema_version, build root, built_at, tool_version
* ``files`` — path, language, content_hash (drives M4 incremental rebuilds),
  size_bytes, parse_error_count, skipped_reason
* ``nodes`` — id, kind, name, qualified_name, file, line span, language,
  attrs (JSON)
* ``edges`` — src, dst, kind, confidence, attrs (JSON)

``save()`` is a transactional full rewrite — the M1 ``build`` command always
rebuilds from scratch. ``load()`` refuses databases written by a different
schema version (a real migration path lands with M4's schema versioning
work).
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

SCHEMA_VERSION = "1"

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
"""


class SchemaVersionError(RuntimeError):
    """The database was written by an incompatible hdl-kgraph version."""


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

    def save(self, graph: nx.MultiDiGraph, files: list[FileMeta], root: Path) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)
            conn.execute("DELETE FROM meta")
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM nodes")
            conn.execute("DELETE FROM edges")
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("root", str(root.resolve())),
                    ("built_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    ("tool_version", __version__),
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
                    (src, dst, data["kind"].value, data["confidence"],
                     json.dumps(data["attrs"], sort_keys=True, default=list))
                    for src, dst, data in graph.edges(data=True)
                ],
            )

    def load(self) -> tuple[nx.MultiDiGraph, list[FileMeta], dict[str, str]]:
        """Load (graph, file metadata, meta key/values) from the database."""
        with sqlite3.connect(self.db_path) as conn:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
            version = meta.get("schema_version")
            if version != SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"{self.db_path} has graph schema version {version!r}; this "
                    f"hdl-kgraph expects {SCHEMA_VERSION!r}. Re-run `hdl-kgraph build`."
                )
            files = [
                FileMeta(
                    path=path,
                    language=Language(language),
                    content_hash=content_hash,
                    size_bytes=size_bytes,
                    parse_error_count=parse_error_count,
                    skipped_reason=skipped_reason,
                )
                for path, language, content_hash, size_bytes, parse_error_count, skipped_reason
                in conn.execute("SELECT * FROM files")
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
