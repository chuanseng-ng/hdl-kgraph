"""Source-level workarounds for ``tree-sitter-systemverilog`` grammar gaps.

The bundled grammar (gmlarumbe ``tree-sitter-systemverilog`` 0.3.1, the latest
release as of writing) rejects a *function call* used as the casting type of a
size cast — for example ``$clog2(QDEPTH)'(1)`` — even though the IEEE 1800 BNF
permits a ``constant_function_call`` there (``casting_type ::=
... | constant_primary | ...``). The construct is common in RTL that sizes a
literal to a derived width.

:func:`normalize_sv_source` rewrites such casts into a form the grammar accepts
by parenthesizing the casting type:

    $clog2(QDEPTH)'(1)   ->   ($clog2(QDEPTH))'(1)

The rewrite is semantically identical and only ever inserts ``(`` / ``)``
*within* a line, never adding or removing newlines. That keeps the
preprocessor's line map (which is line-level only) valid: every output line
still maps to the same original source line.
"""

from __future__ import annotations

_IDENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$"
)


def _prev_significant(text: str, index: int) -> int:
    """Index of the nearest non-whitespace char before *index* (or -1)."""
    j = index - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    return j


def normalize_sv_source(text: str) -> str:
    """Return *text* with function-call size casts made grammar-parseable.

    Wraps a function-call casting type in parentheses so the bundled
    tree-sitter grammar can parse the size cast (see module docstring). The
    result has the same number of lines as the input. When no such cast is
    present the original string is returned unchanged.
    """
    # Collected as (position, char) insertions, applied after the scan so that
    # earlier insertions never disturb indices computed later in the pass.
    inserts: list[tuple[int, str]] = []

    # Stack of (open_paren_index, is_call) for currently open parens, where
    # ``is_call`` records whether the char before "(" is an identifier char.
    stack: list[tuple[int, bool]] = []
    # The most recently closed paren group, kept only while nothing but
    # whitespace has followed it: (open_index, close_index, is_call).
    prev_close: tuple[int, int, bool] | None = None

    in_line_comment = False
    in_block_comment = False
    in_string = False

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if ch == "\\":
                i += 2  # skip escaped char (e.g. \")
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue

        if ch == "(":
            j = _prev_significant(text, i)
            is_call = j >= 0 and text[j] in _IDENT_CHARS
            stack.append((i, is_call))
            prev_close = None
            i += 1
            continue
        if ch == ")":
            if stack:
                open_index, is_call = stack.pop()
                prev_close = (open_index, i, is_call)
            i += 1
            continue

        if ch == "'" and nxt == "(":
            # Size-cast operator. If the casting type is a function call we just
            # closed (``name(...)``), wrap it: name(...) -> (name(...)).
            j = _prev_significant(text, i)
            if (
                j >= 0
                and text[j] == ")"
                and prev_close is not None
                and prev_close[1] == j
                and prev_close[2]
            ):
                open_index = prev_close[0]
                start = open_index - 1
                while start >= 0 and text[start] in _IDENT_CHARS:
                    start -= 1
                name_start = start + 1
                inserts.append((name_start, "("))
                inserts.append((i, ")"))
            i += 1
            continue

        if ch not in " \t\n":
            prev_close = None
        i += 1

    if not inserts:
        return text

    inserts.sort()
    out: list[str] = []
    last = 0
    for pos, char in inserts:
        out.append(text[last:pos])
        out.append(char)
        last = pos
    out.append(text[last:])
    return "".join(out)
