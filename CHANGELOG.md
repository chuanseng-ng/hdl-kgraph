# Changelog

All notable changes to **hdl-kgraph** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the project is in `0.x` (alpha), minor versions may include breaking
changes.

## [Unreleased]

## [0.10.2] - 2026-06-15

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

[Unreleased]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.10.2...HEAD
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
[#73]: https://github.com/chuanseng-ng/hdl-kgraph/issues/73
[#74]: https://github.com/chuanseng-ng/hdl-kgraph/issues/74
[#78]: https://github.com/chuanseng-ng/hdl-kgraph/pull/78
[#81]: https://github.com/chuanseng-ng/hdl-kgraph/issues/81
[#108]: https://github.com/chuanseng-ng/hdl-kgraph/pull/108
