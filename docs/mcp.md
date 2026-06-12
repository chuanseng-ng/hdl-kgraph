# MCP server — AI assistants over the knowledge graph

`hdl-kgraph serve --mcp` exposes the graph to AI assistants via the
[Model Context Protocol](https://modelcontextprotocol.io). The server is
**read-only**: it loads `.hdl-kgraph/graph.db` and never builds or updates
it. Rebuild with `hdl-kgraph build`/`update` at any time — a running server
notices the new database (mtime/size check per call) and reloads.

Requires the `mcp` extra:

```sh
pip install 'hdl-kgraph[mcp]'
```

## One-command setup

```sh
hdl-kgraph build ./rtl        # if you haven't already
hdl-kgraph setup
```

`setup` detects installed assistants and writes the server entry into their
config:

| Assistant | Detection | Config written |
|---|---|---|
| Claude Code | `claude` CLI on PATH (or `CLAUDECODE` env) | project-scope `.mcp.json` in the current directory |
| Claude Desktop | the platform's `Claude/` config directory exists | `claude_desktop_config.json` (with a one-time `.bak` backup) |

Re-running is safe: the `hdl-kgraph` entry is updated in place and every
other key in the file is preserved. Useful flags: `--list` (report
detection only), `--dry-run` (print the resulting JSON without writing),
`--yes` (skip prompts), `--assistant NAME` (restrict targets), `--db PATH`
(point at a specific database).

## Manual configuration

For Claude Code, the CLI equivalent of what `setup` writes:

```sh
claude mcp add hdl-kgraph -- hdl-kgraph serve --mcp --db /path/to/repo/.hdl-kgraph/graph.db
```

For Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "hdl-kgraph": {
      "command": "hdl-kgraph",
      "args": ["serve", "--mcp", "--db", "/path/to/repo/.hdl-kgraph/graph.db"]
    }
  }
}
```

Any other MCP client works with the same command line. For clients that
prefer HTTP:

```sh
hdl-kgraph serve --mcp --http 127.0.0.1:8000   # streamable HTTP at /mcp
```

## Tools

Every list-returning tool paginates: responses carry
`{total, offset, count, truncated, items}`, with `limit` clamped to 500.
Confidence scores follow the project convention (1.0 resolved, 0.8 unique
cross-file match, 0.6 ambiguous, 0.4 heuristic); VHDL names match
case-insensitively everywhere.

| Tool | Arguments | Answers |
|---|---|---|
| `find_module` | `name` (glob ok), `limit` | "is there a module called X?" — with port/parameter/instantiation counts |
| `get_hierarchy` | `top`, `depth` (default 3), `max_nodes` (default 500) | top-level units, or the instance tree under `top` (depth- and node-capped, omissions reported) |
| `who_instantiates` | `name`, `limit`, `offset` | every instantiation site of a unit |
| `port_map` | `module`, `instance` | ports/parameters in declaration order; with `instance`, its connection bindings |
| `impact_of_change` | `target` (file or unit), `max_depth`, `limit`, `offset` | "what breaks if this changes?" — summary first, then affected units nearest-first |
| `clock_domains` | — | clock domains with alias nets and process/signal counts, plus CDC suspects |
| `find_signal_drivers` | `signal`, `module`, `readers`, `limit`, `offset` | "what drives signal X in module Y?" (`readers=true` for the readers) |
| `uvm_topology` | — | UVM components by role and testbench→DUT `TEST_COVERS` links |
| `search_nodes` | `name` glob, `kinds` (e.g. `module`, `signal`, `class`), `file` glob, `limit`, `offset` | anything else — the general node search |

## Cold-checkout walkthrough

The M6 acceptance flow, from nothing to answers:

```sh
git clone <your-design-repo> && cd <your-design-repo>
pip install 'hdl-kgraph[mcp]'
hdl-kgraph build .
hdl-kgraph setup --yes
```

Then ask your assistant, e.g.:

- *"What drives signal `stage` in module `df_top`?"* →
  `find_signal_drivers(signal="stage", module="df_top")`
- *"What breaks if I change `adder`'s ports?"* →
  `impact_of_change(target="adder")` (or `port_map` first for the current
  shape)
