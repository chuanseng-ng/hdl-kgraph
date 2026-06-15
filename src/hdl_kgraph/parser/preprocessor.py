"""Lightweight SystemVerilog preprocessor (M2).

tree-sitter cannot expand macros, so heavily ``ifdef``'d code parses to
garbage and macro-defined module bodies are invisible without expansion
(ROADMAP.md risk #3). This module expands one compilation unit at a time,
in compile order, sharing one :class:`MacroTable` across files the way
simulators carry ``+define+`` and earlier-file defines forward.

* ``\\`define`` (with arguments and per-argument defaults), ``\\`undef``,
  ``\\`ifdef``/``\\`ifndef``/``\\`elsif``/``\\`else``/``\\`endif`` branch
  selection, and ``\\`include`` resolution (including-file directory first,
  then incdirs in order). Included text is spliced into the output so
  header-defined macros are visible.
* Every output line carries a :class:`LineOrigin` mapping it back to the
  original file and line — node spans stay accurate after substitution.
  Directive and suppressed lines become empty output lines, and multi-line
  macro expansions map every produced line to the invocation line.
* **Both-branches mode** (used when no define set is configured): every
  branch of a conditional on an *undefined* name is emitted; the branch
  normal evaluation would select keeps full confidence while the alternative
  branches are stamped ``ambiguous`` (consumers emit them at
  ``CONFIDENCE_AMBIGUOUS``). The asymmetry keeps ``\\`ifndef`` include
  guards and default-``\\`define`` fallbacks at full confidence.
* :class:`PreprocEmitter` converts the recorded events into MACRO /
  INCLUDE_FILE / FILE nodes and DEFINES_MACRO / USES_MACRO / INCLUDES edges,
  deduplicated across compilation units that share headers.

Out of scope (best effort, documented): ``\\`"`` stringification and
``\\`\\`` token pasting are textually stripped during substitution; macro
invocations must close their argument list on the invocation line.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from hdl_kgraph.ids import decl_node_id, file_node_id, stub_node_id
from hdl_kgraph.parser.base import FileIR, within_root
from hdl_kgraph.schema import (
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_RESOLVED,
    Edge,
    EdgeKind,
    Node,
    NodeKind,
)

#: Directives that are part of the language proper: passed through to the
#: parser untouched and never treated as macro uses.
_STANDARD_DIRECTIVES = frozenset(
    {
        "timescale",
        "default_nettype",
        "resetall",
        "celldefine",
        "endcelldefine",
        "pragma",
        "line",
        "unconnected_drive",
        "nounconnected_drive",
        "begin_keywords",
        "end_keywords",
    }
)

_CONDITIONAL_DIRECTIVES = frozenset({"ifdef", "ifndef", "elsif", "else", "endif"})
_PREPROC_DIRECTIVES = _CONDITIONAL_DIRECTIVES | {"define", "undef", "include"}

_DIRECTIVE_RE = re.compile(r"^\s*`(\w+)")
_DEFINE_RE = re.compile(r"^\s*`define\s+(\w+)")
_NAME_RE = re.compile(r"\w+")
_MACRO_USE_RE = re.compile(r"`(\w+)")
_INCLUDE_ARG_RE = re.compile(r'"([^"]+)"|<([^>]+)>')

DEFAULT_MAX_INCLUDE_DEPTH = 64
DEFAULT_MAX_EXPANSION_DEPTH = 64


@dataclass(frozen=True)
class LineOrigin:
    """Original source location of one expanded output line."""

    file: str  # relpath of the file the line came from
    line: int  # 1-based line number in that file
    ambiguous: bool = False  # from a non-selected both-branches region


@dataclass
class MacroDef:
    """One ``\\`define``; ``params`` is None for object-like macros."""

    name: str
    params: list[tuple[str, str | None]] | None  # (name, default)
    body: str  # may contain newlines (backslash continuations)
    file: str  # defining file relpath
    line: int


@dataclass
class MacroEvent:
    """One :class:`MacroTable` mutation, in textual processing order.

    Recorded so M4's incremental ``update`` can replay an unchanged unit's
    effect on the shared table (``\\`define`` params are not recoverable from
    the graph's MACRO nodes) without re-preprocessing the unit.
    """

    op: Literal["define", "undef"]
    name: str
    macro: MacroDef | None = None  # the definition, for op="define"


@dataclass
class MacroUse:
    name: str
    file: str  # use-site relpath
    line: int
    macro: MacroDef | None  # None: undefined at the use site
    ambiguous: bool = False


@dataclass
class IncludeEvent:
    path_text: str  # as written between quotes/brackets
    file: str  # includer relpath
    line: int
    resolved: str | None  # header relpath, None if not found


@dataclass
class PreprocessedFile:
    """Expansion result for one compilation unit."""

    path: str  # unit relpath
    text: str = ""
    line_map: list[LineOrigin] = field(default_factory=list)
    macro_defs: list[MacroDef] = field(default_factory=list)
    macro_events: list[MacroEvent] = field(default_factory=list)
    macro_uses: list[MacroUse] = field(default_factory=list)
    includes: list[IncludeEvent] = field(default_factory=list)
    included_relpaths: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


class MacroTable:
    """Macro definitions carried across files in compile order."""

    def __init__(self, defines: dict[str, str | None] | None = None) -> None:
        self._defs: dict[str, MacroDef] = {}
        for name, value in (defines or {}).items():
            self.define(MacroDef(name=name, params=None, body=value or "", file="", line=0))

    def define(self, macro: MacroDef) -> None:
        self._defs[macro.name] = macro

    def undef(self, name: str) -> None:
        self._defs.pop(name, None)

    def apply(self, event: MacroEvent) -> None:
        """Replay one recorded mutation (M4 incremental update)."""
        if event.op == "define" and event.macro is not None:
            self.define(event.macro)
        else:
            self.undef(event.name)

    def get(self, name: str) -> MacroDef | None:
        return self._defs.get(name)

    def defined(self, name: str) -> bool:
        return name in self._defs


@dataclass
class _Frame:
    """One ``\\`ifdef`` nesting level."""

    dead: bool  # enclosing context was not emitting at push time
    selected: bool  # current branch is the one normal evaluation picks
    ever_selected: bool
    ambiguous: bool  # both-branches chain: non-selected branches still emit


class Preprocessor:
    """Expands compilation units against a shared :class:`MacroTable`."""

    def __init__(
        self,
        *,
        base: Path,
        incdirs: Sequence[Path] = (),
        auto_incdirs: Sequence[Path] = (),
        macros: MacroTable | None = None,
        branch_mode: Literal["select", "both"] = "select",
        max_include_depth: int = DEFAULT_MAX_INCLUDE_DEPTH,
        max_expansion_depth: int = DEFAULT_MAX_EXPANSION_DEPTH,
    ) -> None:
        self.base = base.resolve()
        self.incdirs = [Path(d).resolve() for d in incdirs]
        # Fallback search dirs (discovered source dirs); explicit incdirs win.
        self.auto_incdirs = [Path(d).resolve() for d in auto_incdirs]
        self.macros = macros if macros is not None else MacroTable()
        self.branch_mode = branch_mode
        self.max_include_depth = max_include_depth
        self.max_expansion_depth = max_expansion_depth
        # Memoize resolution so a header included from many files is stat'd once.
        self._include_cache: dict[tuple[Path, str], Path | None] = {}

    def preprocess(self, path: Path, text: str | None = None) -> PreprocessedFile:
        path = path.resolve()
        relpath = self._relpath(path)
        pp = PreprocessedFile(path=relpath)
        if text is None:
            text = path.read_text(errors="replace")
        self._cond: list[_Frame] = []
        self._out: list[str] = []
        self._pp = pp
        self._process(path, relpath, text, include_stack=(path,))
        if self._cond:
            pp.warnings.append(f"{relpath}: unterminated `ifdef at end of file")
            self._cond.clear()
        pp.text = "\n".join(self._out) + ("\n" if self._out else "")
        return pp

    # -- the line loop ---------------------------------------------------------

    def _process(
        self, path: Path, relpath: str, text: str, include_stack: tuple[Path, ...]
    ) -> None:
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            raw = lines[i]
            consumed = 1
            match = _DIRECTIVE_RE.match(raw)
            name = match.group(1) if match else None
            arg_start = match.end() if match else 0
            if name == "define":
                # Join backslash continuations even in dead branches so body
                # lines are never misread as directives or source text.
                while raw.rstrip().endswith("\\") and i + consumed < len(lines):
                    raw = raw.rstrip()[:-1] + "\n" + lines[i + consumed]
                    consumed += 1
                if self._emitting():
                    self._handle_define(raw, relpath, i + 1)
                self._emit_blanks(relpath, i + 1, consumed)
            elif name in _CONDITIONAL_DIRECTIVES:
                self._handle_conditional(name, raw[arg_start:], relpath, i + 1)
                self._emit_blanks(relpath, i + 1, 1)
            elif name == "undef" and self._emitting():
                arg = _NAME_RE.search(raw[arg_start:])
                if arg is not None:
                    self.macros.undef(arg.group(0))
                    self._pp.macro_events.append(MacroEvent(op="undef", name=arg.group(0)))
                self._emit_blanks(relpath, i + 1, 1)
            elif name == "include" and self._emitting():
                self._emit_blanks(relpath, i + 1, 1)
                self._handle_include(raw[arg_start:], path, relpath, i + 1, include_stack)
            elif not self._emitting():
                self._emit_blanks(relpath, i + 1, 1)
            else:
                origin = LineOrigin(file=relpath, line=i + 1, ambiguous=self._ambiguous())
                expanded = self._expand(raw, origin, stack=())
                for out_line in expanded.split("\n"):
                    self._out.append(out_line)
                    self._pp.line_map.append(origin)
            i += consumed

    def _emitting(self) -> bool:
        return all(not f.dead and (f.selected or f.ambiguous) for f in self._cond)

    def _ambiguous(self) -> bool:
        return any(f.ambiguous and not f.selected for f in self._cond if not f.dead)

    def _emit_blanks(self, relpath: str, line: int, count: int) -> None:
        for offset in range(count):
            self._out.append("")
            self._pp.line_map.append(LineOrigin(file=relpath, line=line + offset))

    # -- directives --------------------------------------------------------------

    def _handle_define(self, raw: str, relpath: str, line: int) -> None:
        match = _DEFINE_RE.match(raw)
        if match is None:
            self._pp.warnings.append(f"{relpath}:{line}: malformed `define ignored")
            return
        name = match.group(1)
        rest = raw[match.end() :]
        params: list[tuple[str, str | None]] | None = None
        if rest.startswith("("):  # function-like only when '(' touches the name
            inner, length = _scan_parens(rest)
            if inner is None:
                self._pp.warnings.append(
                    f"{relpath}:{line}: unterminated `define parameter list ignored"
                )
                return
            params = []
            if inner.strip():
                for piece in _split_top_commas(inner):
                    pname, sep, default = piece.partition("=")
                    params.append((pname.strip(), default.strip() if sep else None))
            rest = rest[length:]
        macro = MacroDef(name=name, params=params, body=rest.strip(), file=relpath, line=line)
        self.macros.define(macro)
        self._pp.macro_defs.append(macro)
        self._pp.macro_events.append(MacroEvent(op="define", name=name, macro=macro))

    def _handle_conditional(self, name: str, arg: str, relpath: str, line: int) -> None:
        if name in ("ifdef", "ifndef"):
            tested = _NAME_RE.search(arg)
            if tested is None:
                self._pp.warnings.append(f"{relpath}:{line}: `{name} without a macro name")
            defined = tested is not None and self.macros.defined(tested.group(0))
            dead = not self._emitting()
            selected = defined if name == "ifdef" else not defined
            ambiguous = self.branch_mode == "both" and not defined and not dead
            self._cond.append(
                _Frame(dead=dead, selected=selected, ever_selected=selected, ambiguous=ambiguous)
            )
            return
        if not self._cond:
            self._pp.warnings.append(f"{relpath}:{line}: `{name} without matching `ifdef")
            return
        frame = self._cond[-1]
        if name == "elsif":
            tested = _NAME_RE.search(arg)
            defined = tested is not None and self.macros.defined(tested.group(0))
            frame.selected = not frame.ever_selected and defined
            frame.ever_selected |= frame.selected
            frame.ambiguous |= self.branch_mode == "both" and not defined and not frame.dead
        elif name == "else":
            frame.selected = not frame.ever_selected
            frame.ever_selected = True
        else:  # endif
            self._cond.pop()

    def _handle_include(
        self, arg: str, path: Path, relpath: str, line: int, include_stack: tuple[Path, ...]
    ) -> None:
        origin = LineOrigin(file=relpath, line=line, ambiguous=self._ambiguous())
        if "`" in arg:  # `include `MY_HEADER
            arg = self._expand(arg, origin, stack=())
        match = _INCLUDE_ARG_RE.search(arg)
        if match is None:
            self._pp.warnings.append(f"{relpath}:{line}: malformed `include ignored")
            return
        path_text = match.group(1) or match.group(2)
        resolved = self._resolve_include(path_text, path.parent)
        event = IncludeEvent(path_text=path_text, file=relpath, line=line, resolved=None)
        self._pp.includes.append(event)
        if resolved is None:
            self._pp.warnings.append(f'{relpath}:{line}: cannot resolve `include "{path_text}"')
            return
        if not self._within_allowed(resolved):
            # A `..`/absolute include path that resolved to a real file outside
            # the build root (and outside every configured incdir) would splice
            # (and disclose) out-of-tree source; drop it rather than read it (#68).
            self._pp.warnings.append(
                f'{relpath}:{line}: `include "{path_text}" escapes the build root, skipped'
            )
            return
        header_rel = self._relpath(resolved)
        event.resolved = header_rel  # the INCLUDES edge is real even if not spliced
        if resolved in include_stack:
            self._pp.warnings.append(f"{relpath}:{line}: include cycle via {path_text} skipped")
            return
        if len(include_stack) >= self.max_include_depth:
            self._pp.warnings.append(f"{relpath}:{line}: include depth limit reached")
            return
        self._pp.included_relpaths.add(header_rel)
        try:
            text = resolved.read_text(errors="replace")
        except OSError as exc:
            self._pp.warnings.append(f"{relpath}:{line}: cannot read {path_text}: {exc}")
            return
        self._process(resolved, header_rel, text, include_stack=(*include_stack, resolved))

    def _resolve_include(self, path_text: str, includer_dir: Path) -> Path | None:
        key = (includer_dir, path_text)
        if key in self._include_cache:
            return self._include_cache[key]
        resolved: Path | None = None
        # Explicit incdirs win over the auto-discovered fallback; the loop stops
        # at the first hit, so an already-resolvable include never stats the
        # auto dirs.
        for directory in [includer_dir, *self.incdirs, *self.auto_incdirs]:
            candidate = (directory / path_text).resolve()
            if candidate.is_file():
                resolved = candidate
                break
        self._include_cache[key] = resolved
        return resolved

    def _within_allowed(self, path: Path) -> bool:
        """True if *path* is inside the build root or any configured incdir.

        Confines ``\\`include`` resolution so a crafted ``..``/absolute path
        cannot reach out-of-tree source, while still honoring incdirs the
        operator explicitly trusted (e.g. shared vendor headers); see #68.
        Filelist ``+incdir+`` dirs are already root-contained at parse time.
        """
        return any(within_root(path, root) for root in (self.base, *self.incdirs))

    # -- macro expansion -----------------------------------------------------------

    def _expand(self, text: str, origin: LineOrigin, stack: tuple[str, ...]) -> str:
        out: list[str] = []
        pos = 0
        while True:
            match = _MACRO_USE_RE.search(text, pos)
            if match is None:
                out.append(text[pos:])
                break
            out.append(text[pos : match.start()])
            name = match.group(1)
            pos = match.end()
            if name == "__FILE__":
                out.append(f'"{origin.file}"')
                continue
            if name == "__LINE__":
                out.append(str(origin.line))
                continue
            if name in _STANDARD_DIRECTIVES or name in _PREPROC_DIRECTIVES or name in stack:
                out.append(match.group(0))  # passthrough / recursion guard
                continue
            macro = self.macros.get(name)
            if macro is None:
                self._pp.macro_uses.append(
                    MacroUse(
                        name=name,
                        file=origin.file,
                        line=origin.line,
                        macro=None,
                        ambiguous=origin.ambiguous,
                    )
                )
                out.append(match.group(0))  # leave for tree-sitter's error tolerance
                continue
            replacement, pos = self._expand_one(macro, text, pos, origin)
            if len(stack) >= self.max_expansion_depth:
                self._pp.warnings.append(
                    f"{origin.file}:{origin.line}: macro expansion depth limit at `{name}"
                )
            else:
                replacement = self._expand(replacement, origin, stack=(*stack, name))
            out.append(replacement)
        return "".join(out)

    def _expand_one(
        self, macro: MacroDef, text: str, pos: int, origin: LineOrigin
    ) -> tuple[str, int]:
        """Substitute one use of *macro* found at *pos*; returns (text, new pos)."""
        body = macro.body
        if macro.params is not None:
            while pos < len(text) and text[pos] in " \t":
                pos += 1
            inner, length = _scan_parens(text[pos:]) if text[pos : pos + 1] == "(" else (None, 0)
            if inner is None:
                self._pp.warnings.append(
                    f"{origin.file}:{origin.line}: `{macro.name} arguments must open and "
                    "close on the invocation line; left unexpanded"
                )
                self._pp.macro_uses.append(
                    MacroUse(
                        name=macro.name,
                        file=origin.file,
                        line=origin.line,
                        macro=None,
                        ambiguous=origin.ambiguous,
                    )
                )
                return f"`{macro.name}", pos
            pos += length
            args = [a.strip() for a in _split_top_commas(inner)] if inner.strip() else []
            if len(args) > len(macro.params):
                self._pp.warnings.append(
                    f"{origin.file}:{origin.line}: too many arguments for `{macro.name}"
                )
                args = args[: len(macro.params)]
            bindings: dict[str, str] = {}
            for index, (pname, default) in enumerate(macro.params):
                if index < len(args) and args[index]:
                    bindings[pname] = args[index]
                elif default is not None:
                    bindings[pname] = default
                else:
                    self._pp.warnings.append(
                        f"{origin.file}:{origin.line}: missing argument {pname!r} for `{macro.name}"
                    )
                    bindings[pname] = ""
            if bindings:
                # Substitute all parameters in one pass so an argument value that
                # happens to contain another parameter's name is never re-expanded.
                pattern = re.compile("|".join(rf"\b{re.escape(p)}\b" for p in bindings))
                body = pattern.sub(lambda m: bindings[m.group(0)], body)
        # Best-effort macro operators: stringification and token pasting.
        body = body.replace('`\\`"', '\\"').replace('`"', '"').replace("``", "")
        self._pp.macro_uses.append(
            MacroUse(
                name=macro.name,
                file=origin.file,
                line=origin.line,
                macro=macro,
                ambiguous=origin.ambiguous,
            )
        )
        return body, pos

    def _relpath(self, path: Path) -> str:
        """POSIX path relative to the build root; may contain ``..``."""
        return Path(os.path.relpath(path, self.base)).as_posix()


