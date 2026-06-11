// M1 fixture: instantiates a module with two candidate definitions.
module uses_dup (
    input  logic d,
    output logic q
);

  dup_leaf u_leaf (
      .d(d),
      .q(q)
  );

endmodule
