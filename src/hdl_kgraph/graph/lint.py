"""Lint-flavored analyses over the knowledge graph (M5).

These are graph queries, not a linter: name-level, evidence-scored, and
deliberately conservative about what they exclude (implicit-net stubs,
files with parse errors) so a finding is worth reading. The CLI ``lint``
command always exits 0 — it reports, it does not gate.

Checks:

* ``unconnected-port`` — a resolved instantiation leaves a target port with
  no CONNECTS binding (wildcards expand per-port in the linker, so ``.*``
  covers everything).
* ``open-port`` — an explicitly open binding (``.x()`` / VHDL ``open``);
  informational, the designer said so.
* ``undriven-signal`` — a SIGNAL or output PORT with no incoming DRIVES
  (process, continuous assign, declaration initializer, or instance output).
  Requires the M5 dataflow edges.
* ``unread-signal`` — a SIGNAL nothing READS, asserts on, or covers.
* ``dead-module`` — a module/entity never instantiated and not named as a
  top module; confidence 0.4 because the real top looks identical.
* ``redundant-override`` — a parameter override equal to the declared
  default (whitespace-normalized text comparison).

Known-benign findings can be waived (``[[lint.waivers]]`` in the config or a
``--waiver-file``); :func:`apply_waivers` partitions findings post-run so
library callers always see the raw list.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch, fnmatchcase

import networkx as nx

from hdl_kgraph.config import LintWaiver
from hdl_kgraph.graph.analysis import find_top_modules
from hdl_kgraph.schema import (
    CONFIDENCE_HEURISTIC,
    CONFIDENCE_RESOLVED,
    EdgeKind,
    NodeKind,
)

#: Port directions that require an internal driver.
_OUTPUT_DIRECTIONS = frozenset({"output", "out", "buffer"})

#: Edge kinds that count as "something consumes this signal".
_READ_LIKE = frozenset({EdgeKind.READS, EdgeKind.ASSERTS_ON, EdgeKind.COVERS})


@dataclass
class LintFinding:
    check: str
    node_id: str
    name: str
    file: str
    line: int
    message: str
    confidence: float = CONFIDENCE_RESOLVED
    unit: str = ""  # owning module/entity: waiver `module` patterns match this


def _location(g: nx.MultiDiGraph, node_id: str) -> tuple[str, int]:
    data = g.nodes[node_id]
    return data["file"], data["line_span"][0]


def _is_stub(g: nx.MultiDiGraph, node_id: str) -> bool:
    return bool(g.nodes[node_id]["attrs"].get("unresolved"))


def _declaring_unit(g: nx.MultiDiGraph, node_id: str) -> str | None:
    for parent, _, d in g.in_edges(node_id, data=True):
        if d["kind"] is EdgeKind.DECLARES:
            return parent
    return None


def _unit_name(g: nx.MultiDiGraph, unit_id: str | None) -> str:
    return g.nodes[unit_id]["name"] if unit_id is not None else ""


def unconnected_ports(g: nx.MultiDiGraph) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for inst_id, data in g.nodes(data=True):
        if data["kind"] is not NodeKind.INSTANCE:
            continue
        targets = [
            v
            for _, v, d in g.out_edges(inst_id, data=True)
            if d["kind"] is EdgeKind.INSTANTIATES and not _is_stub(g, v)
        ]
        if len(targets) != 1:
            continue  # unresolved or ambiguous target: port list unreliable
        connected = {
            v for _, v, d in g.out_edges(inst_id, data=True) if d["kind"] is EdgeKind.CONNECTS
        }
        if not connected:
            continue  # no port map at all (e.g. zero-port module)
        file, line = _location(g, inst_id)
        for _, port_id, d in g.out_edges(targets[0], data=True):
            if d["kind"] is not EdgeKind.DECLARES:
                continue
            port = g.nodes[port_id]
            if port["kind"] is not NodeKind.PORT or port_id in connected:
                continue
            findings.append(
                LintFinding(
                    check="unconnected-port",
                    node_id=inst_id,
                    name=data["qualified_name"],
                    file=file,
                    line=line,
                    message=f"port '{port['name']}' of {g.nodes[targets[0]]['name']} "
                    "is not connected",
                    unit=g.nodes[targets[0]]["name"],
                )
            )
    return findings


def open_ports(g: nx.MultiDiGraph) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for inst_id, port_id, d in g.edges(data=True):
        if d["kind"] is not EdgeKind.CONNECTS:
            continue
        expr = str(d["attrs"].get("expr_text", "")).strip()
        if expr and expr.lower() != "open":
            continue
        port_name = d["attrs"].get("port_name") or g.nodes[port_id].get("name", "?")
        file, line = _location(g, inst_id)
        findings.append(
            LintFinding(
                check="open-port",
                node_id=inst_id,
                name=g.nodes[inst_id]["qualified_name"],
                file=file,
                line=line,
                message=f"port '{port_name}' is explicitly left open",
                unit=_unit_name(g, _declaring_unit(g, port_id)),
            )
        )
    return findings


def undriven_signals(
    g: nx.MultiDiGraph, error_files: frozenset[str] = frozenset()
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for node_id, data in g.nodes(data=True):
        kind = data["kind"]
        if kind is NodeKind.SIGNAL:
            if _is_stub(g, node_id):
                continue  # implicit nets are reported as stubs, not lint
        elif kind is NodeKind.PORT:
            if str(data["attrs"].get("direction", "")) not in _OUTPUT_DIRECTIONS:
                continue
            if _is_stub(g, node_id):
                continue
        else:
            continue
        if data["file"] in error_files:
            continue  # partial parse: absence of a driver proves nothing
        unit = _declaring_unit(g, node_id)
        if unit is not None and g.nodes[unit]["kind"] is NodeKind.INTERFACE:
            continue  # interface signals are driven through modports/instances
        if any(d["kind"] is EdgeKind.DRIVES for _, _, d in g.in_edges(node_id, data=True)):
            continue
        what = "output port" if kind is NodeKind.PORT else "signal"
        file, line = _location(g, node_id)
        findings.append(
            LintFinding(
                check="undriven-signal",
                node_id=node_id,
                name=data["qualified_name"],
                file=file,
                line=line,
                message=f"{what} '{data['name']}' is never driven",
                unit=_unit_name(g, unit),
            )
        )
    return findings


def unread_signals(
    g: nx.MultiDiGraph, error_files: frozenset[str] = frozenset()
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for node_id, data in g.nodes(data=True):
        if data["kind"] is not NodeKind.SIGNAL or _is_stub(g, node_id):
            continue
        if data["file"] in error_files:
            continue
        unit = _declaring_unit(g, node_id)
        if unit is not None and g.nodes[unit]["kind"] is NodeKind.INTERFACE:
            continue
        if any(d["kind"] in _READ_LIKE for _, _, d in g.in_edges(node_id, data=True)):
            continue
        file, line = _location(g, node_id)
        findings.append(
            LintFinding(
                check="unread-signal",
                node_id=node_id,
                name=data["qualified_name"],
                file=file,
                line=line,
                message=f"signal '{data['name']}' is never read",
                unit=_unit_name(g, unit),
            )
        )
    return findings


def dead_modules(g: nx.MultiDiGraph, tops: frozenset[str] = frozenset()) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for node_id in find_top_modules(g):
        data = g.nodes[node_id]
        if data["name"] in tops:
            continue
        file, line = _location(g, node_id)
        findings.append(
            LintFinding(
                check="dead-module",
                node_id=node_id,
                name=data["qualified_name"],
                file=file,
                line=line,
                message=f"{data['kind'].value} '{data['name']}' is never instantiated "
                "(dead code, or an unlisted top module)",
                confidence=CONFIDENCE_HEURISTIC,
                unit=data["name"],
            )
        )
    return findings


def _normalize_expr(text: str) -> str:
    return " ".join(str(text).split())


def redundant_overrides(g: nx.MultiDiGraph) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for inst_id, param_id, d in g.edges(data=True):
        if d["kind"] is not EdgeKind.PARAMETERIZES:
            continue
        param = g.nodes[param_id]
        if param["kind"] is not NodeKind.PARAMETER:
            continue
        default = param["attrs"].get("default")
        value = d["attrs"].get("value_text")
        if default is None or value is None:
            continue
        if _normalize_expr(str(value)) != _normalize_expr(str(default)):
            continue
        file, line = _location(g, inst_id)
        findings.append(
            LintFinding(
                check="redundant-override",
                node_id=inst_id,
                name=g.nodes[inst_id]["qualified_name"],
                file=file,
                line=line,
                message=f"parameter '{param['name']}' is overridden with its "
                f"default value ({_normalize_expr(str(default))})",
                unit=_unit_name(g, _declaring_unit(g, param_id)),
            )
        )
    return findings


CHECKS: dict[str, Callable[..., list[LintFinding]]] = {
    "unconnected-port": unconnected_ports,
    "open-port": open_ports,
    "undriven-signal": undriven_signals,
    "unread-signal": unread_signals,
    "dead-module": dead_modules,
    "redundant-override": redundant_overrides,
}


def run_checks(
    g: nx.MultiDiGraph,
    names: Iterable[str] | None = None,
    tops: frozenset[str] = frozenset(),
    error_files: frozenset[str] = frozenset(),
) -> list[LintFinding]:
    """Run the named checks (default: all) and return sorted findings."""
    selected = list(names) if names is not None else list(CHECKS)
    unknown = [n for n in selected if n not in CHECKS]
    if unknown:
        raise ValueError(f"unknown lint check(s): {', '.join(sorted(unknown))}")
    findings: list[LintFinding] = []
    for name in selected:
        if name == "dead-module":
            findings.extend(dead_modules(g, tops))
        elif name in ("undriven-signal", "unread-signal"):
            findings.extend(CHECKS[name](g, error_files))
        else:
            findings.extend(CHECKS[name](g))
    return sorted(findings, key=lambda f: (f.file, f.line, f.check, f.name))


@dataclass
class WaivedFinding:
    finding: LintFinding
    reason: str
    waiver_index: int  # position in the waiver list, for audit output


@dataclass
class WaiverResult:
    kept: list[LintFinding]
    waived: list[WaivedFinding]
    unused: list[int]  # waiver indices that matched nothing (selected checks only)
    unknown: list[int]  # waiver indices naming a check that does not exist


def _name_matches(pattern: str, qualified: str) -> bool:
    # search_nodes convention: a dotted pattern matches the qualified name,
    # a plain one matches the last segment; identifiers are case-sensitive.
    if "." in pattern:
        return fnmatchcase(qualified, pattern)
    return fnmatchcase(qualified.rsplit(".", 1)[-1], pattern)


def _waiver_matches(waiver: LintWaiver, finding: LintFinding) -> bool:
    if waiver.check != finding.check:
        return False
    if waiver.name is not None and not _name_matches(waiver.name, finding.name):
        return False
    if waiver.module is not None and not fnmatchcase(finding.unit, waiver.module):
        return False
    if waiver.file is not None and not fnmatch(finding.file, waiver.file):
        return False
    return waiver.line is None or finding.line == waiver.line


def apply_waivers(
    findings: Iterable[LintFinding],
    waivers: Sequence[LintWaiver],
    selected: Collection[str] | None = None,
) -> WaiverResult:
    """Partition findings into kept and waived; flag stale and unknown waivers.

    A waiver is *unused* only when its check is among the *selected* checks
    (default: all) and it matched nothing — running ``--check open-port``
    must not flag the rest of the waiver list as stale. The first matching
    waiver supplies the reason; every match counts as used, so overlapping
    waivers are not reported stale.
    """
    selected_set = frozenset(selected) if selected is not None else frozenset(CHECKS)
    used = [False] * len(waivers)
    kept: list[LintFinding] = []
    waived: list[WaivedFinding] = []
    for finding in findings:
        matches = [i for i, w in enumerate(waivers) if _waiver_matches(w, finding)]
        for i in matches:
            used[i] = True
        if matches:
            waived.append(WaivedFinding(finding, waivers[matches[0]].reason, matches[0]))
        else:
            kept.append(finding)
    return WaiverResult(
        kept=kept,
        waived=waived,
        unused=[i for i, w in enumerate(waivers) if w.check in selected_set and not used[i]],
        unknown=[i for i, w in enumerate(waivers) if w.check not in CHECKS],
    )
