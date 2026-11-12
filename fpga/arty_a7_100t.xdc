## Vortex-V on the Digilent Arty A7-100T (xc7a100tcsg324-1). ADR 0003, ADR 0011.
## Pin names and sites follow Digilent's Arty A7 master XDC.
## Not yet checked on hardware (M9).

## 100 MHz board clock
set_property -dict { PACKAGE_PIN E3  IOSTANDARD LVCMOS33 } [get_ports { CLK100MHZ }]
create_clock -add -name sys_clk_pin -period 10.00 -waveform {0 5} [get_ports { CLK100MHZ }]

## CK_RST button (active low)
set_property -dict { PACKAGE_PIN C2  IOSTANDARD LVCMOS33 } [get_ports { ck_rst }]

## USB-UART: FPGA transmit
set_property -dict { PACKAGE_PIN D10 IOSTANDARD LVCMOS33 } [get_ports { uart_rxd_out }]

## LD4..LD7
set_property -dict { PACKAGE_PIN H5  IOSTANDARD LVCMOS33 } [get_ports { led[0] }]
set_property -dict { PACKAGE_PIN J5  IOSTANDARD LVCMOS33 } [get_ports { led[1] }]
set_property -dict { PACKAGE_PIN T9  IOSTANDARD LVCMOS33 } [get_ports { led[2] }]
set_property -dict { PACKAGE_PIN T10 IOSTANDARD LVCMOS33 } [get_ports { led[3] }]

## RGB LED LD0
set_property -dict { PACKAGE_PIN E1  IOSTANDARD LVCMOS33 } [get_ports { led0_b }]
set_property -dict { PACKAGE_PIN F6  IOSTANDARD LVCMOS33 } [get_ports { led0_g }]
set_property -dict { PACKAGE_PIN G6  IOSTANDARD LVCMOS33 } [get_ports { led0_r }]

## Asynchronous inputs: the reset is synchronised in vortex_reset_sync
set_false_path -from [get_ports { ck_rst }]

set_property CONFIG_VOLTAGE 3.3 [current_design]
set_property CFGBVS VCCO [current_design]
