#!/usr/bin/env python3
"""Read-latency benchmark: bounded MCP queries vs a full graph load.

The structural query tools answer from a bounded subgraph hydrated through the
SQLite indices (:mod:`hdl_kgraph.storage.query`), so their latency tracks the
*answer* size, not the design size — unlike the old path, which rebuilt the
whole graph in memory on every call. This script builds a large synthetic
design, times each tool through ``GraphQuery``, and checks that every bounded
tool answers well under the latency target and far faster than one full
``SqliteStore.load()`` of the same database.

Usage::

    python scripts/bench_query.py [--files 20000] [--target-ms 250]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_corpus import generate  # noqa: E402

from hdl_kgraph.pipeline import default_db_path, run_build  # noqa: E402
from hdl_kgraph.storage.query import GraphQuery  # noqa: E402
from hdl_kgraph.storage.sqlite_store import SqliteStore  # noqa: E402


def _time_ms(fn: object, repeat: int = 5) -> float:
    """Median wall-clock of *repeat* calls, in milliseconds."""
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()  # type: ignore[operator]
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=20000)
    parser.add_argument("--target-ms", type=float, default=250.0)
    parser.add_argument("--keep", type=Path, default=None)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="hdl-kgraph-qbench-") as tmp:
        root = args.keep if args.keep is not None else Path(tmp)
        count = generate(root, args.files)
        report = run_build(root)
        db = default_db_path(root)
        print(
            f"corpus:        {count} files, {report.node_count} nodes, "
            f"{report.edge_count} edges"
        )

        # Baseline: the old read path materialised this whole graph per call.
        load_ms = _time_ms(lambda: SqliteStore(db).load(), repeat=3)
        print(f"full load():   {load_ms:8.1f} ms   (the old per-call cost)")

        q = GraphQuery(db)
        # Representative names the generator always produces. A "mid" roots a
        # bounded subtree (~20 leaves); "top" and the ubiquitous `y` port span
        # the whole design, so they are reported but not held to the target.
        leaf = "leaf_00001"
        mid = "mid_0000"
        localized: dict[str, object] = {
            "find_module": lambda: q.find_module(leaf, 20),
            "who_instantiates": lambda: q.who_instantiates(leaf, 50, 0),
            "port_map": lambda: q.port_map(leaf, None),
            "get_hierarchy(mid)": lambda: q.hierarchy(mid, 3, 500),
            "top_modules": lambda: q.top_modules(),
            "impact_of_change": lambda: q.impact_of_change(leaf, 1, 100, 0),
            "clock_domains": lambda: q.clock_domains(),  # precomputed: O(1) read
            "uvm_topology": lambda: q.uvm_topology(),
        }
        whole_design: dict[str, object] = {
            "search_nodes(leaf_*)": lambda: q.search_nodes("leaf_*", None, None, 50, 0),
            "get_hierarchy(top)": lambda: q.hierarchy("top", 64, 500),
            "find_signal_drivers(y)": lambda: q.find_signal_drivers("y", None, False, 50, 0),
        }

        print("localized queries (answer << design; held to the target):")
        worst = 0.0
        for name, fn in localized.items():
            ms = _time_ms(fn)
            worst = max(worst, ms)
            speedup = load_ms / ms if ms else float("inf")
            print(f"  {name:24s} {ms:8.1f} ms   ({speedup:7.1f}x vs full load)")

        print("whole-design queries (answer ~ design; inherently O(design)):")
        for name, fn in whole_design.items():
            ms = _time_ms(fn)
            print(f"  {name:24s} {ms:8.1f} ms")

        bounded_ok = worst < args.target_ms
        faster_ok = worst < load_ms
        verdict = "PASS" if bounded_ok and faster_ok else "FAIL"
        print(
            f"target:        every localized tool < {args.target_ms:.0f} ms and "
            f"< full load -> {verdict}"
        )
        return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
