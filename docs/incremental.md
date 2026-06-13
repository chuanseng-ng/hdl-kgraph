# Incremental updates, watch mode, and change analysis

The database is a derived cache that stays fresh as you edit.

## `update`

`update` re-parses only changed/added/removed files plus their dependents —
files that `` `include `` an edited header or expand a macro it defines —
and re-links everything else from stored parse results. One file edited in
a 2000-file design updates in under a second; see
[benchmarks.md](benchmarks.md).

A change to the effective build inputs (defines, incdirs, filelists,
library map) falls back to a full rebuild automatically, as does a database
written by an older schema version — there is no in-place migration,
because rebuild *is* the migration.

```bash
hdl-kgraph update                  # re-parse only what changed, re-link, save
hdl-kgraph update --full           # force a full rebuild
hdl-kgraph watch ./rtl             # debounced update on every save burst
```

`watch` needs the `watchdog` extra (`pip install 'hdl-kgraph[watch]'`) and
survives failing updates (a file deleted mid-update is reported, watching
continues).

## `detect-changes`

```bash
hdl-kgraph detect-changes          # M/A/D lines vs the last build
hdl-kgraph detect-changes --git    # ...or vs git HEAD (any ref works)
hdl-kgraph detect-changes --closure  # include files dirtied via include/macro deps
```

Exit codes follow the `git diff --exit-code` convention so scripts can tell
the cases apart: **0** nothing changed, **1** changes detected, **2** error
(missing database, bad config, ...). `--json` emits the change set as
structured data.

## `impact`

```bash
hdl-kgraph impact rtl/uart_tx.sv   # what does my change affect?
hdl-kgraph impact fifo --files     # affected files instead of design units
```

`impact` walks reverse `INSTANTIATES`/`IMPORTS`/`INCLUDES`/`EXTENDS` (plus
VHDL `USES_PACKAGE`/`IMPLEMENTS`/`BINDS` and macro-use) edges transitively:
the instantiating parents, importers, includers, and subclasses a change
can break. `--max-depth` limits the radius; `--json` emits records for
scripting.
