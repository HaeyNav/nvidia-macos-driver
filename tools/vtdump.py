#!/usr/bin/env python3
"""Read x86_64 kext vtables with classic Mach-O relocations.

Universal headers are big-endian; the selected x86_64 image is little-endian.
Chained fixups are not supported. Kexts must be linked at zero, as before.
"""
import pathlib
import struct
import subprocess
import sys


def bounded(data, offset, size, what):
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise ValueError('truncated or out-of-range ' + what)


def x86_slice(data):
    bounded(data, 0, 4, 'Mach-O magic')
    magic = struct.unpack_from('>I', data)[0]
    if magic in (0xcafebabe, 0xcafebabf):
        bounded(data, 0, 8, 'universal header')
        count = struct.unpack_from('>I', data, 4)[0]
        entry_size = 32 if magic == 0xcafebabf else 20
        bounded(data, 8, count * entry_size, 'universal architecture table')
        selected = []
        table_end = 8 + count * entry_size
        for i in range(count):
            entry = 8 + i * entry_size
            if struct.unpack_from('>I', data, entry)[0] != 0x01000007:
                continue
            offset, size = struct.unpack_from('>QQ' if entry_size == 32 else '>II', data, entry + 8)
            bounded(data, offset, size, 'x86_64 slice')
            if offset < table_end:
                raise ValueError('x86_64 slice overlaps the universal header')
            selected.append(data[offset:offset + size])
        if len(selected) != 1:
            raise ValueError('expected exactly one x86_64 slice')
        data = selected[0]
    bounded(data, 0, 32, 'x86_64 Mach-O header')
    magic, cpu = struct.unpack_from('<II', data)
    if magic != 0xfeedfacf or cpu != 0x01000007:
        raise ValueError('expected a little-endian x86_64 Mach-O image')
    return data


def load(path):
    d = x86_slice(pathlib.Path(path).read_bytes())
    ncmds, cmdbytes = struct.unpack_from('<II', d, 16)
    bounded(d, 32, cmdbytes, 'load commands')
    if ncmds > cmdbytes // 8:
        raise ValueError('load command count exceeds the command region')
    p = 32
    end = p + cmdbytes
    segs = []
    sym = dsym = None
    for _ in range(ncmds):
        if p + 8 > end:
            raise ValueError('truncated load command')
        cmd, sz = struct.unpack_from('<II', d, p)
        if sz < 8 or sz % 8 or sz > end - p:
            raise ValueError('invalid load command size')
        if cmd == 0x19:
            if sz < 72:
                raise ValueError('truncated segment command')
            vm, vms, offset, size = struct.unpack_from('<QQQQ', d, p + 24)
            bounded(d, offset, size, 'segment contents')
            if size > vms:
                raise ValueError('segment file size exceeds its virtual size')
            segs.append((vm, vms, offset, size))
        elif cmd == 0x2:
            if sz < 24 or sym is not None:
                raise ValueError('invalid or duplicate symbol table command')
            sym = struct.unpack_from('<IIII', d, p + 8)
        elif cmd == 0xb:
            if sz < 80 or dsym is not None:
                raise ValueError('invalid or duplicate dynamic symbol table command')
            dsym = struct.unpack_from('<18I', d, p + 8)
        elif cmd == 0x80000034:
            raise ValueError('chained fixups are unsupported; use a classic-relocation kext')
        p += sz
    if p != end or sym is None:
        raise ValueError('incomplete load commands or missing symbol table')
    symoff, nsyms, stroff, strsize = sym
    bounded(d, symoff, nsyms * 16, 'symbol table')
    bounded(d, stroff, strsize, 'string table')
    syms = []
    for i in range(nsyms):
        strx, typ, sect, desc, val = struct.unpack_from('<IBBHQ', d, symoff + i * 16)
        if strx >= strsize:
            raise ValueError('symbol name is outside the string table')
        e = d.find(b'\0', stroff + strx, stroff + strsize)
        if e < 0:
            raise ValueError('unterminated symbol name')
        syms.append((d[stroff + strx:e].decode(), typ, sect, val))
    extrel = {}
    extreloff, nextrel = (dsym[14], dsym[15]) if dsym else (0, 0)
    bounded(d, extreloff, nextrel * 8, 'external relocation table')
    for i in range(nextrel):
        addr, info = struct.unpack_from('<iI', d, extreloff + i * 8)
        index = info & 0xffffff
        if addr < 0 or not (info & (1 << 27)) or index >= len(syms):
            raise ValueError('invalid external relocation')
        # Only absolute, unsigned, 64-bit pointer relocations describe vtable slots.
        if (info >> 28) == 0 and not (info & (1 << 24)) and ((info >> 25) & 3) == 3:
            if addr in extrel:
                raise ValueError('duplicate pointer relocation')
            extrel[addr] = syms[index][0]

    def fo(vm, width=8):
        for v, vs, f, fs in segs:
            if v <= vm < v + vs:
                return f + vm - v if vm - v + width <= fs else None

    byaddr = {}
    for n, t, s, v in syms:
        if s and n and not (t & 0xe0):
            byaddr.setdefault(v, n)
    return d, syms, extrel, fo, byaddr


def vtables(path, want=None):
    d, syms, extrel, fo, byaddr = load(path)
    defined = sorted((v, n) for n, t, s, v in syms if s and n and not (t & 0xe0))
    out = {}
    for i, (v, n) in enumerate(defined):
        if not n.startswith('__ZTV'):
            continue
        cls = n[5:]
        if want and cls not in want:
            continue
        end = next((w for w, _ in defined[i + 1:] if w > v), v + 8 * 400)
        if fo(v, 16) is None:
            raise ValueError('vtable header is not backed by file contents: ' + n)
        slots = []
        for a in range(v + 16, end, 8):  # skip offset-to-top and typeinfo
            off = fo(a)
            if off is None:
                break
            if a in extrel:
                slots.append(extrel[a])
                continue
            val = struct.unpack_from('<Q', d, off)[0]
            slots.append(byaddr.get(val, '0x%x' % val) if val else '0')
        while slots and slots[-1] == '0':
            slots.pop()
        out[cls] = slots
    return out


def main():
    vt = vtables(sys.argv[1], set(sys.argv[2:]) or None)
    names = sorted({s for v in vt.values() for s in v})
    result = subprocess.run(['c++filt'], input='\n'.join(names), capture_output=True,
                            text=True, check=True)
    dm = dict(zip(names, result.stdout.split('\n')))
    for c, slots in vt.items():
        print('## %s %d' % (c, len(slots)))
        for i, x in enumerate(slots):
            print('%3d %s' % (i, dm.get(x, x)))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit('vtdump: ' + str(exc))
