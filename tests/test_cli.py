from click.testing import CliRunner

from hdl_kgraph import __version__
from hdl_kgraph.cli.main import main


def test_version() -> None:
    """--version reports the package version."""
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_commands() -> None:
    """--help lists the available subcommands."""
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "build" in result.output
    assert "status" in result.output


def test_build_not_implemented_exits_nonzero() -> None:
    """The build stub fails loudly rather than pretending to succeed."""
    result = CliRunner().invoke(main, ["build"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_status_not_implemented_exits_nonzero() -> None:
    """The status stub fails loudly rather than pretending to succeed."""
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 1
    assert "not implemented" in result.output
