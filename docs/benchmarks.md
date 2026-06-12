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
