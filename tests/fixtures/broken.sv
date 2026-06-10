// M1 fixture: syntax error mid-file; the valid module after it must still
// be extracted (error tolerance).
module broken_syntax (
    input logic clk
);
  assign garbage = this is not valid systemverilog at all !!!;
endmodule

module survives (
    input  logic clk,
    output logic ok
);
  assign ok = 1'b1;
endmodule
