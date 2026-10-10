"""Compare the reconstructed codec with the original ARM instructions.

Usage: python tools/verify_arm_codec.py /path/to/Colors.exe
Requires pefile and unicorn; the game is deliberately not distributed.
"""
import hashlib
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pefile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_SP, UC_ARM_REG_LR
from colors_server.codec import transform

raw = Path(sys.argv[1]).read_bytes()
assert hashlib.sha256(raw).hexdigest() == 'addaca723aba2dd4beb7449bbbd37a89988cb0345ee5381e08f2c18dc62473cf'
pe = pefile.PE(data=raw)
base = pe.OPTIONAL_HEADER.ImageBase
mapped = pe.get_memory_mapped_image()
uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
uc.mem_map(base, (len(mapped)+4095)&~4095)
uc.mem_write(base, mapped)
uc.mem_map(0x2000000, 0x20000)

def execute(address, data):
    uc.mem_write(0x2000000, data)
    uc.reg_write(UC_ARM_REG_R0, 0x2000000)
    uc.reg_write(UC_ARM_REG_R1, len(data))
    uc.reg_write(UC_ARM_REG_SP, 0x201f000)
    uc.reg_write(UC_ARM_REG_LR, 0x201ff00)
    uc.emu_start(address, 0x201ff00, count=1000000)
    return bytes(uc.mem_read(0x2000000, len(data)))

rng = random.Random(20261010)
for length in (1, 2, 31, 32, 33, 63, 64, 65, 1023, 1024, 1025, 4096):
    data = rng.randbytes(length)
    encrypted = execute(0xe9a08, data)
    assert encrypted == transform(data), ('encrypt', length)
    assert execute(0xe9ae4, encrypted) == data, ('ARM decrypt', length)
    assert transform(encrypted, decrypt=True) == data, ('server decrypt', length)
print('PASS: 12 sizes; original ARM encryption/decryption matches server codec')
