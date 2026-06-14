"""Version-control change detection for ``detect-changes`` (M4+).

``detect-changes`` can diff the working tree against a VCS ref to list the
build inputs that changed, for CI and scripting. This module provides that
detection for **git**, **Subversion (svn)**, and **Perforce (p4)** behind a
common interface, plus :func:`detect_vcs` to auto-detect which one a tree uses.

Every backend returns a :class:`~hdl_kgraph.incremental.ChangeSet` filtered to
build inputs (:func:`~hdl_kgraph.incremental.is_build_input`) so callers stay
VCS-agnostic. Subprocess failures are normalized to ``RuntimeError`` (executable
missing or command failed) the same way :func:`detect_git_changes` does, so the
CLI maps them to its "error" exit code.

Backend notes:

* **git** — reused from :mod:`hdl_kgraph.incremental`; the ref is any git
  revision (default ``HEAD``).
* **svn** — the ref is an svn revision number or keyword (``BASE``/``HEAD``/
  ``rNNN``). Without one we read ``svn status`` (working copy vs base, including
  unversioned ``?`` files, mirroring git's untracked handling).
* **p4** — Perforce is changelist/workspace based, so v1 reports the *local*
  workspace changes (opened files plus on-disk edits reconciled against the
  ``have`` revision). Diffing against an arbitrary submitted changelist is out
  of scope; a configured p4 client/connection is required.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hdl_kgraph.incremental import (
    ChangeSet,
    detect_git_changes,
    is_build_input,
    reject_option_like_ref,
)

__all__ = [
    "detect_vcs",
    "detect_vcs_changes",
    "detect_git_changes",
    "detect_svn_changes",
    "detect_p4_changes",
]

#: Per-VCS default ref used when the user selects a VCS without naming one.
_DEFAULT_REF = {"git": "HEAD", "svn": "BASE", "p4": "have"}


def detect_vcs(base: Path) -> str | None:
    """Auto-detect the VCS managing *base*: ``"git"``/``"svn"``/``"p4"``/None.

    Walks up from *base* looking for a ``.git`` (a file in worktrees, hence
    ``exists`` not ``is_dir``) or ``.svn`` entry; failing that, reports ``p4``
    when a Perforce connection is configured in the environment.
    """
    base = base.resolve()
    for directory in [base, *base.parents]:
        if (directory / ".git").exists():
            return "git"
        if (directory / ".svn").exists():
            return "svn"
    if os.environ.get("P4CONFIG") or os.environ.get("P4PORT"):
        return "p4"
    return None


def detect_vcs_changes(
    base: Path, vcs: str, ref: str | None, suffixes: frozenset[str]
) -> ChangeSet:
    """Dispatch change detection to *vcs*, defaulting *ref* per backend.

    Raises ``RuntimeError`` for an unknown VCS or any backend failure.
    """
    ref = ref if ref is not None else _DEFAULT_REF.get(vcs)
    if vcs == "git":
        return detect_git_changes(base, ref or "HEAD", suffixes)
    if vcs == "svn":
        return detect_svn_changes(base, ref, suffixes)
    if vcs == "p4":
        return detect_p4_changes(base, ref, suffixes)
    raise RuntimeError(f"unknown VCS {vcs!r}")


def _run(argv: list[str], base: Path, tool: str) -> str:
    """Run *argv* in *base*, returning stdout; failures become ``RuntimeError``."""
    try:
        result = subprocess.run(argv, cwd=base, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{tool} executable not found") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip() or f"{tool} {argv[1]} failed") from exc
    return result.stdout


# --------------------------------------------------------------------------- #
# Subversion
# --------------------------------------------------------------------------- #


def detect_svn_changes(base: Path, rev: str | None, suffixes: frozenset[str]) -> ChangeSet:
    """Diff the svn working copy against *rev*, filtered to build inputs.

    ``rev`` ``None`` or ``BASE`` means "working copy vs base" — ``svn status``,
    which also surfaces unversioned (``?``) files as additions, matching git's
    untracked handling. Any other revision uses ``svn diff --summarize -r REV``
    and folds in the still-unversioned files from ``svn status``.
    """
    changes = ChangeSet()
    if rev is None or rev == "BASE":
        entries = _parse_svn_status(_run(["svn", "status"], base, "svn"))
    else:
        reject_option_like_ref(rev, "svn")
        entries = _parse_svn_summarize(_run(["svn", "diff", "--summarize", "-r", rev], base, "svn"))
        # ``summarize`` only sees committed state; add live unversioned files.
        entries += [
            (code, path)
            for code, path in _parse_svn_status(_run(["svn", "status"], base, "svn"))
            if code == "?"
        ]
    for code, path in entries:
        if not is_build_input(path, suffixes):
            continue
        if code in ("A", "?"):
            changes.added.append(path)
        elif code in ("D", "!"):
            changes.removed.append(path)
        else:  # M, R, C, ...
            changes.changed.append(path)
    for bucket in (changes.changed, changes.added, changes.removed):
        bucket.sort()
    return changes


def _parse_svn_status(text: str) -> list[tuple[str, str]]:
    """Parse ``svn status`` lines into ``(code, path)`` pairs.

    The first seven columns are status flags; the path begins at column 9.
    """
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        if len(line) < 9 or line[0] == " ":
            continue
        entries.append((line[0], line[8:].strip()))
    return entries


def _parse_svn_summarize(text: str) -> list[tuple[str, str]]:
    """Parse ``svn diff --summarize`` lines into ``(code, path)`` pairs.

    Each line is a single status token (``A``/``D``/``M``/``R``) then the path.
    """
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        token, _, rest = line.partition(" ")
        rest = rest.strip()
        if token and rest:
            entries.append((token[0], rest))
    return entries


# --------------------------------------------------------------------------- #
# Perforce
# --------------------------------------------------------------------------- #

#: Perforce action -> ChangeSet bucket. ``move/add`` etc. carry a slash.
_P4_ACTION_BUCKET = {
    "edit": "changed",
    "integrate": "changed",
    "add": "added",
    "branch": "added",
    "move/add": "added",
    "delete": "removed",
    "move/delete": "removed",
}


def detect_p4_changes(base: Path, rev: str | None, suffixes: frozenset[str]) -> ChangeSet:
    """Report Perforce workspace changes under *base*, filtered to build inputs.

    Combines ``p4 -ztag status`` (files changed on disk but not yet opened) with
    ``p4 -ztag opened`` (already-opened files). Tagged output gives a
    ``clientFile`` local path, so depot paths never need mapping. v1 always
    compares against the workspace's ``have`` revision; a non-default *rev*
    (changelist) is rejected rather than silently ignored.
    """
    if rev not in (None, "have"):
        raise RuntimeError(
            "Perforce backend compares against the workspace 'have' revision only; "
            "diffing a specific changelist is not supported yet"
        )
    records = _parse_ztag(_run(["p4", "-ztag", "status"], base, "p4"))
    records += _parse_ztag(_run(["p4", "-ztag", "opened"], base, "p4"))
    return _p4_records_to_changeset(records, base, suffixes)


def _parse_ztag(text: str) -> list[dict[str, str]]:
    """Parse ``p4 -ztag`` output: ``... key value`` lines, blank-line separated."""
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if line.startswith("... "):
            key, _, value = line[4:].partition(" ")
            current[key] = value
    if current:
        records.append(current)
    return records


def _p4_records_to_changeset(
    records: list[dict[str, str]], base: Path, suffixes: frozenset[str]
) -> ChangeSet:
    """Turn tagged p4 records into a ``ChangeSet`` (pure; unit-tested directly).

    Uses each record's ``clientFile`` (a local path) made relative to *base*;
    records outside *base* or with an unrecognized/unknown action are skipped.
    """
    base = base.resolve()
    buckets: dict[str, list[str]] = {"changed": [], "added": [], "removed": []}
    seen: set[tuple[str, str]] = set()
    for record in records:
        client = record.get("clientFile")
        bucket = _P4_ACTION_BUCKET.get(record.get("action", ""))
        if client is None or bucket is None:
            continue
        try:
            relpath = Path(client).resolve().relative_to(base).as_posix()
        except ValueError:
            continue
        if not is_build_input(relpath, suffixes) or (bucket, relpath) in seen:
            continue
        seen.add((bucket, relpath))
        buckets[bucket].append(relpath)
    changes = ChangeSet(
        changed=sorted(buckets["changed"]),
        added=sorted(buckets["added"]),
        removed=sorted(buckets["removed"]),
    )
    return changes
