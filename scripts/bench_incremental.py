#!/usr/bin/env python3
"""Incremental-update benchmark: 1 file edited in a 2k-file design.

Target: < 1.5 s since M5 (dataflow edges grew the graph ~76%); the original
M4 target was < 1 s, measured at 0.85 s on the pre-dataflow graph.

Generates a synthetic corpus (scripts/gen_corpus.py), times a full
``build``, touches one leaf module, then times the ``update``. See
docs/benchmarks.md for the procedure and recorded results.

Usage::

    python scripts/bench_incremental.py [--files 2000] [--target-s 1.5]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_corpus import generate  # noqa: E402

from hdl_kgraph.pipeline import run_build, run_update  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=2000)
    # M4 measured 0.85 s against a < 1 s target; M5's dataflow edges grew the
    # graph ~76% and the budget to < 1.5 s (see docs/benchmarks.md).
    parser.add_argument("--target-s", type=float, default=1.5)
    parser.add_argument(
        "--keep", type=Path, default=None, help="generate into this directory and keep it"
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="hdl-kgraph-bench-") as tmp:
        root = args.keep if args.keep is not None else Path(tmp)
        count = generate(root, args.files)
        print(f"corpus:        {count} files under {root}")

        started = time.perf_counter()
        build = run_build(root)
        build_s = time.perf_counter() - started
        print(
            f"full build:    {build_s:.2f}s "
            f"({build.parsed_files} files, {build.node_count} nodes, {build.edge_count} edges)"
        )

        leaf = root / "leaf_00001.sv"
        leaf.write_text(leaf.read_text().replace("+ 1", "+ 2"))
        started = time.perf_counter()
        update = run_update(root)
        update_s = time.perf_counter() - started
        assert update.build is not None
        print(
            f"update:        {update_s:.2f}s "
            f"(re-parsed {len(update.reparsed)}, reused {update.build.reused_files})"
        )

        verdict = "PASS" if update_s < args.target_s else "FAIL"
        print(f"target:        update < {args.target_s:.1f}s -> {verdict}")
        return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
