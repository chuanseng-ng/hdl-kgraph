"""Lightweight phase profiler for the enrichment pass (pass-3 breakdown).

The whole-design ``--enrich`` pass is, on large designs, the dominant build
cost (see ``docs/benchmarks.md``), yet ``--timings`` only reports it as a single
``enrich (pass 3)`` line. This profiler splits that line into the phases the
backends actually spend time in — slang's parse / elaborate / tree-walk stages
and the graph delta-apply — so it is clear *which* part to optimize.

Collection is via :func:`time.perf_counter` spans (nanosecond-cheap), so it is
always on during a profiled pass; the CLI only prints it under ``--timings``.
The active timer is a module global the runner sets for the duration of a pass,
so backends (which implement a plain :class:`~hdl_kgraph.enrich.base`
``EnrichmentBackend`` protocol) can self-instrument without a signature change.
Outside a profiled pass (e.g. a backend unit test) :func:`phase` is a no-op.

Naming convention: a span whose name contains ``/`` (e.g. ``slang/elaborate``)
is a *detail* child of a top-level span (``slang:enrich``); the top-level spans
tile the pass and sum to ``enrich_s``, while the detail spans break one of them
down further. The CLI relies on this to print a totals block and a detail block.
"""

from __future__ import annotations

import contextlib
import time
from collections import defaultdict
from collections.abc import Iterator


class PhaseTimer:
    """Accumulates wall-clock seconds per named phase across a single pass."""

    def __init__(self) -> None:
        self.totals: dict[str, float] = defaultdict(float)

    @contextlib.contextmanager
    def span(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.totals[name] += time.perf_counter() - start


#: Active timer for the in-flight enrichment pass, set by the runner. ``None``
#: when enrichment runs outside a profiled pass, which makes :func:`phase` free.
_active: PhaseTimer | None = None


def set_active(timer: PhaseTimer | None) -> None:
    """Bind (or, with ``None``, clear) the timer :func:`phase` records into."""
    global _active
    _active = timer


@contextlib.contextmanager
def phase(name: str) -> Iterator[None]:
    """Time the wrapped block under *name* in the active timer, if any."""
    if _active is None:
        yield
    else:
        with _active.span(name):
            yield
