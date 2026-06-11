// M5 fixture: dataflow extraction — signals, continuous assigns, always
// blocks, declaration initializers, and instance-port dataflow.
module df_sub (
    input  logic clk,
    input  logic i,
    output logic o
);
  always_ff @(posedge clk) o <= i;
endmodule

module df_top #(
    parameter int WIDTH = 8
) (
    input  logic             clk,
    input  logic             rst_n,
    input  logic [WIDTH-1:0] din,
    output logic [WIDTH-1:0] dout
);
  logic [WIDTH-1:0] stage;
  wire              valid;
  wire  [WIDTH-1:0] doubled = stage << 1;
  logic [WIDTH-1:0] mem [4];

  assign valid = (stage != 0) && en_missing;
  assign dout  = doubled;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) stage <= '0;
    else        stage <= din;
  end

  always_comb begin
    for (int idx = 0; idx < 4; idx++) begin
      mem[idx] = din;
    end
  end

  always_ff @(posedge clk) begin
    if (soft_clr) mem[din[1:0]] <= '0;
  end

  df_sub u_sub (
      .clk(clk),
      .i  (valid),
      .o  (sub_out)
  );
endmodule
