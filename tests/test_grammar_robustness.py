"""Fail-loud grammar validation + complete parse-error counting (#71)."""

from pathlib import Path

import pytest

from hdl_kgraph.parser import systemverilog, vhdl
from hdl_kgraph.parser.base import GrammarMismatchError, validate_grammar
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.vhdl import VhdlParser


class _FakeLanguage:
    """Stands in for a tree-sitter ``Language``: known names resolve, others
    return ``None`` (the real API's "unknown name" signal)."""

    def __init__(self, types: set[str], fields: set[str] = frozenset()) -> None:
        self._types = types
        self._fields = fields

    def id_for_node_kind(self, name: str, named: bool) -> int | None:
        return 1 if name in self._types else None

    def field_id_for_name(self, name: str) -> int | None:
        return 1 if name in self._fields else None


def test_validate_grammar_passes_when_all_present() -> None:
    validate_grammar(_FakeLanguage({"a", "b"}, {"name"}), {"a", "b"}, field_names={"name"})


def test_validate_grammar_reports_missing_node_type() -> None:
    with pytest.raises(GrammarMismatchError, match=r"node types.*'b'"):
        validate_grammar(_FakeLanguage({"a"}), {"a", "b"})


def test_validate_grammar_reports_missing_field() -> None:
    with pytest.raises(GrammarMismatchError, match=r"fields.*'value'"):
        validate_grammar(_FakeLanguage({"a"}), {"a"}, field_names={"value"})


def test_real_grammars_validate() -> None:
    # The shipped grammars must satisfy the parsers' dispatch surface.
    SystemVerilogParser()
    VhdlParser()


def test_sv_parser_detects_grammar_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """A renamed/removed dispatch node-type makes construction fail loudly."""
    monkeypatch.setattr(systemverilog, "_grammar_validated", False)
    dispatch = dict(systemverilog._Walker._DISPATCH)
    dispatch["module_declaration_RENAMED_UPSTREAM"] = dispatch["module_declaration"]
    monkeypatch.setattr(systemverilog._Walker, "_DISPATCH", dispatch)
    with pytest.raises(GrammarMismatchError, match="RENAMED_UPSTREAM"):
        SystemVerilogParser()


def test_vhdl_parser_detects_grammar_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vhdl, "_grammar_validated", False)
    dispatch = dict(vhdl._Walker._DISPATCH)
    dispatch["entity_declaration_RENAMED_UPSTREAM"] = dispatch["entity_declaration"]
    monkeypatch.setattr(vhdl._Walker, "_DISPATCH", dispatch)
    with pytest.raises(GrammarMismatchError, match="RENAMED_UPSTREAM"):
        VhdlParser()


@pytest.mark.parametrize(
    "src",
    [
        "module m;\n  parameter int P = 8'b;\nendmodule\n",  # parameter assignment
        "module m;\n  typedef enum { A, , B } e_t;\nendmodule\n",  # typedef body
        "module m;\n  foo u(.a(), .);\nendmodule\n",  # instantiation
        "module m;\n  import ;\nendmodule\n",  # package import
    ],
)
def test_subtree_consuming_handlers_count_errors(src: str) -> None:
    """Handlers that consume a subtree without re-dispatching (params, typedefs,
    instances, imports) now keep ``parse_error_count`` honest."""
    ir = SystemVerilogParser().parse(Path("t.sv"), src)
    assert ir.parse_error_count >= 1, src
