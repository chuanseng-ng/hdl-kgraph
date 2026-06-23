"""Tcl parser backends — SDC/XDC/UPF constraints first, flow scripts second (M10).

Implementation notes:

* SDC, XDC, and UPF are constrained Tcl subsets, so all three backends share
  one command tokenizer and the ``get_ports``/``get_pins``/``get_cells``/
  ``get_clocks`` object-query parser. Queries resolve to design nodes in
  pass 2 (exact name 1.0; glob patterns 0.8 unique / 0.6 ambiguous).
* Phase 1a (SDC/XDC, **implemented**): ``create_clock``/``create_generated_clock``
  -> CLOCK nodes (virtual and generated clocks supported); ``set_false_path``,
  ``set_multicycle_path``, ``set_input_delay``/``set_output_delay``,
  ``set_clock_groups`` -> TIMING_CONSTRAINT nodes with CONSTRAINS edges.
  ``create_clock`` is authoritative clock evidence: it upgrades M5's 0.4
  CLOCKED_BY heuristics to 1.0 (see :func:`hdl_kgraph.graph.clocks.apply_sdc_clock_evidence`),
  and ``set_clock_groups -asynchronous`` / ``set_false_path`` feed the CDC report
  as declared-safe crossings (see :func:`hdl_kgraph.graph.clocks.cdc_suspects`).
* Tcl is never *evaluated* — only literal ``set NAME value`` variable
  substitution is attempted (see ROADMAP.md "Risks"). Unknown commands and
  malformed input are tolerated: nothing here is a fatal parse error.
* Phase 1b (UPF, IEEE 1801) and Phase 2 (.tcl flow scripts) remain fail-loud
  stubs in this milestone.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from hdl_kgraph.ids import decl_node_id, file_node_id
from hdl_kgraph.parser.base import FileIR, UnresolvedRef, UnsupportedBackendError
from hdl_kgraph.schema import Edge, EdgeKind, Language, Node, NodeKind

SDC_SUFFIXES = frozenset({".sdc", ".xdc"})
UPF_SUFFIXES = frozenset({".upf"})
SCRIPT_SUFFIXES = frozenset({".tcl"})

#: ``get_*`` object-query commands → the query kind recorded on the CONSTRAINS
#: ref (pass-2 resolution maps each kind to the design NodeKind it targets).
_GET_QUERY = {
    "get_ports": "ports",
    "get_pins": "pins",
    "get_cells": "cells",
    "get_clocks": "clocks",
    "all_clocks": "clocks",
}

#: ``set_*`` timing-exception/delay commands → TIMING_CONSTRAINT.
_CONSTRAINT_COMMANDS = frozenset(
    {
        "set_false_path",
        "set_multicycle_path",
        "set_input_delay",
        "set_output_delay",
        "set_clock_groups",
        "set_max_delay",
        "set_min_delay",
    }
)

_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def is_glob(pattern: str) -> bool:
    """True if *pattern* contains a glob metacharacter (SDC uses shell globs)."""
    return any(ch in pattern for ch in "*?[")


def _split_words(text: str) -> list[str]:
    """Split one Tcl command into words, honoring ``{}`` / ``[]`` / ``"..."``.

    Grouping is preserved verbatim (e.g. ``[get_pins u/c[0]]`` stays one word,
    nested ``[0]`` included); the object-query parser unwraps it afterwards.
    """
    words: list[str] = []
    buf: list[str] = []
    brace = brack = 0
    in_quote = False
    for ch in text:
        if in_quote:
            buf.append(ch)
            if ch == '"':
                in_quote = False
            continue
        if ch == '"':
            in_quote = True
            buf.append(ch)
        elif ch == "{":
            brace += 1
            buf.append(ch)
        elif ch == "}":
            brace = max(0, brace - 1)
            buf.append(ch)
        elif ch == "[":
            brack += 1
            buf.append(ch)
        elif ch == "]":
            brack = max(0, brack - 1)
            buf.append(ch)
        elif ch.isspace() and brace == 0 and brack == 0:
            if buf:
                words.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        words.append("".join(buf))
    return words


def _iter_commands(text: str) -> Iterator[tuple[int, list[str]]]:
    """Yield ``(line_number, words)`` for each Tcl command.

    Backslash-newline continuations are joined; ``#`` at command position
    starts a comment (Tcl semantics — an inline ``#`` is not a comment); ``;``
    separates commands on one physical line.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start = i + 1  # 1-indexed line of the command
        raw = lines[i]
        while raw.endswith("\\") and i + 1 < len(lines):
            raw = raw[:-1] + " " + lines[i + 1]
            i += 1
        i += 1
        for piece in raw.split(";"):
            stripped = piece.strip()
            if not stripped or stripped.startswith("#"):
                continue
            words = _split_words(stripped)
            if words:
                yield start, words


def _substitute(words: list[str], variables: dict[str, str]) -> list[str]:
    """Literal ``$VAR`` / ``${VAR}`` substitution; unknown vars are left as-is."""
    if not variables:
        return words

    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return variables.get(name, match.group(0))

    return [_VAR_RE.sub(repl, w) for w in words]


def _object_patterns(word: str, default_kind: str) -> list[tuple[str, str]]:
    """Parse one object-query word into ``(query_kind, pattern)`` pairs.

    Handles ``[get_ports clk]``, ``[get_ports {a b}]``, ``[get_ports value*]``,
    a bare brace list ``{sys_clk div_clk}`` (clock names in ``-group``), and a
    bare name. An unrecognized ``[expr ...]`` yields nothing.
    """
    word = word.strip()
    if word.startswith("[") and word.endswith("]"):
        inner = _split_words(word[1:-1].strip())
        if not inner:
            return []
        kind = _GET_QUERY.get(inner[0])
        if kind is None:
            return []
        out: list[tuple[str, str]] = []
        for arg in inner[1:]:
            for pat in _unbrace(arg):
                out.append((kind, pat))
        return out
    if word.startswith("{") and word.endswith("}"):
        return [(default_kind, name) for name in _unbrace(word)]
    if word:
        return [(default_kind, word)]
    return []


def _unbrace(token: str) -> list[str]:
    """A ``{a b c}`` list → its elements; a bare token → ``[token]``."""
    token = token.strip()
    if token.startswith("{") and token.endswith("}"):
        return token[1:-1].split()
    return [token] if token else []


def _options(words: list[str]) -> dict[str, str]:
    """``-flag value`` pairs from a command's words (value-less flags map to "")."""
    opts: dict[str, str] = {}
    i = 0
    while i < len(words):
        word = words[i]
        if word.startswith("-"):
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if nxt and not nxt.startswith("-") and not nxt.startswith("["):
                opts[word] = nxt
                i += 2
                continue
            opts[word] = ""
        i += 1
    return opts


class SdcParser:
    """SDC/XDC timing-constraint pass-1 parser (M10 first wedge, issue #25)."""

    suffixes = SDC_SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        """Parse one SDC/XDC file into its per-file IR. Tolerates malformed input."""
        relpath = path.as_posix()
        ir = FileIR(path=relpath)
        file_id = file_node_id(relpath)
        ir.nodes.append(
            Node(
                id=file_id,
                kind=NodeKind.FILE,
                name=path.name,
                qualified_name=relpath,
                file=relpath,
                language=Language.TCL,
                attrs={"flavor": "sdc"},
            )
        )
        try:
            self._scan(ir, relpath, file_id, text)
        except Exception as exc:  # defensive: a parser bug must not abort the build
            ir.record_parse_error(f"{relpath}: internal parser error ({exc})")
        return ir

    def _scan(self, ir: FileIR, relpath: str, file_id: str, text: str) -> None:
        variables: dict[str, str] = {}
        used_ids: set[str] = set()
        for idx, (line, raw_words) in enumerate(_iter_commands(text)):
            words = _substitute(raw_words, variables)
            command = words[0]
            if command == "set" and len(words) >= 3 and not words[1].startswith("-"):
                variables[words[1]] = words[2]
            elif command in ("create_clock", "create_generated_clock"):
                self._clock(ir, relpath, file_id, used_ids, command, words, line)
            elif command in _CONSTRAINT_COMMANDS:
                self._constraint(ir, relpath, file_id, used_ids, idx, command, words, line)
            # everything else (set_units, current_design, ...): ignored, not an error

    def _new_node(
        self,
        ir: FileIR,
        relpath: str,
        file_id: str,
        used_ids: set[str],
        kind: NodeKind,
        name: str,
        line: int,
        attrs: dict[str, object],
    ) -> Node:
        node_id = decl_node_id(relpath, kind, name)
        if node_id in used_ids:
            node_id = f"{node_id}@{line}"
        used_ids.add(node_id)
        node = Node(
            id=node_id,
            kind=kind,
            name=name,
            qualified_name=name,
            file=relpath,
            line_span=(line, line),
            language=Language.TCL,
            attrs={k: v for k, v in attrs.items() if v is not None},
        )
        ir.nodes.append(node)
        ir.local_edges.append(Edge(src=file_id, dst=node.id, kind=EdgeKind.DECLARES))
        return node

    def _constrains(
        self, ir: FileIR, src_id: str, query_kind: str, pattern: str, line: int, **extra: object
    ) -> None:
        ir.unresolved_refs.append(
            UnresolvedRef(
                edge_kind=EdgeKind.CONSTRAINS,
                src_id=src_id,
                target_name=pattern,
                line_span=(line, line),
                attrs={"query": query_kind, "pattern": pattern, **extra},
            )
        )

    def _clock(
        self,
        ir: FileIR,
        relpath: str,
        file_id: str,
        used_ids: set[str],
        command: str,
        words: list[str],
        line: int,
    ) -> None:
        opts = _options(words)
        generated = command == "create_generated_clock"
        name = opts.get("-name", "")
        # The clock's own net is the trailing positional object query (the last
        # non-flag word that is an object query) — NOT ``-source`` (that names
        # the generated clock's master, recorded only in attrs).
        positionals = [
            w
            for w in words[1:]
            if not w.startswith("-") and (w.startswith("[") or w.startswith("{"))
        ]
        target = positionals[-1] if positionals else ""
        if not name:
            # An unnamed clock takes the name of its first source object.
            objs = _object_patterns(target, "ports")
            name = objs[0][1] if objs else f"clk@{line}"
        attrs: dict[str, object] = {
            "period": opts.get("-period"),
            "generated": generated or None,
            "virtual": not target or None,
            "master_source": opts.get("-source") or None,
            "divide_by": opts.get("-divide_by"),
            "multiply_by": opts.get("-multiply_by"),
        }
        node = self._new_node(ir, relpath, file_id, used_ids, NodeKind.CLOCK, name, line, attrs)
        for query_kind, pattern in _object_patterns(target, "ports"):
            self._constrains(ir, node.id, query_kind, pattern, line, role="clock_source")

    def _constraint(
        self,
        ir: FileIR,
        relpath: str,
        file_id: str,
        used_ids: set[str],
        idx: int,
        command: str,
        words: list[str],
        line: int,
    ) -> None:
        set_type = command[len("set_") :]
        attrs: dict[str, object] = {"set_type": set_type}
        node = self._new_node(
            ir,
            relpath,
            file_id,
            used_ids,
            NodeKind.TIMING_CONSTRAINT,
            f"{set_type}@{idx}",
            line,
            attrs,
        )
        if command == "set_clock_groups":
            self._clock_groups(ir, node, words, line)
            return
        # -from / -to / -through object queries (and the leading multicycle value).
        endpoints: dict[str, list[str]] = {}
        i = 1
        while i < len(words):
            word = words[i]
            if word in (
                "-from",
                "-to",
                "-through",
                "-rise_from",
                "-fall_from",
                "-rise_to",
                "-fall_to",
            ):
                role = word.lstrip("-").split("_")[-1]  # rise_from -> from
                if i + 1 < len(words):
                    for query_kind, pattern in _object_patterns(words[i + 1], "ports"):
                        endpoints.setdefault(role, []).append(pattern)
                        self._constrains(ir, node.id, query_kind, pattern, line, role=role)
                    i += 2
                    continue
            elif word == "-clock" and i + 1 < len(words):
                node.attrs["clock"] = words[i + 1]
                i += 2
                continue
            elif word.startswith("[") or word.startswith("{"):
                # A trailing/standalone object query (e.g. set_input_delay's port list).
                for query_kind, pattern in _object_patterns(word, "ports"):
                    endpoints.setdefault("to", []).append(pattern)
                    self._constrains(ir, node.id, query_kind, pattern, line, role="to")
            elif not word.startswith("-") and i == 1:
                node.attrs["value"] = word  # multicycle count / delay value
            i += 1
        for role, names in endpoints.items():
            node.attrs[role] = names

    def _clock_groups(self, ir: FileIR, node: Node, words: list[str], line: int) -> None:
        node.attrs["asynchronous"] = "-asynchronous" in words or "-async" in words
        groups: list[list[str]] = []
        i = 1
        while i < len(words):
            if words[i] == "-group" and i + 1 < len(words):
                members = [pat for _kind, pat in _object_patterns(words[i + 1], "clocks")]
                groups.append(members)
                for query_kind, pattern in _object_patterns(words[i + 1], "clocks"):
                    self._constrains(ir, node.id, query_kind, pattern, line, role="group")
                i += 2
                continue
            i += 1
        node.attrs["groups"] = groups


class UpfParser:
    """UPF (IEEE 1801) power-intent pass-1 parser. M10 work item."""

    suffixes = UPF_SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        raise UnsupportedBackendError("UPF parsing lands in milestone M10")


class TclScriptParser:
    """Tool-flow Tcl script pass-1 scanner. M10 work item."""

    suffixes = SCRIPT_SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        raise UnsupportedBackendError("Tcl flow-script scanning lands in milestone M10")
