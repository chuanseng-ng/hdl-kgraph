// M5 fixture: two clock domains with one CDC crossing (the ROADMAP
// acceptance case), an async reset, and a child module whose clock port
// must alias-merge with the top-level clock.
module cdc_child (
    input  logic clk,
    input  logic d,
    output logic q
);
  always_ff @(posedge clk) q <= d;
endmodule

module two_clock_top (
    input  logic clk_a,
    input  logic clk_b,
    input  logic rst_n,
    input  logic da,
    output logic out_b
);
  logic data_a;
  logic data_b;

  always_ff @(posedge clk_a or negedge rst_n) begin
    if (!rst_n) data_a <= 1'b0;
    else        data_a <= da;
  end

  // The crossing: data_a is driven in clk_a's domain, read here in clk_b's.
  always_ff @(posedge clk_b) data_b <= data_a;

  assign out_b = data_b;

  cdc_child u_child (
      .clk(clk_b),
      .d  (data_b),
      .q  (child_q)
  );
endmodule
