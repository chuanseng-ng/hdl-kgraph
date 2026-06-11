// M1 fixture: class declaration and same-file inheritance.
class base_item;
  int unsigned id;

  function new(int unsigned id = 0);
    this.id = id;
  endfunction

  virtual function string describe();
    return $sformatf("item %0d", id);
  endfunction
endclass

class burst_item extends base_item;
  int unsigned len;

  virtual function string describe();
    return $sformatf("burst %0d len %0d", id, len);
  endfunction
endclass
