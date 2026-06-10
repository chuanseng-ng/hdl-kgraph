"""SQLite round-trip tests."""

import json
import sqlite3
from pathlib import Path

import pytest

from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import SCHEMA_VERSION, FileMeta, SchemaVersionError, SqliteStore


def _normalize(attrs: dict) -> str:
    # Tuples become JSON arrays in storage; compare canonically.
    return json.dumps(attrs, sort_keys=True, default=list)


@pytest.fixture
def store(tmp_path: Path, fixtures_dir: Path) -> tuple[SqliteStore, object, list[FileMeta]]:
    parser = SystemVerilogParser()
    irs = [
        parser.parse(Path(p.name), p.read_text())
        for p in sorted(fixtures_dir.iterdir())
        if p.suffix in parser.suffixes
    ]
    graph = build_graph(irs)
    files = [
        FileMeta(
            path=ir.path,
            language=Language.SYSTEMVERILOG,
            content_hash="0" * 64,
            size_bytes=123,
            parse_error_count=ir.parse_error_count,
        )
        for ir in irs
    ]
    store = SqliteStore(tmp_path / ".hdl-kgraph" / "graph.db")
    store.save(graph, files, root=tmp_path)
    return store, graph, files


def test_round_trip_preserves_nodes_and_edges(store) -> None:
    sqlite_store, graph, files = store
    loaded, loaded_files, meta = sqlite_store.load()

    assert set(loaded.nodes) == set(graph.nodes)
    assert loaded.number_of_edges() == graph.number_of_edges()
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["root"]

    for node_id, data in graph.nodes(data=True):
        got = loaded.nodes[node_id]
        assert got["kind"] is data["kind"], node_id
        assert got["language"] is data["language"]
        assert got["line_span"] == data["line_span"]
        assert _normalize(got["attrs"]) == _normalize(data["attrs"])

    want_edges = sorted(
        (u, v, d["kind"].value, d["confidence"], _normalize(d["attrs"]))
        for u, v, d in graph.edges(data=True)
    )
    got_edges = sorted(
        (u, v, d["kind"].value, d["confidence"], _normalize(d["attrs"]))
        for u, v, d in loaded.edges(data=True)
    )
    assert got_edges == want_edges


def test_round_trip_preserves_file_meta(store) -> None:
    sqlite_store, _, files = store
    _, loaded_files, _ = sqlite_store.load()
    assert {f.path for f in loaded_files} == {f.path for f in files}
    by_path = {f.path: f for f in loaded_files}
    broken = by_path["broken.sv"]
    assert broken.parse_error_count > 0
    assert broken.content_hash == "0" * 64
    assert broken.skipped_reason is None


def test_save_is_a_full_rewrite(store) -> None:
    sqlite_store, graph, files = store
    sqlite_store.save(graph, files, root=Path("."))
    loaded, loaded_files, _ = sqlite_store.load()
    assert loaded.number_of_edges() == graph.number_of_edges()
    assert len(loaded_files) == len(files)


def test_schema_version_mismatch_raises(store) -> None:
    sqlite_store, _, _ = store
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    with pytest.raises(SchemaVersionError, match="999"):
        sqlite_store.load()


def test_kinds_rehydrate_as_enums(store) -> None:
    sqlite_store, _, _ = store
    loaded, _, _ = sqlite_store.load()
    kinds = {d["kind"] for _, d in loaded.nodes(data=True)}
    assert NodeKind.MODULE in kinds
    edge_kinds = {d["kind"] for _, _, d in loaded.edges(data=True)}
    assert EdgeKind.DECLARES in edge_kinds
    assert EdgeKind.INSTANTIATES in edge_kinds
