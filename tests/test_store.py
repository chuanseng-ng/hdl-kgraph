"""SQLite round-trip tests."""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import (
    SCHEMA_VERSION,
    FileMeta,
    SchemaVersionError,
    SqliteStore,
    StoredUnit,
)


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
            language=ir.nodes[0].language,  # the FILE node carries the language
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
    assert by_path["adder.v"].language is Language.VERILOG
    assert by_path["simple_counter.sv"].language is Language.SYSTEMVERILOG


def test_save_is_a_full_rewrite(store) -> None:
    sqlite_store, graph, files = store
    sqlite_store.save(graph, files, root=Path("."))
    loaded, loaded_files, _ = sqlite_store.load()
    assert loaded.number_of_edges() == graph.number_of_edges()
    assert len(loaded_files) == len(files)


def test_save_leaves_no_temp_file(store) -> None:
    sqlite_store, _, _ = store
    assert list(sqlite_store.db_path.parent.glob("*.tmp")) == []


def test_save_overwrites_stale_temp_file(store) -> None:
    sqlite_store, graph, files = store
    tmp = sqlite_store.db_path.with_name(sqlite_store.db_path.name + ".tmp")
    tmp.write_text("garbage left by a crashed build")
    sqlite_store.save(graph, files, root=Path("."))
    assert not tmp.exists()
    loaded, _, _ = sqlite_store.load()
    assert loaded.number_of_edges() == graph.number_of_edges()


@pytest.mark.skipif(sys.platform == "win32", reason="open handles block os.replace on Windows")
def test_save_succeeds_while_reader_holds_a_transaction(store) -> None:
    """A rewrite must not raise 'database is locked' under a concurrent
    reader (the MCP server reloading mid-`update`): the swap leaves the
    reader's snapshot untouched and the next load sees the new write."""
    sqlite_store, graph, files = store
    reader = sqlite3.connect(sqlite_store.db_path)
    try:
        reader.execute("BEGIN")
        before = reader.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        sqlite_store.save(graph, files, root=Path("."), options_hash="during-read")
        assert reader.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == before
    finally:
        reader.close()
    _, _, meta = sqlite_store.load()
    assert meta["options_hash"] == "during-read"


def test_save_retries_swap_on_permission_error(store, monkeypatch) -> None:
    """The Windows case: os.replace fails while a reader holds the file open."""
    sqlite_store, graph, files = store
    real_replace, calls = os.replace, []

    def flaky(src: object, dst: object) -> None:
        calls.append((src, dst))
        if len(calls) < 3:
            raise PermissionError("file held open by a reader")
        real_replace(src, dst)

    monkeypatch.setattr("hdl_kgraph.storage.sqlite_store.os.replace", flaky)
    monkeypatch.setattr("hdl_kgraph.storage.sqlite_store.time.sleep", lambda _s: None)
    sqlite_store.save(graph, files, root=Path("."), options_hash="retried")
    assert len(calls) == 3
    _, _, meta = sqlite_store.load()
    assert meta["options_hash"] == "retried"


def test_schema_version_mismatch_raises(store) -> None:
    sqlite_store, _, _ = store
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    with pytest.raises(SchemaVersionError, match="999"):
        sqlite_store.load()


def test_old_database_is_refused(store) -> None:
    """The per-file warnings column bumped the schema to v4: an M5/M6 (v3)
    database must be refused with the rebuild message — rebuild *is* the
    migration."""
    assert SCHEMA_VERSION == "4"
    sqlite_store, _, _ = store
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    with pytest.raises(SchemaVersionError, match="hdl-kgraph build"):
        sqlite_store.load()


def test_units_and_options_hash_round_trip(store) -> None:
    sqlite_store, graph, files = store
    units = {
        "adder.v": StoredUnit(ir='{"path": "adder.v"}', macro_events="[]", included="[]"),
        "top.v": StoredUnit(
            ir='{"path": "top.v"}', macro_events='[{"op": "undef"}]', included='["defs.svh"]'
        ),
    }
    sqlite_store.save(graph, files, root=Path("."), units=units, options_hash="abc123")
    assert sqlite_store.load_units() == units
    _, _, meta = sqlite_store.load()
    assert meta["options_hash"] == "abc123"


def test_save_without_units_clears_stale_units(store) -> None:
    sqlite_store, graph, files = store
    units = {"adder.v": StoredUnit(ir="{}", macro_events="[]", included="[]")}
    sqlite_store.save(graph, files, root=Path("."), units=units)
    sqlite_store.save(graph, files, root=Path("."))
    assert sqlite_store.load_units() == {}


def test_load_units_checks_schema_version(store) -> None:
    sqlite_store, _, _ = store
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    with pytest.raises(SchemaVersionError, match="'1'"):
        sqlite_store.load_units()


def test_kinds_rehydrate_as_enums(store) -> None:
    sqlite_store, _, _ = store
    loaded, _, _ = sqlite_store.load()
    kinds = {d["kind"] for _, d in loaded.nodes(data=True)}
    assert NodeKind.MODULE in kinds
    edge_kinds = {d["kind"] for _, _, d in loaded.edges(data=True)}
    assert EdgeKind.DECLARES in edge_kinds
    assert EdgeKind.INSTANTIATES in edge_kinds


def test_file_warnings_round_trip(store) -> None:
    sqlite_store, graph, files = store
    files = [
        FileMeta(
            path=f.path,
            language=f.language,
            content_hash=f.content_hash,
            size_bytes=f.size_bytes,
            parse_error_count=f.parse_error_count,
            warnings=['top.sv:1: cannot resolve `include "missing.svh"']
            if f.path == "broken.sv"
            else [],
        )
        for f in files
    ]
    sqlite_store.save(graph, files, root=Path("."))
    _, loaded_files, _ = sqlite_store.load()
    by_path = {f.path: f for f in loaded_files}
    assert by_path["broken.sv"].warnings == ['top.sv:1: cannot resolve `include "missing.svh"']
    assert by_path["adder.v"].warnings == []
    assert sqlite_store.load_file_warnings() == {
        "broken.sv": ['top.sv:1: cannot resolve `include "missing.svh"']
    }


def test_load_file_warnings_checks_schema_version(store) -> None:
    sqlite_store, _, _ = store
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    with pytest.raises(SchemaVersionError, match="'1'"):
        sqlite_store.load_file_warnings()
