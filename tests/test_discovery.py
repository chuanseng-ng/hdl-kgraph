"""File discovery and guard tests (exclude globs, size cap, pragma protect)."""

from pathlib import Path

from hdl_kgraph.discovery import discover
from hdl_kgraph.schema import Language


def make(path: Path, content: str = "module m; endmodule\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def by_rel(found: list) -> dict[str, object]:
    return {f.relpath: f for f in found}


def test_discovers_only_hdl_suffixes(tmp_path: Path) -> None:
    make(tmp_path / "a.sv")
    make(tmp_path / "b.v")
    make(tmp_path / "c.svh")
    make(tmp_path / "d.vh")
    make(tmp_path / "ignored.txt")
    make(tmp_path / "alu.vhd")  # VHDL lands in M3
    found = by_rel(discover(tmp_path))
    assert set(found) == {"a.sv", "b.v", "c.svh", "d.vh"}
    assert found["a.sv"].language is Language.SYSTEMVERILOG
    assert found["b.v"].language is Language.VERILOG


def test_exclude_glob_matches_relative_path(tmp_path: Path) -> None:
    make(tmp_path / "rtl" / "keep.sv")
    make(tmp_path / "vendor" / "ip" / "skip.sv")
    found = by_rel(discover(tmp_path, exclude=("vendor/*",)))
    assert found["rtl/keep.sv"].skipped_reason is None
    assert found["vendor/ip/skip.sv"].skipped_reason == "exclude"


def test_size_guard(tmp_path: Path) -> None:
    make(tmp_path / "small.sv")
    make(tmp_path / "huge_netlist.v", "// x\n" * 100_000)
    found = by_rel(discover(tmp_path, max_file_size_kb=64))
    assert found["small.sv"].skipped_reason is None
    assert found["huge_netlist.v"].skipped_reason == "size"


def test_pragma_protect_guard(tmp_path: Path) -> None:
    make(tmp_path / "open.sv")
    make(tmp_path / "encrypted.sv", "`pragma protect begin\ngibberish\n`pragma protect end\n")
    found = by_rel(discover(tmp_path))
    assert found["open.sv"].skipped_reason is None
    assert found["encrypted.sv"].skipped_reason == "pragma_protect"


def test_parseable_files_get_content_hash(tmp_path: Path) -> None:
    make(tmp_path / "a.sv")
    (found,) = discover(tmp_path)
    assert len(found.content_hash) == 64  # sha256 hex


def test_single_file_root(tmp_path: Path) -> None:
    path = make(tmp_path / "only.sv")
    (found,) = discover(path)
    assert found.relpath == "only.sv"
