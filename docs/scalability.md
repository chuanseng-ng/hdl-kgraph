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

`update` writes only the changed rows, and (since the incremental-link path)
*reads* only the changed rows too. `save_incremental` UPSERT/DELETEs just the
delta; when the pipeline links incrementally it passes the dirty files and the
re-resolved clean-ref ids, and `_apply_delta` scopes the diff to exactly those —
reading only the touched files' nodes/edges (plus fileless stub nodes) through
`idx_nodes_file`/`idx_edges_src`, never the whole tables. So a one-file edit
pays a write *and read* cost proportional to the change. `bench_incremental.py`
guards both: on the 2 000-file corpus a one-leaf edit scans **~0.04 %** of the
nodes to diff (down from 100 %).

`link_incremental` (#64) guarantees this is safe: the only rows that can differ
from the stored build are those owned by a touched/removed file, fileless nodes
it may add/drop, and the `affected_srcs` clean refs it re-resolved; every other
row is byte-identical. The byte-identical-rebuild fuzz suite
(`tests/test_incremental_equivalence.py`) pins that a scoped `update` loads back
identical to a full `build`. When the link falls back to a full re-link (VHDL,
binds, enrich), the scope sets are omitted and `_apply_delta` diffs the whole
tables as before — correct for any graph.

### Remaining ceiling (known)

Two parts of the `update` *pipeline* are still O(design) in memory:

1. the incremental linker loads the full prior graph (`SqliteStore.load()`) to
   re-resolve the dirty closure;
2. the precomputed summaries are recomputed over the whole graph each update.

The delta *diff* is now bounded (above), so (1) — making `link_incremental`
re-resolve the dirty closure without holding the entire prior graph in memory —
is the dominant remaining work for true 100 GB incremental `update`, and the
larger/riskier change (it must preserve the same byte-identical contract).
