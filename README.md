# hdl-kgraph

[![CI](https://github.com/chuanseng-ng/hdl-kgraph/actions/workflows/ci.yml/badge.svg)](https://github.com/chuanseng-ng/hdl-kgraph/actions/workflows/ci.yml)

**A knowledge graph for your HDL design.** hdl-kgraph parses SystemVerilog,
Verilog, and VHDL sources and builds a queryable graph of modules, entities,
instances, ports, parameters, signals, classes, packages, and the
relationships between them — design hierarchy, port connectivity, package
imports, class inheritance, clock domains, and more.

> **Status: alpha (v0.1).** SystemVerilog/Verilog structural extraction, the
> pass-2 linker, SQLite persistence, and the `build`/`status`/`query`/`tree`
> CLI are in. The preprocessor and filelists land in M2, VHDL in M3. See
> [ROADMAP.md](ROADMAP.md).

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

Coming next:

```bash
hdl-kgraph build -f sim/tb.f      # drive the build from a filelist (M2)
hdl-kgraph impact rtl/uart_tx.sv  # what does my change affect? (M4)
hdl-kgraph visualize              # interactive HTML graph (M5)
hdl-kgraph serve --mcp            # MCP server for AI assistants (M6)
```

## What gets extracted

- **Design units:** modules, interfaces/modports, packages, programs,
  checkers, UDPs; VHDL entities, architectures, packages, configurations
- **Structure:** instances with port connections and parameter overrides,
  generate blocks, `include`/`define` relationships, filelists
- **Verification:** SV classes (UVM hierarchies via inheritance chains),
  constraints, covergroups, assertions/properties/sequences, clocking blocks
- **Dataflow (M5):** signal drivers/readers, clock and reset trees,
  CDC-suspect crossings

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
