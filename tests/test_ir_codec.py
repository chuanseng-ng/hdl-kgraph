"""Round-trip tests for the pass-1 IR codec (M4 incremental updates)."""

import json
from pathlib import Path

import pytest

from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.preprocessor import MacroDef, MacroEvent
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.vhdl import VhdlParser
from hdl_kgraph.storage.ir_codec import (
    ir_from_json,
    ir_to_json,
    macro_events_from_json,
    macro_events_to_json,
)


def _normalize(value) -> str:
    return json.dumps(value, sort_keys=True, default=list)


@pytest.fixture(scope="module")
def fixture_irs(fixtures_dir: Path) -> list:
    sv = SystemVerilogParser()
    vhdl = VhdlParser()
    irs = []
    for path in sorted(fixtures_dir.iterdir()):
        if path.suffix in sv.suffixes:
            irs.append(sv.parse(Path(path.name), path.read_text()))
        elif path.suffix in vhdl.suffixes:
            irs.append(vhdl.parse(Path(path.name), path.read_text()))
    return irs


def test_ir_round_trip_preserves_everything(fixture_irs) -> None:
    for ir in fixture_irs:
        decoded = ir_from_json(ir_to_json(ir))
        assert decoded.path == ir.path
        assert decoded.parse_error_count == ir.parse_error_count
        assert len(decoded.nodes) == len(ir.nodes)
        for got, want in zip(decoded.nodes, ir.nodes, strict=True):
            assert got.id == want.id
            assert got.kind is want.kind
            assert got.language is want.language
            assert got.line_span == want.line_span
            assert _normalize(got.attrs) == _normalize(want.attrs)
        assert len(decoded.local_edges) == len(ir.local_edges)
        for got, want in zip(decoded.local_edges, ir.local_edges, strict=True):
            assert (got.src, got.dst, got.kind) == (want.src, want.dst, want.kind)
            assert got.confidence == want.confidence
            assert _normalize(got.attrs) == _normalize(want.attrs)
        assert len(decoded.unresolved_refs) == len(ir.unresolved_refs)
        for got, want in zip(decoded.unresolved_refs, ir.unresolved_refs, strict=True):
            assert got.edge_kind is want.edge_kind
            assert (got.src_id, got.target_name) == (want.src_id, want.target_name)
            assert got.line_span == want.line_span
            assert got.confidence == want.confidence
            assert _normalize(got.attrs) == _normalize(want.attrs)


def test_decoded_irs_link_to_an_identical_graph(fixture_irs) -> None:
    original = build_graph(fixture_irs)
    decoded = build_graph([ir_from_json(ir_to_json(ir)) for ir in fixture_irs])
    assert set(decoded.nodes) == set(original.nodes)
    want = sorted(
        (u, v, d["kind"].value, d["confidence"], _normalize(d["attrs"]))
        for u, v, d in original.edges(data=True)
    )
    got = sorted(
        (u, v, d["kind"].value, d["confidence"], _normalize(d["attrs"]))
        for u, v, d in decoded.edges(data=True)
    )
    assert got == want


def test_macro_event_round_trip() -> None:
    events = [
        MacroEvent(
            op="define",
            name="MAX",
            macro=MacroDef(
                name="MAX",
                params=[("a", None), ("b", "0")],
                body="((a) > (b) ? (a) : (b))",
                file="defs.svh",
                line=3,
            ),
        ),
        MacroEvent(
            op="define",
            name="WIDTH",
            macro=MacroDef(name="WIDTH", params=None, body="8", file="defs.svh", line=4),
        ),
        MacroEvent(op="undef", name="WIDTH"),
    ]
    decoded = macro_events_from_json(macro_events_to_json(events))
    assert decoded == events
