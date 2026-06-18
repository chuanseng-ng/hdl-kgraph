# Design proposal: database merge — IP-block assembly & subtree caching

> **Status: proposal, not implemented.** This documents the planned `hdl-kgraph
> merge` feature and why it is scoped the way it is. See
> [benchmarks.md](benchmarks.md) for the timing evidence behind the scope.

## Why (and why this scope)

`--timings` on real designs shows two things. First, re-linking is cheap: pass 2
is a small, *serial-once* cost relative to discover+parse. Second, on enriched
builds **pass 3 (elaboration) dominates** (≈60% of wall-clock on a large MCU)
and a merge cannot parallelize it — elaboration is whole-design.

So merge is **not** worth building as a raw-speed play for enriched builds. It
*is* worth building for two concrete workflows on the **syntactic** graph:

1. **IP-block assembly** — each block is built independently (often by a
   different team or on a different machine), then assembled into one
   SoC-level graph. Value is organizational + cross-machine fan-out.
2. **Subtree caching** — build a stable block once, keep its database, and
   re-link only when a *sibling* block changes, reusing the cached per-file IRs
   instead of re-parsing. Value is avoiding re-parse.

Enrichment is explicitly **out of scope for the merge step**: if needed it runs
once as a whole-design pass *after* the merge (or not at all).

## The merge point: union the IRs, link once

The pass-2 linker is a pure function of the per-file IRs:

```text
link_graph(file_irs) -> (graph, ref_records)        # graph/builder.py:895
```

and those IRs are already persisted per unit in the `file_irs` table
(`storage/sqlite_store.py:105`, `load_units` at `:649`). Cross-file resolution
is **by name** (`definitions[(kind, name)]`), not by path, so a module defined
in block A resolves to an instance in block B *for free* once both IRs are in
one list. Therefore:

> **merge = union the per-file IRs across the source DBs, run `link_graph` once,
> save.**

This reuses the entire linker, and the result is byte-identical to a monolithic
build of the same files. Merging the already-resolved `nodes`/`edges` tables
would instead force us to re-implement resolution, confidence scoring, stub GC,
and `derive_test_covers` — rejected.

## Command

```text
hdl-kgraph merge DB1 DB2 ... --db OUT [--on-conflict error|first|last]
```

All source DBs must share the same build **root** (Mode A — see below) and be at
the current `SCHEMA_VERSION` and `IR_CODEC_VERSION`. Output is a normal,
queryable graph database.

### Steps (new module `hdl_kgraph/merge.py` + `cli/merge.py`)

1. **Open + gate each source** read-only. Reuse `SqliteStore.load_units`,
   `load_file_hashes`, `load_meta`, `_check_version`. Explicitly compare each
   source's `ir_codec_version` (load_units returns opaque IR text and does not
   validate it). **Refuse enriched sources** (non-empty `discrepancies` or any
   `elab:` nodes) so enrichment is never silently dropped.
2. **Decode** each `StoredUnit.ir` → `FileIR` (`storage/ir_codec.json_to_ir`).
3. **Dedup by path with content-hash conflict detection** — *critical*: the
   linker keeps first-occurrence and silently drops a later same-id node
   (`graph/builder.py:348`), so dedup must happen *before* `link_graph` and be
   authoritative. Same path + same `content_hash` → keep one; same path +
   different hash (the same file preprocessed differently) → `--on-conflict`
   (default `error`, naming the file).
4. **Reconstruct FILELIST and VHDL `library` IRs** for the merged set. These are
   generated fresh at build time and are *not* in `file_irs`
   (`pipeline.py:668`); the VHDL `library` node feeds `_referrer_library` /
   `work` resolution (`builder.py:427`,`:439`). Rebuild them from the persisted
   `file:` node `attrs["library"]`. **If this cannot be done faithfully, v1 is
   scoped to SV-only / no-filelist and detect-and-refuses otherwise.**
5. `graph, ref_records = link_graph(combined_irs)`.
6. `summaries = build_summaries(graph)` (do **not** union source summaries —
   they are whole-design). Reuse the kept `StoredUnit`s verbatim.
7. `SqliteStore(OUT).save(graph, files, root, units, ref_records, summaries,
   options_hash=<merged sentinel>)`. The sentinel (e.g. `"merged:" +
   sha(sorted source hashes)`) makes a later `update` fall back to full rebuild
   (`pipeline.py:710`); `update` should detect it and refuse.

### Same-root constraint (Mode A)

Node IDs embed root-relative paths. Requiring all parallel builds to use the
**same `--root R`** (each building a disjoint or overlapping subset via
`--sources`/filelists into its own `--db`) means relpaths — and thus node IDs —
are exactly what a monolithic build would produce, so merge is a direct union
with **no path rewriting** and is provably byte-identical. Per-IP-block assembly
fits naturally: each block is a subtree under `R`.

Different roots (path prefixing/namespacing) is a possible later mode but adds
an error-prone IR path-rewrite for marginal benefit; defer it.

## Subtree caching

Caching is the same machinery viewed incrementally:

- Keep each block's database as a cached artifact (its `file_irs` + `files`).
- When block X changes, rebuild **only X** (`build`/`update` on X's subtree).
- Re-merge: union the unchanged blocks' cached IRs with X's fresh IRs and
  `link_graph` once. The expensive parse is paid only for X; everything else is
  reused from cache. The re-link is the `serial link` cost from `--timings`,
  which the data shows is small.

This needs no new storage — it is the merge command plus a convention of keeping
per-block DBs around. A thin `--cache-dir` helper that maps block → DB path is a
possible ergonomic add-on, not required for v1.

## Correctness traps (must-handle)

1. **Silent node-id dedup** in the linker (`builder.py:348`) — dedup at the
   unit/path level before linking (step 3).
2. **FILELIST / VHDL library not in `file_irs`** — reconstruct or scope out
   (step 4); affects VHDL `work`/library-scoped resolution.
3. **ref_index** — do *not* carry over from sources; `link_graph` returns fresh
   `ref_records`.
4. **Stubs converge for free** — undefined refs across blocks share one
   `unresolved:{kind}:{name}` node (`ids.py:52`).
5. **Schema/codec gating** — refuse mismatched `SCHEMA_VERSION` /
   `IR_CODEC_VERSION` sources.

## Caveats for users

- Partitions must be **preprocessing-self-contained**: a macro defined in one
  block and used by bare name in another won't resolve (same limitation as
  incremental `update`).
- `--enrich` is **post-merge / whole-design only**; enriched source DBs are
  refused.
- A merged DB **does not support `update`** (full rebuild only).
- v1 targets the **same-root** workflow.

## Verification

Mirror `tests/test_incremental_equivalence.py` (`_signature` = ordered
node+edge tuples). Headline gate:

> `merge(build(P0), build(P1), …)` over partitions of a tree with `--root R`
> produces a graph whose `_signature` equals `run_build(whole tree, --root R)`.

Plus: cross-partition resolution at the right confidence; overlap dedup +
conflict (`error`/`first`/`last`); stub convergence across partitions;
order-independence (merge (A,B) == (B,A)); and a VHDL library/filelist case
(the corner that fails first if trap 2 is unhandled — it defines the v1 scope
boundary). Run `ruff`, `mypy`, and the full `pytest` suite.
