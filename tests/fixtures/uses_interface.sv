// M1 fixture: interface instantiation and an interface port.
module bus_consumer (
    bus_if.slave bus
);
  always_comb bus.ready = 1'b1;
endmodule

module uses_interface (
    input logic clk
);

  bus_if #(.DATA_W(64)) u_bus (.clk(clk));

  bus_consumer u_consumer (.bus(u_bus));

endmodule
