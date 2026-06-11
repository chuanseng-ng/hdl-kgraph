// M1 fixture: class extending an external (unresolved) UVM base class.
class smoke_test extends uvm_test;
  virtual task run_phase(uvm_phase phase);
  endtask
endclass

class pkg_scoped extends my_pkg::base_cfg;
endclass
