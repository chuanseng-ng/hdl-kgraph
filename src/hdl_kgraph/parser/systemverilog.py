"""SystemVerilog / Verilog parser backend (M1).

Implementation notes:

* One grammar serves both ``.v`` and ``.sv``: ``tree-sitter-systemverilog``
  (gmlarumbe), chosen by the bake-off recorded in docs/grammar-bakeoff.md.
* The tree is walked manually with a ``node.type`` dispatch table (the Query
  API churned across py-tree-sitter releases; see ROADMAP Risk #5). Node-type
  names follow the IEEE 1800 BNF and were confirmed with
  ``scripts/grammar_bakeoff.py --dump-tree``.
* M1 extracts MODULE, INTERFACE, PACKAGE, PROGRAM, FUNCTION/TASK, PORT,
  PARAMETER, INSTANCE, TYPEDEF/STRUCT/ENUM, and CLASS (declaration + EXTENDS),
  with DECLARES edges locally and INSTANTIATES / CONNECTS / PARAMETERIZES /
  IMPORTS / EXTENDS recorded as :class:`UnresolvedRef` for the pass-2 linker.
* Files containing tree-sitter ERROR nodes still yield partial results; the
  error count is reported in ``FileIR.parse_error_count``.
* M2: ``parse`` accepts the preprocessor's line map. Spans and node ids then
  attribute to the *original* file and line — declarations spliced from a
  ``\\`include`` belong to the header, and nodes from non-selected
  both-branches regions carry ``attrs["conditional"]`` with their DECLARES
  edge and refs at ``CONFIDENCE_AMBIGUOUS``. A node straddling an include
  boundary keeps the start line's file with a collapsed span (documented
  limitation).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_systemverilog
from tree_sitter import Language as TSLanguage
from tree_sitter import Node as TSNode
from tree_sitter import Parser as TSParser

from hdl_kgraph.ids import decl_node_id, file_node_id
from hdl_kgraph.parser.base import FileIR, UnresolvedRef
from hdl_kgraph.parser.preprocessor import LineOrigin
from hdl_kgraph.schema import (
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_RESOLVED,
    Edge,
    EdgeKind,
    Language,
    Node,
    NodeKind,
)

SUFFIXES = frozenset({".v", ".vh", ".sv", ".svh"})
SYSTEMVERILOG_SUFFIXES = frozenset({".sv", ".svh"})

SV_LANGUAGE = TSLanguage(tree_sitter_systemverilog.language())

_HEADER_TYPES = frozenset(
    {
        "module_ansi_header",
        "module_nonansi_header",
        "interface_ansi_header",
        "interface_nonansi_header",
        "program_ansi_header",
        "program_nonansi_header",
    }
)

_IDENTIFIER_TYPES = ("simple_identifier", "escaped_identifier")


def _line_span(node: TSNode) -> tuple[int, int]:
    return (node.start_point[0] + 1, node.end_point[0] + 1)


@dataclass
class _Scope:
    """One level of the declaration-scope stack."""

    node_id: str
    path: str  # dotted qualified-name prefix ("" for file scope)
    port_index: int = 0
    param_index: int = 0
    last_port_direction: str = ""
    # name -> PORT node, for non-ANSI direction back-fill
    ports: dict[str, Node] = field(default_factory=dict)

    def child_path(self, name: str) -> str:
        return f"{self.path}.{name}" if self.path else name


class _Walker:
    def __init__(
        self,
        ir: FileIR,
        relpath: str,
        language: Language,
        source: bytes,
        line_map: Sequence[LineOrigin] | None = None,
    ) -> None:
        self.ir = ir
        self.relpath = relpath
        self.language = language
        self.source = source
        self.line_map = line_map
        self.scopes: list[_Scope] = []
        self._used_ids: set[str] = set()

    # -- small helpers -------------------------------------------------------

    def _origin(self, node: TSNode) -> LineOrigin:
        return self._origin_at(node.start_point[0])

    def _origin_at(self, row: int) -> LineOrigin:
        if not self.line_map:
            return LineOrigin(file=self.relpath, line=row + 1)
        return self.line_map[min(row, len(self.line_map) - 1)]

    def _span(self, node: TSNode) -> tuple[int, int]:
        if self.line_map is None:
            return _line_span(node)
        start = self._origin(node)
        end = self._origin_at(node.end_point[0])
        if end.file != start.file:  # straddles an include boundary
            return (start.line, start.line)
        return (start.line, max(start.line, end.line))

    def _ref_confidence(self, node: TSNode) -> float:
        return CONFIDENCE_AMBIGUOUS if self._origin(node).ambiguous else CONFIDENCE_RESOLVED

    def _text(self, node: TSNode) -> str:
        return self.source[node.start_byte : node.end_byte].decode(errors="replace")

    @staticmethod
    def _child(node: TSNode, *types: str) -> TSNode | None:
        for child in node.children:
            if child.type in types:
                return child
        return None

    @staticmethod
    def _children(node: TSNode, *types: str) -> list[TSNode]:
        return [c for c in node.children if c.type in types]

    def _find_first(self, node: TSNode, type_: str, max_depth: int = 3) -> TSNode | None:
        if max_depth < 0:
            return None
        for child in node.children:
            if child.type == type_:
                return child
            found = self._find_first(child, type_, max_depth - 1)
            if found is not None:
                return found
        return None

    def _identifier(self, node: TSNode) -> str:
        ident = self._child(node, *_IDENTIFIER_TYPES)
        return self._text(ident) if ident is not None else ""

    @property
    def scope(self) -> _Scope:
        return self.scopes[-1]

    def _new_node(self, kind: NodeKind, name: str, ts_node: TSNode, **attrs: object) -> Node:
        """Create a node in the current scope and emit its DECLARES edge."""
        origin = self._origin(ts_node)
        qualified = self.scope.child_path(name)
        node_id = decl_node_id(origin.file, kind, qualified)
        if node_id in self._used_ids:
            node_id = f"{node_id}@{origin.line}"
            if node_id in self._used_ids:  # e.g. the same header spliced twice
                node_id = f"{node_id}.{ts_node.start_point[0] + 1}"
        self._used_ids.add(node_id)
        if origin.ambiguous:
            attrs["conditional"] = True
        node = Node(
            id=node_id,
            kind=kind,
            name=name,
            qualified_name=qualified,
            file=origin.file,
            line_span=self._span(ts_node),
            language=self.language,
            attrs={k: v for k, v in attrs.items() if v is not None},
        )
        self.ir.nodes.append(node)
        # A file-scope declaration spliced from a header belongs to the
        # header's FILE node, not the including unit's.
        src = self.scope.node_id
        if len(self.scopes) == 1 and origin.file != self.relpath:
            src = file_node_id(origin.file)
        self.ir.local_edges.append(
            Edge(
                src=src,
                dst=node.id,
                kind=EdgeKind.DECLARES,
                confidence=(
                    CONFIDENCE_AMBIGUOUS if origin.ambiguous else CONFIDENCE_RESOLVED
                ),
            )
        )
        return node

    # -- traversal -----------------------------------------------------------

    def visit(self, node: TSNode) -> None:
        if node.is_missing:
            self.ir.parse_error_count += 1
        if node.type == "ERROR":
            # Skip the subtree but keep going with siblings: partial results.
            self.ir.parse_error_count += 1
            return
        handler = self._DISPATCH.get(node.type)
        if handler is not None:
            handler(self, node)
        else:
            for child in node.children:
                self.visit(child)

    def _visit_children(self, node: TSNode) -> None:
        for child in node.children:
            self.visit(child)

    def _visit_in_scope(self, node: TSNode, scope_node: Node) -> None:
        self.scopes.append(_Scope(node_id=scope_node.id, path=scope_node.qualified_name))
        try:
            self._visit_children(node)
        finally:
            self.scopes.pop()

    # -- design units ----------------------------------------------------------

    def _on_design_unit(self, node: TSNode) -> None:
        kind = {
            "module_declaration": NodeKind.MODULE,
            "interface_declaration": NodeKind.INTERFACE,
            "program_declaration": NodeKind.PROGRAM,
        }[node.type]
        header = self._child(node, *_HEADER_TYPES) or node
        name = self._identifier(header)
        if not name:
            self._visit_children(node)
            return
        attrs: dict[str, object] = {}
        keyword = self._child(header, "module_keyword")
        if keyword is not None and self._text(keyword) == "macromodule":
            attrs["is_macromodule"] = True
        unit = self._new_node(kind, name, node, **attrs)
        self._visit_in_scope(node, unit)

    def _on_package(self, node: TSNode) -> None:
        name = self._identifier(node)
        if not name:
            self._visit_children(node)
            return
        pkg = self._new_node(NodeKind.PACKAGE, name, node)
        self._visit_in_scope(node, pkg)

    def _on_class(self, node: TSNode) -> None:
        name = self._identifier(node)
        if not name:
            self._visit_children(node)
            return
        is_virtual = self._child(node, "virtual") is not None
        cls = self._new_node(NodeKind.CLASS, name, node, is_virtual=is_virtual or None)
        if self._child(node, "extends") is not None:
            base = self._child(node, "class_type")
            if base is not None:
                self._record_extends(cls, base)
        self._visit_in_scope(node, cls)

    def _record_extends(self, cls: Node, base: TSNode) -> None:
        text = self._text(base)
        param_args = None
        if "#" in text:
            text, _, args = text.partition("#")
            param_args = "#" + args.strip()
        package = None
        if "::" in text:
            package, _, text = text.rpartition("::")
        self.ir.unresolved_refs.append(
            UnresolvedRef(
                edge_kind=EdgeKind.EXTENDS,
                src_id=cls.id,
                target_name=text.strip(),
                line_span=self._span(base),
                attrs={"package": package, "param_args_text": param_args},
                confidence=self._ref_confidence(base),
            )
        )

    # -- ports and parameters ----------------------------------------------------

    def _on_ansi_port(self, node: TSNode) -> None:
        name = ""
        name_node: TSNode | None = None
        for child in reversed(node.children):
            if child.type in _IDENTIFIER_TYPES:
                name_node = child
                name = self._text(child)
                break
        if not name or name_node is None:
            return
        direction_node = self._find_first(node, "port_direction")
        direction = self._text(direction_node) if direction_node is not None else ""
        if direction:
            self.scope.last_port_direction = direction
        else:
            # `input logic a, b` continues the direction of the previous port.
            direction = self.scope.last_port_direction
        type_node = self._child(
            node, "net_port_header", "variable_port_header", "interface_port_header"
        )
        port = self._new_node(
            NodeKind.PORT,
            name,
            node,
            direction=direction or None,
            type_text=self._text(type_node) if type_node is not None else None,
            index=self.scope.port_index,
        )
        self.scope.port_index += 1
        self.scope.ports[name] = port

    def _on_nonansi_port(self, node: TSNode) -> None:
        # `port` inside a non-ANSI header's list_of_ports: name + index now;
        # direction is back-filled by the body's port_declaration.
        name = self._identifier(node) or self._text(node).strip()
        if not name or name in self.scope.ports:
            return
        port = self._new_node(NodeKind.PORT, name, node, index=self.scope.port_index)
        self.scope.port_index += 1
        self.scope.ports[name] = port

    def _on_port_declaration(self, node: TSNode) -> None:
        decl = self._child(node, "input_declaration", "output_declaration", "inout_declaration")
        if decl is None:
            return
        direction = decl.type.split("_", 1)[0]  # input | output | inout
        names = self._child(decl, "list_of_port_identifiers", "list_of_variable_identifiers")
        if names is None:
            return
        for ident in self._children(names, *_IDENTIFIER_TYPES):
            name = self._text(ident)
            port = self.scope.ports.get(name)
            if port is not None:
                port.attrs["direction"] = direction
            else:
                port = self._new_node(
                    NodeKind.PORT, name, node, direction=direction, index=self.scope.port_index
                )
                self.scope.port_index += 1
                self.scope.ports[name] = port

    def _on_parameter_declaration(self, node: TSNode) -> None:
        is_localparam = node.type == "local_parameter_declaration"
        assignments = self._child(node, "list_of_param_assignments", "list_of_type_assignments")
        if assignments is None:
            return
        for assignment in self._children(assignments, "param_assignment", "type_assignment"):
            name = self._identifier(assignment)
            if not name:
                continue
            default = self._child(assignment, "constant_param_expression")
            index = None
            if not is_localparam:
                index = self.scope.param_index
                self.scope.param_index += 1
            self._new_node(
                NodeKind.PARAMETER,
                name,
                assignment,
                is_localparam=is_localparam,
                default=self._text(default) if default is not None else None,
                index=index,
            )

    # -- typedefs ------------------------------------------------------------------

    def _on_type_declaration(self, node: TSNode) -> None:
        name = self._identifier(node)
        if not name:
            return
        data_type = self._child(node, "data_type")
        kind = NodeKind.TYPEDEF
        if data_type is not None:
            if self._child(data_type, "enum") is not None:
                kind = NodeKind.ENUM
            elif self._child(data_type, "struct_union") is not None:
                kind = NodeKind.STRUCT
        type_node = self._new_node(kind, name, node)
        if kind is NodeKind.ENUM and data_type is not None:
            self.scopes.append(_Scope(node_id=type_node.id, path=type_node.qualified_name))
            try:
                for member in self._children(data_type, "enum_name_declaration"):
                    member_name = self._identifier(member)
                    if member_name:
                        self._new_node(NodeKind.ENUM_MEMBER, member_name, member)
            finally:
                self.scopes.pop()

    # -- functions and tasks ----------------------------------------------------------

    def _on_function(self, node: TSNode) -> None:
        body = self._child(node, "function_body_declaration")
        name = self._identifier(body) if body is not None else ""
        if not name:
            return
        fn = self._new_node(NodeKind.FUNCTION, name, node)
        self._visit_in_scope(node, fn)

    def _on_task(self, node: TSNode) -> None:
        body = self._child(node, "task_body_declaration")
        name = self._identifier(body) if body is not None else ""
        if not name:
            return
        task = self._new_node(NodeKind.TASK, name, node)
        self._visit_in_scope(node, task)

    def _on_constructor(self, node: TSNode) -> None:
        self._new_node(NodeKind.FUNCTION, "new", node, is_constructor=True)

    # -- instantiations ------------------------------------------------------------------

    def _on_instantiation(self, node: TSNode) -> None:
        target = self._identifier(node)
        if not target:
            return
        param_overrides = self._collect_param_overrides(node)
        for hier in self._children(node, "hierarchical_instance"):
            name_node = self._child(hier, "name_of_instance")
            inst_name = self._identifier(name_node) if name_node is not None else ""
            if not inst_name:
                continue
            inst = self._new_node(NodeKind.INSTANCE, inst_name, hier, target=target)
            self.ir.unresolved_refs.append(
                UnresolvedRef(
                    edge_kind=EdgeKind.INSTANTIATES,
                    src_id=inst.id,
                    target_name=target,
                    line_span=self._span(hier),
                    confidence=self._ref_confidence(hier),
                )
            )
            for override in param_overrides:
                self.ir.unresolved_refs.append(
                    UnresolvedRef(
                        edge_kind=EdgeKind.PARAMETERIZES,
                        src_id=inst.id,
                        target_name=target,
                        line_span=self._span(hier),
                        attrs=dict(override),
                        confidence=self._ref_confidence(hier),
                    )
                )
            self._collect_connections(hier, inst, target)

    def _collect_param_overrides(self, node: TSNode) -> list[dict[str, object]]:
        pva = self._child(node, "parameter_value_assignment")
        assignments = (
            self._child(pva, "list_of_parameter_value_assignments") if pva is not None else None
        )
        if assignments is None:
            return []
        overrides: list[dict[str, object]] = []
        position = 0
        for child in assignments.children:
            if child.type == "named_parameter_assignment":
                expr = self._child(child, "param_expression")
                overrides.append(
                    {
                        "param_name": self._identifier(child),
                        "position": None,
                        "value_text": self._text(expr) if expr is not None else "",
                    }
                )
            elif child.type == "ordered_parameter_assignment":
                overrides.append(
                    {"param_name": None, "position": position, "value_text": self._text(child)}
                )
                position += 1
        return overrides

    def _collect_connections(self, hier: TSNode, inst: Node, target: str) -> None:
        connections = self._child(hier, "list_of_port_connections")
        if connections is None:
            return
        position = 0
        for child in connections.children:
            attrs: dict[str, object] | None = None
            if child.type == "named_port_connection":
                if self._child(child, ".*") is not None:
                    attrs = {
                        "port_name": None,
                        "position": None,
                        "wildcard": True,
                        "expr_text": ".*",
                    }
                else:
                    expr = self._child(child, "expression")
                    port_name = self._identifier(child)
                    attrs = {
                        "port_name": port_name,
                        "position": None,
                        "wildcard": False,
                        # `.name` shorthand connects the like-named signal.
                        "expr_text": self._text(expr) if expr is not None else port_name,
                    }
            elif child.type == "ordered_port_connection":
                attrs = {
                    "port_name": None,
                    "position": position,
                    "wildcard": False,
                    "expr_text": self._text(child),
                }
                position += 1
            if attrs is not None:
                self.ir.unresolved_refs.append(
                    UnresolvedRef(
                        edge_kind=EdgeKind.CONNECTS,
                        src_id=inst.id,
                        target_name=target,
                        line_span=self._span(child),
                        attrs=attrs,
                        confidence=self._ref_confidence(child),
                    )
                )

    # -- imports -------------------------------------------------------------------------

    def _on_import(self, node: TSNode) -> None:
        for item in self._children(node, "package_import_item"):
            idents = self._children(item, *_IDENTIFIER_TYPES)
            if not idents:
                continue
            package = self._text(idents[0])
            symbol = self._text(idents[1]) if len(idents) > 1 else "*"
            self.ir.unresolved_refs.append(
                UnresolvedRef(
                    edge_kind=EdgeKind.IMPORTS,
                    src_id=self.scope.node_id,
                    target_name=package,
                    line_span=self._span(item),
                    attrs={"symbol": symbol},
                    confidence=self._ref_confidence(item),
                )
            )

    _DISPATCH = {
        "module_declaration": _on_design_unit,
        "interface_declaration": _on_design_unit,
        "program_declaration": _on_design_unit,
        "package_declaration": _on_package,
        "class_declaration": _on_class,
        "ansi_port_declaration": _on_ansi_port,
        "port": _on_nonansi_port,
        "port_declaration": _on_port_declaration,
        "parameter_declaration": _on_parameter_declaration,
        "local_parameter_declaration": _on_parameter_declaration,
        "type_declaration": _on_type_declaration,
        "function_declaration": _on_function,
        "task_declaration": _on_task,
        "class_constructor_declaration": _on_constructor,
        "module_instantiation": _on_instantiation,
        "package_import_declaration": _on_import,
    }


class SystemVerilogParser:
    """Tree-sitter based SystemVerilog/Verilog pass-1 parser."""

    suffixes = SUFFIXES

    def __init__(self) -> None:
        self._parser = TSParser(SV_LANGUAGE)

    def parse(
        self, path: Path, text: str, line_map: Sequence[LineOrigin] | None = None
    ) -> FileIR:
        """Parse one file into its per-file IR.

        *path* should be relative to the build root; it becomes the node-id
        prefix and ``Node.file`` for everything in the file. When *text* is
        preprocessor output, pass its *line_map* so spans and file
        attribution track the original sources (including spliced headers).
        """
        relpath = path.as_posix()
        language = (
            Language.SYSTEMVERILOG if path.suffix in SYSTEMVERILOG_SUFFIXES else Language.VERILOG
        )
        ir = FileIR(path=relpath)
        file_node = Node(
            id=file_node_id(relpath),
            kind=NodeKind.FILE,
            name=path.name,
            qualified_name=relpath,
            file=relpath,
            language=language,
        )
        ir.nodes.append(file_node)
        source = text.encode()
        try:
            tree = self._parser.parse(source)
            walker = _Walker(ir, relpath, language, source, line_map)
            walker.scopes.append(_Scope(node_id=file_node.id, path=""))
            walker.visit(tree.root_node)
        except Exception:  # defensive: a walker bug must not abort the build
            ir.parse_error_count += 1
        return ir
