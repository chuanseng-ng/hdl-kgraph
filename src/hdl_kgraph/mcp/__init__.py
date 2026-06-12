"""MCP server exposing the knowledge graph to AI assistants (M6).

The server is read-only: it loads the graph from an existing
``.hdl-kgraph/graph.db`` and never builds or updates it. fastmcp is an
optional dependency (the ``[mcp]`` extra); importing this package is safe
without it — only :func:`create_server` requires it.
"""

from hdl_kgraph.mcp.server import GraphContext, McpUnavailableError, create_server

__all__ = ["GraphContext", "McpUnavailableError", "create_server"]
