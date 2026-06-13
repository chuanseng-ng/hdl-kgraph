# Build inputs and configuration

`hdl-kgraph build` works on a bare directory tree, but real designs are
driven by filelists, defines, and include directories. All of these are
first-class build inputs, and all of them can come from the command line or
from a config file (CLI flags win).

## Command-line flags

```bash
hdl-kgraph build ./rtl                                # everything under ./rtl
hdl-kgraph build -f sim/tb.f                          # vendor-style filelist
hdl-kgraph build -D SYNTHESIS -D WIDTH=8 -I include   # defines + incdirs
hdl-kgraph build --lib work=./src/vhdl                # VHDL library mapping
hdl-kgraph build --exclude 'vendor/*' --max-file-size 2048
```

- `-f/--filelist` (repeatable) compiles the sources listed in a `.f`/`.vc`
  filelist; the positional `SOURCE` then only sets the build root.
- `-D/--define NAME[=VALUE]` (repeatable) sets a preprocessor define;
  CLI defines override config and filelist defines.
- `-I/--incdir` (repeatable) adds a `` `include `` search directory.
- `--lib NAME=PATH` (repeatable) maps a VHDL library name to a source
  directory (default library is `work`).
- `--exclude GLOB` (repeatable) and `--max-file-size KB` keep generated
  netlists and vendored IP out of the graph.

## Filelists

Filelists support `+incdir+`/`+define+`, nested `-f`, `-y`/`-v` library
dirs, and `$VAR` environment-variable expansion — the dialect simulators
accept. Files are compiled *in filelist order*, threading one macro table
through all files the way simulators carry `+define+` and earlier-file
defines forward.

## Both-branches mode

When no defines are given at all, conditionals on undefined names emit
*both* branches: the side a define-less compile would select at full
confidence, the alternative at 0.6. This keeps `` `ifndef `` include guards
and default-`` `define `` fallbacks at full confidence while still seeing
code hidden behind feature flags.

## `hdl-kgraph.toml`

Repeatable inputs can live in an `hdl-kgraph.toml` at the build root
(found automatically from `SOURCE` upward; CLI flags win):

```toml
[build]
filelists = ["sim/tb.f"]
defines   = ["SYNTHESIS", "WIDTH=8"]
incdirs   = ["include"]
exclude   = ["vendor/*"]
top       = ["soc_top"]  # intended tops; lint's dead-module exempts them

[vhdl.libraries]
work = "src/vhdl"        # or: hdl-kgraph build --lib work=./src/vhdl

[[lint.waivers]]         # acknowledge a known lint finding (see analyses.md)
check  = "open-port"
name   = "soc_top.u_dbg"
reason = "debug port, tied off"
```

Use `--config PATH` to point at a specific file or `--no-config` to ignore
any config.

## Diagnostics: is the graph trustworthy?

Files with syntax errors still yield partial results; the build reports the
parse-error count. To judge whether the graph is trustworthy on real RTL,
`build`/`update`/`watch` take `-v/--verbose`: pipeline stages as they run,
per-file parse-error counts, and the full preprocessor warnings (unresolved
`` `include``s with the search path, malformed `` `define``s, ...). The same
diagnostics are persisted with the build, so

```bash
hdl-kgraph status --errors
```

lists them per file after the fact — including files that were skipped,
with reasons.
