"""Graph builder: pass-2 linker (M1).

Consumes per-file IRs from the parser backends and produces the global
knowledge graph as a NetworkX ``MultiDiGraph`` (persisted via
:mod:`hdl_kgraph.storage.sqlite_store`).

Resolution and confidence (see :mod:`hdl_kgraph.schema`):

* a candidate in the same file resolves at 1.0; a unique cross-file match at
  0.8; multiple candidates get one 0.6 edge each (unless exactly one is in
  the referring file, which wins at 1.0)
* a name with no definition gets a stub node (``attrs["unresolved"] = True``)
  shared by every referrer. The *edge* to a stub keeps confidence 1.0 — the
  reference itself is syntactically certain; the stub node carries the
  uncertainty.
* CONNECTS/PARAMETERIZES resolve against the target's PORT/PARAMETER children
  (positional bindings via declaration-order ``attrs["index"]``) at the
  instantiation confidence; bindings that match no declared child of a
  resolved target fall back to an edge to the target itself at <= 0.6 so the
  mismatch stays visible.

Graph conventions: nodes are keyed by :class:`~hdl_kgraph.schema.Node` id and
carry ``kind``/``name``/``qualified_name``/``file``/``line_span``/
``language``/``attrs`` as data; edges carry ``kind``/``confidence``/``attrs``.

M3 — VHDL and mixed-language linking:

* Same-language candidates are tried first under the existing rules. When a
  name finds none, a **case-insensitive cross-language fallback** runs (VHDL
  instantiation → SV MODULE; SV instantiation → VHDL ENTITY) capped at
  ``CONFIDENCE_UNIQUE_MATCH`` (0.8) — a name match across languages is never
  syntactic, even within one file. Vendor tools may bind cross-language names
  differently (case folding, library prefixes, extended/escaped
  identifiers); the ≤0.8 confidence is the honest contract.
* VHDL→VHDL resolution prefers candidates whose ``attrs["library"]`` matches
  the reference's library (``work`` resolves to the *referrer's* library).
* BINDS refs resolve **first**: each CONFIGURATION's component bindings are
  recorded, then a component-style instantiation inside a configured
  entity/architecture resolves through the matching binding (specific label
  beats ``all`` beats ``others``) with ``attrs["bound_by"]`` naming the
  configuration. Without a binding, default binding applies: a like-named
  entity, then the cross-language fallback. Direct-entity instantiation
  resolves normally; configuration instantiation resolves to the named
  CONFIGURATION's configured entity (``attrs["via_configuration"]``).
* CONNECTS/PARAMETERIZES port and generic names match case-insensitively
  when either side is VHDL.

M5 — dataflow, clocks, and verification refs:

* DRIVES/READS/CLOCKED_BY/RESETS/ASSERTS_ON/COVERS refs are **scoped**: the
  name resolves against the referring node's *enclosing design unit's*
  PORT/SIGNAL children (an ARCHITECTURE also searches its entity's ports),
  never by global name. A match in scope resolves at 1.0 (× the ref's own
  evidence confidence). Names matching a PARAMETER/ENUM_MEMBER (or any other
  non-signal child) are dropped — constants are not dataflow. Unmatched
  names become SIGNAL stub children of the unit at ≤ 0.6, so implicit nets
  and typos stay visible. ASSERTS_ON prefers a sibling PROPERTY/SEQUENCE
  (``assert property (p_handshake)``) before falling back to signals. A ref
  with no enclosing unit (e.g. a covergroup inside a class) is dropped.
* Resolved CONNECTS bindings additionally derive instance-level dataflow
  from the formal port's direction: ``INSTANCE --READS--> actual`` for
  inputs, ``--DRIVES-->`` for outputs/buffers, both (≤ 0.6) for inouts.
  Actual-expression identifiers are re-lexed from ``expr_text`` (a regex
  word lexer — the honest approximation); a multi-identifier actual caps at
  0.6. Edge attrs carry ``via_port``/``derived``/``expr_text``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import networkx as nx

from hdl_kgraph.graph.uvm import derive_test_covers
from hdl_kgraph.ids import file_node_id, stub_node_id
from hdl_kgraph.parser.base import FileIR, UnresolvedRef
from hdl_kgraph.schema import (
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_RESOLVED,
    CONFIDENCE_UNIQUE_MATCH,
    Edge,
    EdgeKind,
    Language,
    Node,
    NodeKind,
)

# Node kinds an INSTANTIATES target name may resolve to, per language family.
_SV_INSTANTIABLE = (NodeKind.MODULE, NodeKind.INTERFACE, NodeKind.PROGRAM, NodeKind.PRIMITIVE)
_VHDL_INSTANTIABLE = (NodeKind.ENTITY,)

# (same-language kinds, cross-language fallback kinds) per edge kind, from
# the perspective of an SV/Verilog referrer; a VHDL referrer swaps the
# instantiable pair. Kinds with no cross-language meaning have an empty
# fallback.
_REF_TARGET_KINDS: dict[EdgeKind, tuple[tuple[NodeKind, ...], tuple[NodeKind, ...]]] = {
    EdgeKind.INSTANTIATES: (_SV_INSTANTIABLE, _VHDL_INSTANTIABLE),
    EdgeKind.CONNECTS: (_SV_INSTANTIABLE, _VHDL_INSTANTIABLE),
    EdgeKind.PARAMETERIZES: (_SV_INSTANTIABLE, _VHDL_INSTANTIABLE),
    EdgeKind.IMPORTS: ((NodeKind.PACKAGE,), ()),
    EdgeKind.EXTENDS: ((NodeKind.CLASS,), ()),
    EdgeKind.IMPLEMENTS: ((NodeKind.ENTITY,), ()),
    EdgeKind.USES_PACKAGE: ((NodeKind.VHDL_PACKAGE,), ()),
    EdgeKind.BINDS: ((NodeKind.ENTITY,), (NodeKind.MODULE,)),
}

_STUB_KIND: dict[EdgeKind, NodeKind] = {
    EdgeKind.INSTANTIATES: NodeKind.MODULE,
    EdgeKind.CONNECTS: NodeKind.MODULE,
    EdgeKind.PARAMETERIZES: NodeKind.MODULE,
    EdgeKind.IMPORTS: NodeKind.PACKAGE,
    EdgeKind.EXTENDS: NodeKind.CLASS,
    EdgeKind.IMPLEMENTS: NodeKind.ENTITY,
    EdgeKind.USES_PACKAGE: NodeKind.VHDL_PACKAGE,
    EdgeKind.BINDS: NodeKind.ENTITY,
}

_VHDL_DEFAULT_LIBRARY = "work"

#: Refs resolved against the enclosing unit's children, not by global name.
_SCOPED_REF_KINDS = frozenset(
    {
        EdgeKind.DRIVES,
        EdgeKind.READS,
        EdgeKind.CLOCKED_BY,
        EdgeKind.RESETS,
        EdgeKind.ASSERTS_ON,
        EdgeKind.COVERS,
    }
)

#: Unit kinds that own the port/signal namespace scoped refs resolve in.
_SCOPED_UNIT_KINDS = frozenset(
    {NodeKind.MODULE, NodeKind.INTERFACE, NodeKind.PROGRAM, NodeKind.ARCHITECTURE, NodeKind.ENTITY}
)

_SIGNAL_KINDS = (NodeKind.PORT, NodeKind.SIGNAL)
_ASSERTABLE_KINDS = (NodeKind.PROPERTY, NodeKind.SEQUENCE)

#: Port directions → derived instance dataflow ("buffer" is a VHDL output).
_PORT_DIRECTION_FLOW: dict[str, tuple[EdgeKind, ...]] = {
    "input": (EdgeKind.READS,),
    "in": (EdgeKind.READS,),
    "output": (EdgeKind.DRIVES,),
    "out": (EdgeKind.DRIVES,),
    "buffer": (EdgeKind.DRIVES,),
    "inout": (EdgeKind.READS, EdgeKind.DRIVES),
}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
#: Sized/based literals and char/bit-string literals, stripped before lexing
#: identifiers out of an actual expression (``2'b00`` is not a read of b00).
_LITERAL_RE = re.compile(r"'s?[bBoOdDhH][0-9a-fA-FxXzZ?_]+|'.'?|\"[^\"]*\"")
_EXPR_KEYWORDS = frozenset(
    {
        # SV operators/keywords plausible inside a connection expression
        "posedge",
        "negedge",
        "signed",
        "unsigned",
        "inside",
        "with",
        # VHDL expression keywords (actuals are lowercased VHDL text)
        "open",
        "others",
        "null",
        "true",
        "false",
        "when",
        "else",
        "and",
        "or",
        "not",
        "xor",
        "nand",
        "nor",
        "xnor",
        "abs",
        "mod",
        "rem",
        "sll",
        "srl",
        "sla",
        "sra",
        "rol",
        "ror",
        "downto",
        "to",
    }
)


def _lex_identifiers(expr_text: str) -> list[str]:
    """Identifier-shaped tokens of an actual expression, in order, deduped."""
    seen: set[str] = set()
    out: list[str] = []
    for token in _IDENT_RE.findall(_LITERAL_RE.sub(" ", expr_text)):
        if token.lower() in _EXPR_KEYWORDS or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _add_node(g: nx.MultiDiGraph, node: Node) -> None:
    g.add_node(
        node.id,
        kind=node.kind,
        name=node.name,
        qualified_name=node.qualified_name,
        file=node.file,
        line_span=node.line_span,
        language=node.language,
        attrs=node.attrs,
    )


def _add_edge(g: nx.MultiDiGraph, edge: Edge) -> None:
    g.add_edge(edge.src, edge.dst, kind=edge.kind, confidence=edge.confidence, attrs=edge.attrs)


class _Linker:
    def __init__(self, file_irs: list[FileIR]) -> None:
        self.graph = nx.MultiDiGraph()
        # (kind, name) -> definition node ids, across all files
        self.definitions: defaultdict[tuple[NodeKind, str], list[str]] = defaultdict(list)
        # (kind, lowercased name) -> ids, for the cross-language fallback only
        self.definitions_ci: defaultdict[tuple[NodeKind, str], list[str]] = defaultdict(list)
        # parent id -> child nodes, for PORT/PARAMETER lookup under a target
        self.children: defaultdict[str, list[Node]] = defaultdict(list)
        self.parent: dict[str, str] = {}  # child id -> declaring scope id
        self.node_file: dict[str, str] = {}
        self.node_obj: dict[str, Node] = {}
        # (entity, architecture | None, component) -> configuration bindings,
        # recorded while resolving BINDS refs (which run first).
        self.bindings: defaultdict[tuple[str, str | None, str], list[dict[str, Any]]] = defaultdict(
            list
        )
        # The same header spliced into several compilation units duplicates
        # its refs across IRs; emitted-edge identity keeps one of each.
        self._emitted: set[tuple[str, str, EdgeKind, float, tuple[tuple[str, str], ...]]] = set()
        # unit id -> name -> child nodes, built lazily for scoped resolution
        self._scope_indices: dict[str, dict[str, list[Node]]] = {}

        seen_local: set[tuple[str, str, EdgeKind]] = set()
        for ir in file_irs:
            for node in ir.nodes:
                if node.id in self.node_obj:
                    # The same header spliced into several compilation units
                    # (or a FILE node emitted by both the parser and the
                    # preprocessor/filelist adapters): first occurrence wins.
                    continue
                _add_node(self.graph, node)
                self.node_obj[node.id] = node
                self.node_file[node.id] = ir.path
                self.definitions[(node.kind, node.name)].append(node.id)
                self.definitions_ci[(node.kind, node.name.lower())].append(node.id)
            for edge in ir.local_edges:
                key = (edge.src, edge.dst, edge.kind)
                if key in seen_local:
                    continue
                seen_local.add(key)
                _add_edge(self.graph, edge)
                if edge.kind is EdgeKind.DECLARES:
                    self.children[edge.src].append(self.node_obj[edge.dst])
                    self.parent[edge.dst] = edge.src

    def link(self, file_irs: list[FileIR]) -> None:
        # BINDS first: configuration bindings must be on record before the
        # component instantiations they override resolve.
        deferred: list[UnresolvedRef] = []
        for ir in file_irs:
            for ref in ir.unresolved_refs:
                if ref.edge_kind is EdgeKind.BINDS:
                    self._resolve(ref)
                else:
                    deferred.append(ref)
        for ref in deferred:
            self._resolve(ref)

    # -- language helpers ------------------------------------------------------

    def _src_language(self, ref: UnresolvedRef) -> Language:
        src = self.node_obj.get(ref.src_id)
        return src.language if src is not None else Language.UNKNOWN

    def _referrer_library(self, src_id: str) -> str:
        """The VHDL library the referring node's file compiles into."""
        relpath = self.node_file.get(src_id, "")
        file_node = self.node_obj.get(file_node_id(relpath))
        if file_node is not None:
            return str(file_node.attrs.get("library", _VHDL_DEFAULT_LIBRARY))
        return _VHDL_DEFAULT_LIBRARY

    def _filter_library(self, candidates: list[str], library: str | None, src_id: str) -> list[str]:
        """Narrow ambiguous VHDL candidates to the named library, if that helps."""
        if library is None or len(candidates) <= 1:
            return candidates
        if library == _VHDL_DEFAULT_LIBRARY:
            library = self._referrer_library(src_id)
        filtered = [
            c for c in candidates if self.node_obj[c].attrs.get("library", library) == library
        ]
        return filtered or candidates

    # -- target resolution ---------------------------------------------------

    def _resolve_target(self, ref: UnresolvedRef) -> tuple[list[str], float, dict[str, Any]]:
        """Return (target ids, confidence, extra edge attrs); stubs if unresolved."""
        src = self.node_obj.get(ref.src_id)
        src_lang = src.language if src is not None else Language.UNKNOWN
        if (
            src is not None
            and src.kind is NodeKind.INSTANCE
            and src_lang is Language.VHDL
            and ref.edge_kind in (EdgeKind.INSTANTIATES, EdgeKind.CONNECTS, EdgeKind.PARAMETERIZES)
        ):
            return self._resolve_vhdl_instance(ref, src)

        same_kinds, cross_kinds = _REF_TARGET_KINDS[ref.edge_kind]
        if src_lang is Language.VHDL and cross_kinds:
            same_kinds, cross_kinds = _VHDL_INSTANTIABLE, _SV_INSTANTIABLE

        candidates: list[str] = []
        for kind in same_kinds:
            candidates.extend(self.definitions.get((kind, ref.target_name), ()))
        if ref.edge_kind is EdgeKind.EXTENDS and ref.attrs.get("package"):
            # Prefer a class declared inside the named package.
            prefix = f"{ref.attrs['package']}."
            scoped = [c for c in candidates if self.node_obj[c].qualified_name.startswith(prefix)]
            if scoped:
                candidates = scoped
        if ref.edge_kind in (EdgeKind.USES_PACKAGE, EdgeKind.BINDS, EdgeKind.IMPLEMENTS):
            candidates = self._filter_library(candidates, ref.attrs.get("library"), ref.src_id)
        if candidates:
            return (*self._score(candidates, ref.src_id), {})
        cross = self._cross_language(cross_kinds, ref.target_name)
        if cross is not None:
            return (*cross, {})
        return [self._stub(ref)], CONFIDENCE_RESOLVED, {}

    def _score(self, candidates: list[str], src_id: str) -> tuple[list[str], float]:
        """The M1 same-language confidence rules."""
        ref_file = self.node_file.get(src_id, "")
        if len(candidates) == 1:
            same_file = self.node_obj[candidates[0]].file == ref_file
            return candidates, CONFIDENCE_RESOLVED if same_file else CONFIDENCE_UNIQUE_MATCH
        local = [c for c in candidates if self.node_obj[c].file == ref_file]
        if len(local) == 1:
            return local, CONFIDENCE_RESOLVED
        return candidates, CONFIDENCE_AMBIGUOUS

    def _cross_language(
        self, kinds: tuple[NodeKind, ...], name: str
    ) -> tuple[list[str], float] | None:
        """Case-insensitive cross-language name match, capped at 0.8."""
        candidates: list[str] = []
        for kind in kinds:
            candidates.extend(self.definitions_ci.get((kind, name.lower()), ()))
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates, CONFIDENCE_UNIQUE_MATCH
        return candidates, CONFIDENCE_AMBIGUOUS

    # -- VHDL instantiation styles ---------------------------------------------

    def _resolve_vhdl_instance(
        self, ref: UnresolvedRef, inst: Node
    ) -> tuple[list[str], float, dict[str, Any]]:
        style = inst.attrs.get("style", "component")
        library = inst.attrs.get("library")
        if style == "configuration":
            return self._resolve_via_configuration(ref, library)
        if style == "component":
            binding = self._binding_for(inst)
            if binding is not None:
                targets, confidence = self._resolve_entity_name(
                    str(binding["target"]), ref, binding.get("library")
                )
                return targets, confidence, {"bound_by": binding["config"]}
        # Direct entity instantiation, or a component's default binding:
        # a like-named entity, then the cross-language fallback.
        targets, confidence = self._resolve_entity_name(ref.target_name, ref, library)
        return targets, confidence, {}

    def _resolve_entity_name(
        self, name: str, ref: UnresolvedRef, library: str | None
    ) -> tuple[list[str], float]:
        candidates = list(self.definitions.get((NodeKind.ENTITY, name), ()))
        candidates = self._filter_library(candidates, library, ref.src_id)
        if candidates:
            return self._score(candidates, ref.src_id)
        cross = self._cross_language(_SV_INSTANTIABLE, name)
        if cross is not None:
            return cross
        return [self._ensure_stub(NodeKind.ENTITY, name, name)], CONFIDENCE_RESOLVED

    def _resolve_via_configuration(
        self, ref: UnresolvedRef, library: str | None
    ) -> tuple[list[str], float, dict[str, Any]]:
        """``u : configuration work.cfg`` resolves to cfg's configured entity."""
        configs = list(self.definitions.get((NodeKind.CONFIGURATION, ref.target_name), ()))
        configs = self._filter_library(configs, library, ref.src_id)
        if len(configs) == 1:
            cfg = self.node_obj[configs[0]]
            entity_name = cfg.attrs.get("of_entity")
            if entity_name:
                targets, confidence = self._resolve_entity_name(
                    str(entity_name), ref, cfg.attrs.get("library")
                )
                return targets, confidence, {"via_configuration": cfg.id}
            return configs, CONFIDENCE_RESOLVED, {}
        if configs:  # ambiguous configuration name: edges to each, 0.6
            return configs, CONFIDENCE_AMBIGUOUS, {}
        stub = self._ensure_stub(NodeKind.CONFIGURATION, ref.target_name, ref.target_name)
        return [stub], CONFIDENCE_RESOLVED, {}

    def _binding_for(self, inst: Node) -> dict[str, Any] | None:
        """The configuration binding governing *inst*, if any.

        Looks up the instance's enclosing architecture (and its entity), then
        matches component bindings: a specific label beats ``all`` beats
        ``others``.
        """
        arch = self.node_obj.get(self.parent.get(inst.id, ""))
        if arch is None or arch.kind is not NodeKind.ARCHITECTURE:
            return None
        entity = str(arch.attrs.get("of_entity", ""))
        component = str(inst.attrs.get("target", ""))
        if not entity or not component:
            return None
        rows: list[dict[str, Any]] = []
        for block in (arch.name, None):
            rows.extend(self.bindings.get((entity, block, component), ()))
        by_rank: dict[int, dict[str, Any]] = {}
        for row in rows:
            instances = row["instances"]
            if isinstance(instances, list) and inst.name in instances:
                rank = 0
            elif instances == "all":
                rank = 1
            elif instances == "others":
                rank = 2
            else:
                continue
            by_rank.setdefault(rank, row)
        for rank in (0, 1, 2):
            if rank in by_rank:
                return by_rank[rank]
        return None

    def _record_binding(self, ref: UnresolvedRef) -> None:
        entity = ref.attrs.get("of_entity")
        component = ref.attrs.get("component")
        if not entity or not component:
            return
        block = ref.attrs.get("block")
        key = (str(entity), str(block) if block is not None else None, str(component))
        self.bindings[key].append(
            {
                "instances": ref.attrs.get("instances", "all"),
                "target": ref.target_name,
                "library": ref.attrs.get("library"),
                "architecture": ref.attrs.get("architecture"),
                "config": ref.src_id,
            }
        )

    def _stub(self, ref: UnresolvedRef) -> str:
        kind = _STUB_KIND[ref.edge_kind]
        qualified = ref.target_name
        if ref.edge_kind is EdgeKind.USES_PACKAGE:
            # Qualify by library so e.g. two libraries' like-named packages
            # never merge; ieee/std packages stay stubs by design.
            library = ref.attrs.get("library") or self._referrer_library(ref.src_id)
            qualified = f"{library}.{ref.target_name}"
        return self._ensure_stub(kind, qualified, ref.target_name)

    def _stub_child(self, parent_id: str, kind: NodeKind, name: str) -> str:
        parent_name = self.node_obj[parent_id].name
        stub_id = self._ensure_stub(kind, f"{parent_name}.{name}", name)
        if not self.graph.has_edge(parent_id, stub_id):
            _add_edge(self.graph, Edge(src=parent_id, dst=stub_id, kind=EdgeKind.DECLARES))
            self.children[parent_id].append(self.node_obj[stub_id])
        return stub_id

    def _ensure_stub(self, kind: NodeKind, qualified: str, name: str) -> str:
        stub_id = stub_node_id(kind, qualified)
        if stub_id not in self.graph:
            stub = Node(
                id=stub_id,
                kind=kind,
                name=name,
                qualified_name=qualified,
                attrs={"unresolved": True},
            )
            _add_node(self.graph, stub)
            self.node_obj[stub_id] = stub
        return stub_id

    # -- scoped (dataflow) resolution --------------------------------------------

    def _enclosing_unit_id(self, node_id: str) -> str | None:
        """Climb DECLARES parents to the design unit that scopes *node_id*."""
        seen: set[str] = set()
        current: str | None = node_id
        while current is not None and current not in seen:
            seen.add(current)
            node = self.node_obj.get(current)
            if node is not None and node.kind in _SCOPED_UNIT_KINDS:
                return current
            current = self.parent.get(current)
        return None

    def _scope_index_for(self, unit_id: str) -> dict[str, list[Node]]:
        """name -> declared children visible to scoped refs inside *unit_id*
        (an ARCHITECTURE sees its entity's ports/generics; ENUM members are
        flattened into the unit's namespace, as in both languages)."""
        index = self._scope_indices.get(unit_id)
        if index is not None:
            return index
        index = defaultdict(list)
        scopes = [unit_id]
        unit = self.node_obj[unit_id]
        if unit.kind is NodeKind.ARCHITECTURE:
            entity_name = unit.attrs.get("of_entity")
            if entity_name:
                candidates = self._filter_library(
                    list(self.definitions.get((NodeKind.ENTITY, str(entity_name)), ())),
                    unit.attrs.get("library"),
                    unit_id,
                )
                scopes.extend(candidates)
        for scope in scopes:
            for child in self.children.get(scope, []):
                index[child.name].append(child)
                if child.kind is NodeKind.ENUM:
                    for member in self.children.get(child.id, []):
                        index[member.name].append(member)
        self._scope_indices[unit_id] = dict(index)
        return self._scope_indices[unit_id]

    def _scoped_signal_target(self, unit_id: str, name: str) -> tuple[str | None, float]:
        """(target id, confidence) for *name* in *unit_id*'s namespace.

        (None, 0) means the name is a constant or some other non-signal
        declaration — not dataflow. An undeclared name gets a SIGNAL stub
        child (implicit nets / typos) at ≤ 0.6.
        """
        matches = self._scope_index_for(unit_id).get(name, [])
        signals = [n for n in matches if n.kind in _SIGNAL_KINDS]
        if signals:
            return signals[0].id, CONFIDENCE_RESOLVED
        if matches:
            return None, 0.0
        return self._stub_child(unit_id, NodeKind.SIGNAL, name), CONFIDENCE_AMBIGUOUS

    def _resolve_scoped(self, ref: UnresolvedRef) -> None:
        unit_id = self._enclosing_unit_id(ref.src_id)
        if unit_id is None:
            return  # e.g. a covergroup in a class: no design-unit namespace
        if ref.edge_kind is EdgeKind.ASSERTS_ON:
            matches = self._scope_index_for(unit_id).get(ref.target_name, [])
            assertables = [n for n in matches if n.kind in _ASSERTABLE_KINDS]
            if assertables:
                self._emit(ref, assertables[0].id, CONFIDENCE_RESOLVED)
                return
        target, confidence = self._scoped_signal_target(unit_id, ref.target_name)
        if target is not None:
            self._emit(ref, target, confidence)

    def _derive_port_dataflow(
        self, ref: UnresolvedRef, port: Node, confidence: float, actual_text: str
    ) -> None:
        """Instance-level DRIVES/READS from a resolved CONNECTS binding."""
        direction = str(port.attrs.get("direction", ""))
        flows = _PORT_DIRECTION_FLOW.get(direction)
        if flows is None or not actual_text or actual_text == ".*":
            return
        identifiers = _lex_identifiers(actual_text)
        if not identifiers:
            return  # a constant actual: no dataflow
        unit_id = self._enclosing_unit_id(ref.src_id)
        if unit_id is None:
            return
        base = confidence if len(identifiers) == 1 else min(confidence, CONFIDENCE_AMBIGUOUS)
        if len(flows) > 1:  # inout: direction is a guess either way
            base = min(base, CONFIDENCE_AMBIGUOUS)
        vhdl = self._src_language(ref) is Language.VHDL
        for name in identifiers:
            target, cap = self._scoped_signal_target(unit_id, name.lower() if vhdl else name)
            if target is None:
                continue
            for kind in flows:
                self._emit_edge(
                    ref.src_id,
                    target,
                    kind,
                    min(base, cap, ref.confidence),
                    {
                        "via_port": port.name,
                        "derived": "connects",
                        "expr_text": actual_text,
                        "line_span": ref.line_span,
                    },
                )

    def _emit_edge(
        self, src: str, dst: str, kind: EdgeKind, confidence: float, attrs: dict[str, Any]
    ) -> None:
        key = (src, dst, kind, confidence, tuple(sorted((k, str(v)) for k, v in attrs.items())))
        if key in self._emitted:
            return
        self._emitted.add(key)
        _add_edge(self.graph, Edge(src=src, dst=dst, kind=kind, confidence=confidence, attrs=attrs))

    # -- per-kind resolution ---------------------------------------------------

    def _resolve(self, ref: UnresolvedRef) -> None:
        if ref.edge_kind in _SCOPED_REF_KINDS:
            self._resolve_scoped(ref)
            return
        if ref.edge_kind is EdgeKind.BINDS and ref.attrs.get("role") == "binding":
            self._record_binding(ref)
        targets, confidence, extra = self._resolve_target(ref)
        if ref.edge_kind in (EdgeKind.CONNECTS, EdgeKind.PARAMETERIZES):
            child_kind = NodeKind.PORT if ref.edge_kind is EdgeKind.CONNECTS else NodeKind.PARAMETER
            for target in targets:
                self._resolve_binding(ref, target, confidence, child_kind, extra)
        else:
            for target in targets:
                self._emit(ref, target, confidence, extra=extra)

    def _resolve_binding(
        self,
        ref: UnresolvedRef,
        target: str,
        confidence: float,
        child_kind: NodeKind,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Resolve one CONNECTS/PARAMETERIZES ref against one target's children."""
        target_node = self.node_obj[target]
        unresolved_target = target_node.attrs.get("unresolved", False)
        kids = [n for n in self.children.get(target, []) if n.kind is child_kind]
        if child_kind is NodeKind.PARAMETER:
            kids = [n for n in kids if not n.attrs.get("is_localparam")]
        kids.sort(key=lambda n: n.attrs.get("index", 0))

        if ref.attrs.get("wildcard"):
            # .* — connect every port of a resolved target; for a stub the
            # port list is unknown, so emit one marker edge to the target.
            if kids and not unresolved_target:
                for kid in kids:
                    self._emit(ref, kid.id, confidence, port_name=kid.name, extra=extra)
                    if ref.edge_kind is EdgeKind.CONNECTS:
                        # .* binds the like-named signal of the parent scope.
                        self._derive_port_dataflow(ref, kid, confidence, kid.name)
            else:
                self._emit(ref, target, confidence, extra=extra)
            return

        name = ref.attrs.get("port_name") or ref.attrs.get("param_name")
        position = ref.attrs.get("position")
        dst: str | None = None
        if name is not None:
            dst = next((k.id for k in kids if k.name == name), None)
            if dst is None and Language.VHDL in (
                self._src_language(ref),
                target_node.language,
            ):
                # VHDL names are case-insensitive: a VHDL formal must match an
                # SV port (and vice versa) regardless of casing.
                lowered = str(name).lower()
                dst = next((k.id for k in kids if k.name.lower() == lowered), None)
            if dst is None and unresolved_target:
                dst = self._stub_child(target, child_kind, str(name))
        elif position is not None and isinstance(position, int) and position < len(kids):
            dst = kids[position].id
        if dst is None:
            # Named binding matching no declared child of a resolved target,
            # or positional overflow: point at the target itself so the
            # mismatch stays visible in the graph.
            self._emit(ref, target, min(confidence, CONFIDENCE_AMBIGUOUS), extra=extra)
        else:
            self._emit(ref, dst, confidence, extra=extra)
            port = self.node_obj.get(dst)
            if (
                ref.edge_kind is EdgeKind.CONNECTS
                and port is not None
                and (port.kind is NodeKind.PORT)
            ):
                self._derive_port_dataflow(
                    ref, port, confidence, str(ref.attrs.get("expr_text") or "")
                )

    def _emit(
        self,
        ref: UnresolvedRef,
        dst: str,
        confidence: float,
        port_name: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        attrs: dict[str, object] = {k: v for k, v in ref.attrs.items() if v is not None}
        if extra:
            attrs.update(extra)
        if port_name is not None:
            attrs["port_name"] = port_name
        attrs["line_span"] = ref.line_span
        # A reference from a non-selected both-branches region caps the edge
        # at the site's own confidence.
        effective = min(confidence, ref.confidence)
        key = (
            ref.src_id,
            dst,
            ref.edge_kind,
            effective,
            tuple(sorted((k, str(v)) for k, v in attrs.items())),
        )
        if key in self._emitted:
            return
        self._emitted.add(key)
        _add_edge(
            self.graph,
            Edge(src=ref.src_id, dst=dst, kind=ref.edge_kind, confidence=effective, attrs=attrs),
        )


def build_graph(file_irs: list[FileIR]) -> nx.MultiDiGraph:
    """Link per-file IRs into the global knowledge graph (pass 2)."""
    linker = _Linker(file_irs)
    linker.link(file_irs)
    for edge in derive_test_covers(linker.graph):
        _add_edge(linker.graph, edge)
    return linker.graph
