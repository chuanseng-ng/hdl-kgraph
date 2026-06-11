// M1 fixture: first of two same-named modules (ambiguous resolution).
module dup_leaf (
    input  logic d,
    output logic q
);
  assign q = d;
endmodule
