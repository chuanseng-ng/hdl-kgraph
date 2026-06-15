"""``hdl-kgraph setup`` tests (M6): detection, JSON/TOML merge, idempotence."""

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from hdl_kgraph.cli.main import main
from hdl_kgraph.mcp import setup as mcp_setup
from hdl_kgraph.mcp.setup import Target, detect_targets, plan_entry, write_config

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib


def _target(path: Path, backup: bool = False) -> Target:
    return Target(name="t", config_path=path, detected=True, backup=backup)


def _toml_target(path: Path) -> Target:
    return Target(
        name="codex",
        config_path=path,
        detected=True,
        backup=False,
        servers_key="mcp_servers",
        fmt="toml",
    )


@pytest.fixture()
def fake_home(monkeypatch, tmp_path: Path) -> Path:
    """An empty home dir so detection never sees the real machine."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mcp_setup, "_home", lambda: home)
    return home


def test_setup_explicit_missing_db_errors(monkeypatch, tmp_path: Path) -> None:
    """An explicit ``--db`` that does not exist is rejected before any config is
    written (matching ``serve``), so assistants are never pointed at a missing DB."""
    monkeypatch.setattr(
        mcp_setup, "detect_targets", lambda *a, **k: [_target(tmp_path / "cfg.json")]
    )
    result = CliRunner().invoke(main, ["setup", "--db", str(tmp_path / "nope.db"), "--yes"])
    assert result.exit_code == 2, result.output
    assert "database not found" in result.output


def test_plan_entry_points_at_serve() -> None:
    db = Path("/work/.hdl-kgraph/graph.db")
    entry = plan_entry(db)
    assert entry["command"] == "hdl-kgraph"
    # str(db) keeps the comparison native to the platform's path separator.
    assert entry["args"] == ["serve", "--mcp", "--db", str(db)]


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


def test_detect_cursor_via_home_dir(monkeypatch, fake_home: Path, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    project = tmp_path / "proj"
    project.mkdir()
    assert not {t.name: t for t in detect_targets(project_dir=project)}["cursor"].detected
    (fake_home / ".cursor").mkdir()
    found = {t.name: t for t in detect_targets(project_dir=project)}["cursor"]
    assert found.detected
    assert found.config_path == project / ".cursor" / "mcp.json"


def test_detect_codex_and_gemini_via_path(monkeypatch, fake_home: Path, tmp_path: Path) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/x" if name in ("codex", "gemini") else None
    )
    targets = {t.name: t for t in detect_targets(project_dir=tmp_path)}
    assert targets["codex"].detected
    assert targets["codex"].config_path == fake_home / ".codex" / "config.toml"
    assert targets["codex"].fmt == "toml"
    assert targets["gemini-cli"].detected
    assert targets["gemini-cli"].config_path == fake_home / ".gemini" / "settings.json"


def test_detect_windsurf_via_config_dir(monkeypatch, fake_home: Path, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert not {t.name: t for t in detect_targets(project_dir=tmp_path)}["windsurf"].detected
    (fake_home / ".codeium" / "windsurf").mkdir(parents=True)
    found = {t.name: t for t in detect_targets(project_dir=tmp_path)}["windsurf"]
    assert found.detected
    assert found.config_path == fake_home / ".codeium" / "windsurf" / "mcp_config.json"


def test_vscode_entry_uses_servers_key_and_stdio_type(tmp_path: Path) -> None:
    target = Target(
        name="vscode",
        config_path=tmp_path / ".vscode" / "mcp.json",
        detected=True,
        backup=False,
        servers_key="servers",
        entry_extras={"type": "stdio"},
    )
    write_config(target, plan_entry(tmp_path / "graph.db"))
    config = json.loads(target.config_path.read_text())
    assert config["servers"]["hdl-kgraph"]["type"] == "stdio"
    assert config["servers"]["hdl-kgraph"]["command"] == "hdl-kgraph"
    assert "mcpServers" not in config


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


def test_toml_write_creates_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert write_config(_toml_target(path), plan_entry(tmp_path / "graph.db")) is True
    data = tomllib.loads(path.read_text())
    assert data["mcp_servers"]["hdl-kgraph"]["command"] == "hdl-kgraph"
    assert data["mcp_servers"]["hdl-kgraph"]["args"][:2] == ["serve", "--mcp"]


def test_toml_write_preserves_comments_and_other_tables(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('# my codex config\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n')
    write_config(_toml_target(path), plan_entry(tmp_path / "graph.db"))
    text = path.read_text()
    assert "# my codex config" in text  # textual merge keeps comments
    data = tomllib.loads(text)
    assert data["model"] == "o3"
    assert data["mcp_servers"]["other"] == {"command": "x"}
    assert "hdl-kgraph" in data["mcp_servers"]


def test_toml_write_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    entry = plan_entry(tmp_path / "graph.db")
    assert write_config(_toml_target(path), entry) is True
    assert write_config(_toml_target(path), entry) is False


def test_toml_replaces_existing_section_in_place(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mcp_servers.hdl-kgraph]\ncommand = "old"\nargs = []\n\n[profile]\nname = "work"\n'
    )
    assert write_config(_toml_target(path), plan_entry(tmp_path / "graph.db")) is True
    data = tomllib.loads(path.read_text())
    assert data["mcp_servers"]["hdl-kgraph"]["command"] == "hdl-kgraph"
    assert data["profile"] == {"name": "work"}
    assert '"old"' not in path.read_text()


def test_malformed_toml_aborts(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("model = =\n")
    with pytest.raises(ValueError, match="not valid TOML"):
        write_config(_toml_target(path), plan_entry(tmp_path / "graph.db"))
    assert path.read_text() == "model = =\n"  # never clobbered


@pytest.fixture()
def detected_project(monkeypatch, fake_home: Path, tmp_path: Path) -> Path:
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


def test_cli_setup_nothing_detected(monkeypatch, fake_home: Path, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(mcp_setup, "_claude_desktop_dir", lambda: tmp_path / "no-desktop")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["setup", "--yes"])
    assert result.exit_code != 0
    assert "no supported AI assistant detected" in result.output


def test_cli_setup_missing_db(monkeypatch, fake_home: Path, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(mcp_setup, "_claude_desktop_dir", lambda: tmp_path / "no-desktop")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["setup", "--yes"])
    assert result.exit_code != 0
    assert "run `hdl-kgraph build` first" in result.output
