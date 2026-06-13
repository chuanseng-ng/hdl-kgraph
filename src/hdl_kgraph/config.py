"""``hdl-kgraph.toml`` configuration (M2).

The config file lives at the build root (or any parent — :func:`find_config`
walks up git-style, like the database). Precedence is CLI > config > defaults
for scalars; repeatable inputs (defines, incdirs, filelists, excludes) are
additive, with CLI entries appended last so they win define-name conflicts.
Filelist-provided ``+define+``/``+incdir+`` rank below both — they are build
inputs, not user overrides.

Schema::

    [build]
    sources   = ["rtl/**/*.sv"]      # source globs (default: whole root)
    filelists = ["sim/tb.f"]
    defines   = ["SYNTHESIS", "WIDTH=8"]
    incdirs   = ["include"]
    top       = ["soc_top"]          # carried for later milestones
    exclude   = ["vendor/*"]
    max_file_size_kb = 1024

    [vhdl.libraries]                 # VHDL library name -> source directory
    work = "src/vhdl"                # (M3; CLI --lib NAME=PATH wins per name)

Relative paths resolve against the config file's own directory, so a config
at the repo root keeps working from any subdirectory.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

CONFIG_FILENAME = "hdl-kgraph.toml"

_BUILD_KEYS = frozenset(
    {"sources", "filelists", "defines", "incdirs", "top", "exclude", "max_file_size_kb"}
)


class ConfigError(Exception):
    """Raised for an unreadable or malformed config file."""


def parse_define(text: str) -> tuple[str, str | None]:
    """Split a ``NAME`` or ``NAME=VALUE`` define string (CLI, TOML, ``+define+``)."""
    name, sep, value = text.partition("=")
    return name, value if sep else None


@dataclass
class BuildConfig:
    """Parsed ``hdl-kgraph.toml`` contents (defaults when no file exists)."""

    path: Path | None = None  # the config file itself, None for pure defaults
    sources: list[str] = field(default_factory=list)  # globs, config-dir relative
    filelists: list[Path] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)
    incdirs: list[Path] = field(default_factory=list)
    top: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_file_size_kb: int | None = None
    vhdl_libraries: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> BuildConfig:
        """Parse *path*; raises :class:`ConfigError` on TOML or type errors."""
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

        config = cls(path=path.resolve())
        base = config.path.parent if config.path is not None else Path.cwd()
        build = data.pop("build", {})
        if not isinstance(build, dict):
            raise ConfigError(f"{path}: [build] must be a table")
        for key in sorted(set(build) - _BUILD_KEYS):
            config.warnings.append(f"unknown key [build].{key} ignored")

        config.sources = _str_list(path, build, "sources")
        config.filelists = [base / p for p in _str_list(path, build, "filelists")]
        config.incdirs = [base / p for p in _str_list(path, build, "incdirs")]
        config.top = _str_list(path, build, "top")
        config.exclude = _str_list(path, build, "exclude")
        config.defines = dict(parse_define(d) for d in _str_list(path, build, "defines"))
        size = build.get("max_file_size_kb")
        if size is not None and not isinstance(size, int):
            raise ConfigError(f"{path}: [build].max_file_size_kb must be an integer")
        config.max_file_size_kb = size

        vhdl = data.pop("vhdl", {})
        libraries = vhdl.get("libraries", {}) if isinstance(vhdl, dict) else {}
        if not isinstance(libraries, dict) or not all(
            isinstance(v, str) for v in libraries.values()
        ):
            raise ConfigError(f"{path}: [vhdl.libraries] must map library names to paths")
        config.vhdl_libraries = {name: base / p for name, p in libraries.items()}

        for section in sorted(data):
            config.warnings.append(f"unknown section [{section}] ignored")
        return config


def _str_list(path: Path, table: dict[str, object], key: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{path}: [build].{key} must be a list of strings")
    return value


def find_config(start: Path) -> Path | None:
    """Locate the nearest ``hdl-kgraph.toml`` from *start* upward (git-style)."""
    start = start.resolve()
    for directory in [start if start.is_dir() else start.parent, *start.parents]:
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


@dataclass
class BuildOptions:
    """Merged build inputs (CLI > config > defaults) handed to the pipeline."""

    filelists: list[Path] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)
    incdirs: list[Path] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    exclude: tuple[str, ...] = ()
    max_file_size_kb: int | None = None
    top: list[str] = field(default_factory=list)
    vhdl_libraries: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    jobs: int | None = None  # pass-1 parse workers; None = auto, 1 = serial


def parse_lib(text: str) -> tuple[str, Path]:
    """Split a ``NAME=PATH`` VHDL library mapping (the ``--lib`` flag).

    Library names are case-insensitive in VHDL and normalized to lowercase.
    """
    name, sep, value = text.partition("=")
    if not sep or not name or not value:
        raise ConfigError(f"--lib expects NAME=PATH, got {text!r}")
    return name.lower(), Path(value).resolve()


def resolve_build_options(
    config: BuildConfig,
    *,
    cli_filelists: Sequence[Path] = (),
    cli_defines: Sequence[str] = (),
    cli_incdirs: Sequence[Path] = (),
    cli_exclude: Sequence[str] = (),
    cli_max_file_size_kb: int | None = None,
    cli_libs: Sequence[str] = (),
) -> BuildOptions:
    """Merge config-file values with CLI flags (CLI appended last, so it wins)."""
    defines = dict(config.defines)
    defines.update(parse_define(d) for d in cli_defines)
    vhdl_libraries = {name.lower(): path for name, path in config.vhdl_libraries.items()}
    vhdl_libraries.update(parse_lib(lib) for lib in cli_libs)
    return BuildOptions(
        filelists=[*config.filelists, *cli_filelists],
        defines=defines,
        incdirs=[*config.incdirs, *cli_incdirs],
        sources=list(config.sources),
        exclude=(*config.exclude, *cli_exclude),
        max_file_size_kb=(
            cli_max_file_size_kb if cli_max_file_size_kb is not None else config.max_file_size_kb
        ),
        top=list(config.top),
        vhdl_libraries=vhdl_libraries,
        warnings=list(config.warnings),
    )
