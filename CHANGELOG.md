# Changelog

All notable changes to **hdl-kgraph** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
As of `1.0`, the public CLI and graph schema are stable: breaking changes bump
the major version, and schema changes ship with a migration.

## [Unreleased]

## [2.0.0] - 2026-06-21

**v2.0 — the out-of-core, bounded-RAM architecture, delivered without a Rust core.**

The v2.0 goal (issue #128) was to break the in-memory-graph RAM ceiling so 10–100 GB designs stay
usable — originally envisioned as a bespoke PyO3 Rust core. Profiling and spikes (M11/M12) showed an
off-the-shelf out-of-core layer clears the wall, so v2 instead landed **incrementally, behind the
existing Python `storage` seam**, as backward-compatible releases 1.8 → 1.15: bounded index-backed
reads (`GraphQuery`), out-of-core whole-design summaries (M12.5: 1.9/1.10), and the memory-bounded
incremental linker — bounded re-link as the default (1.12/1.13), selective IR decode (1.14), and
out-of-core TEST_COVERS re-derivation (1.15). Reads, summaries, linker re-resolution, IR decode, and
TEST_COVERS are now all bounded by the dirty closure / structural subgraph; a 100 GB design loads via
the out-of-core path. **The Rust core (M13) is deferred — off the critical path for the RAM goal.**
This release marks v2 delivered; the one breaking change below is what tips the version to 2.0.0.

### Changed (breaking)

- The CLI whole-design report commands now answer from the **bounded out-of-core path**
  (`GraphQuery`), never `SqliteStore.load()` — completing the M12.5 routing (the MCP tools already
  did). As a result their output aligns with the bounded summary payload the MCP server serves:
  - **`query clock-domains --json`** now emits the summary payload
    `{"domains": [...], "cdc_suspect_count": N, "cdc_suspects": [...]}` (each domain carries
    `clock`/`aliases`/`process_count`/`signal_count`/`min_confidence`) **instead of** the previous
    list of full `ClockDomain` objects with their O(design) `process_ids`/`signal_ids` arrays. The
    text report labels each domain by its **clock net name** (+ aliases), not the node's
    `qualified_name`.
  - **`query cdc`** is now bounded to the **top-50** suspects (matching the persisted summary); its
    text output preserves `read by <qualified_name>` via a bounded id→name lookup.
  - **`query uvm`** is unchanged (byte-identical text and `--json`).
  `reset-tree` and the single-target queries (`instances-of`/`modules`/`drivers`/`unresolved`) are
  unchanged.

## [1.15.0] - 2026-06-21

### Fixed

- **TEST_COVERS edges on incremental `update`** (#119). TEST_COVERS is a cross-file relation — a
  `tb_*` top / `uvm_test` class covers DUT modules anywhere in the design — so the src-scoped delta
  write could not keep it consistent: the bounded (default) re-link never re-derived it (dropping a
  tb-top's coverage edges on edit), and the in-memory path derived it in-graph but the scoped write
  silently dropped the edges whose src lay outside the dirty closure. Both paths now re-derive the
  whole TEST_COVERS set **out-of-core** after the scoped write (`storage.summaries.test_covers_sql`
  hydrates only the structural subgraph — MODULE/ENTITY/INSTANCE/CLASS nodes + DECLARES/
  INSTANTIATES/EXTENDS edges, never the dataflow bulk — and runs the *same* `derive_test_covers`),
  then reconcile it (`SqliteStore.replace_test_covers`). UVM/testbench designs stay bounded and the
  result is **byte-identical** to a full `build`. Pure-SV designs (no `tb_*`/`uvm_test`) are
  unaffected. The equivalence + fuzz suite now includes UVM edit shapes under both link paths.

## [1.14.0] - 2026-06-21

### Changed

- **Selective IR decode** on the bounded (default) `update` path (#119) clears the last
  O(design)-RAM step. `update` no longer decodes *every* clean unit's stored IR: clean units are
  replayed from the small `macro_events` column only (the compile-order prerequisite for dirty
  re-parses), and just the dirty units plus the *affected* clean units the bounded linker
  re-resolves have their full IR blob decoded. The resident IR set is now O(dirty closure), not
  O(design), so the whole `update` pipeline — reads, summaries, linker re-resolution, and IR
  decode — is bounded without a Rust core. The result stays **byte-identical** to a full `build`
  (the equivalence + fuzz suite runs over both link paths). Bind/configuration directives still
  need every unit's IR, so that case transparently retries with the full-decode path;
  `--no-bounded-link`, VHDL, and enrich keep the previous full-decode flow. See
  [docs/scalability.md](docs/scalability.md).

## [1.13.0] - 2026-06-20

### Changed

- The memory-bounded incremental re-link (#119) is now the **default** for `hdl-kgraph update`:
  an incremental `update` re-resolves the dirty closure straight from SQLite instead of loading
  the whole prior graph, removing the last O(design)-RAM step from the common `update` path. The
  result is byte-identical to a full `build` (the equivalence + fuzz suite runs over both paths).
  Pass `--no-bounded-link` to fall back to the previous in-memory re-link. VHDL / binds / enrich
  still fall back to a full re-link regardless. (Introduced opt-in as `--bounded-link` in 1.12.0.)

### Added

- `hdl-kgraph update --bounded-link` (opt-in, experimental) re-links incrementally **without
  loading the whole prior graph** (#119). It re-resolves the dirty closure straight from SQLite —
  the unchanged resolution engine fed by lazy `idx_nodes_kind_name`/`idx_edges_*` lookups, with a
  bounded stub-GC over only the stub neighbourhood — and writes the same scoped delta. The default
  `update` path is unchanged; the result is **byte-identical** to a full `build`, now pinned by
  `tests/test_incremental_equivalence.py` parametrized over **both** link paths (including the
  randomized fuzz). This removes the last O(design)-RAM step from `update` on the opt-in path; a
  later release will flip it to the default. See [docs/scalability.md](docs/scalability.md).

## [1.11.0] - 2026-06-20

### Added

- `hdl-kgraph bench-link [--json] [--sample N]` reports **incremental-link locality** — how many
  pass-2 references a single-file edit re-resolves vs a full re-link, as a content-free
  distribution (`reresolved_refs` and `locality_ratio` p50/p90/max). Computed from a built
  `graph.db` alone (the persisted `ref_index` + include/macro dependency graph), so it runs
  post-install with no source tree; a low ratio quantifies how much a memory-bounded incremental
  linker (#119) would save on a given design. The byte-identical correctness of an actual bounded
  re-link is validated separately by the M13 spike (`scripts/spike_m13_link.py`,
  [docs/v2/m13_link_spike.md](docs/v2/m13_link_spike.md)).

## [1.10.0] - 2026-06-20

### Changed

- UVM-topology reports are now served **out-of-core** when the persisted whole-design
  summary is absent — the companion change to 1.9.0's clock/CDC fallback. Previously a
  database with no `uvm_topology` summary fell back to loading the **entire** graph into
  memory to recompute it. `GraphQuery.uvm_topology()` now hydrates only the bounded class
  subgraph (CLASS nodes + EXTENDS/TEST_COVERS edges) and runs the same analysis on it
  (`storage/summaries.py`), with results byte-identical to the NetworkX path. Both
  whole-design summaries (clock/CDC and UVM) are now bounded.

## [1.9.0] - 2026-06-20

### Changed

- Clock-domain / CDC reports are now served **out-of-core** when the persisted whole-design
  summary is absent. Previously a database with no `clock_domains` summary (one migrated from
  a pre-v8 schema, or any build that did not persist it) fell back to loading the **entire**
  graph into memory to recompute the report — the O(design)-RAM wall. The `GraphQuery` reader
  now computes it directly from SQLite instead (`storage/summaries.py`), with results
  byte-identical to the NetworkX path. UVM topology keeps the full-load fallback for now.

## [1.8.0] - 2026-06-20

### Added

- `hdl-kgraph review [--json] [--metrics]` emits a **content-free review digest** of a
  built graph — counts, ratios, distributions, and build timings, with **no identifiers**
  (no module/clock/signal names, file paths, or expression text). It's designed to be
  snapshotted out of an isolated/air-gapped environment (where the source and `graph.db`
  cannot leave) and **diffed across builds** to review parse health, link quality, design
  shape, and performance. The digest consolidates the `meta`/`files` tables, node/edge-kind
  histograms, unresolved-stub ratio, edge-confidence distribution, and the persisted
  clock/CDC/UVM summaries as counts; `--metrics` adds fan-in/hub/community metrics (values
  only). See [docs/review.md](docs/review.md).
- `build`/`update` now persist content-free build telemetry (`build_stats`: per-phase
  timings + the `enriched` flag) into the `meta` table, so `review` can report `timings_s`
  from a static database. Databases built before this release simply report `timings_s:
  null` (no migration needed — `meta` is key/value).

## [1.7.0] - 2026-06-19

### Added

- `hdl-kgraph merge DB1 DB2 ... --db OUT` assembles several independently-built
  block databases into one SoC-level graph (IP-block assembly). It unions the
  per-file IRs across the sources and re-links once, so the result is
  byte-identical to a monolithic `build` of the same files under the same
  `--root` (Mode A). All sources must share the build root; FILELIST and VHDL
  `library` adapter nodes are reconstructed faithfully from each source graph.
  `--on-conflict error|first|last` controls overlapping files that differ.
  Enriched source databases are refused (enrich the merged design as a
  whole-design step instead), and a merged database falls back to a full
  rebuild on `update`. See [docs/merge-design.md](docs/merge-design.md).
- **Subtree caching** workflow on top of `merge`: keep each block's database as
  a cached artifact, rebuild only the block that changed, and re-merge — the
  unchanged blocks' cached per-file IRs are reused instead of being re-parsed,
  so the only parse cost paid is for the changed block while the pass-2 link is
  paid once. `merge` now reports its link/total wall-clock, and
  `scripts/bench_merge.py` measures the re-parse-only-the-changed-block payoff
  (see [docs/benchmarks.md](docs/benchmarks.md) and
  [docs/merge-design.md](docs/merge-design.md)).

## [1.6.0] - 2026-06-19

### Removed

- The 1.5.0 instance-body deduplication in `build --enrich` (and its
  `walk_bodies` timing line) is removed: measured on two real designs it never
  fired. slang canonicalizes identical instance bodies in C++, but pyslang
  returns a fresh wrapper object per `.body` access, so identity-based dedup
  finds no shared bodies (`walk_instances == unique bodies`, 1.0x, on both a
  small CPU block and a multi-million-instance SoC). Outputs were always
  identical; the change was simply inert, so it is reverted to keep the walk
  honest. The pass-3 profiling (`slang/walk_*`, `walk_instances`) from 1.4.0 is
  retained. See [docs/benchmarks.md](docs/benchmarks.md).

## [1.5.0] - 2026-06-19

### Changed

- Enrichment (`build --enrich`) skips re-descending into instance bodies it has
  already walked. slang canonicalizes identical instance bodies (same module +
  parameters share one body), but the pass-3 elaborated-tree walk previously
  re-walked every duplicate — the dominant build cost on unroll-heavy designs
  (a wide instance array walked the same body once per element). The walk now
  descends into each unique body once and records the rest at their parent
  level; output is unchanged (the `children` map is keyed by definition and
  folded by max, and parameterized specializations keep distinct bodies). The
  `--timings` breakdown gains a `walk_bodies` line (unique bodies + dedup
  factor). See [docs/benchmarks.md](docs/benchmarks.md).

## [1.4.0] - 2026-06-19

### Added

- `build --enrich --timings` now splits the dominant `slang/walk_tree` phase
  into `slang/walk_members` (forcing slang's lazy member elaboration) and
  `slang/walk_hierpath` (reconstructing each instance's `hierarchicalPath`), and
  reports `walk_instances` — the count of elaborated instances visited with the
  derived per-instance cost. This pinpoints whether the elaborated-tree walk is
  super-linear (rising cost per instance) and which term to optimize. Measured
  with bare `perf_counter` accumulators so the per-node instrumentation does not
  distort the hot loop. See [docs/benchmarks.md](docs/benchmarks.md).

## [1.3.0] - 2026-06-18

### Added

- `build --enrich --timings` now breaks the `enrich (pass 3)` line into its
  internal phases (slang parse / `getRoot` elaboration / elaborated-tree walk /
  summarize / graph delta-apply), so it is clear which part of elaboration
  dominates. Collected by a near-free `perf_counter` profiler on the real code
  path (`hdl_kgraph.enrich._profile`). See [docs/benchmarks.md](docs/benchmarks.md).
- [docs/merge-design.md](docs/merge-design.md): design proposal for a
  `hdl-kgraph merge` command (IP-block assembly + subtree caching), scoped from
  the `--timings` evidence — merge the per-file IRs and re-link once, with
  enrichment kept as a post-merge whole-design step.

## [1.2.0] - 2026-06-17

### Added

- `build`/`update`/`watch` gain `--allow-outside-root`: an opt-in flag that
  honors filelist source/`-v`/`-y`/`-f` and `+incdir+` tokens resolving outside
  the build root instead of dropping them. The default keeps the #68
  containment (out-of-tree tokens dropped with a warning); use the flag only
  with filelists you trust.
- `build --timings`: prints a per-phase wall-clock breakdown (discover, parse
  [pass 0+1], link [pass 2], enrich, persist) plus a parallelizable-vs-serial
  summary. A capacity-planning aid for deciding whether a distributed build +
  database merge would pay off — the discover+parse work is per-partition
  parallelizable, while the pass-2 link is paid once over the combined graph.
  See [docs/benchmarks.md](docs/benchmarks.md).

## [Released - pypi]

## [1.1.0] - 2026-06-16

### Added

- `hdl-kgraph tools` command group: the nine MCP tools (`find-module`,
  `hierarchy`, `who-instantiates`, `port-map`, `impact`, `clock-domains`,
  `find-signal-drivers`, `uvm-topology`, `search-nodes`) as plain commands that
  print the same JSON envelope to stdout. For environments where MCP cannot be
  configured: an agent can shell out instead. Uses the bounded, index-backed
  reader (not a full-graph load), so it stays fast on large designs, and needs
  only the base install — no `[mcp]` extra. See [docs/mcp.md](docs/mcp.md).
- `hdl-kgraph setup` now also seeds each detected assistant's instruction file
  (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, a Cursor/Windsurf rule, or
  `.github/copilot-instructions.md`) with notes on querying the graph — telling
  the assistant to prefer the graph over grepping raw RTL and documenting both
  the MCP tools and the `hdl-kgraph tools` CLI fallback. The notes live in a
  managed `<!-- hdl-kgraph:start -->`…`<!-- hdl-kgraph:end -->` block (rewritten
  in place, surrounding content preserved); `--no-instructions` skips it.

## [1.0.1] - 2026-06-16

### Changed

- Documentation updated for the v1 release: the README status, `CONTRIBUTING`,
  and the changelog versioning note now describe the project as stable rather
  than alpha, and the roadmap no longer pins v1.0 to the deferred M8
  C/C++/Python boundary work.
- PyPI `Development Status` classifier moved from `3 - Alpha` to
  `5 - Production/Stable`.

## [1.0.0] - 2026-06-15

First stable release. v1.0 is a stability/API-freeze baseline on top of the
0.16.1 surface — no new features land in this version; it marks milestones
M1–M7 as delivered with a stable CLI and graph schema.

### Delivered (M1–M7)

- **Extraction.** SystemVerilog/Verilog and VHDL parsing (tree-sitter,
  `ERROR`-tolerant) with mixed-language pass-2 linking; design hierarchy, port
  connectivity, parameters, packages, class/UVM inheritance, and the
  clock/reset/CDC dataflow graph, each cross-file edge carrying a confidence
  score.
- **Real-world build inputs.** `.f` filelists, the SV preprocessor
  (defines, includes, `` `ifdef `` branch selection), VHDL library mapping, and
  `hdl-kgraph.toml` config, with per-file diagnostics.
- **Incremental rebuilds.** `update`/`watch` re-parse and re-link only the
  changed files and their dependents; `detect-changes`/`impact` answer "what
  changed and what does it affect?" in CI.
- **Analyses & visualization.** Clock domains, reset trees, CDC suspects,
  signal drivers/readers, UVM topology, graph lint, fan-in/out and hub/bridge
  metrics, plus a self-contained interactive HTML visualization.
- **MCP server.** `setup`/`serve` expose read-only, paginated tools so AI
  assistants can query the design.
- **Semantic enrichment.** Opt-in `build --enrich` overlays the pyslang and
  GHDL frontends for elaborated precision, with a discrepancy report.

### Stability

- Scalability hardening for 10–100+ GB designs: bounded, index-backed reads,
  precomputed whole-design summaries, and dirty-closure-scoped incremental
  writes.
- Stable public CLI with a unified exit-code / empty-result contract, and a
  versioned SQLite schema with a migration ladder (no forced full re-parse on
  a schema bump).

## [0.16.1] - 2026-06-15

### Fixed

- `serve --http` now parses IPv6 `[host]:port` addresses (e.g. `[::1]:8123`)
  instead of mangling them, and rejects out-of-range ports (only `1`–`65535`).
- `setup --db <path>` validates the database exists before writing assistant
  configs, matching `serve`, so a typo no longer points assistants at a missing
  database. (Both were pre-existing issues surfaced while reviewing the CLI split.)

## [0.16.0] - 2026-06-15

### Changed

- Split the 1.8k-LOC `cli/main.py` god module into focused submodules
  (`cli/_options.py`, `cli/_common.py`, `cli/build.py`, `cli/query.py`,
  `cli/analyze.py`, `cli/serve.py`); `cli/main.py` is now a ~70-line assembler
  that registers the commands. Entry points and the `main`/`_ProgressRenderer`
  import paths are unchanged. No behavior change — completes #70 ([#70]).

## [0.15.0] - 2026-06-15

### Changed

- Move the graph traversals that were inlined in CLI command handlers into
  `graph.analysis` (`resolve_unit`, `instantiation_count`, `node_kind_histogram`,
  `edge_kind_histogram`) and share one JSON/pagination renderer (`cli.render`)
  between the CLI and the MCP server, so neither the `status`/`modules`/`tree`
  commands nor the MCP tools re-implement graph logic or serialization. No
  behavior change ([#70]).
  *(First step of #70; the per-command file split of `cli/main.py` remains.)*

## [0.14.0] - 2026-06-15

### Changed

- Extract a shared `_WalkerBase` for the SystemVerilog and VHDL tree-sitter
  parsers (node-text/child helpers, the dispatch-driven `visit`, and parse-error
  counting), so cross-cutting traversal fixes are made once instead of twice.
  The ERROR-node policy is now an explicit, documented `ERROR_POLICY` per
  language (`skip` for SV, `descend` for VHDL) rather than two independently
  drifting `visit` implementations ([#72]).

## [0.13.1] - 2026-06-15

### Added

- Tests for previously-unexercised degradation paths: a corrupt stored IR row
  falling back to a fresh parse, an internal parser-walker exception being
  caught, non-UTF-8 sources tolerated via `errors="replace"`, and pass-1
  `FileIR`/`UnresolvedRef` pickling (the cross-process worker contract) ([#75]).
- Branch coverage (`[tool.coverage.run] branch = true`) so resilience/fallback
  branches count toward the gate instead of being half-covered by a line touch,
  and the CI test leg installs the `layout` extra (numpy/scipy) so those viz
  branches run instead of silently skipping ([#75]).

### Fixed

- Harden the VHDL-enrichment test skip-guard: `find_spec("pyGHDL.libghdl")`
  raises `ModuleNotFoundError` when the binary is present but the bindings are
  not (e.g. a distro `ghdl` package), so guard it instead of letting collection
  fail ([#75]).

## [0.13.0] - 2026-06-15

### Added

- Parsers now validate the loaded tree-sitter grammar at construction
  (`validate_grammar` / `GrammarMismatchError`): if the grammar is missing a
  node type the SystemVerilog/VHDL walker dispatches on, hdl-kgraph fails loudly
  with an actionable message instead of silently under-extracting after an
  upstream grammar rename ([#71]).

### Fixed

- Keep `parse_error_count` honest for the SystemVerilog parameter, typedef,
  instantiation, and package-import subtrees: these handlers consume their
  subtree without re-dispatching, so syntax errors inside them were previously
  uncounted ([#71]).

## [0.12.0] - 2026-06-15

### Changed

- Unify the CLI exit-code contract so scripts and CI can rely on it (`git
  diff --exit-code` style): `0` success — including an empty report; `1` a
  documented negative result (`detect-changes` found changes, or a name lookup
  matched nothing); `2` any error. Application/usage errors now exit `2`
  (previously `1`). The policy is documented in `hdl-kgraph --help` ([#73]).

### Fixed

- `query drivers --json` now exits `1` (not `0`) when the signal matches
  nothing, matching its text mode and `query instances-of` ([#73]).
- `build`/`update` convert an unexpected pipeline failure into a clean exit-`2`
  error instead of leaking a raw traceback, and `update` no longer trips a bare
  `assert` when it produces no build report ([#73]).

## [0.11.0] - 2026-06-15

### Added

- SQLite schema migration ladder: `update`/`watch` now upgrade an older database
  in place when a registered, additive, IR-compatible step exists (e.g. the
  `v7 → v8` summaries table) instead of forcing a full re-parse; transitions with
  no registered path — or a change to the persisted IR encoding, now versioned
  explicitly via `ir_codec.IR_CODEC_VERSION` — still fall back to a rebuild. Read
  commands stay read-only. Policy documented in `docs/schema-migrations.md` ([#74]).



### Changed

- Clarify the v1 scope: re-frame M8–M10 as an exploratory / community-contribution
  track (one wedge at a time, SDC/XDC first) and defer the M8 API/schema freeze
  until the migration ladder ([#74]) and the unified CLI exit-code contract
  ([#73]) land and the surface proves stable on real designs ([#81]).

## [0.10.1] - 2026-06-15

### Changed

- Bound the incremental linker to the dirty closure for large designs: scope the
  incremental delta write to the changed set and hydrate `find_signal_drivers`
  edges per module instead of across the whole graph ([#108]).

### Fixed

- Correct `` `include `` handling in the incremental parse path.

## [0.9.0] - 2026-06-15

### Added

- Precompute whole-design summaries and add a read-latency benchmark.
- Optional bearer-token authentication for the MCP HTTP transport ([#69]).

### Changed

- Serve MCP queries from bounded subgraphs instead of loading the full graph.
- Auto-resolve `` `include `` directives against discovered source directories,
  and confine filelist and `` `include `` path resolution to the build root
  ([#68]).

### Security

- Pin and verify the integrity of the vendored `d3.v7.min.js`, and mark the
  bundle as binary so Windows checkouts preserve its hash ([#78]).

## [0.8.2] - 2026-06-15

Maintenance release — version bump only, no functional changes.

## [0.8.1] - 2026-06-15

### Added

- `visualize`: highlight a searched node's neighbors and relationship lines.
- `visualize`: `--kinds` / `--exclude-kinds` node-category filters.

### Changed

- SystemVerilog incremental pass-2 linking foundation: mutate the prior graph in
  place instead of rebuild-and-splice ([#64]).
- Isolate parser-worker failures and add incremental-link scoping telemetry
  ([#65]).

### Fixed

- Incremental-link include-splicing edge duplication and `node_file` context
  ([#64]).
- Anchor reset/clock name regexes ([#76]) and harden unsupported-suffix routing
  ([#77]).

## [0.7.5] - 2026-06-14

### Added

- `ref_index` substrate for incremental linking ([#64]).
- Incremental-equals-full byte-identity gate: edit matrix and fuzz tests ([#64]).

### Changed

- Move `pyslang` / `pyVHDLModel` to an optional `enrich` extra ([#67]).
- Incremental update persistence writes only the changed rows ([#63]).

### Fixed

- Constrain the `visualize` graph view to the `--top` subtree ([#59]).

### Security

- Guard the VCS ref/revision against argument injection ([#66]).

## [0.7.4] - 2026-06-14

### Added

- `enriched` report command summarizing the `--enrich` delta.

## [0.7.2] - 2026-06-14

### Added

- Semantic enrichment via native frontends: pyslang for SystemVerilog and a
  GHDL backend for VHDL (M7).

### Changed

- Parse function-call size casts such as `` $clog2(N)'(v) ``.

### Fixed

- `visualize`: static-tier crash from a zoom reference used before declaration;
  stop fitting the graph on tab reveal; keep the canvas off the hierarchy tab.

## [0.6.5] - 2026-06-13

### Added

- `visualize`: collapsed community view with drill-down, two-level
  `--collapse --full` aggregation, and search-driven auto-expand
  (viz-scalability P3).
- `visualize`: precomputed-layout tier with a canvas renderer (P1–P2) and an
  export escape hatch — GraphML / GEXF / JSON (P5).
- Perforce and SVN support in `detect-changes`.
- Lint waivers via `[[lint.waivers]]`.

### Changed

- `visualize`: gzip-compress large inline payloads, with an inline-payload size
  guard and `--force-inline` override (P4).

## [0.6.4] - 2026-06-13

### Added

- Issue, feature-request, and pull-request templates.

### Changed

- Parallelize pass-1 parsing over a process pool.
- `detect-changes`: diff hashes without rehydrating the full graph.
- `metrics`: sample betweenness centrality above a node-count threshold.

### Fixed

- Linker: consistent confidence for multi-match scoped signals and
  both-branches duplicates.

## [0.6.3] - 2026-06-13

### Added

- Show build progress by default with a live per-file counter.
- List exact parse errors with `file:line` locations instead of just counts.

### Changed

- CLI polish: `--json` coverage, `detect-changes` exit codes, and
  serve / visualize / watch fixes.
- Upper-bound the tree-sitter dependencies, exercise the `[watch]` extra in CI,
  and gate PyPI publishing on the test suite.

---

Releases before `0.6.3` predate this changelog; their history lives in the git
log.

[Unreleased]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v1.6.0...HEAD
[1.6.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.16.1...v1.0.0
[0.16.1]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.16.0...v0.16.1
[0.16.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.13.1...v0.14.0
[0.13.1]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.10.2...v0.11.0
[0.10.2]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.9.0...v0.10.1
[0.9.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.8.2...v0.9.0
[0.8.2]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.7.5...v0.8.1
[0.7.5]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.7.4...v0.7.5
[0.7.4]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.7.2...v0.7.4
[0.7.2]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.6.5...v0.7.2
[0.6.5]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.6.4...v0.6.5
[0.6.4]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.6.3...v0.6.4
[0.6.3]: https://github.com/chuanseng-ng/hdl-kgraph/releases/tag/v0.6.3

[#59]: https://github.com/chuanseng-ng/hdl-kgraph/pull/59
[#63]: https://github.com/chuanseng-ng/hdl-kgraph/pull/63
[#64]: https://github.com/chuanseng-ng/hdl-kgraph/pull/64
[#65]: https://github.com/chuanseng-ng/hdl-kgraph/pull/65
[#66]: https://github.com/chuanseng-ng/hdl-kgraph/pull/66
[#67]: https://github.com/chuanseng-ng/hdl-kgraph/pull/67
[#68]: https://github.com/chuanseng-ng/hdl-kgraph/pull/68
[#69]: https://github.com/chuanseng-ng/hdl-kgraph/pull/69
[#76]: https://github.com/chuanseng-ng/hdl-kgraph/pull/76
[#77]: https://github.com/chuanseng-ng/hdl-kgraph/pull/77
[#70]: https://github.com/chuanseng-ng/hdl-kgraph/issues/70
[#71]: https://github.com/chuanseng-ng/hdl-kgraph/issues/71
[#72]: https://github.com/chuanseng-ng/hdl-kgraph/issues/72
[#73]: https://github.com/chuanseng-ng/hdl-kgraph/issues/73
[#74]: https://github.com/chuanseng-ng/hdl-kgraph/issues/74
[#75]: https://github.com/chuanseng-ng/hdl-kgraph/issues/75
[#78]: https://github.com/chuanseng-ng/hdl-kgraph/pull/78
[#81]: https://github.com/chuanseng-ng/hdl-kgraph/issues/81
[#108]: https://github.com/chuanseng-ng/hdl-kgraph/pull/108
