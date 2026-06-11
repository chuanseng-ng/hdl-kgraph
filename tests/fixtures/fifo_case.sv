// M3 fixture: uppercase SV module name matched case-insensitively by the
// lowercase VHDL `fifo` component in vhdl_top.vhd (cross-language, 0.8).
module FIFO (
    input  logic clk,
    output logic full
);

  assign full = 1'b0;

endmodule
