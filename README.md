# hdl-kgraph

[![CI](https://github.com/chuanseng-ng/hdl-kgraph/actions/workflows/ci.yml/badge.svg)](https://github.com/chuanseng-ng/hdl-kgraph/actions/workflows/ci.yml)

**A knowledge graph for your HDL design.** hdl-kgraph parses SystemVerilog,
Verilog, and VHDL sources and builds a queryable graph of modules, entities,
instances, ports, parameters, signals, classes, packages, and the
relationships between them — design hierarchy, port connectivity, package
imports, class inheritance, clock domains, and more.

> **Status: alpha (v0.7).** Milestones M1–M6 are in: SystemVerilog/Verilog
> and VHDL extraction with mixed-language linking, the SV preprocessor and
> real-world build inputs (`.f` filelists, defines, `hdl-kgraph.toml`),
> incremental rebuilds and watch mode, the clock/reset/CDC/lint/metrics
> analyses with an interactive visualization, and an MCP server so AI
> assistants can query the design. M7 adds opt-in semantic enrichment
> (`build --enrich`): the pyslang frontend elaborates the design — unrolling
> parameterized generates so instance counts match reality — and records a
> [discrepancy report](docs/enrichment.md). See the
> [roadmap](#roadmap-at-a-glance).

## Why

HDL codebases are graphs — hierarchy, connectivity, clock domains — but the
tools that understand them are locked inside simulators and synthesis flows.
hdl-kgraph extracts that structure into a local-first SQLite database you can
query from the CLI, scripts, or AI assistants via MCP. The architecture
follows [code-review-graph](https://github.com/tirth8205/code-review-graph),
adapted for hardware.

## Quickstart

```bash
pip install hdl-kgraph

hdl-kgraph build ./rtl            # parse sources -> ./rtl/.hdl-kgraph/graph.db
hdl-kgraph build -f sim/tb.f      # or drive the build from a vendor-style filelist
hdl-kgraph status                 # files, parse errors, node/edge counts
hdl-kgraph tree soc_top           # print the design hierarchy from a top module
hdl-kgraph query instances-of fifo
hdl-kgraph query unresolved       # what couldn't be resolved (vendor IP, macros)
```

Most commands take `--json` for scripting. Unresolved targets render as
`[?]` and ambiguous matches as `[~0.6]`: every cross-file edge carries a
confidence score — the graph's honest contract about what was proven
syntactically vs inferred by name matching
([docs/extraction.md](docs/extraction.md)).

## Features

**Real-world build inputs.** `.f` filelists (`+incdir+`/`+define+`, nested
`-f`, `$VAR` expansion), preprocessor defines and include dirs, VHDL library
mapping, `--exclude`/`--max-file-size` for vendored IP, and an
`hdl-kgraph.toml` config. Per-file diagnostics (`build -v`,
`status --errors`) tell you which files have parse errors or preprocessor
warnings and why files were skipped.
→ [docs/build-inputs.md](docs/build-inputs.md)

**Incremental updates.** `update` re-parses only changed files plus their
include/macro dependents — one edit in a 2000-file design lands in under a
second — and `watch` does it on every save burst. `detect-changes`
(exit codes: 0 clean, 1 dirty, 2 error; diffs against git, svn, or Perforce)
and `impact` answer "what changed, and what does it affect?" in CI.
→ [docs/incremental.md](docs/incremental.md)

**Mixed Verilog/VHDL designs link into one hierarchy.** `tree` and `query`
cross the language boundary in both directions; cross-language matches are
scored ≤0.8, never 1.0.
→ [docs/extraction.md](docs/extraction.md)

**Analyses.** Clock domains, reset trees, CDC suspects, signal
drivers/readers, UVM topology, graph-level lint checks, fan-in/out and
hub/bridge metrics, and a self-contained interactive HTML visualization
(`visualize`: hierarchy + force-directed views, community filters,
`--collapse` for subsystem supernodes, `--layout` tiers for large designs).
→ [docs/analyses.md](docs/analyses.md)

**AI assistants over MCP.** `hdl-kgraph setup` detects installed assistants
(Claude Code/Desktop, Cursor, Codex, Windsurf, Gemini CLI, VS Code) and
writes their MCP config; `hdl-kgraph serve` exposes nine read-only,
paginated tools (`pip install 'hdl-kgraph[mcp]'`).
→ [docs/mcp.md](docs/mcp.md)

## What gets extracted

- **Design units:** modules, interfaces, packages, programs; VHDL entities,
  architectures, packages, configurations
- **Structure:** instances with port connections and parameter overrides,
  `include`/`define` relationships, filelists
- **Verification:** SV classes (UVM hierarchies via inheritance chains),
  constraints, covergroups, assertions/properties/sequences, clocking blocks
- **Dataflow:** signal drivers/readers (process-, assign-, and
  instance-level), clock and reset trees, CDC-suspect crossings

Modports, checkers, UDPs, and generate blocks are *not* extracted yet. The
full list, the confidence convention, and the schema pointers live in
[docs/extraction.md](docs/extraction.md).

## Roadmap at a glance

| Milestone | Theme |
|---|---|
| M1 (v0.1) | SystemVerilog/Verilog structural graph + CLI |
| M2 (v0.2) | Preprocessor, `.f` filelists, includes |
| M3 (v0.3) | VHDL + mixed-language linking |
| M4 (v0.4) | Incremental updates, watch mode, impact analysis |
| M5 (v0.5) | Clock/reset/CDC analyses, lint checks, visualization |
| M6 (v0.6) | MCP server for AI assistants |
| M7 (v0.7) | Semantic enrichment via native frontends (pyslang elaboration; GHDL/VHDL planned) |
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
