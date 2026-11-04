#!/usr/bin/env python3
"""Extract a specific string resource value from resources.arsc by resource ID."""
import struct, sys

def main(path, resid):
    data = open(path, 'rb').read()
    # resid = 0xPPTTEEEE
    pkg_id = (resid >> 24) & 0xff
    type_id = (resid >> 16) & 0xff
    entry_id = resid & 0xffff

    # header
    package_count = struct.unpack_from('<I', data, 8)[0]

    def read_string_pool(off):
        # ResChunk_header: type(u16), header_size(u16), size(u32)
        _, header_size, size = struct.unpack_from('<HHI', data, off)
        string_count, style_count, flags = struct.unpack_from('<III', data, off + 8)
        strings_start = struct.unpack_from('<I', data, off + 20)[0]
        is_utf8 = flags & 0x100
        # string offsets
        offsets_off = off + header_size
        offsets = [struct.unpack_from('<I', data, offsets_off + 4*i)[0] for i in range(string_count)]
        def get_str(i):
            so = strings_start + offsets[i]
            if is_utf8:
                # utf8: u8 length, u8 length2, then bytes
                l1 = data[so]; l2 = data[so+1]
                if l1 & 0x80:
                    # 2-byte length
                    l = ((l1 & 0x7f) << 8) | l2
                    return data[so+2:so+2+l].decode('utf-8', 'replace')
                else:
                    return data[so+1:so+1+l1].decode('utf-8', 'replace')
            else:
                # utf16: u16 length
                l = struct.unpack_from('<H', data, so)[0]
                return data[so+2:so+2+l*2].decode('utf-16-le', 'replace')
        return get_str

    # global string pool at offset 12 (after header)
    global_get = read_string_pool(12)

    # walk packages
    off = 12
    # skip global string pool
    _, _, gs_size = struct.unpack_from('<HHI', data, off)
    off += gs_size

    for _ in range(package_count):
        ctype, header_size, size = struct.unpack_from('<HHI', data, off)
        pid = struct.unpack_from('<I', data, off + 8)[0]
        if pid != pkg_id:
            off += size
            continue
        # package: ResTable_package -> id(4) name[128](256) then typeStrings,lastPublicType,keyStrings
        type_strings_off = struct.unpack_from('<I', data, off + 12 + 256)[0]
        key_strings_off = struct.unpack_from('<I', data, off + 12 + 256 + 8)[0]
        # key string pool (to map entry name)
        key_get = read_string_pool(off + key_strings_off)
        # type string pool (type names)
        type_get = read_string_pool(off + type_strings_off)

        # iterate chunks inside package
        p = off + header_size
        end = off + size
        while p < end:
            t, hsz, csize = struct.unpack_from('<HHI', data, p)
            if t == 0x0201:  # ResTable_typeSpec
                pass
            elif t == 0x0202:  # ResTable_type
                tid = data[p+8]  # id (u8 at offset 8)
                if tid != type_id:
                    p += csize
                    continue
                entry_count = struct.unpack_from('<I', data, p + 12)[0]
                entries_start = struct.unpack_from('<I', data, p + 16)[0]
                # entry offsets
                eo = p + hsz
                entry_off = struct.unpack_from('<I', data, eo + 4*entry_id)[0]
                if entry_off == 0xffffffff:
                    print("entry not found")
                    return
                # entry at p + entries_start + entry_off
                e = p + entries_start + entry_off
                # ResTable_entry: size(u16), flags(u16), key_index(u32)
                flags = struct.unpack_from('<H', data, e + 2)[0]
                key_idx = struct.unpack_from('<I', data, e + 4)[0]
                name = key_get(key_idx)
                if flags & 0x0001:  # FLAG_COMPLEX
                    print("complex entry:", name)
                    return
                # Res_value: size(u16), res0(u8), dataType(u8), data(u32)
                v = e + 8
                dataType = data[v+3]
                dval = struct.unpack_from('<I', data, v+4)[0]
                if dataType == 0x03:  # TYPE_STRING
                    print(f"{name} = {global_get(dval)!r}")
                elif dataType == 0x01:  # TYPE_REFERENCE
                    print(f"{name} = @ref 0x{dval:08x}")
                else:
                    print(f"{name} = type {dataType} val {dval}")
                return
            p += csize
        break
    print("resource not found")

if __name__ == '__main__':
    main(sys.argv[1], int(sys.argv[2], 16))
