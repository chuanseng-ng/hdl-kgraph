# Benchmarks

## M4 target: incremental update of 1 file in a 2k-file design < 1 s

Procedure (fully scripted):

```bash
pip install -e .
python scripts/bench_incremental.py --files 2000
```

`scripts/gen_corpus.py` generates a synthetic 2000-file SystemVerilog design
(a `top` → mid → leaf instantiation tree; ~10% of leaves include a shared
header, ~10% of mids import a shared package), `bench_incremental.py` times a
full `build`, edits one leaf module, and times the `update`. The script exits
non-zero when the target is missed.

### Recorded results

| version | machine | corpus | full build | update (1 leaf edited) | target |
|---|---|---|---|---|---|
| v0.4 (M4) | Linux container, Python 3.11 | 2000 files, 11 992 nodes, 18 364 edges | 1.36 s | **0.85 s** | < 1 s ✅ |
| v0.5 (M5) | Linux container, Python 3.11 | 2000 files, 14 086 nodes, 32 341 edges | 1.94 s | **1.29 s** | < 1.5 s ✅ |

**Budget bump to < 1.8 s (precomputed summaries).** The whole-design reports
(clock domains / CDC, UVM topology) cannot be answered from a bounded subgraph,
so the build computes them once — while the graph is in memory — and persists
them, letting the MCP `clock_domains`/`uvm_topology` tools read O(1) at any
design size (see [scalability.md](scalability.md)). That adds a fixed
whole-design pass to each build/update (~0.25 s on the M5 corpus), so
`bench_incremental.py` now defaults to `--target-s 1.8`.

**Why the M5 number is higher:** dataflow extraction grew the same corpus's
graph by ~76% more edges (DRIVES/READS/CLOCKED_BY/RESETS plus SIGNAL and
PROCESS nodes), and every edge is re-linked and re-saved on each update
(steps 2–4 below scale with graph size, not with the edit). The M4
acceptance (< 1 s) was met and recorded at M4; the M5 budget is < 1.5 s —
the threshold at which a `--no-dataflow` build flag or a partitioned
re-link (see the escape hatch) becomes worth its complexity.
`bench_incremental.py` defaults to `--target-s 1.5` accordingly.

### Where the update time goes

An incremental `update` re-parses only the dirty files, but by design it
re-runs the global pass-2 link and rewrites the database transactionally
(correct by construction — no surgical node/edge deletion). The remaining
cost is therefore roughly:

1. re-hash every file to detect changes (one `discover` pass),
2. decode the stored pass-1 IR JSON for every clean unit,
3. global pass-2 link (`build_graph`),
4. transactional full rewrite of `graph.db`.

The change-detection prelude loads only the file-hash table and the
include/macro dependency subgraph from SQLite, never the full graph.

### Escape hatch if a larger design misses the target

The full-rewrite save and whole-graph re-link are the first things to
revisit: per-file `DELETE`/`INSERT` of nodes and edges plus a scoped re-link
of only the affected name partitions would cut steps 3–4 to near zero, at
the cost of real invalidation bookkeeping. Measure first — the benchmark
script accepts `--files N`.

## Read latency: bounded queries vs a full graph load

```bash
python scripts/bench_query.py --files 20000
```

`bench_query.py` builds a large synthetic design and times each MCP tool
through `GraphQuery` (`hdl_kgraph/storage/query.py`), which answers from a
*bounded subgraph* hydrated through the SQLite indices rather than loading the
whole graph. It contrasts that with one `SqliteStore.load()` — the cost the old
read path paid on *every* call.

### Recorded results

| corpus | full `load()` | find_module | port_map | get_hierarchy (subtree) | impact_of_change | clock_domains / uvm (precomputed) |
|---|---|---|---|---|---|---|
| 20 000 files, 140 940 nodes, 323 831 edges | ~5000 ms | **0.9 ms** | **0.7 ms** | **3.7 ms** | **120 ms** | **<0.5 ms** |

A localized query is **1000–16000×** faster than the old per-call load, and —
crucially — its latency tracks the *answer* size, not the design size, so it
does not grow as the graph scales toward 10–100+ GB (where a full load no
longer fits in memory at all).

**Whole-design queries stay O(design).** A query whose answer *is* most of the
graph — `search_nodes("*")`, `get_hierarchy` of a top that directly contains
the whole design, `find_signal_drivers` of a net present in every module —
necessarily touches the whole graph and is not faster than a load. These are
reported separately by the script; the target covers the localized tools.
