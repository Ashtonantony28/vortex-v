/* Vortex-V conformance link map: entry at RESET_PC, tohost at TOHOST_ADDR. */
OUTPUT_ARCH("riscv")
ENTRY(_start)
SECTIONS
{
  . = 0x80000000;
  .text.init : { *(.text.init) }
  . = 0x80001000;
  .tohost : { *(.tohost) }
  . = 0x80002000;
  .text : { *(.text) }
  . = ALIGN(0x1000);
  .data : { *(.data) }
  .bss : { *(.bss) }
  _end = .;
}
