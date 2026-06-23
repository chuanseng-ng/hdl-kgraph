# What gets extracted, and how much to trust it

## Extracted constructs

- **Design units:** SystemVerilog/Verilog modules, interfaces, packages,
  programs; VHDL entities, architectures, packages, package bodies,
  configurations
- **Structure:** instances with port connections and parameter overrides,
  ports, parameters/generics, typedefs/structs/enums, functions and tasks,
  `` `include ``/`` `define `` relationships, filelists
- **Verification:** SV classes with inheritance chains (UVM hierarchies and
  roles), constraints, covergroups/coverpoints, assertions, properties,
  sequences, clocking blocks
- **Dataflow:** processes (always blocks, continuous assigns, VHDL
  processes), signals with drivers/readers (process-, assign-, and
  instance-level), clock and reset relationships, CDC-suspect crossings
- **DPI-C boundary (M8):** SystemVerilog `import "DPI-C"`/`export "DPI-C"`
  declarations linked to their C/C++ function definitions via `FOREIGN_BINDS`
  edges (see below)
- **cocotb boundary (M8):** Python cocotb testbenches linked to the DUT they
  drive — `TEST_COVERS` to the DUT module, `READS`/`DRIVES` for `dut.<signal>`
  access (see below)

### C/C++ DPI-C linking

`.c`/`.h` files are parsed with `tree-sitter-c` and
`.cpp`/`.cc`/`.cxx`/`.hpp`/`.hh`/`.hxx` with `tree-sitter-cpp`; each top-level
function **definition** (and prototype **declaration**) becomes a `FUNCTION`
node tagged with its language. An SV `import "DPI-C"` prototype becomes a
`FUNCTION`/`TASK` node and a `FOREIGN_BINDS` edge to the C function it binds —
matched by the **linkage name** (the `c_name = function …` alias if present,
else the SV name). An `export "DPI-C"` binds back to the SV subprogram it
names. Confidence follows the usual contract: a unique cross-file match is
`0.8`, an unresolved foreign name degrades to a `FUNCTION` stub.

Scope (the honest contract): DPI uses C linkage, so a **bare-name** match is the
right tier — C++ name mangling is not modeled (functions in `extern "C"` and
`namespace` blocks are recorded under their bare names), and the C preprocessor
(`#include`/`#define`) and full C type/width modeling are out of scope.

### Python cocotb testbenches

A `.py` file is parsed (with `tree-sitter-python`) **only if it mentions
`cocotb`** — discovery content-sniffs for it, so ordinary Python scripts never
enter the graph. Each `@cocotb.test`-decorated function becomes a `FUNCTION`
node (`language=python`, `attrs["is_cocotb_test"]`) with:

- a `TEST_COVERS` edge to the DUT module (confidence `0.4`);
- `READS`/`DRIVES` edges (confidence `0.6`) for each `dut.<signal>` access —
  `dut.sig.value = …` / `dut.sig.setimmediatevalue(…)` are `DRIVES`, everything
  else (`x = dut.sig.value`, `RisingEdge(dut.clk)`) is `READS` — resolved
  against the DUT module's ports/signals.

The toplevel is chosen by the *runner*, not named in the test, so the **DUT is
resolved heuristically**: the configured top module(s) (`[build].top` in
`hdl-kgraph.toml`) when present, else a filename heuristic (`test_fifo.py` →
`fifo`, `fifo_tb.py` → `fifo`). Scope (the honest contract): `dut.<signal>` is
resolved one level deep (hierarchical `dut.sub.sig` is best-effort), an unknown
signal is skipped rather than stubbed, and the DUT is a name guess — never
elaboration. Because the DUT link is cross-file, `update` re-links a cocotb
design fully (still re-parsing only changed files), like VHDL.

### Not extracted yet

Interface **modports**, **checkers**, **UDPs** (primitives), and **generate
blocks** are defined in the graph schema but have no extraction support yet
— code inside generate blocks is still walked (instances in a generate
block are attributed to the enclosing module), but the blocks themselves do
not appear as scopes. If one of these matters to you, an issue with the
smallest HDL file that needs it is the most useful contribution.

## Confidence: the honest contract

Every cross-file edge carries a confidence score, so the graph is explicit
about what was proven syntactically vs inferred by name matching:

| Score | Meaning |
|---|---|
| `1.0` | resolved (same-file definition, or unique match with imports honored) |
| `0.8` | unique cross-file name match (or cross-language match) |
| `0.6` | ambiguous (multiple candidates; one edge per candidate) |
| `0.4` | heuristic (e.g. `CLOCKED_BY` inferred from `clk`/`clock` naming) |

Unresolved targets (vendor IP, encrypted models, missing includes) become
stub nodes marked `unresolved`, rendered as `[?]` by `tree` and listed by
`query unresolved` — the graph is always connected and queries never
dead-end silently. Ambiguous matches render as `[~0.6]`.

## Mixed Verilog/VHDL designs link into one hierarchy

VHDL names are case-insensitive (normalized to lowercase, original casing
kept in attrs); `tree` and `query` cross the language boundary in both
directions, and a VHDL configuration overriding a component's default
binding is honored. Cross-language matches are by name at confidence ≤0.8 —
never 1.0 — because vendor tools may bind differently (case folding,
library prefixes, extended/escaped identifiers, generic-dependent
wrappers); the score is the honest contract.

The full node/edge schema and the per-milestone extraction details are in
[ROADMAP.md](../ROADMAP.md).
