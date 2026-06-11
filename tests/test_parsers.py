"""Parser backends route unambiguously by suffix and fail loudly until implemented."""

from itertools import combinations
from pathlib import Path

import pytest

from hdl_kgraph.parser.perl import PerlParser
from hdl_kgraph.parser.sln import SlnParser
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.tcl import SdcParser, TclScriptParser, UpfParser
from hdl_kgraph.parser.vhdl import VhdlParser

BACKENDS_AND_MILESTONES = [
    (SystemVerilogParser, "M1"),
    (VhdlParser, "M3"),
    (SdcParser, "M10"),
    (UpfParser, "M10"),
    (TclScriptParser, "M10"),
    (PerlParser, "M10"),
    (SlnParser, "M10"),
]


def test_suffix_sets_disjoint() -> None:
    """No two backends claim the same file suffix (routing must be unambiguous)."""
    backends = [backend for backend, _ in BACKENDS_AND_MILESTONES]
    for a, b in combinations(backends, 2):
        assert not (a.suffixes & b.suffixes), f"{a.__name__} and {b.__name__} overlap"


@pytest.mark.parametrize(("backend", "milestone"), BACKENDS_AND_MILESTONES)
def test_stubs_fail_loudly(backend: type, milestone: str) -> None:
    """Unimplemented backends raise NotImplementedError naming their milestone."""
    with pytest.raises(NotImplementedError, match=milestone):
        backend().parse(Path("x"), "")
