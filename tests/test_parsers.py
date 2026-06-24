"""Parser backends route unambiguously by suffix and fail loudly until implemented."""

from itertools import combinations
from pathlib import Path

import pytest

from hdl_kgraph.parser.c import CParser, CppParser
from hdl_kgraph.parser.perl import PerlParser
from hdl_kgraph.parser.python import PythonParser
from hdl_kgraph.parser.sln import SlnParser
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.tcl import SdcParser, TclScriptParser, UpfParser
from hdl_kgraph.parser.vhdl import VhdlParser

ALL_BACKENDS = [
    SystemVerilogParser,
    VhdlParser,
    CParser,
    CppParser,
    PythonParser,
    SdcParser,
    UpfParser,
    TclScriptParser,
    PerlParser,
    SlnParser,
]

# Backends not yet implemented, with the milestone their NotImplementedError names.
# SdcParser (M10 first wedge, #25) and UpfParser (M10 second wedge) are implemented,
# so they are no longer here.
STUB_BACKENDS_AND_MILESTONES = [
    (TclScriptParser, "M10"),
    (PerlParser, "M10"),
    (SlnParser, "M10"),
]


def test_suffix_sets_disjoint() -> None:
    """No two backends claim the same file suffix (routing must be unambiguous)."""
    for a, b in combinations(ALL_BACKENDS, 2):
        assert not (a.suffixes & b.suffixes), f"{a.__name__} and {b.__name__} overlap"


@pytest.mark.parametrize(("backend", "milestone"), STUB_BACKENDS_AND_MILESTONES)
def test_stubs_fail_loudly(backend: type, milestone: str) -> None:
    """Unimplemented backends raise UnsupportedBackendError naming their milestone.

    The error subclasses ``NotImplementedError`` (so legacy call sites keep
    working) but is a distinct, catchable type a future router can handle.
    """
    from hdl_kgraph.parser.base import UnsupportedBackendError

    with pytest.raises(NotImplementedError, match=milestone):
        backend().parse(Path("x"), "")
    with pytest.raises(UnsupportedBackendError, match=milestone):
        backend().parse(Path("x"), "")


def test_unimplemented_suffixes_stay_out_of_discovery_routing() -> None:
    """The stub backends' suffixes must not be discoverable until implemented.

    The crash they would otherwise raise is only unreachable because discovery
    never routes these suffixes to a parser (issue #77). Lock that in: a build
    only ever dispatches suffixes in ``discovery.SUFFIXES`` to SV/VHDL.
    """
    from hdl_kgraph import discovery

    for backend, _ in STUB_BACKENDS_AND_MILESTONES:
        assert not (backend.suffixes & discovery.SUFFIXES), backend.__name__


def test_c_family_suffixes_route_through_discovery() -> None:
    """The M8 C/C++ backends are implemented, so their suffixes are discoverable."""
    from hdl_kgraph import discovery

    assert CParser.suffixes <= discovery.SUFFIXES
    assert CppParser.suffixes <= discovery.SUFFIXES
    assert PythonParser.suffixes <= discovery.SUFFIXES


def test_sdc_suffixes_route_through_discovery() -> None:
    """The M10 SDC/XDC and UPF backends are implemented, so their suffixes are discoverable."""
    from hdl_kgraph import discovery

    assert SdcParser.suffixes <= discovery.SUFFIXES
    assert UpfParser.suffixes <= discovery.SUFFIXES


def test_filelist_routes_unsupported_suffix_to_skip(tmp_path: Path) -> None:
    """A constraints/script file with no parser is skipped, never parsed.

    ``.tcl`` flow scripts are still a fail-loud stub (UPF landed; flow scripts did not).
    """
    from hdl_kgraph.discovery import check_file

    script = tmp_path / "flow.tcl"
    script.write_text("read_verilog top.v\n")
    found = check_file(script, tmp_path)
    assert found.skipped_reason == "unsupported"
