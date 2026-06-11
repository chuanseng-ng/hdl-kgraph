# hdl-kgraph

[![CI](https://github.com/chuanseng-ng/hdl-kgraph/actions/workflows/ci.yml/badge.svg)](https://github.com/chuanseng-ng/hdl-kgraph/actions/workflows/ci.yml)

**A knowledge graph for your HDL design.** hdl-kgraph parses SystemVerilog,
Verilog, and VHDL sources and builds a queryable graph of modules, entities,
instances, ports, parameters, signals, classes, packages, and the
relationships between them — design hierarchy, port connectivity, package
imports, class inheritance, clock domains, and more.

> **Status: alpha (v0.5).** SystemVerilog/Verilog structural extraction, the
> pass-2 linker, SQLite persistence, the `build`/`status`/`query`/`tree`
> CLI, real-world inputs — the SV preprocessor, `.f` filelists, include
> dirs, and `hdl-kgraph.toml` config — VHDL with mixed-language linking
> (entities, architectures, packages, configurations, `--lib` library
> mapping), incremental rebuilds (`update`, `watch`, `detect-changes`,
> `impact`), and the M5 analyses — dataflow, clock domains / reset tree /
> CDC suspects, lint checks, graph metrics, UVM topology, and an
> interactive `visualize` — are in. See [ROADMAP.md](ROADMAP.md).

## Why

HDL codebases are graphs — hierarchy, connectivity, clock domains — but the
tools that understand them are locked inside simulators and synthesis flows.
hdl-kgraph extracts that structure into a local-first SQLite database you can
query from the CLI, scripts, or (later) AI assistants via MCP. The
architecture follows
[code-review-graph](https://github.com/tirth8205/code-review-graph), adapted
for hardware.

## Quickstart

```bash
pip install hdl-kgraph

hdl-kgraph build ./rtl            # parse sources -> ./rtl/.hdl-kgraph/graph.db
hdl-kgraph build -f sim/tb.f      # drive the build from a vendor-style filelist
hdl-kgraph build -D SYNTHESIS -D WIDTH=8 -I include   # defines + incdirs
hdl-kgraph status                 # files, parse errors, node/edge counts
hdl-kgraph tree soc_top           # print the design hierarchy from a top module
hdl-kgraph query instances-of fifo
hdl-kgraph query unresolved       # what couldn't be resolved (vendor IP, macros)
```

`build` accepts `--exclude GLOB` (repeatable) and `--max-file-size KB` to keep
generated netlists and vendored IP out of the graph. Files with syntax errors
still yield partial results; `status` reports the parse-error count.
Unresolved instance targets render as `[?]` in `tree` and ambiguous matches as
`[~0.6]` — see the confidence convention in [ROADMAP.md](ROADMAP.md).

Filelists support `+incdir+`/`+define+`, nested `-f`, `-y`/`-v` library
dirs, and `$VAR` expansion. When no defines are given at all, conditionals
on undefined names emit *both* branches: the side a define-less compile
would select at full confidence, the alternative at 0.6. Repeatable inputs
can also live in an `hdl-kgraph.toml` at the build root (CLI flags win):

```toml
[build]
filelists = ["sim/tb.f"]
defines   = ["SYNTHESIS", "WIDTH=8"]
incdirs   = ["include"]
exclude   = ["vendor/*"]

[vhdl.libraries]
work = "src/vhdl"        # or: hdl-kgraph build --lib work=./src/vhdl
```

**Mixed Verilog/VHDL designs link into one hierarchy.** VHDL names are
case-insensitive (normalized to lowercase, original casing kept in attrs);
`tree` and `query` cross the language boundary in both directions, and a
VHDL configuration overriding a component's default binding is honored.
Cross-language matches are by name at confidence ≤0.8 — never 1.0 — because
vendor tools may bind differently (case folding, library prefixes,
extended/escaped identifiers, generic-dependent wrappers); the score is the
honest contract.

**Incremental updates** keep the graph fresh as you edit. `update` re-parses
only changed/added/removed files plus their dependents — files that
`` `include `` an edited header or expand a macro it defines — and re-links
everything else from stored parse results (one file edited in a 2000-file
design updates in under a second; see [docs/benchmarks.md](docs/benchmarks.md)).
A change to the effective build inputs (defines, incdirs, filelists, library
map) falls back to a full rebuild automatically, as does a database written
by an older schema version — the database is a derived cache, so rebuild *is*
the migration.

```bash
hdl-kgraph update                  # re-parse only what changed, re-link, save
hdl-kgraph detect-changes          # M/A/D lines vs the last build; exit 1 if dirty
hdl-kgraph detect-changes --git    # ...or vs git HEAD (any ref works)
hdl-kgraph impact rtl/uart_tx.sv   # what does my change affect?
hdl-kgraph impact fifo --files     # affected files instead of design units
hdl-kgraph watch ./rtl             # debounced update on every save burst
```

`impact` walks reverse `INSTANTIATES`/`IMPORTS`/`INCLUDES`/`EXTENDS` (plus
VHDL `USES_PACKAGE`/`IMPLEMENTS`/`BINDS` and macro-use) edges transitively:
the instantiating parents, importers, includers, and subclasses a change can
break. `watch` needs the `watchdog` extra: `pip install 'hdl-kgraph[watch]'`.

**Analyses (M5)** turn structure into insight. Dataflow (`DRIVES`/`READS`)
is extracted from always/process blocks, continuous assigns, and instance
port directions; clocks and resets carry evidence scores (sensitivity-list
proof = 1.0, name-pattern heuristics = 0.4):

```bash
hdl-kgraph query clock-domains     # clock nets, alias-merged across hierarchy
hdl-kgraph query cdc               # signals driven in domain A, read in domain B
hdl-kgraph query reset-tree        # async vs (heuristic) sync resets
hdl-kgraph query drivers ready     # what drives signal 'ready' (--readers flips it)
hdl-kgraph query uvm               # UVM components by role + TEST_COVERS links
hdl-kgraph lint                    # unconnected ports, undriven/unread signals,
                                   #   dead modules, redundant parameter overrides
hdl-kgraph metrics --communities   # fan-in/out, hubs/bridges, Louvain subsystems
hdl-kgraph visualize -o graph.html # self-contained interactive HTML (d3 vendored,
                                   #   opens air-gapped): hierarchy + force views,
                                   #   filter by node/edge kind and clock domain
```

CDC findings are *suspects*, not violations — synchronizers are not
recognized (SDC `set_clock_groups` suppression lands with M10). `lint`
always exits 0; it is a report, not a gate. All new commands take `--json`.

Coming next:

```bash
hdl-kgraph serve --mcp            # MCP server for AI assistants (M6)
```

## What gets extracted

- **Design units:** modules, interfaces/modports, packages, programs,
  checkers, UDPs; VHDL entities, architectures, packages, configurations
- **Structure:** instances with port connections and parameter overrides,
  generate blocks, `include`/`define` relationships, filelists
- **Verification:** SV classes (UVM hierarchies via inheritance chains),
  constraints, covergroups, assertions/properties/sequences, clocking blocks
- **Dataflow:** signal drivers/readers (process-, assign-, and
  instance-level), clock and reset trees, CDC-suspect crossings

Every cross-file edge carries a confidence score (resolved → heuristic), so
the graph is honest about what was proven syntactically vs inferred by name
matching. The full schema is documented in [ROADMAP.md](ROADMAP.md).

## Roadmap at a glance

| Milestone | Theme |
|---|---|
| M1 (v0.1) | SystemVerilog/Verilog structural graph + CLI |
| M2 (v0.2) | Preprocessor, `.f` filelists, includes |
| M3 (v0.3) | VHDL + mixed-language linking |
| M4 (v0.4) | Incremental updates, watch mode, impact analysis |
| M5 (v0.5) | Clock/reset/CDC analyses, lint checks, visualization |
| M6 (v0.6) | MCP server for AI assistants |
| M7 (v0.7) | Semantic enrichment (pyslang, GHDL) |
| M8 (v1.0) | C/C++/Python boundary (DPI-C, cocotb), stable API |
| M9 (v1.x) | Chisel/FIRRTL, Amaranth, SpinalHDL |
| M10 (v1.x) | Tcl/SDC/UPF constraints, Perl scripting, SLN portable stimulus |

Details and acceptance criteria: [ROADMAP.md](ROADMAP.md).

## Development

```bash
git clone https://github.com/chuanseng-ng/hdl-kgraph
cd hdl-kgraph
pip install -e .[dev]
ruff check . && ruff format --check . && mypy && pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md). The single most useful contribution
right now: the smallest HDL file that breaks extraction.

## License

[MIT](LICENSE)
