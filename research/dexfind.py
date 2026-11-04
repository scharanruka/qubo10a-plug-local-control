#!/usr/bin/env python3
"""Map DEX string constants (BLE UUIDs) back to the classes/methods that reference them."""
import struct, sys

def uleb128(data, off):
    result = 0; shift = 0
    while True:
        b = data[off]; off += 1
        result |= (b & 0x7f) << shift
        if not (b & 0x80): break
        shift += 7
    return result, off

def read_mutf8(data, off):
    _sz, off = uleb128(data, off)
    end = data.index(0, off)
    return data[off:end].decode('utf-8', 'replace'), end + 1

# opcode -> instruction width in 16-bit code units
def op_width(op):
    if op in (0x00,): return 1
    if 0x01 <= op <= 0x0D: return 1
    if 0x0E <= op <= 0x11: return 1
    if op == 0x12: return 1
    if op == 0x13: return 2
    if op == 0x14: return 3
    if op == 0x15: return 2
    if op == 0x16: return 2
    if op == 0x17: return 3
    if op == 0x18: return 5
    if op == 0x19: return 2
    if op == 0x1A: return 2
    if op == 0x1B: return 3
    if op == 0x1C: return 2
    if 0x1D <= op <= 0x1E: return 1
    if op == 0x1F: return 2
    if op == 0x20: return 2
    if op == 0x21: return 1
    if op == 0x22: return 2
    if op == 0x23: return 2
    if op == 0x24: return 3
    if op == 0x25: return 3
    if op == 0x26: return 3
    if op == 0x27: return 1
    if op == 0x28: return 1
    if op == 0x29: return 2
    if op == 0x2A: return 3
    if 0x2B <= op <= 0x2C: return 3
    if 0x2D <= op <= 0x31: return 2
    if 0x32 <= op <= 0x37: return 2
    if 0x38 <= op <= 0x3D: return 2
    if 0x3E <= op <= 0x43: return 1
    if 0x44 <= op <= 0x51: return 2
    if 0x52 <= op <= 0x5F: return 2
    if 0x60 <= op <= 0x6D: return 2
    if 0x6E <= op <= 0x72: return 2
    if 0x74 <= op <= 0x78: return 3
    if 0x90 <= op <= 0xAF: return 2
    if 0xB0 <= op <= 0xCF: return 2
    if 0xD0 <= op <= 0xE2: return 2
    if op == 0xFA: return 4
    if op == 0xFB: return 4
    if op == 0xFC: return 3
    if op == 0xFD: return 3
    return 1  # unknown, skip one

def main(path, needles):
    data = open(path, 'rb').read()
    string_ids_size = struct.unpack_from('<I', data, 56)[0]
    string_ids_off  = struct.unpack_from('<I', data, 60)[0]
    type_ids_size   = struct.unpack_from('<I', data, 64)[0]
    type_ids_off    = struct.unpack_from('<I', data, 68)[0]
    method_ids_size = struct.unpack_from('<I', data, 88)[0]
    method_ids_off  = struct.unpack_from('<I', data, 92)[0]
    class_defs_size = struct.unpack_from('<I', data, 96)[0]
    class_defs_off  = struct.unpack_from('<I', data, 100)[0]

    strings = []
    for i in range(string_ids_size):
        off = struct.unpack_from('<I', data, string_ids_off + 4*i)[0]
        s, _ = read_mutf8(data, off)
        strings.append(s)

    needle_idxs = {}
    for n in needles:
        try:
            needle_idxs[strings.index(n)] = n
        except ValueError:
            pass
    if not needle_idxs:
        print("no needles found"); return

    def type_descriptor(ti):
        di = struct.unpack_from('<I', data, type_ids_off + 4*ti)[0]
        return strings[di]

    def method_of(mid):
        ci, pi, ni = struct.unpack_from('<HHI', data, method_ids_off + 8*mid)
        return ci, strings[ni]

    for c in range(class_defs_size):
        cd = class_defs_off + 32*c
        class_idx = struct.unpack_from('<I', data, cd)[0]
        class_data_off = struct.unpack_from('<I', data, cd+24)[0]
        if class_data_off == 0:
            continue
        cls_name = type_descriptor(class_idx)
        off = class_data_off
        s_sz, off = uleb128(data, off); i_sz, off = uleb128(data, off)
        d_sz, off = uleb128(data, off); v_sz, off = uleb128(data, off)
        for _ in range(s_sz + i_sz):
            _, off = uleb128(data, off); _, off = uleb128(data, off)
        midx = 0
        for m in range(d_sz + v_sz):
            diff, off = uleb128(data, off)
            _acc, off = uleb128(data, off)
            code_off, off = uleb128(data, off)
            midx += diff
            if code_off == 0 or midx >= method_ids_size or midx < 0:
                continue
            mclass_idx, mname = method_of(midx)
            insns_size = struct.unpack_from('<I', data, code_off + 12)[0]
            insns_off = code_off + 16
            j = 0
            while j < insns_size:
                cu = struct.unpack_from('<H', data, insns_off + 2*j)[0]
                op = cu & 0xff
                if op == 0x1A:
                    sidx = struct.unpack_from('<H', data, insns_off + 2*j + 2)[0]
                    if sidx in needle_idxs:
                        print(f"{cls_name} :: {mname} :: {needle_idxs[sidx]}")
                elif op == 0x1B:
                    sidx = struct.unpack_from('<I', data, insns_off + 2*j + 2)[0]
                    if sidx in needle_idxs:
                        print(f"{cls_name} :: {mname} :: {needle_idxs[sidx]}")
                j += op_width(op)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2:])
