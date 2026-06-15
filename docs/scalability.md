# Scalability: reads & writes on a 10–100+ GB graph

A large design's knowledge graph can reach tens or hundreds of GB. This page
describes how reads and incremental writes stay usable at that size, and where
the remaining ceilings are.

## The problem

`SqliteStore.load()` rebuilds the **entire** graph as a `networkx.MultiDiGraph`.
That is the right tool for a full re-link, an export, or visualization, but it
makes every query pay for the whole design — and NetworkX's in-memory form is
several times the on-disk size, so a 10 GB graph needs tens of GB of RAM and a
100 GB graph does not load at all. Before this work the MCP server loaded the
whole graph and reloaded it after every `update`, so the first AI-assistant
query after any rebuild waited on a multi-minute (eventually impossible) load.

## Reads: bounded, index-backed queries

`hdl_kgraph/storage/query.py` (`GraphQuery`) answers each MCP/CLI query by
hydrating only the **bounded subgraph** the query touches, selected through the
existing SQLite indices, then running the *same* `graph/analysis.py` function on
that small graph — so results are byte-identical to the full-graph path
(`tests/test_query.py` sweeps every name in the fixture corpus to pin this).

| Tool | How it stays bounded |
|---|---|
| `find_module`, `search_nodes` | indexed `nodes` lookup by `kind`/`name` (GLOB) / `file` |
| `who_instantiates` | name → ids, then `edges WHERE dst IN (…) AND kind='instantiates'` |
| `port_map` | unit → `DECLARES` children (+ instance `CONNECTS`), one indexed hop |
| `get_hierarchy(top)` | BFS over `DECLARES`/`INSTANTIATES`/`IMPLEMENTS`, capped by depth/nodes |
| `get_hierarchy()` (tops) | pure SQL set-difference: no incoming `INSTANTIATES` |
| `impact_of_change` | hydrate only the reverse-dependency closure `impact_radius` walks |
| `find_signal_drivers` | signals by name, then their `DRIVES`/`READS` edges |

Each call opens a fresh read connection, so a concurrent `update`/`watch` swap
is always observed — no cache, no staleness window.

A localized query is 1000–16000× faster than the old per-call load and its
latency tracks the *answer* size, not the design size (see
[benchmarks.md](benchmarks.md)). A query whose answer *is* the whole design
(`search_nodes("*")`, a top that contains everything) is still O(design) — that
is intrinsic, not a regression.

## Whole-design summaries: precomputed, not re-scanned

Clock-domain/CDC and UVM-topology reports scan every `CLOCKED_BY`/`DRIVES`/
`READS`/`EXTENDS` edge, so they cannot be bounded. Instead the build computes
them once — while the graph is already in memory — and persists the result to
the `summaries` table (`graph/summary.py`); the MCP tools read a small JSON blob
in well under a millisecond at any design size. The build computes them on a
full `build` and refreshes them on `update` (a database older than schema v8 has
no summaries table, so the reader falls back to a one-off full load).

## Writes

`update` writes only the changed rows: `save_incremental` diffs the new graph
against the stored rows and UPSERT/DELETEs just the delta, so a one-file edit
pays a write *volume* proportional to the change, not the design
(`scripts/bench_incremental.py` guards this).

### Remaining ceiling (known)

The `update` *pipeline* is still O(design) in a few places that a future change
should bound for true 100 GB incrementality:

1. the incremental linker loads the full prior graph (`SqliteStore.load()`) to
   re-resolve the dirty closure;
2. `_apply_delta` reads the full `nodes`/`edges` tables into memory to diff them
   against the new graph (the *write* is delta, but the *diff* scans both);
3. the precomputed summaries are recomputed over the whole graph each update.

These keep `update` memory-bound by the design size even though its write volume
is bounded. Bounding (1) and (2) — e.g. diffing only the dirty files' rows via a
denormalized `edges.file` column and the linker's affected-ref set — is the next
step; it is gated by the linker also becoming prior-graph-bounded, so the two
should land together.
