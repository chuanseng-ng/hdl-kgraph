// M1 fixture: instantiates a module that exists nowhere in the corpus.
module missing_child (
    input  logic clk,
    output logic done
);

  ghost_mod u_ghost (
      .clk (clk),
      .done(done)
  );

endmodule