def _scan_parens(text: str) -> tuple[str | None, int]:
    """Scan a balanced ``(...)`` at ``text[0]``; returns (inner, chars consumed)."""
    depth = 0
    in_string = False
    for index, char in enumerate(text):
        if in_string:
            if char == '"' and text[index - 1] != "\\":
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[1:index], index + 1
    return None, 0


def _split_top_commas(text: str) -> list[str]:
    """Split on commas not nested in parens/brackets/braces/strings."""
    pieces: list[str] = []
    depth = 0
    in_string = False
    start = 0
    for index, char in enumerate(text):
        if in_string:
            if char == '"' and text[index - 1] != "\\":
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            pieces.append(text[start:index])
            start = index + 1
    pieces.append(text[start:])
    return pieces


class PreprocEmitter:
    """Converts preprocessor events into nodes/edges, deduplicated across units.

    Units sharing a header would otherwise re-emit the header's MACRO nodes
    and DEFINES_MACRO/INCLUDES edges; one emitter instance spans the whole
    build and keeps first occurrences only. Minimal FILE nodes are emitted
    for headers so edges never dangle — the pass-2 linker keeps the parser's
    richer FILE node when one exists.
    """

    def __init__(self) -> None:
        self._macro_ids: dict[tuple[str, str, int], str] = {}
        self._used_macro_ids: set[str] = set()
        self._seen_nodes: set[str] = set()
        self._seen_edges: set[tuple[str, str, str]] = set()

    def emit(self, pp: PreprocessedFile, ir: FileIR) -> None:
        """Append *pp*'s MACRO/FILE/INCLUDE_FILE nodes and edges to *ir*."""
        for macro in pp.macro_defs:
            macro_id = self._ensure_macro_node(ir, macro)
            self._file_stub(ir, macro.file)
            self._add_edge_once(
                ir, Edge(src=file_node_id(macro.file), dst=macro_id, kind=EdgeKind.DEFINES_MACRO)
            )

        for use in self._aggregate_uses(pp):
            if use.macro is None:
                dst = stub_node_id(NodeKind.MACRO, use.name)
                self._add_node_once(
                    ir,
                    Node(
                        id=dst,
                        kind=NodeKind.MACRO,
                        name=use.name,
                        qualified_name=use.name,
                        attrs={"unresolved": True},
                    ),
                )
            else:
                # Also materializes macros that came from +define+/--define/
                # config rather than a `define in some source file.
                dst = self._ensure_macro_node(ir, use.macro)
            self._file_stub(ir, use.file)
            self._add_edge_once(
                ir,
                Edge(
                    src=file_node_id(use.file),
                    dst=dst,
                    kind=EdgeKind.USES_MACRO,
                    confidence=CONFIDENCE_AMBIGUOUS if use.ambiguous else CONFIDENCE_RESOLVED,
                    attrs={"line": use.line},
                ),
            )

        for event in pp.includes:
            if event.resolved is None:
                dst = stub_node_id(NodeKind.INCLUDE_FILE, event.path_text)
                self._add_node_once(
                    ir,
                    Node(
                        id=dst,
                        kind=NodeKind.INCLUDE_FILE,
                        name=event.path_text,
                        qualified_name=event.path_text,
                        attrs={"unresolved": True},
                    ),
                )
            else:
                dst = file_node_id(event.resolved)
                self._file_stub(ir, event.resolved)
            self._file_stub(ir, event.file)
            self._add_edge_once(
                ir,
                Edge(
                    src=file_node_id(event.file),
                    dst=dst,
                    kind=EdgeKind.INCLUDES,
                    attrs={"line": event.line},
                ),
            )

    def _macro_id(self, macro: MacroDef) -> str:
        key = (macro.file, macro.name, macro.line)
        macro_id = self._macro_ids.get(key)
        if macro_id is None:
            if macro.file:
                macro_id = decl_node_id(macro.file, NodeKind.MACRO, macro.name)
            else:  # +define+/--define/config: no defining file
                macro_id = f"macro:{macro.name}"
            if macro_id in self._used_macro_ids:  # redefinition later in the file
                macro_id = f"{macro_id}@{macro.line}"
            self._used_macro_ids.add(macro_id)
            self._macro_ids[key] = macro_id
        return macro_id

    def _ensure_macro_node(self, ir: FileIR, macro: MacroDef) -> str:
        macro_id = self._macro_id(macro)
        self._add_node_once(
            ir,
            Node(
                id=macro_id,
                kind=NodeKind.MACRO,
                name=macro.name,
                qualified_name=macro.name,
                file=macro.file,
                line_span=(macro.line, macro.line),
                attrs={
                    "body": macro.body,
                    "arity": None if macro.params is None else len(macro.params),
                    **({} if macro.file else {"from_options": True}),
                },
            ),
        )
        return macro_id

    def _aggregate_uses(self, pp: PreprocessedFile) -> list[MacroUse]:
        """First use per (file, macro, ambiguity); repeats add no information."""
        seen: set[tuple[str, str, int | None, bool]] = set()
        firsts: list[MacroUse] = []
        for use in pp.macro_uses:
            key = (
                use.file,
                use.name,
                None if use.macro is None else use.macro.line,
                use.ambiguous,
            )
            if key not in seen:
                seen.add(key)
                firsts.append(use)
        return firsts

    def _file_stub(self, ir: FileIR, relpath: str) -> None:
        self._add_node_once(
            ir,
            Node(
                id=file_node_id(relpath),
                kind=NodeKind.FILE,
                name=relpath.rsplit("/", 1)[-1],
                qualified_name=relpath,
                file=relpath,
            ),
        )

    def _add_node_once(self, ir: FileIR, node: Node) -> bool:
        if node.id in self._seen_nodes:
            return False
        self._seen_nodes.add(node.id)
        ir.nodes.append(node)
        return True

    def _add_edge_once(self, ir: FileIR, edge: Edge) -> None:
        key = (edge.src, edge.dst, edge.kind.value)
        if key not in self._seen_edges:
            self._seen_edges.add(key)
            ir.local_edges.append(edge)
