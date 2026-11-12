/* Vortex-V SoC bare-metal layout (ADR 0011). RAM is 16 KiB at 0x80000000.
   0x80001000 is tohost (M1 contract), so code and read-only data must end
   below it; writable data, bss and the stack live above it. */
OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY
{
  CODE (rx)  : ORIGIN = 0x80000000, LENGTH = 4K
  DATA (rw)  : ORIGIN = 0x80002000, LENGTH = 8K
}

SECTIONS
{
  .text : { *(.text.init) *(.text .text.*) } > CODE
  .rodata : { *(.rodata .rodata.* .srodata .srodata.*) } > CODE
  .data : { *(.data .data.* .sdata .sdata.*) } > DATA
  .bss (NOLOAD) : {
    _bss_start = .;
    *(.bss .bss.* .sbss .sbss.* COMMON)
    _bss_end = .;
  } > DATA
  _stack_top = ORIGIN(DATA) + LENGTH(DATA);
  /DISCARD/ : { *(.comment) *(.riscv.attributes) *(.note*) *(.eh_frame*) }
}
