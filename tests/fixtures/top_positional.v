// M1 fixture: positional port connections and positional parameter override.
module top_positional (
    input  wire [7:0] x,
    input  wire [7:0] y,
    output wire [7:0] s,
    output wire       c
);

  adder #(8) u_adder (x, y, 1'b0, s, c);

endmodule
