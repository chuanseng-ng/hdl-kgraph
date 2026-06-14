"""Multi-VCS change detection for ``detect-changes`` (git/svn/p4).

git lives behind the same :func:`detect_vcs_changes` dispatcher and is already
covered by ``test_incremental.test_detect_git_changes``. svn gets a live
integration test (skipped when the tools are absent); p4 — which needs a server
— is covered by unit-testing the pure output parser on captured ``p4 -ztag``
output.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hdl_kgraph.discovery import SUFFIXES
from hdl_kgraph.vcs import (
    _p4_records_to_changeset,
    _parse_svn_status,
    _parse_svn_summarize,
    _parse_ztag,
    detect_p4_changes,
    detect_svn_changes,
    detect_vcs,
    detect_vcs_changes,
)


def test_detect_vcs_autodetect(tmp_path: Path) -> None:
    assert detect_vcs(tmp_path) is None
    (tmp_path / "svnwc").mkdir()
    (tmp_path / "svnwc" / ".svn").mkdir()
    assert detect_vcs(tmp_path / "svnwc") == "svn"
    (tmp_path / "gitwc").mkdir()
    (tmp_path / "gitwc" / ".git").mkdir()
    assert detect_vcs(tmp_path / "gitwc") == "git"
    # git wins when both are present walking up; a nested file still detects.
    (tmp_path / "gitwc" / "sub").mkdir()
    assert detect_vcs(tmp_path / "gitwc" / "sub") == "git"


def test_detect_svn_changes(tmp_path: Path) -> None:
    if shutil.which("svn") is None or shutil.which("svnadmin") is None:
        pytest.skip("svn not available")
    repo = tmp_path / "repo"
    wc = tmp_path / "wc"
    subprocess.run(["svnadmin", "create", str(repo)], check=True)
    url = repo.as_uri()
    subprocess.run(["svn", "checkout", "-q", url, str(wc)], check=True)
    (wc / "a.sv").write_text("module a;\nendmodule\n")
    (wc / "notes.txt").write_text("not hdl\n")
    subprocess.run(["svn", "add", "-q", "a.sv", "notes.txt"], cwd=wc, check=True)
    subprocess.run(["svn", "commit", "-q", "-m", "add"], cwd=wc, check=True)

    (wc / "a.sv").write_text("module a2;\nendmodule\n")
    (wc / "new.svh").write_text("`define N 1\n")  # unversioned -> added
    (wc / "notes.txt").write_text("still not hdl\n")

    changes = detect_svn_changes(wc, None, SUFFIXES)
    assert changes.changed == ["a.sv"]
    assert changes.added == ["new.svh"]
    assert changes.removed == []


def test_parse_svn_status() -> None:
    text = (
        "M       a.sv\n"
        "A       added.sv\n"
        "D       gone.sv\n"
        "?       new.svh\n"
        "!       missing.sv\n"
        "        unchanged-prop-line\n"
    )
    assert _parse_svn_status(text) == [
        ("M", "a.sv"),
        ("A", "added.sv"),
        ("D", "gone.sv"),
        ("?", "new.svh"),
        ("!", "missing.sv"),
    ]


def test_parse_svn_summarize() -> None:
    text = "M       rtl/a.sv\nA       rtl/b.sv\nD       rtl/c.sv\n"
    assert _parse_svn_summarize(text) == [
        ("M", "rtl/a.sv"),
        ("A", "rtl/b.sv"),
        ("D", "rtl/c.sv"),
    ]


def test_parse_ztag() -> None:
    text = (
        "... clientFile /ws/rtl/a.sv\n"
        "... action edit\n"
        "\n"
        "... clientFile /ws/rtl/b.sv\n"
        "... action add\n"
    )
    assert _parse_ztag(text) == [
        {"clientFile": "/ws/rtl/a.sv", "action": "edit"},
        {"clientFile": "/ws/rtl/b.sv", "action": "add"},
    ]


def test_p4_records_to_changeset(tmp_path: Path) -> None:
    base = tmp_path
    records = [
        {"clientFile": str(base / "rtl/a.sv"), "action": "edit"},
        {"clientFile": str(base / "rtl/new.svh"), "action": "add"},
        {"clientFile": str(base / "rtl/gone.sv"), "action": "delete"},
        {"clientFile": str(base / "moved.sv"), "action": "move/add"},
        {"clientFile": str(base / "notes.txt"), "action": "edit"},  # not a build input
        {"clientFile": str(base / "rtl/a.sv"), "action": "edit"},  # duplicate
        {"clientFile": "/elsewhere/x.sv", "action": "edit"},  # outside base
    ]
    changes = _p4_records_to_changeset(records, base, SUFFIXES)
    assert changes.changed == ["rtl/a.sv"]
    assert changes.added == ["moved.sv", "rtl/new.svh"]
    assert changes.removed == ["rtl/gone.sv"]


def test_detect_p4_changes_rejects_nondefault_rev(tmp_path: Path) -> None:
    # A specific changelist isn't supported yet; reject it (before any p4 call)
    # rather than silently reporting workspace state against the wrong baseline.
    with pytest.raises(RuntimeError, match="changelist is not supported"):
        detect_p4_changes(tmp_path, "12345", SUFFIXES)


@pytest.mark.parametrize("rev", ["--config-dir=/tmp/evil", "-rphony", "-"])
def test_detect_svn_changes_rejects_option_like_rev(tmp_path: Path, rev: str) -> None:
    """A revision that looks like an svn option is rejected before svn runs."""
    with pytest.raises(RuntimeError, match="looks like an option"):
        detect_svn_changes(tmp_path, rev, SUFFIXES)
    # Same guard via the public dispatcher.
    with pytest.raises(RuntimeError, match="looks like an option"):
        detect_vcs_changes(tmp_path, "svn", rev, SUFFIXES)
