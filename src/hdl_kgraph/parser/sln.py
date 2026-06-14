"""SLN (Cadence Perspec System Level Notation) parser backend (M10).

Implementation notes:

* SLN is Cadence's portable-stimulus language for Perspec System Verifier.
  It is proprietary with no public grammar, so extraction targets the
  documented subset, best-effort (see ROADMAP.md "Risks"). Accellera PSS
  (``.pss``), the openly specified sibling format, is the natural follow-on.
* Extracts actions, scenarios, and resources -> SCENARIO/ACTION nodes
  (resources in attrs); scenario -> DUT module linkage via TEST_COVERS,
  resolved by name in pass 2.
* ``.sln`` collides with Visual Studio solution files: content-sniff for the
  ``Microsoft Visual Studio Solution File`` header and skip such files.
"""

from __future__ import annotations

from pathlib import Path

from hdl_kgraph.parser.base import FileIR, UnsupportedBackendError

SUFFIXES = frozenset({".sln"})


class SlnParser:
    """Perspec SLN scenario pass-1 parser. M10 work item."""

    suffixes = SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        raise UnsupportedBackendError("SLN parsing lands in milestone M10")
