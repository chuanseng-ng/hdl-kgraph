"""Tests for hdl-kgraph.toml loading and CLI/config precedence merging."""

from pathlib import Path

import pytest

from hdl_kgraph.config import (
    CONFIG_FILENAME,
    BuildConfig,
    ConfigError,
    find_config,
    parse_define,
    resolve_build_options,
)


def write_config(directory: Path, text: str) -> Path:
    path = directory / CONFIG_FILENAME
    path.write_text(text)
    return path


def test_parse_define() -> None:
    assert parse_define("SYNTHESIS") == ("SYNTHESIS", None)
    assert parse_define("WIDTH=8") == ("WIDTH", "8")
    assert parse_define("EXPR=a=b") == ("EXPR", "a=b")
    assert parse_define("EMPTY=") == ("EMPTY", "")


def test_load_full_config(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [build]
        sources = ["rtl/**/*.sv"]
        filelists = ["sim/tb.f"]
        defines = ["SYNTHESIS", "WIDTH=8"]
        incdirs = ["include"]
        top = ["soc_top"]
        exclude = ["vendor/*"]
        max_file_size_kb = 2048

        [vhdl.libraries]
        work = "src/vhdl"
        """,
    )
    config = BuildConfig.load(path)
    assert config.path == path.resolve()
    assert config.sources == ["rtl/**/*.sv"]
    assert config.filelists == [tmp_path / "sim/tb.f"]
    assert config.defines == {"SYNTHESIS": None, "WIDTH": "8"}
    assert config.incdirs == [tmp_path / "include"]
    assert config.top == ["soc_top"]
    assert config.exclude == ["vendor/*"]
    assert config.max_file_size_kb == 2048
    assert config.vhdl_libraries == {"work": tmp_path / "src/vhdl"}
    assert config.warnings == []


def test_load_empty_config(tmp_path: Path) -> None:
    config = BuildConfig.load(write_config(tmp_path, ""))
    assert config.filelists == []
    assert config.defines == {}
    assert config.max_file_size_kb is None


def test_load_invalid_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid TOML"):
        BuildConfig.load(write_config(tmp_path, "[build\n"))


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        BuildConfig.load(tmp_path / CONFIG_FILENAME)


def test_load_wrong_types(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="list of strings"):
        BuildConfig.load(write_config(tmp_path, "[build]\ndefines = [1]\n"))
    with pytest.raises(ConfigError, match="integer"):
        BuildConfig.load(write_config(tmp_path, "[build]\nmax_file_size_kb = 'big'\n"))


def test_load_warns_on_unknown_keys(tmp_path: Path) -> None:
    config = BuildConfig.load(
        write_config(tmp_path, "[build]\nbogus = 1\n\n[mystery]\nx = 2\n")
    )
    assert any("bogus" in w for w in config.warnings)
    assert any("mystery" in w for w in config.warnings)


def test_find_config_walks_up(tmp_path: Path) -> None:
    path = write_config(tmp_path, "")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_config(nested) == path
    assert find_config(tmp_path) == path


def test_find_config_absent(tmp_path: Path) -> None:
    assert find_config(tmp_path) is None


def test_resolve_precedence(tmp_path: Path) -> None:
    config = BuildConfig.load(
        write_config(
            tmp_path,
            """
            [build]
            filelists = ["a.f"]
            defines = ["WIDTH=8", "SYNTHESIS"]
            incdirs = ["inc"]
            exclude = ["vendor/*"]
            max_file_size_kb = 2048
            """,
        )
    )
    options = resolve_build_options(
        config,
        cli_filelists=[tmp_path / "b.f"],
        cli_defines=["WIDTH=16", "SIM"],
        cli_incdirs=[tmp_path / "cli_inc"],
        cli_exclude=["gen/*"],
        cli_max_file_size_kb=512,
    )
    # Additive for repeatables, config first; CLI wins define-name conflicts.
    assert options.filelists == [tmp_path / "a.f", tmp_path / "b.f"]
    assert options.defines == {"WIDTH": "16", "SYNTHESIS": None, "SIM": None}
    assert options.incdirs == [tmp_path / "inc", tmp_path / "cli_inc"]
    assert options.exclude == ("vendor/*", "gen/*")
    assert options.max_file_size_kb == 512


def test_resolve_defaults() -> None:
    options = resolve_build_options(BuildConfig())
    assert options.filelists == []
    assert options.max_file_size_kb is None
