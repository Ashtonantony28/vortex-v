"""Build a bare-metal program for the Vortex-V SoC (ADR 0011).

    python3 sw/build.py hello [out_dir]

Compiles sw/<name>/*.c with sw/common/crt0.S and link.ld for RV64IM (no C
extension, no CSRs: the core has neither yet), then writes <name>.elf,
<name>.bin and <name>.hex. The .hex has one 64-bit little-endian word per line
from 0x80000000, the format vortex_ram's INIT_FILE expects.
"""
import subprocess
import sys
from pathlib import Path

SW = Path(__file__).resolve().parent
GCC = "riscv64-unknown-elf-gcc"
OBJCOPY = "riscv64-unknown-elf-objcopy"
CFLAGS = ["-march=rv64im", "-mabi=lp64", "-mcmodel=medany", "-O2", "-ffreestanding",
          "-fno-builtin", "-nostdlib", "-nostartfiles", "-static", "-Wall", "-Werror",
          "-Wl,--no-warn-rwx-segments"]
RAM_BASE = 0x8000_0000


def build(name, out_dir=None):
    src_dir = SW / name
    out = Path(out_dir) if out_dir else SW / "build"
    out.mkdir(parents=True, exist_ok=True)
    elf, binf, hexf = (out / f"{name}{ext}" for ext in (".elf", ".bin", ".hex"))
    subprocess.run([GCC, *CFLAGS, f"-I{SW / 'common'}", f"-T{SW / 'common' / 'link.ld'}",
                    str(SW / "common" / "crt0.S"), *map(str, sorted(src_dir.glob("*.c"))),
                    "-o", str(elf)], check=True, capture_output=True, text=True)
    # Flat image from RAM_BASE; --gap-fill keeps the hole up to the data section.
    subprocess.run([OBJCOPY, "-O", "binary", "--gap-fill", "0", str(elf), str(binf)],
                   check=True, capture_output=True, text=True)
    image = binf.read_bytes()
    image += bytes(-len(image) % 8)
    words = [int.from_bytes(image[i:i + 8], "little") for i in range(0, len(image), 8)]
    hexf.write_text("".join(f"{w:016x}\n" for w in words))
    return hexf


if __name__ == "__main__":
    print(build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
