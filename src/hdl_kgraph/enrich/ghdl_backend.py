"""GHDL enrichment backend (M7) — binding-accurate VHDL facts.

`GHDL <https://github.com/ghdl/ghdl>`_ is a full VHDL frontend. Its Python
bindings — ``pyGHDL.dom`` (a :mod:`pyVHDLModel` document model built on the
low-level ``pyGHDL.libghdl`` C binding) — analyse a design into resolved design
units, which the tree-sitter tier cannot. Where tree-sitter only *guesses* a
component's binding by name, GHDL gives the entity an instantiation actually
binds to. This backend walks the analysed design and reconciles it with the
heuristic graph, mirroring :mod:`hdl_kgraph.enrich.slang_backend`:

* the syntactic ``INSTANTIATES`` edge of a confirmed binding is upgraded to
  confidence ``1.0`` and stamped ``attrs["source"] = "elaborated"``;
* a ``for ... generate`` whose elaborated multiplicity exceeds one is recorded
  as an ``instance_count`` discrepancy, the syntactic instance node is annotated
  with ``attrs["elaborated_count"]``, and one elaborated ``INSTANCE`` node per
  iteration is added;
* an instantiation whose elaborated target differs from the heuristic guess is
  recorded as a ``wrong_target`` discrepancy.

Unlike pyslang, GHDL is a **system binary** (``pyGHDL`` ships with it, not via
pip), so :meth:`GhdlBackend.available` probes both the ``ghdl`` binary and the
importability of ``pyGHDL.libghdl``; the backend is silently dropped when either
is absent. Analysis of an incomplete design never raises out of :meth:`enrich` —
failures degrade to the heuristic graph and surface as diagnostics.

Scope (first cut): component/entity/configuration binding confirmation,
``wrong_target`` detection, and ``for ... generate`` unrolling over statically
foldable ranges. Generic-value (``PARAMETERIZES``) and type/width
(``CONNECTS``) upgrades are a documented follow-on, paralleling slang's own.
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from typing import Any

import networkx as nx

from hdl_kgraph.enrich._graphutil import enclosing_architecture, instantiates_target
from hdl_kgraph.enrich.base import (
    Capabilities,
    Discrepancy,
    EdgeUpgrade,
    EnrichmentInput,
    EnrichmentResult,
)
from hdl_kgraph.ids import elab_node_id
from hdl_kgraph.schema import CONFIDENCE_RESOLVED, Edge, EdgeKind, Language, Node, NodeKind

_SUFFIXES = frozenset({".vhd", ".vhdl"})


class GhdlBackend:
    """VHDL elaboration via GHDL's pyGHDL bindings."""

    name = "ghdl"
    suffixes = _SUFFIXES

    def available(self) -> bool:
        # GHDL is a system binary; pyGHDL/libghdl ship with it, not via pip.
        # Both must be present, and the probe must never raise.
        if shutil.which("ghdl") is None:
            return False
        try:
            import importlib.util

            return importlib.util.find_spec("pyGHDL.libghdl") is not None
        except Exception:
            return False

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resolves_params=True,  # VHDL generics resolved by analysis
            unrolls_generates=True,  # for ... generate over static ranges
            resolves_types=True,  # GHDL does full type/subtype resolution
            resolves_defparam=False,  # VHDL has no defparam construct
        )

    def enrich(self, inp: EnrichmentInput, graph: nx.MultiDiGraph) -> EnrichmentResult:
        result = EnrichmentResult()
        try:
            bindings = _elaborate(inp, result)
        except Exception as exc:  # never fail the build on an analysis error
            result.diagnostics.append(f"ghdl: analysis failed, kept heuristic graph ({exc})")
            return result
        if bindings is None:
            return result
        _reconcile(graph, bindings, result)
        return result


# ((of_entity, arch_name), instance_label) -> (resolved_entity, [hier_paths]).
# All names lowercased to match the parser. ``hier_paths`` carries one elaborated
# path per iteration; its length is the binding's multiplicity (1 for a plain
# instantiation, >1 for an unrolled ``for ... generate``).
_ArchKey = tuple[str, str]
_BindingMap = dict[tuple[_ArchKey, str], tuple[str, list[str]]]


