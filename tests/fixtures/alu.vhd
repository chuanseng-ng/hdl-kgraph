-- M3 fixture: entity/architecture pair with a generic and a process.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity alu is
  generic (
    WIDTH : positive := 8
  );
  port (
    a      : in  std_logic_vector(WIDTH - 1 downto 0);
    b      : in  std_logic_vector(WIDTH - 1 downto 0);
    op     : in  std_logic;
    result : out std_logic_vector(WIDTH - 1 downto 0)
  );
end entity alu;

architecture rtl of alu is
begin
  process (a, b, op)
  begin
    if op = '0' then
      result <= std_logic_vector(unsigned(a) + unsigned(b));
    else
      result <= std_logic_vector(unsigned(a) - unsigned(b));
    end if;
  end process;
end architecture rtl;
