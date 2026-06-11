"""Watch mode (M4): debounced re-`update` on filesystem changes.

watchdog (the ``[watch]`` extra) feeds raw filesystem events into a queue;
:func:`watch_loop` debounces them — an editor save burst, atomic-rename
dance, or `git checkout` collapses into one ``update`` per quiet period.
Event payloads are only a trigger: ``update`` re-hashes the tree itself, so
spurious or coalesced events cost one no-op update, never a wrong graph.

Events under ``.hdl-kgraph/`` are ignored (the update writing ``graph.db``
must not re-trigger the watcher), as is anything that is not a build input
(HDL sources, ``.f``/``.vc`` filelists, ``hdl-kgraph.toml``).
"""

from __future__ import annotations

import contextlib
import queue
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.discovery import SUFFIXES
from hdl_kgraph.incremental import is_build_input
from hdl_kgraph.pipeline import DB_DIRNAME, UpdateReport, run_update

DEFAULT_QUIET_S = 0.3


class WatchUnavailableError(RuntimeError):
    """watchdog is not installed (the ``[watch]`` extra)."""


def is_watch_relevant(path: str) -> bool:
    """True when a filesystem event on *path* should trigger an update."""
    if DB_DIRNAME in Path(path).parts:
        return False
    return is_build_input(path, SUFFIXES)


class Debouncer:
    """Accumulates event paths; ready after *quiet_s* without new events."""

    def __init__(self, quiet_s: float = DEFAULT_QUIET_S) -> None:
        self.quiet_s = quiet_s
        self._pending: set[str] = set()
        self._last = 0.0

    def note(self, path: str, now: float) -> None:
        if is_watch_relevant(path):
            self._pending.add(path)
            self._last = now

    def ready(self, now: float) -> bool:
        return bool(self._pending) and now - self._last >= self.quiet_s

    def drain(self) -> set[str]:
        batch, self._pending = self._pending, set()
        return batch


def watch_loop(
    events: queue.Queue,
    on_batch: Callable[[set[str]], None],
    *,
    quiet_s: float = DEFAULT_QUIET_S,
    clock: Callable[[], float] = time.monotonic,
    max_batches: int | None = None,
) -> int:
    """Drain *events* forever (or for *max_batches* quiet bursts, for tests).

    Returns the number of batches delivered to *on_batch*.
    """
    debouncer = Debouncer(quiet_s)
    poll_s = max(quiet_s / 2, 0.01)
    batches = 0
    while max_batches is None or batches < max_batches:
        with contextlib.suppress(queue.Empty):
            debouncer.note(events.get(timeout=poll_s), clock())
        if debouncer.ready(clock()):
            on_batch(debouncer.drain())
            batches += 1
    return batches


def run_watch(
    root: Path,
    db_path: Path | None = None,
    options: BuildOptions | None = None,
    *,
    quiet_s: float = DEFAULT_QUIET_S,
    on_report: Callable[[UpdateReport], None] = lambda report: None,
) -> None:
    """Run an initial ``update``, then one per debounced change burst.

    Blocks until interrupted (KeyboardInterrupt propagates after the
    observer shuts down cleanly). Raises :class:`WatchUnavailableError`
    when watchdog is missing.
    """
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ModuleNotFoundError as exc:
        raise WatchUnavailableError(
            "watch mode needs the watchdog package; install with: pip install 'hdl-kgraph[watch]'"
        ) from exc

    root = root.resolve()
    base = root.parent if root.is_file() else root
    events: queue.Queue = queue.Queue()

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event: Any) -> None:
            if event.is_directory:
                return
            for path in (getattr(event, "src_path", ""), getattr(event, "dest_path", "")):
                if path:
                    events.put(str(path))

    def do_update(_batch: set[str]) -> None:
        on_report(run_update(root, db_path, options))

    do_update(set())  # initial sync (full build if no database yet)
    observer = Observer()
    observer.schedule(_Handler(), str(base), recursive=True)
    observer.start()
    try:
        watch_loop(events, do_update, quiet_s=quiet_s)
    finally:
        observer.stop()
        observer.join()
