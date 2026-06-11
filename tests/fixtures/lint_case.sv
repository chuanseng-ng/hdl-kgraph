// M5 fixture: one planted finding per lint check.
module lint_leaf #(
    parameter int WIDTH = 8
) (
    input  logic             clk,
    input  logic             en,
    input  logic [WIDTH-1:0] d,
    output logic [WIDTH-1:0] q,
    output logic             extra
);
  always_ff @(posedge clk) if (en) q <= d;
  assign extra = 1'b0;
endmodule

module lint_top (
    input  logic       clk,
    output logic [7:0] out
);
  logic [7:0] unread_wire;   // driven below, read by nothing
  logic [7:0] undriven_bus;  // read below, driven by nothing

  assign unread_wire = {8{clk}};
  assign out = undriven_bus;

  lint_leaf #(
      .WIDTH(8)              // redundant: equals the default
  ) u_leaf (
      .clk(clk),
      .en (),                // explicitly open
      .d  (undriven_bus)
      // .q and .extra left unconnected
  );
endmodule

module lint_dead;            // never instantiated
endmodule
