-- M3 fixture: package + package body + a consumer (USES_PACKAGE edges,
-- VHDL_PACKAGE / PACKAGE_BODY nodes, FUNCTION extraction).
library ieee;
use ieee.std_logic_1164.all;

package util_pkg is
  constant DATA_WIDTH : integer := 8;
  function clog2(n : integer) return integer;
end package util_pkg;

package body util_pkg is
  function clog2(n : integer) return integer is
    variable result : integer := 0;
    variable value  : integer := n - 1;
  begin
    while value > 0 loop
      result := result + 1;
      value  := value / 2;
    end loop;
    return result;
  end function;
end package body util_pkg;

library ieee;
use ieee.std_logic_1164.all;
use work.util_pkg.all;

entity pkg_user is
  port (x : in std_logic; y : out std_logic);
end entity pkg_user;

architecture rtl of pkg_user is
begin
  y <= x;
end architecture rtl;
