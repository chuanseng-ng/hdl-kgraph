"""Analyses over the knowledge graph (M4-M5).

Planned analyses:

* design hierarchy tree from a top module, consumed by the ``tree`` CLI
  command planned for M1 (not yet stubbed in the CLI)
* impact radius: transitively affected modules via INSTANTIATES / IMPORTS /
  INCLUDES / EXTENDS, including reverse `include and macro edges (M4)
* clock-domain report, reset tree, CDC-suspect crossings from
  CLOCKED_BY / RESETS / DRIVES / READS edges (M5)
* lint-flavored checks: unconnected ports, undriven/unread signals,
  never-instantiated modules (M5)
* graph metrics: fan-in/fan-out, hub/bridge nodes (betweenness), Louvain
  community detection for subsystem discovery (M5)
* UVM topology: EXTENDS chains to uvm_* bases, TEST_COVERS (M5)
"""
