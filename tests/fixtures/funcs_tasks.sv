// M1 fixture: function and task declarations inside a module.
module funcs_tasks (
    input  logic [7:0] in,
    output logic [7:0] out
);

  function automatic logic [7:0] invert(input logic [7:0] v);
    return ~v;
  endfunction

  task automatic pulse(output logic p);
    p = 1'b1;
    p = 1'b0;
  endtask

  assign out = invert(in);

endmodule
