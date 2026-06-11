-- M3 fixture: intentional syntax errors; the parser must still produce
-- partial results (the valid entity) and count the errors.
entity broken_ok is
  port (x : in std_logic; y : out std_logic);
end entity broken_ok;

entity broken is
  port (x : in std_logic
end entity
architecture rtl of broken
