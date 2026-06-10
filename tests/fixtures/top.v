// M1 fixture: instantiation with parameter override and named port connections.
module top (
    input  wire       clk,
    input  wire       rst_n,
    output wire [15:0] value
);

  simple_counter #(
      .WIDTH(16)
  ) u_counter (
      .clk  (clk),
      .rst_n(rst_n),
      .en   (1'b1),
      .count(value)
  );

endmodule
