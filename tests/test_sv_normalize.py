"""Unit tests for the SystemVerilog source normalizer (grammar workarounds)."""

from hdl_kgraph.parser.sv_normalize import normalize_sv_source


def test_wraps_system_function_call_cast() -> None:
    assert normalize_sv_source("a = $clog2(8)'(1);") == "a = ($clog2(8))'(1);"


def test_wraps_user_function_call_cast_with_nested_args() -> None:
    assert normalize_sv_source("a = width(N+1)'(x);") == "a = (width(N+1))'(x);"
    assert normalize_sv_source("a = f(g(2))'(x);") == "a = (f(g(2)))'(x);"


def test_wraps_multiple_casts_on_one_line() -> None:
    src = "a = $clog2(D)'(1) + $clog2(D+1)'(2);"
    assert normalize_sv_source(src) == "a = ($clog2(D))'(1) + ($clog2(D+1))'(2);"


def test_leaves_non_call_casts_untouched() -> None:
    for src in ("a = 4'(1);", "a = W'(1);", "a = (W)'(1);", "a = type_t'(x);"):
        assert normalize_sv_source(src) == src


def test_leaves_based_literals_and_patterns_untouched() -> None:
    for src in ("a = 4'b0;", "a = '0;", "a = '1;", "a = '{1, 2};"):
        assert normalize_sv_source(src) == src


def test_ignores_casts_inside_line_comment() -> None:
    src = "a = 0; // example $clog2(8)'(1)\n"
    assert normalize_sv_source(src) == src


def test_ignores_casts_inside_block_comment() -> None:
    src = "/* see $clog2(8)'(1) */ a = 0;"
    assert normalize_sv_source(src) == src


def test_ignores_casts_inside_string() -> None:
    src = 'a = "$clog2(8)\'(1)";'
    assert normalize_sv_source(src) == src


def test_preserves_line_count() -> None:
    src = "module m;\n  a = $clog2(8)'(1);\n  b = $clog2(9)'(2);\nendmodule\n"
    out = normalize_sv_source(src)
    assert out.count("\n") == src.count("\n")
    assert "($clog2(8))'(1)" in out and "($clog2(9))'(2)" in out


def test_returns_input_unchanged_when_no_cast() -> None:
    src = "module m; logic [$clog2(8)-1:0] a; endmodule"
    assert normalize_sv_source(src) is src
