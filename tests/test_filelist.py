"""Tests for the .f/.vc filelist parser."""

from pathlib import Path

from hdl_kgraph.ids import file_node_id, filelist_node_id
from hdl_kgraph.parser.filelist import (
    Filelist,
    filelist_irs,
    flattened_defines,
    flattened_files,
    flattened_incdirs,
    parse_filelist,
)
from hdl_kgraph.schema import EdgeKind, NodeKind


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_basic_tokens(tmp_path: Path) -> None:
    fl = parse_filelist(
        write(
            tmp_path / "tb.f",
            """
            // a comment
            # another comment
            rtl/a.sv            // trailing comment
            +incdir+include+inc2
            +define+USE_FIFO+WIDTH=8
            rtl/b.sv
            -y cells
            -v prims.v
            """,
        ),
        env={},
    )
    assert fl.files == [tmp_path / "rtl/a.sv", tmp_path / "rtl/b.sv"]
    assert fl.incdirs == [tmp_path / "include", tmp_path / "inc2"]
    assert fl.defines == {"USE_FIFO": None, "WIDTH": "8"}
    assert fl.library_dirs == [tmp_path / "cells"]
    assert fl.library_files == [tmp_path / "prims.v"]
    assert fl.warnings == []


def test_nested_filelist_preserves_order(tmp_path: Path) -> None:
    write(tmp_path / "sub/common.f", "c.sv\n+incdir+sub_inc\n+define+FROM_CHILD\n")
    fl = parse_filelist(write(tmp_path / "tb.f", "a.sv\n-f sub/common.f\nb.sv\n"), env={})
    # The nested list's files merge at the -f position.
    assert flattened_files(fl) == [
        tmp_path / "a.sv",
        tmp_path / "sub/c.sv",  # relative to the nested filelist's directory
        tmp_path / "b.sv",
    ]
    assert tmp_path / "sub/sub_inc" in flattened_incdirs(fl)
    assert flattened_defines(fl) == {"FROM_CHILD": None}


def test_cycle_detection_warns(tmp_path: Path) -> None:
    write(tmp_path / "a.f", "x.sv\n-f b.f\n")
    write(tmp_path / "b.f", "-f a.f\ny.sv\n")
    fl = parse_filelist(tmp_path / "a.f", env={})
    assert flattened_files(fl) == [tmp_path / "x.sv", tmp_path / "y.sv"]
    warnings = [w for n in [fl, *fl.nested] for w in n.warnings]
    assert any("cycle" in w for w in warnings)


def test_self_cycle(tmp_path: Path) -> None:
    fl = parse_filelist(write(tmp_path / "a.f", "-f a.f\nx.sv\n"), env={})
    assert fl.files == [tmp_path / "x.sv"]
    assert any("cycle" in w for w in fl.warnings)


def test_env_expansion(tmp_path: Path) -> None:
    fl = parse_filelist(
        write(tmp_path / "tb.f", "+incdir+$IP_ROOT/include\n${IP_ROOT}/top.sv\n$UNSET/x.sv\n"),
        env={"IP_ROOT": str(tmp_path / "ip")},
    )
    assert fl.incdirs == [tmp_path / "ip/include"]
    assert fl.files[0] == tmp_path / "ip/top.sv"
    assert any("UNSET" in w for w in fl.warnings)
    # Unset vars leave the token as-is.
    assert fl.files[1].name == "x.sv"


def test_unknown_options_tolerated(tmp_path: Path) -> None:
    fl = parse_filelist(write(tmp_path / "tb.f", "-sv\n+libext+.v\na.sv\n"), env={})
    assert fl.files == [tmp_path / "a.sv"]
    assert len(fl.warnings) == 2
    assert all("skipped" in w for w in fl.warnings)


def test_flag_at_eof(tmp_path: Path) -> None:
    fl = parse_filelist(write(tmp_path / "tb.f", "a.sv\n-f"), env={})
    assert fl.files == [tmp_path / "a.sv"]
    assert any("end of file" in w for w in fl.warnings)


def test_duplicate_files_first_wins(tmp_path: Path) -> None:
    fl = parse_filelist(write(tmp_path / "tb.f", "a.sv\nb.sv\na.sv\n"), env={})
    assert flattened_files(fl) == [tmp_path / "a.sv", tmp_path / "b.sv"]


def test_missing_filelist_warns(tmp_path: Path) -> None:
    fl = parse_filelist(tmp_path / "nope.f", env={})
    assert fl.files == []
    assert any("cannot read" in w for w in fl.warnings)


def test_filelist_irs(tmp_path: Path) -> None:
    write(tmp_path / "common.f", "c.sv\n")
    fl = parse_filelist(write(tmp_path / "tb.f", "a.sv\n-f common.f\nb.sv\n-v prims.v\n"), env={})
    irs = filelist_irs(fl, tmp_path)
    assert [ir.path for ir in irs] == ["tb.f", "common.f"]

    top = irs[0]
    filelist_nodes = [n for n in top.nodes if n.kind is NodeKind.FILELIST]
    assert [n.id for n in filelist_nodes] == [filelist_node_id("tb.f")]
    # Minimal FILE nodes keep the graph connected even for unparsed files.
    file_ids = {n.id for n in top.nodes if n.kind is NodeKind.FILE}
    assert file_ids == {file_node_id("a.sv"), file_node_id("b.sv"), file_node_id("prims.v")}

    refs = [e for e in top.local_edges if e.kind is EdgeKind.REFERENCES_FILE]
    by_dst = {e.dst: e.attrs for e in refs}
    assert by_dst[file_node_id("a.sv")] == {"order": 0, "role": "compile"}
    assert by_dst[file_node_id("b.sv")] == {"order": 2, "role": "compile"}
    assert by_dst[file_node_id("prims.v")] == {"role": "library"}

    includes = [e for e in top.local_edges if e.kind is EdgeKind.INCLUDES]
    assert [(e.dst, e.attrs["order"]) for e in includes] == [(filelist_node_id("common.f"), 1)]


def test_filelist_irs_deduplicates_nested(tmp_path: Path) -> None:
    write(tmp_path / "common.f", "c.sv\n")
    fl = parse_filelist(write(tmp_path / "tb.f", "-f common.f\n-f common.f\n"), env={})
    irs = filelist_irs(fl, tmp_path)
    assert [ir.path for ir in irs] == ["tb.f", "common.f"]


def test_nested_property() -> None:
    child = Filelist(path=Path("/x/common.f"))
    parent = Filelist(path=Path("/x/tb.f"), entries=[Path("/x/a.sv"), child])
    assert parent.files == [Path("/x/a.sv")]
    assert parent.nested == [child]
