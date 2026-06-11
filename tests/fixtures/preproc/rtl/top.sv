`include "defines.svh"

module top (
    input logic clk,
    input logic rst
);
`ifdef USE_FIFO
  `MAKE_FIFO(u_fifo)
`else
  stack u_stack (.clk(clk), .rst(rst));
`endif
endmodule
