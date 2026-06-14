# Semantic enrichment (M7)

The default build is **syntactic**: tree-sitter sees one `hierarchical_instance`
per instantiation, so a `generate` loop or an instance array collapses to a
single `INSTANCE` node, parameter overrides stay unevaluated, and ambiguous
cross-file names resolve only by heuristic (see the
[confidence convention](../ROADMAP.md#confidence-convention)).

*Enrichment* runs a native HDL frontend that genuinely **elaborates** the
design — resolving parameters, unrolling generates, applying `defparam` — and
feeds the result back as a strict overlay on the heuristic graph. It is
**opt-in**:

```console
$ hdl-kgraph build ./rtl --enrich
...
  enriched via slang: 12 edge(s) upgraded
  discrepancies: 3 (`hdl-kgraph discrepancies` lists them)
```

Because elaboration is a whole-design operation, `--enrich` re-runs it on every
`update` (it cannot reuse per-file results), so it is off by default; a plain
`build` is unchanged and never invokes a native frontend.

## What the overlay does

A backend never replaces the graph — it returns *deltas* that the runner merges
in place. Tree-sitter stays the always-works baseline; if a backend is missing
or cannot elaborate part of the design, the heuristic graph is preserved and the
failure surfaces as a diagnostic.

- **Confirmation:** a heuristic edge the backend agrees with is upgraded to
  confidence `1.0` and stamped `attrs["source"] = "elaborated"` (plus
  `attrs["backend"]`). Edges with no `source` attr are heuristic.
- **Generate/array unrolling:** a syntactic instance whose elaborated
  multiplicity exceeds one is annotated with `attrs["elaborated_count"]`, and one
  elaborated `INSTANCE` node per iteration is added (id
  `elab:instance:<hierarchical.path>`) with `INSTANTIATES`/`DECLARES` edges at
  `1.0`, so the graph reflects elaborated reality.
- **Disagreement → discrepancy, not overwrite:** a binding whose elaborated
  target differs from the heuristic guess is recorded as a `wrong_target`
  discrepancy rather than silently rewritten.

## The discrepancy report

Findings are persisted in the `discrepancies` table and surfaced by:

```console
$ hdl-kgraph discrepancies
1 discrepancy finding(s):
       1 instance_count
[instance_count] soc_top.u_lane (target lane) elaborates to 8 instances; tree-sitter saw 1 (via slang)
    heuristic: 1  elaborated: 8
```

`--json` emits the same findings for tooling. Discrepancy kinds:
`instance_count`, `wrong_target` (and the reserved `missing_edge` / `extra_edge`).

## Backends

Enrichment backends ship in the **core install** (no optional extra to add):

| Backend | Package | Status |
|---|---|---|
| `slang` (SystemVerilog/Verilog) | [`pyslang`](https://pypi.org/project/pyslang/) | shipping — generate unroll + `INSTANTIATES` confirmation |
| `ghdl` (VHDL) | the `ghdl` binary (`pyGHDL`/`libghdl` ship with it) | shipping — binding confirmation + `wrong_target` + `for ... generate` unroll |

`pyslang` is a core pip dependency, so `slang` works out of the box. **GHDL is a
system binary, not a pip package** — its `pyGHDL`/`libghdl` Python bindings are
installed alongside it (`apt install ghdl` / `conda install ghdl` / `brew install
ghdl`), so `ghdl` enrichment activates only when that binary is present and is
silently skipped otherwise. (`pyVHDLModel` is a document model used by `pyGHDL`,
not an elaborator on its own.)

The interface lives in
[`hdl_kgraph/enrich/base.py`](../src/hdl_kgraph/enrich/base.py)
(`EnrichmentBackend`, `EnrichmentResult`, `Discrepancy`); the merge plumbing is
`add_or_upgrade_edge`/`ensure_node` in
[`graph/builder.py`](../src/hdl_kgraph/graph/builder.py).

## Scope (v0.7)

The first cut covers instance-count correction and `INSTANTIATES` confirmation —
enough to satisfy the acceptance criterion (instance counts match elaborated
reality on parameterized generates). For VHDL the `ghdl` backend's emphasis is
binding accuracy: it confirms component/entity/configuration bindings, flags a
`wrong_target` where a configuration rebinds an instance the heuristic guessed by
name, and unrolls `for ... generate` over statically foldable ranges. Full
type/width propagation onto signals, `CONNECTS`/`PARAMETERIZES` value upgrades,
and generic-bounded generate ranges are a follow-on within M7.
