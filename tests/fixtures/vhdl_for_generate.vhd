-- M7 fixture: VHDL `for ... generate` — tree-sitter counts the single
-- syntactic `u_leaf` instantiation as one instance; GHDL elaborates the
-- generate loop (literal range 0 to 3) to four, mirroring param_generate.sv.
library ieee;
use ieee.std_logic_1164.all;

entity leaf is
  port (d : in std_logic; q : out std_logic);
end entity leaf;

architecture rtl of leaf is
begin
  q <= d;
end architecture rtl;

entity gen_top is
  port (d : in std_logic_vector(3 downto 0); q : out std_logic_vector(3 downto 0));
end entity gen_top;

architecture rtl of gen_top is
  component leaf is
    port (d : in std_logic; q : out std_logic);
  end component;
begin
  g_leaf : for i in 0 to 3 generate
    u_leaf : leaf
      port map (d => d(i), q => q(i));
  end generate g_leaf;
end architecture rtl;
