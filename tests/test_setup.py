"""``hdl-kgraph setup`` tests (M6): detection, JSON merge, idempotence."""

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from hdl_kgraph.cli.main import main
from hdl_kgraph.mcp import setup as mcp_setup
from hdl_kgraph.mcp.setup import Target, detect_targets, plan_entry, write_config


def _target(path: Path, backup: bool = False) -> Target:
    return Target(name="t", config_path=path, detected=True, backup=backup)


def test_plan_entry_points_at_serve() -> None:
    entry = plan_entry(Path("/work/.hdl-kgraph/graph.db"))
    assert entry["command"] == "hdl-kgraph"
    assert entry["args"] == ["serve", "--mcp", "--db", "/work/.hdl-kgraph/graph.db"]


def test_detect_claude_code_via_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setenv("CLAUDECODE", "1")
    targets = {t.name: t for t in detect_targets(project_dir=tmp_path)}
    assert targets["claude-code"].detected
    assert targets["claude-code"].config_path == tmp_path / ".mcp.json"


def test_detect_claude_desktop_via_config_dir(monkeypatch, tmp_path: Path) -> None:
    desktop = tmp_path / "Claude"
    monkeypatch.setattr(mcp_setup, "_claude_desktop_dir", lambda: desktop)
    assert not {t.name: t for t in detect_targets()}["claude-desktop"].detected
    desktop.mkdir()
    found = {t.name: t for t in detect_targets()}["claude-desktop"]
    assert found.detected
    assert found.config_path == desktop / "claude_desktop_config.json"


def test_write_config_creates_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    assert write_config(_target(path), plan_entry(tmp_path / "graph.db")) is True
    config = json.loads(path.read_text())
    assert config["mcpServers"]["hdl-kgraph"]["command"] == "hdl-kgraph"


def test_write_config_preserves_other_content(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}))
    write_config(_target(path), plan_entry(tmp_path / "graph.db"))
    config = json.loads(path.read_text())
    assert config["theme"] == "dark"
    assert config["mcpServers"]["other"] == {"command": "x"}
    assert "hdl-kgraph" in config["mcpServers"]


def test_write_config_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    entry = plan_entry(tmp_path / "graph.db")
    assert write_config(_target(path), entry) is True
    assert write_config(_target(path), entry) is False


def test_backup_created_once_for_user_configs(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}")
    write_config(_target(path, backup=True), plan_entry(tmp_path / "a.db"))
    backup = tmp_path / "config.json.bak"
    assert json.loads(backup.read_text()) == {}
    write_config(_target(path, backup=True), plan_entry(tmp_path / "b.db"))
    assert json.loads(backup.read_text()) == {}  # still the original


def test_no_backup_for_project_scope(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text("{}")
    write_config(_target(path), plan_entry(tmp_path / "graph.db"))
    assert not (tmp_path / ".mcp.json.bak").exists()


def test_malformed_json_aborts(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        write_config(_target(path), plan_entry(tmp_path / "graph.db"))
    assert path.read_text() == "{not json"  # never clobbered


@pytest.fixture()
def detected_project(monkeypatch, tmp_path: Path) -> Path:
    """cwd with a fake graph.db where only claude-code is detected."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(mcp_setup, "_claude_desktop_dir", lambda: tmp_path / "no-desktop")
    db = tmp_path / ".hdl-kgraph" / "graph.db"
    db.parent.mkdir()
    db.touch()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_setup_list_only(detected_project: Path) -> None:
    result = CliRunner().invoke(main, ["setup", "--list"])
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    assert "not detected" not in lines["claude-code"]
    assert "detected" in lines["claude-code"]
    assert "not detected" in lines["claude-desktop"]
    assert not (detected_project / ".mcp.json").exists()


def test_cli_setup_dry_run_writes_nothing(detected_project: Path) -> None:
    result = CliRunner().invoke(main, ["setup", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would write" in result.output
    assert not (detected_project / ".mcp.json").exists()


def test_cli_setup_writes_and_is_idempotent(detected_project: Path) -> None:
    result = CliRunner().invoke(main, ["setup", "--yes"])
    assert result.exit_code == 0, result.output
    config = json.loads((detected_project / ".mcp.json").read_text())
    args = config["mcpServers"]["hdl-kgraph"]["args"]
    assert args[:3] == ["serve", "--mcp", "--db"]
    assert args[3].endswith("graph.db")
    again = CliRunner().invoke(main, ["setup", "--yes"])
    assert "already up to date" in again.output


def test_cli_setup_unknown_assistant(detected_project: Path) -> None:
    result = CliRunner().invoke(main, ["setup", "--assistant", "nope"])
    assert result.exit_code != 0
    assert "unknown assistant" in result.output


def test_cli_setup_nothing_detected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(mcp_setup, "_claude_desktop_dir", lambda: tmp_path / "no-desktop")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["setup", "--yes"])
    assert result.exit_code != 0
    assert "no supported AI assistant detected" in result.output


def test_cli_setup_missing_db(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(mcp_setup, "_claude_desktop_dir", lambda: tmp_path / "no-desktop")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["setup", "--yes"])
    assert result.exit_code != 0
    assert "run `hdl-kgraph build` first" in result.output
