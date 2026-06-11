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

VHDL library/work scoping, bind directives, and cross-language linking land
in M3.
"""

from __future__ import annotations

from collections import defaultdict

import networkx as nx

from hdl_kgraph.ids import stub_node_id
from hdl_kgraph.parser.base import FileIR, UnresolvedRef
from hdl_kgraph.schema import (
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_RESOLVED,
    CONFIDENCE_UNIQUE_MATCH,
    Edge,
    EdgeKind,
    Node,
    NodeKind,
)

# Node kinds an INSTANTIATES target name may resolve to.
_INSTANTIABLE = (NodeKind.MODULE, NodeKind.INTERFACE, NodeKind.PROGRAM, NodeKind.PRIMITIVE)

_REF_TARGET_KINDS: dict[EdgeKind, tuple[NodeKind, ...]] = {
    EdgeKind.INSTANTIATES: _INSTANTIABLE,
    EdgeKind.CONNECTS: _INSTANTIABLE,
    EdgeKind.PARAMETERIZES: _INSTANTIABLE,
    EdgeKind.IMPORTS: (NodeKind.PACKAGE,),
    EdgeKind.EXTENDS: (NodeKind.CLASS,),
}

_STUB_KIND: dict[EdgeKind, NodeKind] = {
    EdgeKind.INSTANTIATES: NodeKind.MODULE,
    EdgeKind.CONNECTS: NodeKind.MODULE,
    EdgeKind.PARAMETERIZES: NodeKind.MODULE,
    EdgeKind.IMPORTS: NodeKind.PACKAGE,
    EdgeKind.EXTENDS: NodeKind.CLASS,
}


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
        # parent id -> child nodes, for PORT/PARAMETER lookup under a target
        self.children: defaultdict[str, list[Node]] = defaultdict(list)
        self.node_file: dict[str, str] = {}
        self.node_obj: dict[str, Node] = {}

        for ir in file_irs:
            for node in ir.nodes:
                _add_node(self.graph, node)
                self.node_obj[node.id] = node
                self.node_file[node.id] = ir.path
                self.definitions[(node.kind, node.name)].append(node.id)
            for edge in ir.local_edges:
                _add_edge(self.graph, edge)
                if edge.kind is EdgeKind.DECLARES:
                    self.children[edge.src].append(self.node_obj[edge.dst])

    def link(self, file_irs: list[FileIR]) -> None:
        for ir in file_irs:
            for ref in ir.unresolved_refs:
                self._resolve(ref)

    # -- target resolution ---------------------------------------------------

    def _resolve_target(self, ref: UnresolvedRef) -> tuple[list[str], float]:
        """Return (target node ids, confidence); creates a stub if unresolved."""
        candidates: list[str] = []
        for kind in _REF_TARGET_KINDS[ref.edge_kind]:
            candidates.extend(self.definitions.get((kind, ref.target_name), ()))
        if ref.edge_kind is EdgeKind.EXTENDS and ref.attrs.get("package"):
            # Prefer a class declared inside the named package.
            prefix = f"{ref.attrs['package']}."
            scoped = [c for c in candidates if self.node_obj[c].qualified_name.startswith(prefix)]
            if scoped:
                candidates = scoped
        if not candidates:
            return [self._stub(ref)], CONFIDENCE_RESOLVED
        ref_file = self.node_file.get(ref.src_id, "")
        if len(candidates) == 1:
            same_file = self.node_obj[candidates[0]].file == ref_file
            return candidates, CONFIDENCE_RESOLVED if same_file else CONFIDENCE_UNIQUE_MATCH
        local = [c for c in candidates if self.node_obj[c].file == ref_file]
        if len(local) == 1:
            return local, CONFIDENCE_RESOLVED
        return candidates, CONFIDENCE_AMBIGUOUS

    def _stub(self, ref: UnresolvedRef) -> str:
        kind = _STUB_KIND[ref.edge_kind]
        return self._ensure_stub(kind, ref.target_name, ref.target_name)

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

    # -- per-kind resolution ---------------------------------------------------

    def _resolve(self, ref: UnresolvedRef) -> None:
        targets, confidence = self._resolve_target(ref)
        if ref.edge_kind in (EdgeKind.INSTANTIATES, EdgeKind.IMPORTS, EdgeKind.EXTENDS):
            for target in targets:
                self._emit(ref, target, confidence)
        else:  # CONNECTS / PARAMETERIZES
            child_kind = NodeKind.PORT if ref.edge_kind is EdgeKind.CONNECTS else NodeKind.PARAMETER
            for target in targets:
                self._resolve_binding(ref, target, confidence, child_kind)

    def _resolve_binding(
        self, ref: UnresolvedRef, target: str, confidence: float, child_kind: NodeKind
    ) -> None:
        """Resolve one CONNECTS/PARAMETERIZES ref against one target's children."""
        unresolved_target = self.node_obj[target].attrs.get("unresolved", False)
        kids = [n for n in self.children.get(target, []) if n.kind is child_kind]
        if child_kind is NodeKind.PARAMETER:
            kids = [n for n in kids if not n.attrs.get("is_localparam")]
        kids.sort(key=lambda n: n.attrs.get("index", 0))

        if ref.attrs.get("wildcard"):
            # .* — connect every port of a resolved target; for a stub the
            # port list is unknown, so emit one marker edge to the target.
            if kids and not unresolved_target:
                for kid in kids:
                    self._emit(ref, kid.id, confidence, port_name=kid.name)
            else:
                self._emit(ref, target, confidence)
            return

        name = ref.attrs.get("port_name") or ref.attrs.get("param_name")
        position = ref.attrs.get("position")
        dst: str | None = None
        if name is not None:
            dst = next((k.id for k in kids if k.name == name), None)
            if dst is None and unresolved_target:
                dst = self._stub_child(target, child_kind, str(name))
        elif position is not None and isinstance(position, int) and position < len(kids):
            dst = kids[position].id
        if dst is None:
            # Named binding matching no declared child of a resolved target,
            # or positional overflow: point at the target itself so the
            # mismatch stays visible in the graph.
            self._emit(ref, target, min(confidence, CONFIDENCE_AMBIGUOUS))
        else:
            self._emit(ref, dst, confidence)

    def _emit(
        self, ref: UnresolvedRef, dst: str, confidence: float, port_name: str | None = None
    ) -> None:
        attrs: dict[str, object] = {k: v for k, v in ref.attrs.items() if v is not None}
        if port_name is not None:
            attrs["port_name"] = port_name
        attrs["line_span"] = ref.line_span
        _add_edge(
            self.graph,
            Edge(src=ref.src_id, dst=dst, kind=ref.edge_kind, confidence=confidence, attrs=attrs),
        )


def build_graph(file_irs: list[FileIR]) -> nx.MultiDiGraph:
    """Link per-file IRs into the global knowledge graph (pass 2)."""
    linker = _Linker(file_irs)
    linker.link(file_irs)
    return linker.graph
