// M1 fixture: .* wildcard port connection.
module wildcard_leaf (
    input  logic clk,
    input  logic rst_n,
    output logic ready
);
  assign ready = rst_n;
endmodule

module wildcard_conn (
    input  logic clk,
    input  logic rst_n,
    output logic ready
);

  wildcard_leaf u_leaf (.*);

endmodule
