`ifndef DEFINES_SVH
`define DEFINES_SVH

`ifndef DEPTH
`define DEPTH 2
`endif

`define MAKE_FIFO(name, depth = `DEPTH) \
  fifo #(.DEPTH(depth)) name ( \
    .clk(clk), \
    .rst(rst));

`endif
