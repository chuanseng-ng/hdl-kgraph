// M1 fixture: package with typedef, enum, struct, localparam, and a function.
package my_pkg;

  localparam int CRC_INIT = 8'hFF;

  typedef logic [31:0] word_t;

  typedef enum logic [1:0] {
    IDLE,
    BUSY,
    DONE
  } state_e;

  typedef struct packed {
    word_t addr;
    word_t data;
    logic  we;
  } req_t;

  function automatic logic [7:0] crc8(input logic [7:0] d);
    return d ^ 8'h07;
  endfunction

endpackage
