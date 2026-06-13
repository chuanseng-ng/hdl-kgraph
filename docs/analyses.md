# Analyses

The M5 analyses turn structure into insight. Dataflow (`DRIVES`/`READS`) is
extracted from always/process blocks, continuous assigns, and instance port
directions; clocks and resets carry evidence scores (sensitivity-list proof
= 1.0, name-pattern heuristics = 0.4).

```bash
hdl-kgraph query clock-domains     # clock nets, alias-merged across hierarchy
hdl-kgraph query cdc               # signals driven in domain A, read in domain B
hdl-kgraph query reset-tree        # async vs (heuristic) sync resets
hdl-kgraph query drivers ready     # what drives signal 'ready' (--readers flips it)
hdl-kgraph query uvm               # UVM components by role + TEST_COVERS links
hdl-kgraph lint                    # unconnected ports, undriven/unread signals,
                                   #   dead modules, redundant parameter overrides
hdl-kgraph metrics --communities   # fan-in/out, hubs/bridges, Louvain subsystems
hdl-kgraph visualize -o graph.html # self-contained interactive HTML
```

All of these take `--json` for scripting.

## Caveats — reports, not gates

- **CDC findings are *suspects*, not violations** — synchronizers are not
  recognized (SDC `set_clock_groups` suppression lands with M10). Review
  each finding.
- **`lint` always exits 0**; it is a report, not a gate. Signal-level
  checks skip files with parse errors and implicit-net stubs so a finding
  is worth reading; confidences below 1.0 mark heuristics.
- **`metrics`** computes fan-in/fan-out and betweenness centrality on the
  module-level instantiation projection; `--limit/-n` caps the listing,
  `--communities` adds Louvain community (subsystem) suggestions.

## Visualization

`visualize` writes a single self-contained HTML file — D3 is vendored and
the graph data embedded, so it opens air-gapped and can be attached to a
review or bug report as-is. Two views: a collapsible hierarchy and a
force-directed graph with node-kind / edge-kind / clock-domain filters.

- The default payload is the module-level projection, which stays
  responsive on large designs; `--full` embeds every node and edge.
- `--top NAME` roots the hierarchy view at a module (an unknown name is an
  error, like `tree`).
- Scaling strategy for very large designs: [viz-scalability.md](viz-scalability.md).
