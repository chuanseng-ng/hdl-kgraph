-- M3 fixture: VHDL-top / Verilog-leaf — a VHDL architecture instantiating
-- the SV `simple_counter` module via a component (default binding crosses
-- the language boundary), the SV `FIFO` module via a lowercase component
-- (case-folded match), and the VHDL `alu` entity directly.
library ieee;
use ieee.std_logic_1164.all;

entity vhdl_top is
  port (
    clk    : in  std_logic;
    rst_n  : in  std_logic;
    en     : in  std_logic;
    a      : in  std_logic_vector(7 downto 0);
    b      : in  std_logic_vector(7 downto 0);
    op     : in  std_logic;
    result : out std_logic_vector(7 downto 0);
    count  : out std_logic_vector(7 downto 0);
    full   : out std_logic
  );
end entity vhdl_top;

architecture rtl of vhdl_top is
  component simple_counter is
    generic (WIDTH : integer := 8);
    port (
      clk   : in  std_logic;
      rst_n : in  std_logic;
      en    : in  std_logic;
      count : out std_logic_vector(WIDTH - 1 downto 0)
    );
  end component;

  component fifo is
    port (
      clk  : in  std_logic;
      full : out std_logic
    );
  end component;
begin
  u_counter : simple_counter
    generic map (WIDTH => 8)
    port map (
      clk   => clk,
      rst_n => rst_n,
      en    => en,
      count => count
    );

  u_fifo : fifo
    port map (
      clk  => clk,
      full => full
    );

  u_alu : entity work.alu(rtl)
    generic map (WIDTH => 8)
    port map (
      a      => a,
      b      => b,
      op     => op,
      result => result
    );
end architecture rtl;
