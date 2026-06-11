// M1 fixture: SV interface declaration (modport extraction lands post-M1).
interface bus_if #(
    parameter int DATA_W = 32
) (
    input logic clk
);

  logic              valid;
  logic              ready;
  logic [DATA_W-1:0] data;

  modport master(output valid, output data, input ready);
  modport slave(input valid, input data, output ready);

endinterface
