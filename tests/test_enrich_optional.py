"""Enrichment is an optional extra: the tool degrades cleanly without it.

These tests must run even on a bare ``pip install`` (no ``enrich`` extra), so —
unlike ``test_enrich.py`` — they never import pyslang and instead simulate its
absence by forcing the backends unavailable. They verify that:

* ``available_backends()`` returns an empty list when no native frontend is
  installed (rather than raising on a missing import), and
* ``build --enrich`` on such an install records an actionable warning pointing
  at ``pip install 'hdl-kgraph[enrich]'`` instead of failing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.enrich import available_backends
from hdl_kgraph.enrich.ghdl_backend import GhdlBackend
from hdl_kgraph.enrich.slang_backend import SlangBackend
from hdl_kgraph.pipeline import run_build


def test_available_backends_empty_when_frontends_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No installed frontend -> no backends, and no import error leaks out."""
    monkeypatch.setattr(SlangBackend, "available", lambda self: False)
    monkeypatch.setattr(GhdlBackend, "available", lambda self: False)
    assert available_backends() == []
    assert available_backends(["slang"]) == []


def test_slang_available_probe_is_import_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The availability probe returns False on ImportError, never raises."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "pyslang":
            raise ImportError("simulated: pyslang not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert SlangBackend().available() is False


def test_build_enrich_without_backends_warns_with_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`build --enrich` with no backend installed degrades to a helpful warning."""
    (tmp_path / "a.sv").write_text("module a;\nendmodule\n")
    # Simulate a bare install: no enrichment backend is available.
    monkeypatch.setattr("hdl_kgraph.pipeline.available_backends", lambda names=None: [])

    report = run_build(tmp_path, options=BuildOptions(enrich=True))

    assert any("hdl-kgraph[enrich]" in w for w in report.warnings), report.warnings
    # The heuristic graph is still produced — enrichment is purely additive.
    assert report.parsed_files == 1
