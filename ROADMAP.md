# hdl-kgraph Roadmap

`hdl-kgraph` builds a queryable knowledge graph from hardware description language
(HDL) source code — SystemVerilog, Verilog, and VHDL first, with C/C++/Python,
emerging HDLs (Chisel, Amaranth, SpinalHDL), and EDA flow languages (Tcl/SDC
constraints, UPF power intent, Perl scripting, SLN portable stimulus) as future
targets. The architecture is
modeled on [code-review-graph](https://github.com/tirth8205/code-review-graph):
Python 3.10+, tree-sitter parsing, NetworkX graph algorithms, SQLite persistence,
a CLI, and (later) an MCP server for AI assistants. Distribution is via pip/PyPI.

**MVP line:** Milestones M1–M4. M5–M6 are value-add. M7–M10 are stretch goals.

---

## Knowledge graph schema

The schema is the core of the project. It lives in `src/hdl_kgraph/schema.py` from
day one so every milestone extends rather than reworks it.

### Node kinds

Every node carries: `id`, `kind`, `name`, `qualified_name`, `file`, `line_span`,
`language`, and a free-form `attrs` dict.

| Group | Kinds | Notable attrs |
|---|---|---|
| Structure | `FILE`, `FILELIST`, `LIBRARY` | path, content hash, language, work library |
| Verilog/SV design units | `MODULE`, `PROGRAM`, `INTERFACE`, `MODPORT`, `PACKAGE`, `CHECKER`, `PRIMITIVE` (UDP) | `is_macromodule`, `is_celldefine` |
| VHDL design units | `ENTITY`, `ARCHITECTURE`, `VHDL_PACKAGE`, `PACKAGE_BODY`, `CONFIGURATION`, `CONTEXT` | library, original casing (names normalized lowercase) |
| Behavioral | `FUNCTION`, `TASK`, `PROCESS` (VHDL process / SV always block), `GENERATE_BLOCK` | `always_ff`/`always_comb`/`always_latch`, sensitivity list |
| OOP / verification | `CLASS`, `CONSTRAINT`, `COVERGROUP`, `COVERPOINT`, `PROPERTY`, `SEQUENCE`, `ASSERTION`, `CLOCKING_BLOCK` | `is_virtual`, UVM base (via EXTENDS chain) |
| Data | `PORT`, `PARAMETER` (param/localparam/generic), `SIGNAL` (net/variable/VHDL signal), `TYPEDEF`, `STRUCT`, `ENUM`, `ENUM_MEMBER` | direction, width expression, data type, default |
| Elaboration | `INSTANCE` | instance name, target name, parameter overrides |
| Preprocessor | `MACRO` (`` `define ``), `INCLUDE_FILE` | body, arity, guard macro |
| Constraints / scenarios | `CLOCK`, `TIMING_CONSTRAINT`, `POWER_DOMAIN`, `SCENARIO`, `ACTION` | period/waveform, virtual/generated clock master, constraint command, supply/isolation strategies, scenario resources |

### Edge kinds

Every edge carries: `src`, `dst`, `kind`, `confidence`, and `attrs`.

| Kind | Meaning |
|---|---|
| `DECLARES` | scope → declaration (file→module, module→port/signal/param, class→method, …) |
| `INSTANTIATES` | instance → target module/entity (parent scope `DECLARES` the instance) |
| `CONNECTS` | instance → port binding (named / positional / `.*` wildcard) |
| `PARAMETERIZES` | instance → parameter override |
| `IMPORTS` | scope → SV package (wildcard vs explicit symbol) |
| `INCLUDES` | file → file (`` `include ``) |
| `DEFINES_MACRO` / `USES_MACRO` | file/scope ↔ macro |
| `EXTENDS` | SV class inheritance |
| `IMPLEMENTS` | VHDL architecture → entity |
| `BINDS` | SV `bind` directive / VHDL configuration → target |
| `USES_PACKAGE` | VHDL `library`/`use` clause |
| `DRIVES` / `READS` | process/assign/instance port → signal (dataflow) |
| `CLOCKED_BY` / `RESETS` | process/module → clock/reset signal |
| `ASSERTS_ON` / `COVERS` | assertion/covergroup → signal/property |
| `TEST_COVERS` | testbench/UVM test/SLN scenario → DUT module |
| `FOREIGN_BINDS` | SV DPI-C import/export ↔ C function (M8) |
| `GENERATED_FROM` | generated HDL → generator source (M9 Chisel/Amaranth/SpinalHDL, M10 Perl codegen) |
| `CONSTRAINS` | timing constraint/clock/power domain → port/signal/instance/clock (M10) |
| `REFERENCES_FILE` | Perl/Tcl script → HDL file it reads/compiles/generates (M10) |

### Confidence convention

| Score | Meaning |
|---|---|
| `1.0` | syntactically resolved within the compilation unit |
| `0.8` | cross-file name match, unique candidate |
| `0.6` | ambiguous name match (multiple candidates; all edges emitted) |
| `0.4` | heuristic (e.g. `CLOCKED_BY` inferred from `clk`/`clock` naming) |

Unresolved targets become stub nodes with `attrs["unresolved"] = True` so the graph
is always connected and queries never dead-end silently.

### Two-pass build architecture

- **Pass 1 (parse):** each file is parsed independently into a per-file IR of
  declarations and unresolved references. Parallelizable by design (currently
  run serially; parallel execution is tracked in issue #26).
- **Pass 2 (link):** cross-file resolution — instance→definition, package imports,
  VHDL library/work scoping, bind/configuration resolution — with confidence scoring.

This split makes incremental updates (M4) cheap: re-run pass 1 only for changed
files, then re-run the fast global pass 2.

---

## M1 — v0.1: SystemVerilog/Verilog structural graph (MVP)

**Goal:** `pip install -e . && hdl-kgraph build ./rtl` produces a queryable design
hierarchy graph for a Verilog/SystemVerilog codebase.

- [x] Schema module: `NodeKind`/`EdgeKind` enums, `Node`/`Edge` dataclasses,
      confidence convention documented in docstrings
- [x] Grammar bake-off: evaluate `gmlarumbe/tree-sitter-systemverilog` vs
      `tree-sitter/tree-sitter-verilog` against the fixture corpus; pick one grammar
      for both `.v` and `.sv` (see Risks) — **winner: `tree-sitter-systemverilog`;
      results in docs/grammar-bakeoff.md**
- [x] tree-sitter SV parser extracting: `MODULE`, `INTERFACE`, `PACKAGE`, `PROGRAM`,
      `FUNCTION`/`TASK`, `PORT`, `PARAMETER`, `INSTANCE`, `TYPEDEF`/`STRUCT`/`ENUM`,
      `CLASS` (declaration + `EXTENDS` only)
- [x] Edges: `DECLARES`, `INSTANTIATES`, `CONNECTS` (named + positional),
      `PARAMETERIZES`, `IMPORTS`, `EXTENDS`
- [x] Pass-2 linker with confidence scoring and unresolved stub nodes
- [x] NetworkX in-memory graph + SQLite persistence (`nodes`, `edges`, `files`
      tables; content-hash column added now for M4)
- [x] CLI: `build`, `status`, `query` (e.g. `hdl-kgraph query instances-of fifo`),
      `tree` (print design hierarchy from a top module)
- [x] Error tolerance: files with tree-sitter ERROR nodes still yield partial
      results; parse-error count surfaces in `status`
- [x] File-size guards and exclude-glob config (huge generated netlists,
      `` `pragma protect `` encrypted IP)
- [x] Test corpus: 10–15 small fixtures (plain Verilog, SV interfaces, a class,
      an unresolved instance)
- [x] Claim the `hdl-kgraph` name on PyPI with a 0.1 release (release workflow
      and docs/releasing.md are in; publishing is a maintainer action)

**Acceptance:** builds a graph from a real OSS design (e.g. ibex-class repo);
`tree` prints the correct hierarchy; ≥90% of fixture constructs extracted;
CI green on Python 3.10–3.13.

## M2 — v0.2: Real-world inputs — preprocessor, filelists, includes (MVP)

**Goal:** works on projects as they actually exist: `` `define ``/`` `ifdef `` soup,
`.f` filelists, include directories.

- [x] Filelist parser: `.f`/`.vc` (`+incdir+`, `+define+`, nested `-f`, `-y`/`-v`
      library dirs, env-var expansion); `FILELIST` nodes; file order preserved
      (`-y` dirs are recorded on the FILELIST node only; on-demand library
      module lookup is M3+ territory)
- [x] Lightweight SV preprocessor: `` `define `` (with arguments),
      `` `ifdef ``/`` `ifndef ``/`` `elsif `` branch selection from configured
      defines, `` `include `` resolution → `INCLUDES`/`DEFINES_MACRO`/`USES_MACRO`
      edges; line map back to original source for accurate spans —
      **`` `" `` stringification / ``` `` ``` pasting are best-effort textual,
      and macro arguments must close on the invocation line (documented)**
- [x] "Both branches" mode when no define set is given (emit both sides of
      `` `ifdef ``; the branch a define-less compile would select keeps full
      confidence, alternatives are emitted at 0.6 — this keeps `` `ifndef ``
      include guards and default-define fallbacks at 1.0)
- [x] Config file `hdl-kgraph.toml`: source globs, filelists, defines, include
      dirs, VHDL library map (carried; consumed in M3), top modules
- [x] CLI: `build -f tb.f`, `--define`, `--incdir`

**Acceptance:** builds cleanly from an unmodified vendor-style `.f` file;
macro-instantiated modules resolve after expansion; line mapping verified by tests.

## M3 — v0.3: VHDL + mixed-language designs (MVP)

**Goal:** first-class VHDL extraction and Verilog↔VHDL linking — completes the
"HDL" promise of the project name.

- [x] tree-sitter VHDL parser: `ENTITY`, `ARCHITECTURE` (+`IMPLEMENTS`),
      `VHDL_PACKAGE`/`PACKAGE_BODY`, `CONFIGURATION` (+`BINDS`),
      generics→`PARAMETER`, ports, signals, processes, component and direct
      entity instantiation — **grammar: `jpt13653903/tree-sitter-vhdl` (PyPI
      `tree-sitter-vhdl`); caveats in docs/grammar-bakeoff.md. Component
      declarations are deliberately not graph nodes: instantiations carry the
      style and the linker resolves through configuration/default binding**
- [x] Case-insensitive name normalization (original casing kept in attrs)
- [x] Library/work mapping (`--lib work=./src` style config); `LIBRARY` nodes;
      `USES_PACKAGE` edges; component-vs-entity binding resolution (specific
      label > `all` > `others`; `ieee`/`std` packages stay library-qualified
      stubs by design)
- [x] Cross-language pass-2 linking: VHDL component instantiating an SV module and
      vice versa (case-insensitive name match, capped at confidence 0.8 even
      within one file; vendor name-mangling caveats documented in README)
- [x] `tree` and `query` work across language boundaries (entities expand
      through their architectures, printed as `name(arch)`)

**Acceptance:** mixed Verilog-top/VHDL-leaf and VHDL-top/Verilog-leaf fixtures both
produce a single connected hierarchy; a VHDL configuration overriding a default
binding is honored.

## M4 — v0.4: Incremental updates, watch mode, impact analysis (MVP)

**Goal:** fast enough to live alongside an editor; answers "what does my change
affect?"

- [x] Content-hash incremental rebuild: `update` re-parses changed/added/removed
      files plus their preprocessor-dependent files (reverse `INCLUDES` /
      `USES_MACRO` closure; a changed `.f` define or incdir dirties all files in
      that filelist — via the build-options fingerprint, which falls back to a
      full rebuild), then re-links pass 2 — **per-unit pass-1 IRs (plus macro
      event logs) persist in the `file_irs` table; unchanged units re-link
      without re-parsing**
- [x] `watch` via watchdog (debounced); `detect-changes` (vs git HEAD or last build)
- [x] Impact radius: `impact <file|module>` → transitively affected modules via
      `INSTANTIATES`/`IMPORTS`/`INCLUDES`/`EXTENDS` (reverse `` `include `` and
      macro edges included — a header change dirties all users; VHDL
      `USES_PACKAGE`/`IMPLEMENTS`/`BINDS` covered too)
- [x] SQLite schema versioning + migration guard (schema v2; the database is a
      derived cache, so the migration path is a rebuild — read commands refuse
      with a clear message, `update`/`watch` fall back to a full rebuild)
- [x] Documented benchmark target: incremental update of 1 file in a 2k-file
      design < 1 s — **0.85 s measured; procedure and results in
      docs/benchmarks.md (`scripts/bench_incremental.py`)**

**Acceptance:** editing one file and running `update` re-parses only that file;
`impact` correctly flags parents/importers/includers in fixtures; watch mode
survives rapid save bursts.

## M5 — v0.5: HDL analyses + visualization

**Goal:** insights, not just structure.

- [x] Dataflow edges: `DRIVES`/`READS` from continuous assigns, always/process
      blocks, and instance port directions — **always blocks / assigns become
      PROCESS nodes (`always@<line>` / `assign@<line>`); refs resolve against
      the enclosing unit's PORT/SIGNAL children, never by global name;
      undeclared names become implicit SIGNAL stubs at ≤ 0.6; instance-level
      flow is derived from resolved CONNECTS bindings + port directions;
      `query drivers <signal>` pre-stages M6's `find_signal_drivers`**
- [x] `CLOCKED_BY`/`RESETS` extraction (sensitivity-list evidence = 1.0;
      name-pattern heuristic = 0.4) → clock-domain report, reset tree, and
      CDC-suspect crossings (signal driven in domain A, read in domain B) —
      **`query clock-domains` / `reset-tree` / `cdc`; clock nets alias-merge
      across the hierarchy through single-identifier port connections; VHDL
      `rising_edge()` is 1.0 evidence; combinational paths bridge one step
      (no fixpoint); synchronizers are not recognized — these are suspects,
      not violations (M10's SDC `set_clock_groups` is the planned suppressor)**
- [x] Lint-flavored analyses: unconnected/dangling ports, undriven/unread signals,
      never-instantiated modules (dead code), parameter overrides equal to defaults
      — **`hdl-kgraph lint [--check NAME] [--top NAME] [--json]`, always exits 0;
      signal checks skip parse-error files and implicit-net stubs; explicitly
      open `.x()` bindings reported separately from unconnected ports**
- [x] Graph metrics: module fan-in/fan-out, hub/bridge detection (betweenness),
      community detection (Louvain via NetworkX) for subsystem discovery —
      **`hdl-kgraph metrics [--limit N] [--communities]` over the module-level
      instantiation projection (entities absorb their architectures); Louvain
      seeded for run-to-run determinism; articulation points flag true bridges**
- [x] `visualize` → self-contained D3.js HTML (hierarchy view + force-directed
      view; filter by node kind, edge kind, clock domain) — **d3 v7 vendored
      (ISC; `viz/static/LICENSE.d3`) so the artifact opens air-gapped; the
      force view renders on canvas (SVG dies near 1k nodes) and defaults to
      the module projection, `--full` embeds everything; < 0.8-confidence
      edges drawn dashed**
- [x] SV verification constructs: `ASSERTION`/`PROPERTY`/`SEQUENCE`,
      `COVERGROUP`/`COVERPOINT`, `CONSTRAINT`, `CLOCKING_BLOCK` nodes;
      `ASSERTS_ON`/`COVERS` edges; UVM topology report (`EXTENDS` chains to
      `uvm_*` bases, `TEST_COVERS`) — **`query uvm`; `ASSERTS_ON` resolves to a
      sibling PROPERTY/SEQUENCE before signals; `TEST_COVERS` is a 0.4
      tb-name-pattern heuristic (tb tops → instantiated DUTs, uvm_test
      subclasses → the same DUTs); immediate (procedural) assertions deferred.
      Schema is v3 — pass-1 IRs changed, so the first `update` after upgrading
      falls back to one full rebuild**

**Acceptance:** the clock-domain report on a two-clock fixture identifies both
domains and the CDC point; visualization renders a 1k-node graph without freezing;
a UVM example testbench yields a component-tree report.

## M6 — v0.6: MCP server + AI-assistant integration

**Goal:** AI assistants can query the design directly.

- [x] fastmcp server (`hdl-kgraph serve --mcp`), shipped as the `[mcp]` extra —
      **lazy import with a clear install hint; the CI lint job intentionally
      runs without the extra so the core never grows a hard fastmcp dependency**
- [x] Tools: `find_module`, `get_hierarchy`, `who_instantiates`, `port_map`,
      `impact_of_change`, `clock_domains`, `find_signal_drivers`, `uvm_topology`,
      `search_nodes` — **thin wrappers over `graph/analysis.py` (the drivers
      query and impact-seed resolution moved out of the CLI; `port_map` and
      `search_nodes` are new analysis functions the CLI can reuse);
      `find_signal_drivers` takes the module scope the acceptance question
      needs, with VHDL architectures answering for their entities**
- [x] Read-only stdio and HTTP modes; responses sized for LLM context windows
      (pagination, summaries) — **stdio default, `--http HOST:PORT` for
      streamable HTTP; every list tool returns a
      `{total, offset, count, truncated, items}` envelope (limit clamped to
      500), hierarchy defaults to depth 3 with a 500-node cap, impact leads
      with a summary so truncated pages still answer "what breaks"; the server
      stats the database per call and reloads when `update`/`watch` rewrite it**
- [x] Docs: Claude Code / Claude Desktop configuration snippets — **docs/mcp.md:
      tool reference, transports, cold-checkout walkthrough**
- [x] `hdl-kgraph setup`: detect installed assistants and write their MCP
      config — **Claude Code via project-scope `.mcp.json`, Claude Desktop via
      its platform config file; idempotent merge that preserves other servers,
      one-time `.bak` backups for user-level files, `--list`/`--dry-run`/
      `--yes`; extensible one-entry-per-assistant registry in `mcp/setup.py`**

**Acceptance:** from a cold checkout, an AI assistant can answer "what drives
signal X in module Y" and "what breaks if I change this port" using MCP tools only.

**Post-M6 (does not gate MVP or v0.6):** visualization scalability for very
large designs — tiered precomputed-layout / aggregation / export strategy.
Phases 1–2 (canvas renderer hygiene; precomputed "static" layout tier with
`--layout auto|live|static` auto-routing) are delivered; Phases 3–6
(aggregation/drill-down, payload compression, GraphML/GEXF export, WebGL)
remain parked. Analysis and phased plan in
[docs/viz-scalability.md](docs/viz-scalability.md).

## M7 — v0.7: Semantic enrichment via native frontends (stretch)

**Goal:** elaboration-accurate facts where native parsers are available;
tree-sitter remains the always-works baseline.

- [ ] Enrichment plugin interface: parser backends declare capabilities; results
      merge with higher confidence
- [ ] pyslang backend (`[slang]` extra): true type/width resolution, parameterized
      generate elaboration, `defparam`, accurate symbol binding → edges upgraded
      to 1.0
- [ ] pyVHDLModel / GHDL analysis backend (`[ghdl]` extra): VHDL semantic and
      overload resolution
- [ ] Discrepancy report: where heuristic edges disagreed with elaboration

**Acceptance:** on fixtures with parameterized generates, instance counts match
elaborated reality; the tool still works with zero optional extras installed.

## M8 — v1.0: C/C++/Python boundary + API stability (stretch)

**Goal:** the full system picture — DPI, cosim, testbench scripting.

- [ ] DPI-C linking: SV `import "DPI-C"`/`export "DPI-C"` ↔ C/C++ function
      definitions (tree-sitter-c/cpp) via `FOREIGN_BINDS` edges
- [ ] Python testbench scanning: cocotb `dut.signal` attribute access →
      `READS`/`DRIVES` (confidence 0.6); pytest/cocotb test discovery →
      `TEST_COVERS`
- [ ] Stable public Python API (`hdl_kgraph.api`), semver commitment, schema
      freeze with documented migration policy
- [ ] PyPI 1.0 release; documentation site

**Acceptance:** a cocotb-driven SV design with DPI-C calls shows one connected
graph spanning all three languages.

## M9 — v1.x: Emerging HDLs (stretch)

**Goal:** Chisel/FIRRTL, Amaranth, SpinalHDL, Bluespec support.

- [ ] Pragmatic first step: parse their **generated Verilog** and link back to
      sources via emitted locators (Chisel `// @[Foo.scala 42:11]`, SpinalHDL
      comments, FIRRTL source locators) → `GENERATED_FROM` edges
- [ ] Direct FIRRTL parsing as a second step (FIRRTL is a small, well-specified IR)
- [ ] Amaranth: Python AST scan of `m.submodules` structure
- [ ] Each generator shipped as an optional extra

## M10 — v1.x: EDA flow languages — Tcl/SDC/UPF, Perl, SLN (stretch)

**Goal:** capture the flow *around* the RTL: timing constraints as authoritative
clock evidence, power intent, legacy script codegen lineage, and portable-stimulus
scenario coverage.

- [ ] SDC/XDC parsing (Tcl subset): `create_clock`/`create_generated_clock` →
      `CLOCK` nodes (virtual and generated clocks supported); `set_false_path`,
      `set_multicycle_path`, `set_input_delay`/`set_output_delay`,
      `set_clock_groups` → `TIMING_CONSTRAINT` nodes with `CONSTRAINS` edges;
      `get_ports`/`get_pins`/`get_cells`/`get_clocks` object queries resolved to
      design nodes (exact match 1.0; glob patterns 0.8/0.6)
- [ ] M5 synergy: `create_clock` is authoritative `CLOCKED_BY` evidence — upgrades
      the 0.4 name heuristic to 1.0; `set_clock_groups -asynchronous` and
      `set_false_path` feed the CDC report as declared-safe crossings
- [ ] UPF (IEEE 1801) power intent: `create_power_domain` → `POWER_DOMAIN` nodes
      with `CONSTRAINS` edges to their elements; supply nets/sets and isolation/
      retention/level-shifter strategies in attrs; power-domain report (domains,
      strategies, domain-crossing suspects) analogous to the CDC report
- [ ] Tcl flow scripts: `read_verilog`/`read_vhdl`/`analyze`/`add_files` →
      `REFERENCES_FILE` edges; `source` chains → `INCLUDES`; literal `set`
      variable substitution only — Tcl is never evaluated (see Risks)
- [ ] Perl legacy scripting: detect HDL files a script reads/writes/generates
      (`open()` of `.v`/`.sv` paths, heredoc-embedded Verilog) →
      `REFERENCES_FILE` + `GENERATED_FROM` lineage for generated RTL;
      expectations modest — codegen patterns, not Perl semantics
      (tree-sitter-perl exists if needed)
- [ ] SLN (Cadence Perspec System Level Notation) portable stimulus:
      actions/scenarios/resources → `SCENARIO`/`ACTION` nodes; scenario → DUT
      linkage via `TEST_COVERS`; Accellera PSS (`.pss`), the open sibling format,
      is the natural follow-on
- [ ] `.sln` disambiguation: content-sniff the Visual Studio solution header and
      skip non-SLN files
- [ ] Fixtures: an SDC and a UPF for the counter fixtures, a flow `.tcl`, a Perl
      heredoc codegen script, a minimal SLN scenario

**Acceptance:** an SDC on the two-clock M5 fixture upgrades both clock domains to
confidence 1.0 and the declared false path suppresses the CDC suspect; the UPF
fixture yields a power-domain report listing the domain and its isolated
instances; the Perl codegen fixture yields a `GENERATED_FROM` edge from its
emitted Verilog; the SLN scenario fixture links to the DUT module via
`TEST_COVERS`; a Visual Studio `.sln` is recognized and skipped.

---

## Risks

1. **SystemVerilog tree-sitter grammar quality — the #1 risk.** The original
   `tree-sitter/tree-sitter-verilog` grammar covers IEEE 1800 poorly (classes,
   constraints, assertions, and many SV-2017 constructs produce ERROR nodes). The
   actively maintained `gmlarumbe/tree-sitter-systemverilog` (used by Emacs
   `verilog-ts-mode`, validated against the sv-tests suite) is the strong
   candidate and can serve both `.v` and `.sv`. Mitigation: the grammar choice is
   isolated behind `parser/base.py`, so swapping costs one module; the first M1
   task is a grammar bake-off against the fixture corpus.
2. **VHDL grammar:** `alemuller/tree-sitter-vhdl` is unmaintained;
   `jpt13653903/tree-sitter-vhdl` is the maintained option. Case-insensitivity and
   library/work scoping must be handled in *our* layer, not the grammar.
3. **The preprocessor is the hard problem.** tree-sitter cannot expand macros;
   heavily `` `ifdef ``'d code parses to garbage, and macro-defined module bodies
   are invisible without expansion. M2's preprocessor with line mapping is the
   difference between a toy and real-world usability — budget it generously.
4. **No elaboration in the tree-sitter tier:** parameterized generates,
   `defparam`, configurations, and instance arrays mean instance counts/bindings
   are approximations until M7. Confidence scoring is the honest contract with
   users — document it prominently.
5. **py-tree-sitter API churn:** the 0.21→0.23 transition broke the `Language`
   constructor and query APIs. Pin the floor at the version actually coded
   against; CI on 3.13 catches wheel-availability gaps.
6. **PyPI name:** verify `hdl-kgraph` is unclaimed and claim it early with a
   placeholder release.
7. **Real-world inputs:** encrypted IP (`` `pragma protect ``), megabyte-scale
   generated netlists, and vendor primitive libraries can hang naive parsers —
   file-size guards and exclude globs ship in M1.
8. **Tcl and Perl are full programming languages** — static extraction is
   best-effort by design. SDC and UPF are constrained Tcl subsets and tractable;
   arbitrary flow scripts (loops, procs, `eval`) are out of scope, with only
   literal `set` substitution attempted. Confidence scoring and
   `REFERENCES_FILE` attrs are the honest contract.
9. **`.sln` name collision and proprietary syntax:** `.sln` is overwhelmingly
   Visual Studio solution files in the wild — content sniffing is mandatory
   before parsing. Perspec SLN has no public grammar; the parser targets the
   documented subset, with Accellera PSS (`.pss`, openly specified) as the
   safer long-term sibling target.
