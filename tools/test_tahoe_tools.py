"""Exercise the real Mach-O reader and Tahoe generator without Apple hardware."""
import importlib.util
import pathlib
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import vtdump

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'tahoe_generator', ROOT / 'kexts/NVRM/accel/gen_tahoe_fwd.py')
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


def thin():
    """One external vtable slot in a classic-relocation x86_64 kext."""
    strings = b'\0__ZTV4Test\0__ZN4Test3runEv\0_end\0'
    symoff, stroff = 288, 336
    reloff = (stroff + len(strings) + 7) & ~7
    size = reloff + 8
    segment = struct.pack('<II16sQQQQiiII', 0x19, 72, b'__DATA',
                          0, size, 0, size, 7, 3, 0, 0)
    symtab = struct.pack('<6I', 2, 24, symoff, 3, stroff, len(strings))
    dysym = [0] * 18
    dysym[14:16] = [reloff, 1]
    commands = segment + symtab + struct.pack('<20I', 0xb, 80, *dysym)
    data = bytearray(size)
    data[:32] = struct.pack('<8I', 0xfeedfacf, 0x1000007, 3, 11,
                            3, len(commands), 0, 0)
    data[32:32 + len(commands)] = commands
    for index, (name, typ, sect, address) in enumerate([
        (b'__ZTV4Test', 0xf, 1, 256),
        (b'__ZN4Test3runEv', 1, 0, 0),
        (b'_end', 0xf, 1, 280),
    ]):
        struct.pack_into('<IBBHQ', data, symoff + 16 * index,
                         strings.index(name), typ, sect, 0, address)
    data[stroff:stroff + len(strings)] = strings
    struct.pack_into('<iI', data, reloff, 272, 1 | (3 << 25) | (1 << 27))
    return bytes(data)


def universal(payload, wide=False, cpu=0x1000007):
    offset = 64
    if wide:
        entry = struct.pack('>IIQQII', cpu, 3, offset, len(payload), 3, 0)
    else:
        entry = struct.pack('>5I', cpu, 3, offset, len(payload), 3)
    header = struct.pack('>II', 0xcafebabf if wide else 0xcafebabe, 1) + entry
    return header.ljust(offset, b'\0') + payload


