"""Parallel pass-1 parsing (issue #26): worker pool builds match serial builds."""

import json
import os
from pathlib import Path

import pytest

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.pipeline import (
    DEFAULT_JOBS_CAP,
    MIN_PARALLEL_FILES,
    MIN_PARALLEL_KB,
    _effective_jobs,
    _parse_sv_task,
    _parse_vhdl_task,
    run_build,
    run_update,
)
from hdl_kgraph.storage.sqlite_store import SqliteStore


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Mixed SV/VHDL tree with include/define chains and a both-branches ifdef."""
    (tmp_path / "defs.svh").write_text("`define WIDTH 8\n`define MODE 1\n")
    (tmp_path / "leaf.sv").write_text(
        '`include "defs.svh"\n'
        "module leaf(input logic [`WIDTH-1:0] a, output logic [`WIDTH-1:0] y);\n"
        "  assign y = a;\n"
        "endmodule\n"
    )
    (tmp_path / "my_pkg.sv").write_text("package my_pkg;\n  localparam int K = 4;\nendpackage\n")
    (tmp_path / "mid.sv").write_text(
        "module mid(input logic [7:0] a, output logic [7:0] y);\n"
        "  import my_pkg::*;\n"
        "  leaf u_leaf(.a(a), .y(y));\n"
        "endmodule\n"
    )
    (tmp_path / "cond.sv").write_text(
        "module cond(input logic c, output logic y);\n"
        "`ifdef FAST\n"
        "  assign y = c;\n"
        "`else\n"
        "  assign y = ~c;\n"
        "`endif\n"
        "endmodule\n"
    )
    (tmp_path / "alu.vhd").write_text(
        "entity alu is\n  port (a : in bit; y : out bit);\nend entity;\n"
        "architecture rtl of alu is\nbegin\n  y <= a;\nend architecture;\n"
    )
    for i in range(4):
        (tmp_path / f"unit{i}.sv").write_text(
            f"module unit{i}(input logic a, output logic y);\n"
            f"  mid u_mid(.a(8'(a)), .y());\n"
            "endmodule\n"
        )
    (tmp_path / "top.sv").write_text(
        "module top(input logic [7:0] a, output logic [7:0] y);\n"
        "  mid u_mid(.a(a), .y(y));\n"
        "  cond u_cond(.c(a[0]), .y());\n"
        "endmodule\n"
    )
    return tmp_path


def _load(db: Path):
    graph, files, _ = SqliteStore(db).load()
    return graph, files


def _edge_set(g):
    return sorted(
        (u, v, d["kind"].value, d["confidence"], json.dumps(d["attrs"], sort_keys=True))
        for u, v, d in g.edges(data=True)
    )


def test_parallel_build_matches_serial(corpus: Path) -> None:
    serial_db = corpus / "serial.db"
    parallel_db = corpus / "parallel.db"
    serial = run_build(corpus, db_path=serial_db, options=BuildOptions(jobs=1))
    parallel = run_build(corpus, db_path=parallel_db, options=BuildOptions(jobs=2))
    for field in (
        "parsed_files",
        "vhdl_files",
        "skipped",
        "node_count",
        "edge_count",
        "macros_defined",
        "includes_resolved",
        "includes_unresolved",
        "parse_error_count",
        "preproc_warning_count",
    ):
        assert getattr(parallel, field) == getattr(serial, field), field
    g_serial, f_serial = _load(serial_db)
    g_parallel, f_parallel = _load(parallel_db)
    assert set(g_parallel.nodes) == set(g_serial.nodes)
    assert _edge_set(g_parallel) == _edge_set(g_serial)
    assert sorted(f.path for f in f_parallel) == sorted(f.path for f in f_serial)


def test_update_parallel_matches_full_rebuild(corpus: Path) -> None:
    run_build(corpus, options=BuildOptions(jobs=2))
    path = corpus / "mid.sv"
    path.write_text(path.read_text().replace("u_leaf", "u_leaf2"))
    update = run_update(corpus, options=BuildOptions(jobs=2))
    assert update.build is not None and update.build.reused_files > 0
    db = update.build.db_path
    incremental, _ = _load(db)
    run_build(corpus, options=BuildOptions(jobs=1))
    full, _ = _load(db)
    assert set(incremental.nodes) == set(full.nodes)
    assert _edge_set(incremental) == _edge_set(full)


def test_worker_functions_parse_in_process() -> None:
    sv = _parse_sv_task("a.sv", "module a;\nendmodule\n", [])
    assert any(n.kind.value == "module" for n in sv.nodes)
    vhdl = _parse_vhdl_task("b.vhd", "entity b is\nend entity;\n", "work")
    assert any(n.kind.value == "entity" for n in vhdl.nodes)


def test_effective_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    big = MIN_PARALLEL_KB * 1024
    # Explicit --jobs wins regardless of build size; floor is 1.
    assert _effective_jobs(BuildOptions(jobs=3), candidates=1, candidate_bytes=10) == 3
    assert _effective_jobs(BuildOptions(jobs=0), candidates=1000, candidate_bytes=big) == 1
    # Auto mode: serial below either threshold (few files, or many tiny ones).
    assert _effective_jobs(BuildOptions(), MIN_PARALLEL_FILES - 1, big) == 1
    assert _effective_jobs(BuildOptions(), MIN_PARALLEL_FILES, big - 1) == 1
    monkeypatch.setattr(os, "cpu_count", lambda: 32)
    assert _effective_jobs(BuildOptions(), MIN_PARALLEL_FILES, big) == DEFAULT_JOBS_CAP
    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    assert _effective_jobs(BuildOptions(), MIN_PARALLEL_FILES, big) == 2


def test_worker_failure_is_isolated(corpus: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A parser-worker exception is recorded and skipped, not a full-build abort (#65)."""
    from hdl_kgraph import pipeline

    real = pipeline._parse_sv_task

    def flaky(relpath, text, line_map):
        if relpath.endswith("cond.sv"):
            raise RuntimeError("simulated worker crash")
        return real(relpath, text, line_map)

    monkeypatch.setattr(pipeline, "_parse_sv_task", flaky)
    report = run_build(corpus, options=BuildOptions(jobs=1))
    # The build completed (partial), recorded the failure, and kept the rest.
    assert report.worker_failures == 1
    assert "cond.sv" in report.file_errors
    assert any("worker failed" in w and "cond.sv" in w for w in report.warnings)
    graph, _ = _load(report.db_path)
    by_name: dict[str, list] = {}
    for _, d in graph.nodes(data=True):
        by_name.setdefault(d["name"], []).append(d)
    assert "top" in by_name and "mid" in by_name  # other units still parsed
    # The crashed unit was never parsed: top's dangling instantiation leaves a
    # "cond" node, but only as an unresolved stub, never a real parsed MODULE.
    cond_nodes = by_name.get("cond", [])
    assert cond_nodes, "expected an unresolved `cond` stub from top's dangling instantiation"
    assert all(d["attrs"].get("unresolved") for d in cond_nodes)


