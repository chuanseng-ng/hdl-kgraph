"""Auto-configuration of AI assistants for the MCP server (M6 ``setup``).

A small detection registry, not assistant SDKs: each supported assistant is
a config file we know how to find and how to merge an ``mcpServers`` entry
into. fastmcp is *not* required here — this module only edits JSON files.

Safety rules: the ``hdl-kgraph`` entry is updated in place and every other
key in the file is preserved; user-level files get a one-time ``.bak``
backup before the first modification; malformed JSON aborts rather than
clobbering.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SERVER_KEY = "hdl-kgraph"


@dataclass
class Target:
    """One configurable assistant: where its config lives and if it's here."""

    name: str
    config_path: Path
    detected: bool
    backup: bool  # user-level configs get a .bak; project-scope files don't

    def merged_config(self, entry: dict[str, object]) -> dict[str, object]:
        """The config file content after adding/updating our server entry."""
        config = load_config(self.config_path)
        servers = config.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError(f"{self.config_path}: 'mcpServers' is not an object")
        servers[SERVER_KEY] = entry
        return config


def plan_entry(db_path: Path) -> dict[str, object]:
    """The MCP server entry every assistant config gets."""
    return {"command": "hdl-kgraph", "args": ["serve", "--mcp", "--db", str(db_path)]}


def _claude_desktop_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "Claude"
    return Path.home() / ".config" / "Claude"


def detect_targets(project_dir: Path | None = None) -> list[Target]:
    """All known assistant targets with their detection state.

    Extending support is one entry here: name, config path, detection test.
    """
    project_dir = project_dir or Path.cwd()
    desktop_dir = _claude_desktop_dir()
    return [
        Target(
            name="claude-code",
            # Project-scope .mcp.json, which Claude Code auto-discovers.
            config_path=project_dir / ".mcp.json",
            detected=bool(shutil.which("claude") or os.environ.get("CLAUDECODE")),
            backup=False,
        ),
        Target(
            name="claude-desktop",
            config_path=desktop_dir / "claude_desktop_config.json",
            detected=desktop_dir.is_dir(),
            backup=True,
        ),
    ]


def load_config(path: Path) -> dict[str, object]:
    """Existing config JSON, ``{}`` if absent; ValueError if unusable."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: not valid JSON ({exc}); fix or remove it first") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object at the top level")
    return data


def write_config(target: Target, entry: dict[str, object]) -> bool:
    """Merge *entry* into the target's config file. True if the file changed."""
    existing = load_config(target.config_path)
    merged = target.merged_config(entry)
    if existing == merged and target.config_path.is_file():
        return False
    if target.backup and target.config_path.is_file():
        backup_path = target.config_path.with_suffix(target.config_path.suffix + ".bak")
        if not backup_path.exists():
            shutil.copy2(target.config_path, backup_path)
    target.config_path.parent.mkdir(parents=True, exist_ok=True)
    target.config_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return True