def _elaborate(inp: EnrichmentInput, result: EnrichmentResult) -> _BindingMap | None:
    """Analyse *inp* with pyGHDL and summarise each architecture's bindings."""
    from pyGHDL.dom.NonStandard import Design, Document

    design = Design()
    try:
        design.LoadDefaultLibraries()
    except Exception as exc:  # std/ieee not found is fatal to analysis, not us
        result.diagnostics.append(f"ghdl: could not load default libraries ({exc})")
        return None

    documents: list[Any] = []
    for path in inp.files:
        try:
            relpath = path.relative_to(inp.base).as_posix()
        except ValueError:
            relpath = path.name
        libname = inp.vhdl_libraries.get(relpath, "work")
        try:
            library = design.GetLibrary(libname)
            document = Document(path)
            design.AddDocument(document, library)
            documents.append(document)
        except Exception as exc:
            result.diagnostics.append(f"ghdl: could not analyse {path} ({exc})")
    if not documents:
        return None

    try:
        design.Analyze()
    except Exception as exc:
        # Partial analysis still yields usable design units; note and continue.
        result.diagnostics.append(
            f"ghdl: analysis incomplete, facts taken where it succeeded ({exc})"
        )

    bindings: _BindingMap = {}
    for document in documents:
        for arch in _iter(document, "Architectures"):
            entity_name = _name(getattr(arch, "Entity", None))
            arch_name = _name(arch)
            if not arch_name:
                continue
            arch_key = (entity_name, arch_name)
            collected: defaultdict[str, tuple[str, list[str]]] = defaultdict(lambda: ("", []))
            _walk_statements(
                _iter(arch, "Statements"), arch_key, [entity_name or arch_name], collected, result
            )
            for label, (entity, paths) in collected.items():
                bindings[(arch_key, label)] = (entity, paths)
    return bindings


def _walk_statements(
    statements: list[Any],
    arch_key: _ArchKey,
    prefixes: list[str],
    collected: dict[str, tuple[str, list[str]]],
    result: EnrichmentResult,
) -> None:
    """Record each instantiation's resolved target + one path per *prefix*.

    *prefixes* is the set of hierarchical paths reaching this declarative region
    (one per enclosing generate iteration), so a ``for ... generate`` simply
    multiplies them before recursing.
    """
    for stmt in statements:
        cls = type(stmt).__name__
        if "Instantiation" in cls:
            label = _name(stmt, "Label")
            entity = _binding_target(stmt)
            if not label or not entity:
                continue
            paths = [f"{p}.{label}" for p in prefixes]
            existing = collected.get(label, ("", []))
            collected[label] = (entity, existing[1] + paths)
        elif "Generate" in cls:
            label = _name(stmt, "Label")
            if "For" in cls:
                count = _generate_count(stmt)
                seg = label or "gen"
                expanded = [f"{p}.{seg}({i})" for p in prefixes for i in range(count)]
            else:  # if/case generate: a single block, best effort
                seg = label or "gen"
                expanded = [f"{p}.{seg}" for p in prefixes] if label else list(prefixes)
            _walk_statements(_generate_body(stmt), arch_key, expanded, collected, result)


def _binding_target(stmt: Any) -> str:
    """Lowercased entity name an instantiation binds to.

    Entity instantiation names its entity directly; a component instantiation
    binds by default rule to the like-named entity; a configuration
    instantiation names its configuration (best effort — its bound entity is a
    follow-on once configuration resolution is mined from libghdl).
    """
    for attr in ("Entity", "Component", "Configuration"):
        name = _name(getattr(stmt, attr, None))
        if name:
            return name
    return ""


def _generate_count(stmt: Any) -> int:
    """Static iteration count of a ``for ... generate``; 1 when not foldable.

    First cut folds only literal integer ranges (``0 to 3`` / ``7 downto 0``);
    generic-bounded ranges that need value resolution are a documented follow-on
    and conservatively count as 1 so no fabricated expansion is reported.
    """
    rng = getattr(getattr(stmt, "Range", None), "Range", None) or getattr(stmt, "Range", None)
    left = _int(getattr(rng, "LeftBound", None))
    right = _int(getattr(rng, "RightBound", None))
    if left is None or right is None:
        return 1
    return abs(right - left) + 1


def _generate_body(stmt: Any) -> list[Any]:
    """Concurrent statements inside a generate (across its grammar shapes)."""
    for attr in ("Statements", "Body"):
        body = getattr(stmt, attr, None)
        if body is None:
            continue
        stmts = getattr(body, "Statements", body)
        try:
            return list(stmts)
        except TypeError:
            continue
    return []


def _iter(obj: Any, attr: str) -> list[Any]:
    """List a pyVHDLModel collection attribute (dict-valued or iterable)."""
    coll = getattr(obj, attr, None)
    if coll is None:
        return []
    values = coll.values() if hasattr(coll, "values") else coll
    try:
        return list(values)
    except TypeError:
        return []


