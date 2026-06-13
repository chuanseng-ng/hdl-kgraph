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

## Waiving lint findings

Known-benign findings (an intentional open port, a module only a future
top instantiates, a signal driven through a construct the graph does not
model) can be waived so the remaining report stays worth reading. Waivers
live in `hdl-kgraph.toml` — discovered from the build root, `--config PATH`
/ `--no-config` override — or in extra files passed with `--waiver-file`:

```toml
[[lint.waivers]]
check  = "open-port"             # required: exact check name
name   = "soc_top.u_dbg"         # glob on the finding name
reason = "debug port, tied off"  # required: the reviewable justification

[[lint.waivers]]
check  = "unconnected-port"
module = "fifo_*"                # glob on the owning module: one waiver
reason = "status outputs unused" # covers every instantiation
```

- `check` plus at least one of `name`/`module`/`file` is required; all
  given criteria must match. `reason` is mandatory — a waiver is a
  reviewed decision, not a mute button.
- `name` and `module` are case-sensitive globs (like `query search`); a
  dotted `name` pattern matches the qualified name, a plain one the last
  segment. `file` globs the root-relative path, like `[build].exclude`.
- Waived findings are dropped from the report; the footer counts them
  (`3 finding(s), 2 waived`) and `--show-waived` lists them with reasons.
  `--json` returns `{"findings", "waived", "unused_waivers", "counts"}`.
- A waiver that matches nothing is reported stale on stderr (the exit
  code stays 0), so waiver lists do not rot as the design evolves.

`lint` also honors `[build].top` from the config as `--top` exemptions
for `dead-module` (CLI flags are additive).

## Visualization

`visualize` writes a single self-contained HTML file — D3 is vendored and
the graph data embedded, so it opens air-gapped and can be attached to a
review or bug report as-is. Two views: a collapsible hierarchy and a
force-directed graph with node-kind / edge-kind / clock-domain filters.

- The default payload is the module-level projection, which stays
  responsive on large designs; `--full` embeds every node and edge.
- `--top NAME` roots the hierarchy view at a module (an unknown name is an
  error, like `tree`).
- A payload past the inline size limit is refused with guidance (drop
  `--full` or narrow with `--top`); `--force-inline` writes it anyway.
- Scaling strategy for very large designs: [viz-scalability.md](viz-scalability.md).
