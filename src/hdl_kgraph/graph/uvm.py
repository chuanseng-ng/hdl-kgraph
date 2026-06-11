"""UVM topology report and TEST_COVERS derivation (M5).

UVM base classes are virtually never in the parsed sources, so the M1
linker's unresolved-stub mechanism is the anchor: ``class my_env extends
uvm_env`` leaves an EXTENDS edge to the ``uvm_env`` stub, and walking each
class's EXTENDS chain (cycle-guarded) to the first ``uvm_*`` name classifies
it by role. Parameterized bases and ``uvm_test_base``-style projects match
by prefix.

TEST_COVERS is a 0.4 name-pattern heuristic, emitted post-link:

* a top-level module matching ``tb_*`` / ``*_tb`` / ``*_testbench`` /
  ``test_*`` covers each resolved module it directly instantiates, and
* every uvm_test-derived class covers those same DUT modules (the tb top
  is where the UVM run lives; finer binding would need elaboration).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import networkx as nx

from hdl_kgraph.schema import CONFIDENCE_HEURISTIC, Edge, EdgeKind, NodeKind

#: Checked in order — uvm_sequencer must match before uvm_sequence.
_UVM_ROLES: tuple[tuple[str, str], ...] = (
    ("uvm_test", "test"),
    ("uvm_env", "env"),
    ("uvm_agent", "agent"),
    ("uvm_driver", "driver"),
    ("uvm_monitor", "monitor"),
    ("uvm_scoreboard", "scoreboard"),
    ("uvm_sequencer", "sequencer"),
    ("uvm_sequence", "sequence"),
    ("uvm_subscriber", "subscriber"),
    ("uvm_component", "component"),
    ("uvm_object", "object"),
)

ROLE_ORDER = [role for _, role in _UVM_ROLES] + ["other"]

_TB_NAME_RE = re.compile(r"^tb_|_tb$|_testbench$|^test_", re.IGNORECASE)


@dataclass
class UvmComponent:
    """A class whose inheritance chain reaches a uvm_* base."""

    class_id: str
    name: str
    role: str
    base_chain: list[str]  # names from the class's parent up to the uvm base
    file: str
    line: int


def _extends_chain(g: nx.MultiDiGraph, class_id: str, max_depth: int = 64) -> list[str]:
    """Base-class names from *class_id* upward (first base on ambiguity)."""
    chain: list[str] = []
    seen = {class_id}
    current = class_id
    for _ in range(max_depth):
        bases = sorted(
            v for _, v, d in g.out_edges(current, data=True) if d["kind"] is EdgeKind.EXTENDS
        )
        if not bases or bases[0] in seen:
            break
        current = bases[0]
        seen.add(current)
        chain.append(g.nodes[current]["name"])
    return chain


def _role_of(base_name: str) -> str:
    for prefix, role in _UVM_ROLES:
        if base_name.startswith(prefix):
            return role
    return "other"


def uvm_topology(g: nx.MultiDiGraph) -> list[UvmComponent]:
    """Every class that derives (transitively) from a ``uvm_*`` base."""
    components: list[UvmComponent] = []
    for node_id, data in g.nodes(data=True):
        if data["kind"] is not NodeKind.CLASS or data["attrs"].get("unresolved"):
            continue
        chain = _extends_chain(g, node_id)
        base = next((name for name in chain if name.startswith("uvm_")), None)
        if base is None:
            continue
        components.append(
            UvmComponent(
                class_id=node_id,
                name=data["name"],
                role=_role_of(base),
                base_chain=chain,
                file=data["file"],
                line=data["line_span"][0],
            )
        )
    return sorted(components, key=lambda c: (ROLE_ORDER.index(c.role), c.name))


def derive_test_covers(g: nx.MultiDiGraph) -> list[Edge]:
    """TEST_COVERS edges (0.4, name-pattern evidence) — see module docstring."""
    duts: set[str] = set()
    edges: list[Edge] = []
    emitted: set[tuple[str, str]] = set()

    def emit(src: str, dst: str) -> None:
        if (src, dst) in emitted:
            return
        emitted.add((src, dst))
        edges.append(
            Edge(
                src=src,
                dst=dst,
                kind=EdgeKind.TEST_COVERS,
                confidence=CONFIDENCE_HEURISTIC,
                attrs={"evidence": "name_pattern"},
            )
        )

    for tb_id, data in g.nodes(data=True):
        if data["kind"] is not NodeKind.MODULE or data["attrs"].get("unresolved"):
            continue
        if not _TB_NAME_RE.search(data["name"]):
            continue
        if any(d["kind"] is EdgeKind.INSTANTIATES for _, _, d in g.in_edges(tb_id, data=True)):
            continue  # not a top: it is itself part of a larger bench
        for _, inst_id, d in g.out_edges(tb_id, data=True):
            if d["kind"] is not EdgeKind.DECLARES:
                continue
            if g.nodes[inst_id]["kind"] is not NodeKind.INSTANCE:
                continue
            for _, target, dd in g.out_edges(inst_id, data=True):
                if dd["kind"] is not EdgeKind.INSTANTIATES:
                    continue
                target_data = g.nodes[target]
                if target_data["kind"] in (NodeKind.MODULE, NodeKind.ENTITY) and not (
                    target_data["attrs"].get("unresolved")
                ):
                    duts.add(target)
                    emit(tb_id, target)

    if duts:
        for component in uvm_topology(g):
            if component.role != "test":
                continue
            for dut in sorted(duts):
                emit(component.class_id, dut)
    return edges
