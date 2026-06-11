"""Watch-mode tests (M4): debounce logic, no real filesystem watching."""

import queue
from itertools import count

import pytest

from hdl_kgraph.watch import Debouncer, is_watch_relevant, watch_loop


def test_relevance_filter() -> None:
    assert is_watch_relevant("rtl/top.sv")
    assert is_watch_relevant("rtl/alu.vhd")
    assert is_watch_relevant("sim/tb.f")
    assert is_watch_relevant("hdl-kgraph.toml")
    assert not is_watch_relevant("notes.txt")
    assert not is_watch_relevant("top.sv.swp")  # editor swap file
    # The update writing graph.db must never re-trigger the watcher.
    assert not is_watch_relevant(".hdl-kgraph/graph.db")
    assert not is_watch_relevant("/abs/project/.hdl-kgraph/graph.db")


def test_debouncer_collapses_a_burst() -> None:
    debouncer = Debouncer(quiet_s=0.3)
    for ms in range(0, 50):  # 50 events, 1ms apart: one save burst
        debouncer.note(f"rtl/file{ms}.sv", now=ms / 1000)
        assert not debouncer.ready(now=ms / 1000)
    assert not debouncer.ready(now=0.05 + 0.29)  # still inside the quiet window
    assert debouncer.ready(now=0.049 + 0.3)
    batch = debouncer.drain()
    assert len(batch) == 50
    assert not debouncer.ready(now=10.0)  # drained: nothing pending


def test_debouncer_new_event_restarts_the_window() -> None:
    debouncer = Debouncer(quiet_s=0.3)
    debouncer.note("a.sv", now=0.0)
    debouncer.note("b.sv", now=0.25)
    assert not debouncer.ready(now=0.3)  # 0.05s after the last event
    assert debouncer.ready(now=0.55)


def test_debouncer_ignores_irrelevant_paths() -> None:
    debouncer = Debouncer(quiet_s=0.0)
    debouncer.note(".hdl-kgraph/graph.db", now=0.0)
    debouncer.note("README.md", now=0.0)
    assert not debouncer.ready(now=1.0)


def test_watch_loop_delivers_one_batch_per_burst() -> None:
    events: queue.Queue = queue.Queue()
    for i in range(50):
        events.put(f"rtl/file{i}.sv")
    events.put("ignored.txt")
    batches: list[set[str]] = []
    ticks = count()
    watch_loop(
        events,
        batches.append,
        quiet_s=0.01,
        clock=lambda: next(ticks) * 0.001,
        max_batches=1,
    )
    assert len(batches) == 1
    assert len(batches[0]) == 50


def test_watch_loop_real_observer_smoke(tmp_path) -> None:
    pytest.importorskip("watchdog")
    import threading

    from hdl_kgraph.pipeline import run_build
    from hdl_kgraph.watch import run_watch

    (tmp_path / "a.sv").write_text("module a;\nendmodule\n")
    run_build(tmp_path)
    reports = []
    done = threading.Event()

    def on_report(report) -> None:
        reports.append(report)
        if len(reports) >= 2:
            done.set()
            raise KeyboardInterrupt  # stop the loop from inside

    import contextlib

    def runner() -> None:
        with contextlib.suppress(KeyboardInterrupt):
            run_watch(tmp_path, quiet_s=0.05, on_report=on_report)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    # First report is the initial sync; then trigger one real event burst.
    import time

    deadline = time.monotonic() + 10
    while not reports and time.monotonic() < deadline:
        time.sleep(0.02)
    (tmp_path / "a.sv").write_text("module a2;\nendmodule\n")
    done.wait(timeout=10)
    thread.join(timeout=10)
    assert len(reports) >= 2
    assert reports[0].up_to_date  # initial sync right after build
    assert reports[1].reparsed == {"a.sv": "changed"}
