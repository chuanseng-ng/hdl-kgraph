// M7 enrichment fixture: a parameterized generate loop. The tree-sitter tier
// sees one `u_leaf` instance; elaboration unrolls it to N (=4) instances.
module leaf #(
    parameter int W = 1
) (
    input  logic         clk,
    input  logic [W-1:0] d,
    output logic [W-1:0] q
);
  always_ff @(posedge clk) q <= d;
endmodule

module param_top #(
    parameter int N = 4
) (
    input  logic         clk,
    input  logic [N-1:0] din,
    output logic [N-1:0] dout
);
  genvar i;
  generate
    for (i = 0; i < N; i++) begin : g_leaf
      leaf #(.W(1)) u_leaf (
          .clk(clk),
          .d  (din[i]),
          .q  (dout[i])
      );
    end
  endgenerate
endmodule
