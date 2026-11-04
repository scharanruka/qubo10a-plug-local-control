#!/usr/bin/env python3
"""Dump `const-string` -> `sput-object` field assignments in a class's <clinit> (for static String fields)."""
import struct, sys

def uleb128(data, off):
    r = 0; s = 0
    while True:
        b = data[off]; off += 1
        r |= (b & 0x7f) << s
        if not (b & 0x80): break
        s += 7
    return r, off

def read_mutf8(data, off):
    _sz, off = uleb128(data, off)
    end = data.index(0, off)
    return data[off:end].decode('utf-8', 'replace'), end + 1

def op_width(op):
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
    return 1

def main(path, target_class_desc):
    data = open(path, 'rb').read()
    string_ids_size = struct.unpack_from('<I', data, 56)[0]
    string_ids_off  = struct.unpack_from('<I', data, 60)[0]
    type_ids_size   = struct.unpack_from('<I', data, 64)[0]
    type_ids_off    = struct.unpack_from('<I', data, 68)[0]
    field_ids_size  = struct.unpack_from('<I', data, 80)[0]
    field_ids_off   = struct.unpack_from('<I', data, 84)[0]
    method_ids_size = struct.unpack_from('<I', data, 88)[0]
    method_ids_off  = struct.unpack_from('<I', data, 92)[0]
    class_defs_size = struct.unpack_from('<I', data, 96)[0]
    class_defs_off  = struct.unpack_from('<I', data, 100)[0]

    strings = []
    for i in range(string_ids_size):
        off = struct.unpack_from('<I', data, string_ids_off + 4*i)[0]
        s, _ = read_mutf8(data, off)
        strings.append(s)

    def type_desc(ti):
        return strings[struct.unpack_from('<I', data, type_ids_off + 4*ti)[0]]

    # field_id -> (class_name, field_name)
    fields = []
    for i in range(field_ids_size):
        ci, ti, ni = struct.unpack_from('<HHI', data, field_ids_off + 8*i)
        fields.append((type_desc(ci), strings[ni]))

    def method_of(mid):
        ci, pi, ni = struct.unpack_from('<HHI', data, method_ids_off + 8*mid)
        return strings[ni]

    for c in range(class_defs_size):
        cd = class_defs_off + 32*c
        class_idx = struct.unpack_from('<I', data, cd)[0]
        cls = type_desc(class_idx)
        if cls != target_class_desc:
            continue
        cdo = struct.unpack_from('<I', data, cd+24)[0]
        if cdo == 0: continue
        off = cdo
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
            if code_off == 0: continue
            mname = method_of(midx)
            if mname != '<clinit>': continue
            insns_size = struct.unpack_from('<I', data, code_off + 12)[0]
            insns_off = code_off + 16
            j = 0
            reg_str = {}
            while j < insns_size:
                cu = struct.unpack_from('<H', data, insns_off + 2*j)[0]
                op = cu & 0xff
                if op == 0x1A:
                    reg = (cu >> 8) & 0xff
                    sidx = struct.unpack_from('<H', data, insns_off + 2*j + 2)[0]
                    reg_str[reg] = strings[sidx]
                    j += 2; continue
                elif op == 0x1B:
                    reg = (cu >> 8) & 0xff
                    sidx = struct.unpack_from('<I', data, insns_off + 2*j + 2)[0]
                    reg_str[reg] = strings[sidx]
                    j += 3; continue
                elif op == 0x69:  # sput-object vAA, field@BBBB
                    reg = (cu >> 8) & 0xff
                    fidx = struct.unpack_from('<H', data, insns_off + 2*j + 2)[0]
                    if fidx < len(fields):
                        clsname, fname = fields[fidx]
                        if reg in reg_str and (fname.startswith('WIFI_') or fname.startswith('BLE_')):
                            print(f"{clsname}.{fname} = {reg_str[reg]!r}")
                    j += 2; continue
                elif op == 0x62:  # sget-object
                    j += 2; continue
                elif op in (0x6C, 0x6D):  # iput-object / ... actually 0x6C=iput-object, 0x6D=iput-wide? careful
                    j += 2; continue
                j += op_width(op)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
