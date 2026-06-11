// M1 fixture: wildcard and explicit package imports.
module imports_pkg
  import my_pkg::*;
(
    input logic clk,
    input logic rst_n
);

  import my_pkg::word_t;

  state_e state;
  word_t  scratch;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) state <= IDLE;
    else state <= BUSY;
  end

endmodule
