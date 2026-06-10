# hdl-kgraph Roadmap

`hdl-kgraph` builds a queryable knowledge graph from hardware description language
(HDL) source code — SystemVerilog, Verilog, and VHDL first, with C/C++/Python and
emerging HDLs (Chisel, Amaranth, SpinalHDL) as future targets. The architecture is
modeled on [code-review-graph](https://github.com/tirth8205/code-review-graph):
Python 3.10+, tree-sitter parsing, NetworkX graph algorithms, SQLite persistence,
a CLI, and (later) an MCP server for AI assistants. Distribution is via pip/PyPI.

**MVP line:** Milestones M1–M4. M5–M6 are value-add. M7–M9 are stretch goals.

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
| `TEST_COVERS` | testbench/UVM test → DUT module |
| `FOREIGN_BINDS` | SV DPI-C import/export ↔ C function (M8) |
| `GENERATED_FROM` | generated Verilog → Chisel/Amaranth/SpinalHDL source (M9) |

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
  declarations and unresolved references. Embarrassingly parallel.
- **Pass 2 (link):** cross-file resolution — instance→definition, package imports,
  VHDL library/work scoping, bind/configuration resolution — with confidence scoring.

This split makes incremental updates (M4) cheap: re-run pass 1 only for changed
files, then re-run the fast global pass 2.

---

## M1 — v0.1: SystemVerilog/Verilog structural graph (MVP)

**Goal:** `pip install -e . && hdl-kgraph build ./rtl` produces a queryable design
hierarchy graph for a Verilog/SystemVerilog codebase.

- [ ] Schema module: `NodeKind`/`EdgeKind` enums, `Node`/`Edge` dataclasses,
      confidence convention documented in docstrings
- [ ] Grammar bake-off: evaluate `gmlarumbe/tree-sitter-systemverilog` vs
      `tree-sitter/tree-sitter-verilog` against the fixture corpus; pick one grammar
      for both `.v` and `.sv` (see Risks)
- [ ] tree-sitter SV parser extracting: `MODULE`, `INTERFACE`, `PACKAGE`, `PROGRAM`,
      `FUNCTION`/`TASK`, `PORT`, `PARAMETER`, `INSTANCE`, `TYPEDEF`/`STRUCT`/`ENUM`,
      `CLASS` (declaration + `EXTENDS` only)
- [ ] Edges: `DECLARES`, `INSTANTIATES`, `CONNECTS` (named + positional),
      `PARAMETERIZES`, `IMPORTS`, `EXTENDS`
- [ ] Pass-2 linker with confidence scoring and unresolved stub nodes
- [ ] NetworkX in-memory graph + SQLite persistence (`nodes`, `edges`, `files`
      tables; content-hash column added now for M4)
- [ ] CLI: `build`, `status`, `query` (e.g. `hdl-kgraph query instances-of fifo`),
      `tree` (print design hierarchy from a top module)
- [ ] Error tolerance: files with tree-sitter ERROR nodes still yield partial
      results; parse-error count surfaces in `status`
- [ ] File-size guards and exclude-glob config (huge generated netlists,
      `` `pragma protect `` encrypted IP)
- [ ] Test corpus: 10–15 small fixtures (plain Verilog, SV interfaces, a class,
      an unresolved instance)
- [ ] Claim the `hdl-kgraph` name on PyPI with a 0.1 release

**Acceptance:** builds a graph from a real OSS design (e.g. ibex-class repo);
`tree` prints the correct hierarchy; ≥90% of fixture constructs extracted;
CI green on Python 3.10–3.13.

## M2 — v0.2: Real-world inputs — preprocessor, filelists, includes (MVP)

**Goal:** works on projects as they actually exist: `` `define ``/`` `ifdef `` soup,
`.f` filelists, include directories.

- [ ] Filelist parser: `.f`/`.vc` (`+incdir+`, `+define+`, nested `-f`, `-y`/`-v`
      library dirs, env-var expansion); `FILELIST` nodes; file order preserved
- [ ] Lightweight SV preprocessor: `` `define `` (with arguments),
      `` `ifdef ``/`` `ifndef ``/`` `elsif `` branch selection from configured
      defines, `` `include `` resolution → `INCLUDES`/`DEFINES_MACRO`/`USES_MACRO`
      edges; line map back to original source for accurate spans
- [ ] "Both branches" mode when no define set is given (emit both sides of
      `` `ifdef `` at confidence 0.6)
- [ ] Config file `hdl-kgraph.toml`: source globs, filelists, defines, include
      dirs, VHDL library map, top modules
- [ ] CLI: `build -f tb.f`, `--define`, `--incdir`

**Acceptance:** builds cleanly from an unmodified vendor-style `.f` file;
macro-instantiated modules resolve after expansion; line mapping verified by tests.

## M3 — v0.3: VHDL + mixed-language designs (MVP)

**Goal:** first-class VHDL extraction and Verilog↔VHDL linking — completes the
"HDL" promise of the project name.

- [ ] tree-sitter VHDL parser: `ENTITY`, `ARCHITECTURE` (+`IMPLEMENTS`),
      `VHDL_PACKAGE`/`PACKAGE_BODY`, `CONFIGURATION` (+`BINDS`),
      generics→`PARAMETER`, ports, signals, processes, component and direct
      entity instantiation
- [ ] Case-insensitive name normalization (original casing kept in attrs)
- [ ] Library/work mapping (`--lib work=./src` style config); `LIBRARY` nodes;
      `USES_PACKAGE` edges; component-vs-entity binding resolution
- [ ] Cross-language pass-2 linking: VHDL component instantiating an SV module and
      vice versa (name match, confidence 0.8; vendor name-mangling caveats
      documented)
- [ ] `tree` and `query` work across language boundaries

**Acceptance:** mixed Verilog-top/VHDL-leaf and VHDL-top/Verilog-leaf fixtures both
produce a single connected hierarchy; a VHDL configuration overriding a default
binding is honored.

## M4 — v0.4: Incremental updates, watch mode, impact analysis (MVP)

**Goal:** fast enough to live alongside an editor; answers "what does my change
affect?"

- [ ] Content-hash incremental rebuild: `update` re-parses only
      changed/added/removed files, then re-links pass 2
- [ ] `watch` via watchdog (debounced); `detect-changes` (vs git HEAD or last build)
- [ ] Impact radius: `impact <file|module>` → transitively affected modules via
      `INSTANTIATES`/`IMPORTS`/`INCLUDES`/`EXTENDS` (reverse `` `include `` and
      macro edges included — a header change dirties all users)
- [ ] SQLite schema versioning + migration guard
- [ ] Documented benchmark target: incremental update of 1 file in a 2k-file
      design < 1 s

**Acceptance:** editing one file and running `update` re-parses only that file;
`impact` correctly flags parents/importers/includers in fixtures; watch mode
survives rapid save bursts.

## M5 — v0.5: HDL analyses + visualization

**Goal:** insights, not just structure.

- [ ] Dataflow edges: `DRIVES`/`READS` from continuous assigns, always/process
      blocks, and instance port directions
- [ ] `CLOCKED_BY`/`RESETS` extraction (sensitivity-list evidence = 1.0;
      name-pattern heuristic = 0.4) → clock-domain report, reset tree, and
      CDC-suspect crossings (signal driven in domain A, read in domain B)
- [ ] Lint-flavored analyses: unconnected/dangling ports, undriven/unread signals,
      never-instantiated modules (dead code), parameter overrides equal to defaults
- [ ] Graph metrics: module fan-in/fan-out, hub/bridge detection (betweenness),
      community detection (Louvain via NetworkX) for subsystem discovery
- [ ] `visualize` → self-contained D3.js HTML (hierarchy view + force-directed
      view; filter by node kind, edge kind, clock domain)
- [ ] SV verification constructs: `ASSERTION`/`PROPERTY`/`SEQUENCE`,
      `COVERGROUP`/`COVERPOINT`, `CONSTRAINT`, `CLOCKING_BLOCK` nodes;
      `ASSERTS_ON`/`COVERS` edges; UVM topology report (`EXTENDS` chains to
      `uvm_*` bases, `TEST_COVERS`)

**Acceptance:** the clock-domain report on a two-clock fixture identifies both
domains and the CDC point; visualization renders a 1k-node graph without freezing;
a UVM example testbench yields a component-tree report.

## M6 — v0.6: MCP server + AI-assistant integration

**Goal:** AI assistants can query the design directly.

- [ ] fastmcp server (`hdl-kgraph serve --mcp`), shipped as the `[mcp]` extra
- [ ] Tools: `find_module`, `get_hierarchy`, `who_instantiates`, `port_map`,
      `impact_of_change`, `clock_domains`, `find_signal_drivers`, `uvm_topology`,
      `search_nodes`
- [ ] Read-only stdio and HTTP modes; responses sized for LLM context windows
      (pagination, summaries)
- [ ] Docs: Claude Code / Claude Desktop configuration snippets

**Acceptance:** from a cold checkout, an AI assistant can answer "what drives
signal X in module Y" and "what breaks if I change this port" using MCP tools only.

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
