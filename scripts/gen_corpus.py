#!/usr/bin/env python3
"""Generate a synthetic SystemVerilog design for benchmarking (M4).

Layout (default 2000 files): a module tree — ``top`` instantiates mid
modules, each mid instantiates a slice of leaves — with one shared header
included by ~10% of leaves and one package imported by ~10% of mids.
Realistic enough to exercise the include/macro dirty closure without
being a real design.

Usage::

    python scripts/gen_corpus.py /tmp/corpus --files 2000
"""

from __future__ import annotations

import argparse
from pathlib import Path

HEADER_EVERY = 10  # every Nth leaf includes the shared header
IMPORT_EVERY = 10  # every Nth mid imports the shared package
LEAVES_PER_MID = 20


def generate(root: Path, files: int) -> int:
    """Write a *files*-file design under *root*; returns the file count."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "defs.svh").write_text("`define BENCH_WIDTH 8\n`define BENCH_RESET 1'b0\n")
    (root / "bench_pkg.sv").write_text(
        "package bench_pkg;\n  localparam int LANES = 4;\n  typedef logic [7:0] byte_t;\n"
        "endpackage\n"
    )
    budget = files - 3  # defs.svh + bench_pkg.sv + top.sv
    mids = max(1, budget // (LEAVES_PER_MID + 1))
    leaves = budget - mids

    for i in range(leaves):
        include = '`include "defs.svh"\n' if i % HEADER_EVERY == 0 else ""
        width = "`BENCH_WIDTH" if i % HEADER_EVERY == 0 else "8"
        (root / f"leaf_{i:05d}.sv").write_text(
            f"{include}module leaf_{i:05d}(\n"
            f"    input  logic [{width}-1:0] a,\n"
            f"    input  logic [{width}-1:0] b,\n"
            f"    output logic [{width}-1:0] y\n"
            ");\n"
            f"  assign y = a + b + {i % 7};\nendmodule\n"
        )

    for m in range(mids):
        imported = "  import bench_pkg::*;\n" if m % IMPORT_EVERY == 0 else ""
        start = m * LEAVES_PER_MID
        body = "".join(
            f"  leaf_{i:05d} u_leaf_{i:05d}(.a(a), .b(b), .y(taps[{i - start}]));\n"
            for i in range(start, min(start + LEAVES_PER_MID, leaves))
        )
        count = max(1, min(start + LEAVES_PER_MID, leaves) - start)
        (root / f"mid_{m:04d}.sv").write_text(
            f"module mid_{m:04d}(\n"
            "    input  logic [7:0] a,\n"
            "    input  logic [7:0] b,\n"
            "    output logic [7:0] y\n"
            ");\n"
            f"{imported}"
            f"  logic [7:0] taps [{count}];\n"
            f"{body}"
            "  assign y = taps[0];\nendmodule\n"
        )

    top_body = "".join(
        f"  mid_{m:04d} u_mid_{m:04d}(.a(a), .b(b), .y(ys[{m}]));\n" for m in range(mids)
    )
    (root / "top.sv").write_text(
        "module top(\n    input  logic [7:0] a,\n    input  logic [7:0] b,\n"
        "    output logic [7:0] y\n);\n"
        f"  logic [7:0] ys [{mids}];\n{top_body}  assign y = ys[0];\nendmodule\n"
    )
    return leaves + mids + 3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="output directory")
    parser.add_argument("--files", type=int, default=2000, help="total file count")
    args = parser.parse_args()
    count = generate(args.root, args.files)
    print(f"wrote {count} files under {args.root}")


if __name__ == "__main__":
    main()