def test_incremental_link_reresolves_few_refs(tmp_path: Path) -> None:
    """An internal one-file edit re-resolves far fewer refs than the design total (#64/#65)."""
    # Pure-SV design (a VHDL file would force the conservative full-link fallback).
    (tmp_path / "leaf.sv").write_text(
        "module leaf(input logic a, output logic y);\n  assign y = a;\nendmodule\n"
    )
    (tmp_path / "mid.sv").write_text(
        "module mid(input logic a, output logic y);\n  leaf u_leaf(.a(a), .y(y));\nendmodule\n"
    )
    for i in range(6):
        (tmp_path / f"unit{i}.sv").write_text(
            f"module unit{i}(input logic a, output logic y);\n  mid u(.a(a), .y(y));\nendmodule\n"
        )
    (tmp_path / "top.sv").write_text(
        "module top(input logic a, output logic y);\n  mid u_mid(.a(a), .y(y));\nendmodule\n"
    )
    run_build(tmp_path, options=BuildOptions(jobs=1))
    leaf = tmp_path / "leaf.sv"
    leaf.write_text(leaf.read_text().replace("assign y = a;", "assign y = ~a;"))
    update = run_update(tmp_path, options=BuildOptions(jobs=1))
    assert update.build is not None
    assert update.build.incremental_link is True
    total, reresolved = update.build.refs_total, update.build.refs_reresolved
    assert total > 0
    # The scoping invariant: editing one leaf re-resolves only its closure, not
    # the whole design — re-resolving everything would defeat the point.
    assert reresolved < total
    assert reresolved * 2 <= total, f"reresolved {reresolved} of {total} refs is not well-scoped"
