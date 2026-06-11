// M3 fixture: Verilog-top / VHDL-leaf — an SV module instantiating the
// VHDL `alu` entity (cross-language name match, confidence 0.8).
module mixed_sv_top (
    input  logic       clk,
    input  logic [7:0] a,
    input  logic [7:0] b,
    input  logic       op,
    output logic [7:0] result
);

  alu #(
      .WIDTH(8)
  ) u_alu (
      .a     (a),
      .b     (b),
      .op    (op),
      .result(result)
  );

endmodule
