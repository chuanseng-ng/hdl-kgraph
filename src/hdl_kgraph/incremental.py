"""Change detection and dirty-set closure for incremental rebuilds (M4).

``update`` re-parses the files whose content hash changed plus their
preprocessor-dependent files, derived from the *stored* graph:

* reverse ``INCLUDES`` — editing (or removing) a header dirties every unit
  that spliced it;
* ``DEFINES_MACRO`` → reverse ``USES_MACRO`` — editing a file that defines a
  macro dirties every file that expanded it;
* unresolved ``INCLUDE_FILE`` stubs — an *added* file whose path now
  satisfies a previously failing ``\\`include`` dirties the includers.

Known limit (documented): the closure reads the stored graph, so an edit
that *newly* defines a macro some other unchanged file already uses by name
is not detected — ``update --full`` covers that case. Changed filelist
defines/incdirs change the build-options fingerprint instead and trigger a
full rebuild upstream.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from hdl_kgraph.config import CONFIG_FILENAME
from hdl_kgraph.schema import EdgeKind, NodeKind

_FILE_ID_PREFIX = "file:"

#: Non-HDL inputs that still invalidate a build (filelists, config).
EXTRA_SUFFIXES = frozenset({".f", ".vc"})


@dataclass
class ChangeSet:
    """Hash-diff of the stored file table against the current tree."""

    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.changed or self.added or self.removed)


def diff_hashes(stored: Mapping[str, str], current: Mapping[str, str]) -> ChangeSet:
    """Compare content hashes by relpath (skipped files hash equal-empty)."""
    return ChangeSet(
        changed=sorted(p for p, h in current.items() if p in stored and stored[p] != h),
        added=sorted(p for p in current if p not in stored),
        removed=sorted(p for p in stored if p not in current),
    )


def _file_relpath(node_id: str) -> str | None:
    if node_id.startswith(_FILE_ID_PREFIX):
        return node_id[len(_FILE_ID_PREFIX) :]
    return None


def dirty_closure(graph: nx.MultiDiGraph, seeds: Mapping[str, str]) -> dict[str, str]:
    """Expand *seeds* (relpath -> reason) through include/macro dependents.

    Returns seeds plus every transitively dependent file, each mapped to the
    reason it must be re-parsed.
    """
    dirty = dict(seeds)
    pending = list(seeds)
    while pending:
        relpath = pending.pop()
        file_id = _FILE_ID_PREFIX + relpath
        if file_id not in graph:
            continue
        dependents: list[tuple[str, str]] = []
        for src, _, data in graph.in_edges(file_id, data=True):
            if data["kind"] is EdgeKind.INCLUDES:
                includer = _file_relpath(src)
                if includer is not None:
                    dependents.append((includer, f"includes {relpath}"))
        for _, macro_id, data in graph.out_edges(file_id, data=True):
            if data["kind"] is not EdgeKind.DEFINES_MACRO:
                continue
            macro_name = graph.nodes[macro_id]["name"]
            for user, _, use in graph.in_edges(macro_id, data=True):
                if use["kind"] is EdgeKind.USES_MACRO:
                    user_rel = _file_relpath(user)
                    if user_rel is not None:
                        dependents.append((user_rel, f"uses `{macro_name}"))
        for dep, reason in dependents:
            if dep not in dirty:
                dirty[dep] = reason
                pending.append(dep)
    return dirty


def newly_resolvable_includes(graph: nx.MultiDiGraph, added: Iterable[str]) -> dict[str, str]:
    """Files whose unresolved ``\\`include`` may now resolve to an added file."""
    added = list(added)
    if not added:
        return {}
    dirty: dict[str, str] = {}
    for node_id, data in graph.nodes(data=True):
        if data["kind"] is not NodeKind.INCLUDE_FILE or not data["attrs"].get("unresolved"):
            continue
        path_text = data["name"]
        if not any(a == path_text or a.endswith("/" + path_text) for a in added):
            continue
        for src, _, edge in graph.in_edges(node_id, data=True):
            if edge["kind"] is EdgeKind.INCLUDES:
                includer = _file_relpath(src)
                if includer is not None:
                    dirty.setdefault(includer, f"include {path_text!r} now resolvable")
    return dirty


def is_build_input(relpath: str, suffixes: frozenset[str]) -> bool:
    """True for paths whose change can invalidate a build (HDL/.f/config)."""
    path = Path(relpath)
    return path.suffix in suffixes or path.suffix in EXTRA_SUFFIXES or path.name == CONFIG_FILENAME


def detect_git_changes(base: Path, ref: str, suffixes: frozenset[str]) -> ChangeSet:
    """Diff the working tree against *ref*, filtered to build inputs.

    Raises ``RuntimeError`` when git is unavailable or *base* is not inside
    a work tree.
    """
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-status", ref, "--"],
            cwd=base,
            capture_output=True,
            text=True,
            check=True,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=base,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git executable not found") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip() or "git diff failed") from exc

    changes = ChangeSet()
    for line in diff.stdout.splitlines():
        status, _, rest = line.partition("\t")
        if not rest:
            continue
        # Renames/copies (R100\told\tnew) list old then new path.
        paths = rest.split("\t")
        if status.startswith(("R", "C")) and len(paths) == 2:
            old, new = paths
            if is_build_input(old, suffixes):
                changes.removed.append(old)
            if is_build_input(new, suffixes):
                changes.added.append(new)
            continue
        path = paths[0]
        if not is_build_input(path, suffixes):
            continue
        if status.startswith("A"):
            changes.added.append(path)
        elif status.startswith("D"):
            changes.removed.append(path)
        else:
            changes.changed.append(path)
    changes.added.extend(p for p in untracked.stdout.splitlines() if is_build_input(p, suffixes))
    for bucket in (changes.changed, changes.added, changes.removed):
        bucket.sort()
    return changes