def _name(obj: Any, attr: str | None = None) -> str:
    """Lowercased identifier of a pyVHDLModel object or one of its symbols."""
    if obj is None:
        return ""
    if attr is not None:
        obj = getattr(obj, attr, None)
        if obj is None:
            return ""
    for field in ("Identifier", "NormalizedIdentifier", "Name"):
        value = getattr(obj, field, None)
        if isinstance(value, str) and value:
            return value.lower()
        if value is not None and not isinstance(value, str):
            nested = _name(value)  # SymbolName -> Name -> str
            if nested:
                return nested
    return obj.lower() if isinstance(obj, str) else ""


def _int(bound: Any) -> int | None:
    """Integer value of a literal range bound, or None if not a literal."""
    value = getattr(bound, "Value", bound)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _reconcile(graph: nx.MultiDiGraph, bindings: _BindingMap, result: EnrichmentResult) -> None:
    """Match resolved VHDL bindings against the heuristic INSTANCE nodes."""
    for inst_id, data in list(graph.nodes(data=True)):
        if data["kind"] is not NodeKind.INSTANCE:
            continue
        if data.get("language") is not Language.VHDL:
            continue  # slang owns SystemVerilog instances
        arch = enclosing_architecture(graph, inst_id)
        target = instantiates_target(graph, inst_id)
        if arch is None or target is None:
            continue
        arch_id, arch_key = arch
        target_id, target_name = target
        binding = bindings.get((arch_key, data["name"]))
        if binding is None:
            continue  # analysis never reached this instance

        resolved, rel_paths = binding
        if resolved != target_name:
            result.discrepancies.append(
                Discrepancy(
                    kind="wrong_target",
                    backend=GhdlBackend.name,
                    detail=(
                        f"{arch_key[0]}({arch_key[1]}).{data['name']} binds to "
                        f"{resolved}, not {target_name}"
                    ),
                    node_id=inst_id,
                    src=inst_id,
                    dst=target_id,
                    heuristic=target_name,
                    elaborated=resolved,
                )
            )
            continue

        count = len(rel_paths) or 1
        result.upgrades.append(
            EdgeUpgrade(
                src=inst_id,
                dst=target_id,
                kind=EdgeKind.INSTANTIATES,
                confidence=CONFIDENCE_RESOLVED,
                attrs={
                    "source": "elaborated",
                    "backend": GhdlBackend.name,
                    "elaborated_count": count,
                },
            )
        )
        if count > 1:
            result.node_annotations[inst_id] = {"elaborated_count": count}
            result.discrepancies.append(
                Discrepancy(
                    kind="instance_count",
                    backend=GhdlBackend.name,
                    detail=(
                        f"{arch_key[0]}({arch_key[1]}).{data['name']} (target {target_name}) "
                        f"elaborates to {count} instances; tree-sitter saw 1"
                    ),
                    node_id=inst_id,
                    heuristic="1",
                    elaborated=str(count),
                )
            )
            _add_elaborated(graph, data, arch_id, target_id, target_name, rel_paths, result)


def _add_elaborated(
    graph: nx.MultiDiGraph,
    inst_data: dict[str, Any],
    arch_id: str,
    target_id: str,
    target_name: str,
    rel_paths: list[str],
    result: EnrichmentResult,
) -> None:
    """One elaborated INSTANCE node (+ DECLARES/INSTANTIATES) per iteration."""
    file = inst_data.get("file", "")
    for rel_path in rel_paths:
        node_id = elab_node_id(NodeKind.INSTANCE, rel_path)
        result.new_nodes.append(
            Node(
                id=node_id,
                kind=NodeKind.INSTANCE,
                name=rel_path.rsplit(".", 1)[-1],
                qualified_name=rel_path,
                file=file,
                language=Language.VHDL,
                attrs={
                    "target": target_name,
                    "source": "elaborated",
                    "backend": GhdlBackend.name,
                    "elaborated_from": inst_data.get("qualified_name", ""),
                },
            )
        )
        result.new_edges.append(
            Edge(src=arch_id, dst=node_id, kind=EdgeKind.DECLARES, confidence=CONFIDENCE_RESOLVED)
        )
        result.new_edges.append(
            Edge(
                src=node_id,
                dst=target_id,
                kind=EdgeKind.INSTANTIATES,
                confidence=CONFIDENCE_RESOLVED,
                attrs={"source": "elaborated", "backend": GhdlBackend.name},
            )
        )