class MachOReader(unittest.TestCase):
    def read(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'NVAccel'
            path.write_bytes(data)
            return vtdump.vtables(path)

    def test_thin_external_slot(self):
        self.assertEqual(self.read(thin()), {'4Test': ['__ZN4Test3runEv']})

    def test_universal_32_and_64_match_thin(self):
        for wide in (False, True):
            with self.subTest(wide=wide):
                self.assertEqual(self.read(universal(thin(), wide)), self.read(thin()))

    def test_x86_slice_can_follow_another_architecture(self):
        arm = bytearray(thin())
        struct.pack_into('<I', arm, 4, 0x100000c)
        x86 = thin()
        first, second = 64, (64 + len(arm) + 7) & ~7
        header = struct.pack('>II', 0xcafebabe, 2)
        header += struct.pack('>5I', 0x100000c, 0, first, len(arm), 3)
        header += struct.pack('>5I', 0x1000007, 3, second, len(x86), 3)
        data = (header.ljust(first, b'\0') + arm).ljust(second, b'\0') + x86
        self.assertEqual(self.read(data), self.read(x86))

    def test_universal_rejects_duplicate_and_out_of_bounds_slices(self):
        duplicate = bytearray(universal(thin()))
        struct.pack_into('>I', duplicate, 4, 2)
        duplicate[28:48] = duplicate[8:28]
        outside = bytearray(universal(thin()))
        struct.pack_into('>I', outside, 16, len(outside) + 1)
        overlap = bytearray(universal(thin()))
        struct.pack_into('>I', overlap, 16, 8)
        for data in (duplicate, outside, overlap):
            with self.subTest(data=data[:48]):
                with self.assertRaises(ValueError):
                    self.read(data)

    def test_defined_pointer_without_dynamic_symbol_table(self):
        data = bytearray(thin())
        struct.pack_into('<I', data, 128, 0x777)  # replace LC_DYSYMTAB
        struct.pack_into('<BB', data, 308, 0xf, 1)
        struct.pack_into('<Q', data, 312, 240)
        struct.pack_into('<Q', data, 272, 240)
        self.assertEqual(self.read(data), {'4Test': ['__ZN4Test3runEv']})

    def test_chained_fixups_are_explicitly_rejected(self):
        data = bytearray(thin())
        struct.pack_into('<I', data, 128, 0x80000034)
        with self.assertRaisesRegex(ValueError, 'chained fixups'):
            self.read(data)

    def test_invalid_input_is_rejected_with_diagnostic(self):
        wrong_cpu = bytearray(thin())
        struct.pack_into('<I', wrong_cpu, 4, 0x100000c)
        zero_command = bytearray(thin())
        struct.pack_into('<I', zero_command, 36, 0)
        bad_string = bytearray(thin())
        struct.pack_into('<I', bad_string, 288, 0xffffffff)
        bad_relocation = bytearray(thin())
        struct.pack_into('<I', bad_relocation, len(bad_relocation) - 4,
                         100 | (3 << 25) | (1 << 27))
        for data in (b'', thin()[:20], thin()[:-1], wrong_cpu, zero_command,
                     bad_string, bad_relocation, universal(thin(), cpu=0x100000c)):
            with self.subTest(length=len(data), prefix=data[:8]):
                with self.assertRaises(ValueError):
                    self.read(data)


class ForwardGenerator(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        (self.root / 'iofam').mkdir()
        (self.root / 're').mkdir()
        (self.root / 'iofam/IOBase.h').write_text(
            'virtual void dirty(unsigned int, unsigned int);\n'
            'virtual void dirty(unsigned long long, unsigned long long);\n')
        self.output = self.root / 'tahoe_fwd.h'
        self.output.write_text('previous valid header\n')
        self.missing = self.root / 'missing.txt'
        self.symbol = '__ZN6IOBase5dirtyEyy'
        self.missing.write_text(self.symbol + '\n')
        for name, value in [('HERE', str(self.root)), ('BASES', {'NVTest': 'IOBase'})]:
            p = patch.object(generator, name, value)
            p.start()
            self.addCleanup(p.stop)

    def generate(self, symbols=None, returncode=0):
        symbols = [self.symbol] if symbols is None else symbols
        result = subprocess.CompletedProcess(
            ['c++filt'], returncode,
            stdout='IOBase::dirty(unsigned long long, unsigned long long)\n',
            stderr='demangler failed' if returncode else '')
        with patch.object(generator, 'vtables', return_value={'6NVTest': symbols}), \
             patch.object(generator.subprocess, 'run', return_value=result):
            generator.main('fixture-kext', str(self.missing))

    def test_same_arity_overload_keeps_exact_parameter_types(self):
        self.generate()
        header = self.output.read_text()
        self.assertIn('dirty(unsigned long long a0, unsigned long long a1)', header)
        self.assertNotIn('dirty(unsigned int a0, unsigned int a1)', header)
        self.assertIn('jmpq *16', header)

    def test_unhandled_missing_symbol_preserves_previous_header(self):
        self.missing.write_text(self.symbol + '\n__ZN6IOBase7unknownEv\n')
        with self.assertRaises(ValueError):
            self.generate()
        self.assertEqual(self.output.read_text(), 'previous valid header\n')

    def test_demangler_failure_preserves_previous_header(self):
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.generate(returncode=1)
        self.assertEqual(self.output.read_text(), 'previous valid header\n')

    def test_empty_missing_list_preserves_previous_header(self):
        self.missing.write_text('')
        with self.assertRaisesRegex(ValueError, 'empty'):
            self.generate()
        self.assertEqual(self.output.read_text(), 'previous valid header\n')

    def test_atomic_replace_failure_preserves_previous_header(self):
        with patch.object(generator.os, 'replace', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                self.generate()
        self.assertEqual(self.output.read_text(), 'previous valid header\n')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ['iofam', 'missing.txt', 're', 'tahoe_fwd.h'])

    def test_nested_types_do_not_change_arity(self):
        self.assertEqual(generator.split_types('Pair<int, long> const&, void (*)(int, int)'),
                         ['Pair<int, long> const&', 'void (*)(int, int)'])

    def test_multiline_declarations_preserve_const_and_overloads(self):
        (self.root / 'iofam/IOBase.h').write_text(
            'virtual void dirty(\nunsigned int, unsigned int\n) const;\n'
            'virtual void dirty(unsigned int, unsigned int);\n')
        declarations = generator.decls()
        for const in (True, False):
            self.assertIn(generator.signature('IOBase', 'dirty',
                                              ['unsigned int', 'unsigned int'], const), declarations)


class ProductionHeaders(unittest.TestCase):
    def test_checked_in_forwarders_have_exact_source_declarations(self):
        declarations = generator.decls()
        header = (ROOT / 'kexts/NVRM/accel/tahoe_fwd.h').read_text(encoding='utf-8')
        count = 0
        for match in re.finditer(r'__attribute__\(\(naked\)\) (.+?) (\w+)::(\w+)\((.*?)\)( const)? \{', header):
            ret, cls, name, params, const = match.groups()
            args = [re.sub(r' a\d+$', '', p) for p in generator.split_types(params)]
            key_suffix = generator.signature('', name, args, bool(const))[1:]
            candidates = [value for key, value in declarations.items() if key[1:] == key_suffix]
            self.assertIn((ret, args), candidates, (cls, name, args))
            count += 1
        self.assertEqual(count, 68)

    @unittest.skipUnless(sys.platform == 'darwin', 'requires Apple clang and the macOS linker')
    def test_native_linked_kext_and_universal_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source = root / 'probe.cpp'
            binary = root / 'probe.kextbin'
            source.write_text('class Probe { public: virtual unsigned long long value(); };\n'
                              'unsigned long long Probe::value() { return 42; }\n')
            subprocess.run(['xcrun', 'clang++', '-arch', 'x86_64', '-fapple-kext', '-mkernel',
                            '-fno-rtti', '-fno-exceptions', '-nostdlib', '-Xlinker', '-kext',
                            str(source), '-o', str(binary)], check=True, capture_output=True, timeout=60)
            expected = vtdump.vtables(binary)
            self.assertEqual(expected['5Probe'], ['__ZN5Probe5valueEv'])
            fat = root / 'probe-universal'
            fat.write_bytes(universal(binary.read_bytes()))
            self.assertEqual(vtdump.vtables(fat), expected)


if __name__ == '__main__':
    unittest.main()
