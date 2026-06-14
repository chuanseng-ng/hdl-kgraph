// Exercises function-call size casts: $clog2(...)'(value).
// The bundled tree-sitter grammar rejects these unless the casting type is
// parenthesized; the parser normalizes the source before parsing.
module clog2_cast #(
    parameter int QDEPTH = 8
) (
    input  logic clk,
    output logic [$clog2(QDEPTH)-1:0]   q_head,
    output logic [$clog2(QDEPTH+1)-1:0] q_count
);
    always_ff @(posedge clk) begin
        q_head  <= q_head  + $clog2(QDEPTH)'(1);
        q_count <= q_count + $clog2(QDEPTH+1)'(1);
    end
endmodule
