"""hdl-kgraph CLI.

Only ``build`` and ``status`` stubs exist today. The command surface grows
with the milestones (see ROADMAP.md): ``query``/``tree`` complete M1,
filelist/define options arrive in M2, ``update``/``watch``/``impact`` in M4,
``visualize`` in M5, and ``serve`` in M6.
"""

from __future__ import annotations

import sys

import click

from hdl_kgraph import __version__


@click.group()
@click.version_option(version=__version__, prog_name="hdl-kgraph")
def main() -> None:
    """Build and query a knowledge graph of your HDL design."""


@main.command()
@click.argument("source", type=click.Path(exists=True), required=False)
def build(source: str | None) -> None:
    """Build the knowledge graph from HDL sources. [milestone M1]"""
    click.echo("hdl-kgraph build: not implemented yet (milestone M1)", err=True)
    sys.exit(1)


@main.command()
def status() -> None:
    """Show graph statistics for the current build. [milestone M1]"""
    click.echo("hdl-kgraph status: not implemented yet (milestone M1)", err=True)
    sys.exit(1)
