# Vortex-V Arty A7-100T bitstream, Vivado non-project flow (ADR 0011).
#
#   python3 sw/build.py hello fpga/out          # program image -> fpga/out/hello.hex
#   vivado -mode batch -source fpga/build.tcl    # from the repo root
#
# Not yet run: Vivado is not available in the CI or cloud environment (M9).
# Outputs land in fpga/out/: reports (timing, utilisation) and arty_a7_top.bit.

set part     xc7a100tcsg324-1
set out_dir  fpga/out
set hex      [file normalize $out_dir/hello.hex]
file mkdir $out_dir

proc read_filelist {f} {
  set fh [open $f r]
  foreach line [split [read $fh] "\n"] {
    set line [string trim $line]
    if {$line eq "" || [string match "//*" $line]} { continue }
    read_verilog -sv $line
  }
  close $fh
}
read_filelist rtl/core/files.f
read_filelist rtl/soc/files.f
read_filelist rtl/fpga/files.f
read_xdc fpga/arty_a7_100t.xdc

synth_design -top arty_a7_top -part $part -verilog_define VORTEX_XILINX \
  -generic "INIT_FILE=\"$hex\""
opt_design
place_design
phys_opt_design
route_design

report_utilization    -file $out_dir/utilization.rpt
report_timing_summary -file $out_dir/timing.rpt
write_bitstream -force $out_dir/arty_a7_top.bit
