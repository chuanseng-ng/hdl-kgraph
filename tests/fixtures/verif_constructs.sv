// M5 fixture: SV verification constructs — properties, sequences,
// assertions (all four statement flavors), covergroups, clocking blocks,
// and class constraints.
module verif_dut (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       req,
    input  logic       gnt,
    input  logic [7:0] data
);
  default clocking cb @(posedge clk); endclocking

  property p_handshake;
    @(posedge clk) disable iff (!rst_n) req |-> ##[1:3] gnt;
  endproperty

  sequence s_pulse;
    req ##1 !req;
  endsequence

  a_handshake: assert property (p_handshake);
  assert property (@(posedge clk) gnt |-> req);
  c_pulse: cover property (s_pulse);
  m_no_grant_idle: assume property (@(posedge clk) !(req && gnt) || req);

  covergroup cg_bus @(posedge clk);
    cp_data: coverpoint data;
    coverpoint req;
  endgroup
endmodule

class verif_item;
  rand bit [7:0] addr;
  rand bit [7:0] burst;
  constraint c_addr { addr inside {[0:127]}; }
  constraint c_burst { burst > 0; }
endclass
