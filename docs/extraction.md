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
- **SDC/XDC timing constraints (M10):** `create_clock` → `CLOCK` nodes and
  authoritative clock evidence; `set_*_path`/`set_*_delay`/`set_clock_groups` →
  `TIMING_CONSTRAINT` nodes; object queries → `CONSTRAINS` edges (see below)
- **UPF power intent (M10):** `create_power_domain` → `POWER_DOMAIN` nodes; its
  `-elements` → `CONSTRAINS` edges; isolation/retention strategies in `attrs`
  (see below)
- **Tcl flow scripts (M10):** `read_verilog`/`read_vhdl`/`read_sdc`/`analyze`/
  `add_files`/`source` → `REFERENCES_FILE` edges to the files they name
  (see below)

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

### SDC/XDC timing constraints

`.sdc`/`.xdc` files are scanned by a hand-written Tcl-subset parser (no Tcl
evaluation — only literal `set NAME value` substitution is applied; see
ROADMAP "Risks"). `create_clock`/`create_generated_clock` become `CLOCK` nodes
(`language=tcl`; `attrs` carry `period`/`generated`/`divide_by`/`virtual`);
`set_false_path`/`set_multicycle_path`/`set_input_delay`/`set_output_delay`/
`set_clock_groups` become `TIMING_CONSTRAINT` nodes (`attrs["set_type"]` plus
the from/to/group lists). Each `get_ports`/`get_pins`/`get_cells`/`get_clocks`
object query resolves to the design node it names via a `CONSTRAINS` edge —
exact unique match at 1.0, a glob (`value*`) at 0.8 (unique) / 0.6 (ambiguous);
a constraint naming an object the design lacks is **skipped, not stubbed**.

Two analyses consume this (the M5 synergy):

- **Clock evidence.** A `create_clock` on a net is authoritative, so every
  `CLOCKED_BY` edge it backs is upgraded from the 0.4 name heuristic to 1.0
  (`attrs["evidence"]="sdc_create_clock"`).
- **CDC suppression.** A crossing covered by `set_clock_groups -asynchronous`
  (cross-group clock pair) or `set_false_path` is flagged `declared_safe`; the
  `clock_domains`/`cdc` report partitions these out of the active suspect list
  and reports a `cdc_suppressed_count`.

Because both are cross-file/design-wide, `update` re-links an SDC-bearing
design fully (still re-parsing only changed files), like cocotb/VHDL.

### UPF power intent

`.upf` files are scanned by the same Tcl-subset parser as SDC (they share one
base; UPF is also never evaluated, only literal `set` substitution).
`create_power_domain` becomes a `POWER_DOMAIN` node (`language=tcl`); its
`-elements` resolve to the design's instances via `CONSTRAINS` edges — reusing
the SDC `cells` query, so an exact unique match is 1.0 and a glob is 0.8/0.6,
and an element the design lacks is **skipped, not stubbed**. The `.` element
(the design root) is recorded in `attrs` but draws no edge. The `-supply` and
the `set_isolation`/`set_retention`/`set_level_shifter` strategies that name the
domain via `-domain` are folded into the domain's `attrs` (each strategy keeps
its `applies_to`/`isolation_signal`/`clamp_value`/… options).

The **power-domain report** (`power_domains` query / MCP tool, a persisted
summary with an out-of-core SQL fallback, and an `analyze` digest line) lists
each domain with its resolved element instances, its strategies, and whether it
is isolated — the power-intent analogue of the clock-domain report. Like SDC,
`update` re-links a UPF-bearing design fully.

### Tcl flow scripts

`.tcl` flow scripts are scanned by the same Tcl-subset parser (no evaluation;
only literal `set` substitution). The file-reading commands — `read_verilog`,
`read_systemverilog`, `read_vhdl`, `read_sdc`/`read_xdc`/`read_upf`, `analyze`,
`add_files`, and `source` — become `REFERENCES_FILE` edges from the script to
the file each names, with `attrs["mode"]` recording the kind (`read`/`analyze`/
`add`/`source`). A path argument is told from a flag value heuristically (it has
a directory separator or a recognized HDL/script suffix), so `-format verilog`
is not mistaken for a file. Paths are resolved relative to the script and
normalized to the build-root relpath keyspace.

Resolution happens in pass 2, where the full file set is known: a referenced
file that is part of the build binds to its real `FILE` node; one outside the
analyzed set (a generated or out-of-tree source, or a missing `source`d helper)
binds to an `unresolved:file:` stub — a distinct id, so it never shadows a real
`FILE` node and never raises a dangling-endpoint warning. Like the other Tcl
wedges, `update` re-links a flow-script-bearing design fully. Perl and SLN
remain unimplemented (fail-loud stubs).

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
