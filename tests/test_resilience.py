"""Degradation-path coverage (#75): the build must survive corrupt caches,
internal parser bugs, non-UTF-8 sources, and cross-process serialization.
"""

import pickle
import sqlite3
from pathlib import Path

import pytest

from hdl_kgraph.parser import systemverilog, vhdl
from hdl_kgraph.parser.base import FileIR, UnresolvedRef
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.vhdl import VhdlParser
from hdl_kgraph.pipeline import run_build, run_update
from hdl_kgraph.storage.sqlite_store import SqliteStore


def _graph(root: Path):
    graph, _, _ = SqliteStore(root / ".hdl-kgraph" / "graph.db").load()
    return graph


# --- internal parser bug: a walker exception must not abort the build -------


def test_sv_walker_internal_error_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: object, node: object) -> None:
        raise RuntimeError("walker bug")

    monkeypatch.setattr(systemverilog._Walker, "visit", boom)
    ir = SystemVerilogParser().parse(Path("t.sv"), "module t;\nendmodule\n")
    assert ir.parse_error_count >= 1
    assert any("internal parser error" in e for e in ir.parse_errors)


def test_vhdl_walker_internal_error_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: object, node: object) -> None:
        raise RuntimeError("walker bug")

    monkeypatch.setattr(vhdl._Walker, "visit", boom)
    ir = VhdlParser().parse(Path("t.vhd"), "entity t is\nend entity;\n")
    assert ir.parse_error_count >= 1
    assert any("internal parser error" in e for e in ir.parse_errors)


# --- corrupt stored IR row: fall back to a fresh parse ----------------------


def test_corrupt_stored_ir_falls_back_to_fresh_parse(tmp_path: Path) -> None:
    (tmp_path / "leaf.sv").write_text("module leaf;\nendmodule\n")
    (tmp_path / "top.sv").write_text("module top;\n  leaf u();\nendmodule\n")
    run_build(tmp_path)

    # Corrupt leaf's persisted pass-1 IR so decoding it raises.
    db = tmp_path / ".hdl-kgraph" / "graph.db"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE file_irs SET ir = '{not valid json' WHERE path = 'leaf.sv'")

    # Touch a different file so update runs; the corrupt (unchanged) unit must
    # be re-parsed fresh instead of crashing.
    (tmp_path / "top.sv").write_text("module top;\n  leaf u();\n// touched\nendmodule\n")
    report = run_update(tmp_path)
    assert report.full_rebuild_reason is None or report.build is not None
    assert "leaf.sv::module:leaf" in _graph(tmp_path)


# --- non-UTF-8 sources: errors="replace", never a UnicodeDecodeError ---------


def test_non_utf8_sources_are_tolerated(tmp_path: Path) -> None:
    (tmp_path / "bad.sv").write_bytes(b"module bad;\n// \xff\xfe\xff garbage\nendmodule\n")
    (tmp_path / "bad.vhd").write_bytes(
        b"entity bad_e is\nend entity;  -- \xff\xfe\narchitecture a of bad_e is\nbegin\nend a;\n"
    )
    run_build(tmp_path)  # must not raise UnicodeDecodeError
    graph = _graph(tmp_path)
    assert "bad.sv::module:bad" in graph
    assert any(d["name"] == "bad_e" for _, d in graph.nodes(data=True))


# --- cross-process serialization: pass-1 IR must pickle round-trip ----------


def test_file_ir_with_refs_pickles_round_trip() -> None:
    """ProcessPoolExecutor ships FileIR/UnresolvedRef over a pipe (pickled);
    a non-picklable field would only surface under a real pool, so pin it here."""
    ir = SystemVerilogParser().parse(
        Path("m.sv"),
        "module m;\n  sub u(.a(x), .b(y));\n  import pkg::*;\nendmodule\n",
    )
    assert ir.unresolved_refs  # the instance/import refs we want to round-trip
    restored = pickle.loads(pickle.dumps(ir))
    assert isinstance(restored, FileIR)
    assert [n.id for n in restored.nodes] == [n.id for n in ir.nodes]
    assert all(isinstance(r, UnresolvedRef) for r in restored.unresolved_refs)
    assert len(restored.unresolved_refs) == len(ir.unresolved_refs)
