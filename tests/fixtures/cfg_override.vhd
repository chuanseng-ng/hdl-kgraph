-- M3 fixture: a configuration overriding a component's default binding.
-- Without the configuration, component `leaf_default` would bind to the
-- like-named entity; `cfg_top_special` rebinds u_leaf to leaf_special.
library ieee;
use ieee.std_logic_1164.all;

entity leaf_default is
  port (d : in std_logic; q : out std_logic);
end entity leaf_default;

architecture rtl of leaf_default is
begin
  q <= d;
end architecture rtl;

entity leaf_special is
  port (d : in std_logic; q : out std_logic);
end entity leaf_special;

architecture rtl of leaf_special is
begin
  q <= not d;
end architecture rtl;

entity cfg_top is
  port (d : in std_logic; q : out std_logic);
end entity cfg_top;

architecture rtl of cfg_top is
  component leaf_default is
    port (d : in std_logic; q : out std_logic);
  end component;
begin
  u_leaf : leaf_default
    port map (d => d, q => q);
end architecture rtl;

configuration cfg_top_special of cfg_top is
  for rtl
    for u_leaf : leaf_default
      use entity work.leaf_special(rtl);
    end for;
  end for;
end configuration cfg_top_special;
