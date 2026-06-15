"""Shared CLI error/load/progress plumbing (split out of the CLI god module)."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import networkx as nx

from hdl_kgraph.pipeline import find_db
from hdl_kgraph.storage.sqlite_store import SchemaVersionError, SqliteStore


class CliError(click.ClickException):
    """An application/usage error.

    Exits ``2`` — distinct from a documented *negative result* (exit ``1``,
    e.g. ``detect-changes`` finding changes, or a name lookup matching nothing)
    and from success (``0``). This mirrors the ``git diff --exit-code``
    convention so scripts can tell "broken" from "found nothing". See the
    exit-code policy in :func:`main`'s help.
    """

    exit_code = 2


def _run_pipeline(action: Callable[[], Any], what: str) -> Any:
    """Run a build/update pipeline call, converting an unexpected failure into a
    clean exit-2 error instead of letting a raw traceback escape (OSError, a
    parser ``RuntimeError``, an internal invariant, …)."""
    try:
        return action()
    except click.exceptions.Exit:
        raise
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 — last line of defense for the CLI
        raise CliError(f"{what} failed: {type(exc).__name__}: {exc}") from exc


class _ProgressRenderer:
    """Default-on pipeline progress on stderr (so stdout reports stay clean).

    ``stage`` prints one line per pipeline stage; ``tick`` drives the
    pass 0+1 per-file counter — a single ``\\r``-rewritten line when stderr
    is a terminal, a milestone line every ``MILESTONE_EVERY`` files
    otherwise (CI logs, pipes). ``finish`` terminates a pending live line
    so later output starts on a fresh line.
    """

    MILESTONE_EVERY = 25
    MIN_INTERVAL_S = 0.1

    def __init__(self) -> None:
        # Resolve stderr at command runtime, not import time: Click's
        # CliRunner patches sys.stderr around each invocation.
        self._stream = sys.stderr
        self._isatty = bool(getattr(self._stream, "isatty", lambda: False)())
        self._live_len = 0  # width of the pending \r-rewritten line (0 = none)
        self._last_draw = 0.0
        self._last_milestone = 0

    def stage(self, line: str) -> None:
        self.finish()
        self._stream.write(line + "\n")
        self._stream.flush()
        self._last_milestone = 0

    def tick(self, done: int, total: int) -> None:
        if self._isatty:
            now = time.monotonic()
            if done != total and now - self._last_draw < self.MIN_INTERVAL_S:
                return
            text = f"pass 0+1: parsing {done}/{total} file(s)..."
            # Pad over any leftover from a longer previous draw.
            pad = " " * max(self._live_len - len(text), 0)
            self._stream.write("\r" + text + pad)
            self._stream.flush()
            self._live_len = len(text)
            self._last_draw = now
        elif done == total or done - self._last_milestone >= self.MILESTONE_EVERY:
            self._stream.write(f"pass 0+1: parsing {done}/{total} file(s)\n")
            self._stream.flush()
            self._last_milestone = done

    def finish(self) -> None:
        if self._live_len:
            self._stream.write("\n")
            self._stream.flush()
            self._live_len = 0


def _resolve_db(db_path: Path | None) -> Path:
    """The database path to read, defaulting to the nearest one upward."""
    if db_path is None:
        db_path = find_db(Path.cwd())
        if db_path is None:
            raise CliError(
                "no .hdl-kgraph/graph.db found here or in any parent directory; "
                "run `hdl-kgraph build` first or pass --db"
            )
    if not db_path.is_file():
        raise CliError(f"database not found: {db_path}")
    return db_path


def _load(db_path: Path | None) -> tuple[nx.MultiDiGraph, list, dict[str, str]]:
    try:
        return SqliteStore(_resolve_db(db_path)).load()
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
