// M5 fixture: UVM-style testbench topology — a tb-pattern top wrapping a
// DUT, and a small component hierarchy extending (unresolved) uvm_* bases.
module tb_verif_top;
  logic clk, rst_n, req, gnt;
  logic [7:0] data;

  verif_dut u_dut (
      .clk  (clk),
      .rst_n(rst_n),
      .req  (req),
      .gnt  (gnt),
      .data (data)
  );
endmodule

class verif_driver extends uvm_driver;
endclass

class verif_monitor extends uvm_monitor;
endclass

class verif_agent extends uvm_agent;
endclass

class verif_scoreboard extends uvm_scoreboard;
endclass

class verif_env extends uvm_env;
endclass

class verif_base_test extends uvm_test;
endclass

class verif_smoke_test extends verif_base_test;
endclass
