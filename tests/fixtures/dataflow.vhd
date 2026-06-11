-- M5 fixture: VHDL dataflow — clocked process via rising_edge, concurrent
-- assignments, sensitivity-list reads, reset name heuristic.
library ieee;
use ieee.std_logic_1164.all;

entity df_reg is
  port (
    clk   : in  std_logic;
    rst_n : in  std_logic;
    d     : in  std_logic;
    q     : out std_logic
  );
end entity df_reg;

architecture rtl of df_reg is
  signal stage : std_logic;
begin
  q <= stage;

  reg_p : process (clk, rst_n)
  begin
    if rst_n = '0' then
      stage <= '0';
    elsif rising_edge(clk) then
      stage <= d;
    end if;
  end process;
end architecture rtl;
