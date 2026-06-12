"""File discovery and guard tests (exclude globs, size cap, pragma protect)."""

import os
import sys
from pathlib import Path

import pytest

from hdl_kgraph.discovery import discover, glob_sources
from hdl_kgraph.schema import Language

needs_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs privileges on Windows"
)


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
    make(tmp_path / "alu.vhd")
    make(tmp_path / "pkg.vhdl")
    found = by_rel(discover(tmp_path))
    assert set(found) == {"a.sv", "b.v", "c.svh", "d.vh", "alu.vhd", "pkg.vhdl"}
    assert found["a.sv"].language is Language.SYSTEMVERILOG
    assert found["b.v"].language is Language.VERILOG
    assert found["alu.vhd"].language is Language.VHDL
    assert found["pkg.vhdl"].language is Language.VHDL


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


@needs_symlinks
def test_discovers_through_symlinked_directory(tmp_path: Path) -> None:
    make(tmp_path / "external" / "ip.sv")
    root = tmp_path / "root"
    make(root / "top.sv")
    os.symlink(tmp_path / "external", root / "vendor")
    found = by_rel(discover(root))
    assert set(found) == {"top.sv", "vendor/ip.sv"}
    found = by_rel(discover(root, exclude=("vendor/*",)))
    assert found["vendor/ip.sv"].skipped_reason == "exclude"


@needs_symlinks
def test_symlink_loop_terminates(tmp_path: Path) -> None:
    root = tmp_path / "root"
    make(root / "top.sv")
    make(root / "sub" / "leaf.sv")
    os.symlink(root, root / "sub" / "loop")
    found = by_rel(discover(root))
    assert set(found) == {"top.sv", "sub/leaf.sv"}


@needs_symlinks
def test_symlinked_file_alias_is_deduped(tmp_path: Path) -> None:
    root = tmp_path / "root"
    make(root / "a.sv")
    os.symlink(root / "a.sv", root / "alias.sv")
    (found,) = discover(root)
    assert found.relpath == "a.sv"


def test_glob_sources_keeps_glob_semantics(tmp_path: Path) -> None:
    make(tmp_path / "rtl" / "top.sv")
    make(tmp_path / "rtl" / "core" / "alu.sv")
    make(tmp_path / "rtl" / "core.txt")
    assert glob_sources(tmp_path, "rtl/*.sv") == [tmp_path / "rtl" / "top.sv"]
    assert glob_sources(tmp_path, "rtl/**/*.sv") == [
        tmp_path / "rtl" / "core" / "alu.sv",
        tmp_path / "rtl" / "top.sv",
    ]
    assert glob_sources(tmp_path, "rtl/c?re/alu.sv") == [tmp_path / "rtl" / "core" / "alu.sv"]


@needs_symlinks
def test_glob_sources_through_symlinked_directory(tmp_path: Path) -> None:
    make(tmp_path / "external" / "ip.sv")
    root = tmp_path / "root"
    make(root / "rtl" / "top.sv")
    os.symlink(tmp_path / "external", root / "rtl" / "vendor")
    assert glob_sources(root, "rtl/**/*.sv") == [
        root / "rtl" / "top.sv",
        root / "rtl" / "vendor" / "ip.sv",
    ]
