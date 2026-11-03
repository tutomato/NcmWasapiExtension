"""Synthetic PE fixtures only: no DLL loading, processes, COM, or audio."""
import copy
import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent
if not (SOURCE/'compatibility.py').is_file():
    SOURCE = SOURCE.parent/'src/python'
sys.path.insert(0, str(SOURCE))
from compatibility import (DEFAULT_PROFILE, PEError, PEImage, compile_pattern,
                           detect_worker, pattern_matches, scan_client)


def pe_fixture(*, worker=True, duplicate=False, exports=(), forwarded=None,
               text_rva=0x1000, import_name='CreateThread', worker_span=77):
    """A small PE32+ image with independent text/rdata/pdata sections."""
    raw = bytearray(0x1200)
    raw[:2] = b'MZ'
    struct.pack_into('<I', raw, 60, 0x80)
    raw[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', raw, 0x84, 0x8664, 3, 0, 0, 0, 0xf0, 0x2022)
    optional = 0x98
    struct.pack_into('<H', raw, optional, 0x20b)
    struct.pack_into('<Q', raw, optional + 24, 0x180000000)
    struct.pack_into('<II', raw, optional + 32, 0x1000, 0x200)
    struct.pack_into('<II', raw, optional + 56, text_rva + 0x4000, 0x400)
    struct.pack_into('<I', raw, optional + 108, 16)
    data_rva, pdata_rva = text_rva + 0x2000, text_rva + 0x3000
    sections = [('.text', text_rva, 0x400, 0x400, 0x60000020),
                ('.rdata', data_rva, 0x800, 0x800, 0x40000040),
                ('.pdata', pdata_rva, 0x200, 0x1000, 0x40000040)]
    for index, (name, rva, size, offset, flags) in enumerate(sections):
        base = optional + 0xf0 + index * 40
        raw[base:base + 8] = name.encode().ljust(8, b'\0')
        struct.pack_into('<IIII', raw, base + 8, size, rva, size, offset)
        struct.pack_into('<I', raw, base + 36, flags)
    # The loaded code itself is never executed by any test.
    raw[0x400:0x800] = b'\xcc' * 0x400
    functions = []
    if worker:
        signature, _ = compile_pattern(DEFAULT_PROFILE['worker']['pattern'])
        raw[0x440:0x440 + len(signature)] = signature
        functions.append((text_rva + 0x40, text_rva + 0x40 + worker_span, data_rva + 0x700))
        if duplicate:
            raw[0x4c0:0x4c0 + len(signature)] = signature
            functions.append((text_rva + 0xc0, text_rva + 0xc0 + worker_span, data_rva + 0x700))
        # A separate runtime function references the worker as CreateThread's lpStartAddress.
        functions.append((text_rva + 0x180, text_rva + 0x1c0, data_rva + 0x700))
        xref = text_rva + 0x190
        code = bytearray(b'\x4c\x8d\x05\0\0\0\0\x33\xd2\x33\xc9\xff\x15\0\0\0\0')
        struct.pack_into('<i', code, 3, (text_rva + 0x40) - (xref + 7))
        struct.pack_into('<i', code, 13, (data_rva + 0x5a0) - (xref + 17))
        raw[0x590:0x590 + len(code)] = code
        struct.pack_into('<IIIII', raw, 0xd00, data_rva + 0x580, 0, 0,
                         data_rva + 0x550, data_rva + 0x5a0)
        raw[0xd50:0xd50 + 13] = b'KERNEL32.dll\0'
        struct.pack_into('<QQ', raw, 0xd80, data_rva + 0x5c0, 0)
        struct.pack_into('<QQ', raw, 0xda0, data_rva + 0x5c0, 0)
        name = import_name.encode() + b'\0'
        raw[0xdc0:0xdc0 + len(name) + 2] = b'\0\0' + name
        struct.pack_into('<II', raw, optional + 112 + 8, data_rva + 0x500, 40)
        for index, function in enumerate(sorted(functions)):
            struct.pack_into('<III', raw, 0x1000 + index * 12, *function)
        struct.pack_into('<II', raw, optional + 112 + 3 * 8, pdata_rva, len(functions) * 12)
    raw[0xf00] = 1  # Readable unwind-info header.
    if exports:
        exports = list(exports)
        struct.pack_into('<II', raw, optional + 112, data_rva, 0x400)
        struct.pack_into('<IIIII', raw, 0x800 + 20, len(exports), len(exports),
                         data_rva + 0x40, data_rva + 0x80, data_rva + 0xc0)
        cursor = 0x100
        for index, name in enumerate(exports):
            target = text_rva + 0x300 + index * 4
            if forwarded == name:
                forward = b'elsewhere.' + name.encode() + b'\0'
                raw[0x800 + cursor:0x800 + cursor + len(forward)] = forward
                target = data_rva + cursor
                cursor += len(forward)
            struct.pack_into('<I', raw, 0x840 + index * 4, target)
            struct.pack_into('<I', raw, 0x880 + index * 4, data_rva + cursor)
            struct.pack_into('<H', raw, 0x8c0 + index * 2, index)
            value = name.encode() + b'\0'
            raw[0x800 + cursor:0x800 + cursor + len(value)] = value
            cursor += len(value)
            raw[0x700 + index * 4] = 0xc3
        assert cursor < 0x400
    return raw


class ImageTests(unittest.TestCase):
    def test_valid_headers_sections_functions_and_imports(self):
        image = PEImage(pe_fixture())
        self.assertEqual(image.architecture, 'x64')
        self.assertEqual(image.rva_offset(0x1040, 77), 0x440)
        self.assertEqual(image.imports()[0x35a0], ('kernel32.dll', 'CreateThread'))
        self.assertEqual(image.runtime_functions()[0].end, 0x108d)
        self.assertTrue(detect_worker(image)['ok'])

    def test_shifted_rva_and_relocations_still_match(self):
        raw = pe_fixture(text_rva=0x9000)
        # Both bytes were rel32 fields masked by the compatibility pattern.
        raw[0x440 + 22:0x440 + 26] = b'\x11\x22\x33\x44'
        raw[0x440 + 27:0x440 + 31] = b'\x55\x66\x77\x88'
        result = detect_worker(PEImage(raw))
        self.assertTrue(result['ok'])
        self.assertEqual(result['workerRva'], 0x9040)

    def test_noncritical_code_and_header_changes_are_allowed(self):
        raw = pe_fixture()
        raw[0x410] ^= 1
        struct.pack_into('<I', raw, 0x88, 12345678)  # COFF timestamp.
        self.assertTrue(detect_worker(PEImage(raw))['ok'])

    def test_fixed_pattern_change_is_rejected(self):
        raw = pe_fixture()
        raw[0x440 + 40] ^= 1
        self.assertEqual(detect_worker(PEImage(raw))['reason'], 'worker-pattern-missing')

    def test_duplicate_worker_is_rejected(self):
        self.assertEqual(detect_worker(PEImage(pe_fixture(duplicate=True)))['reason'], 'worker-pattern-ambiguous')

    def test_pattern_inside_function_is_not_a_function_entry(self):
        raw = pe_fixture()
        struct.pack_into('<II', raw, 0x1000, 0x103f, 0x108d)
        self.assertEqual(detect_worker(PEImage(raw))['reason'], 'worker-pattern-missing')

    def test_incorrect_function_span_is_rejected(self):
        self.assertEqual(detect_worker(PEImage(pe_fixture(worker_span=76)))['reason'], 'worker-pattern-missing')

    def test_missing_create_thread_reference_is_rejected(self):
        self.assertEqual(detect_worker(PEImage(pe_fixture(import_name='OtherThread')))['reason'], 'worker-xref-missing')

    def test_wrong_lea_target_is_rejected(self):
        raw = pe_fixture()
        raw[0x593] ^= 1
        self.assertEqual(detect_worker(PEImage(raw))['reason'], 'worker-xref-missing')

    def test_reference_outside_runtime_function_is_rejected(self):
        raw = pe_fixture()
        struct.pack_into('<II', raw, 0x100c, 0x1180, 0x1190)
        self.assertEqual(detect_worker(PEImage(raw))['reason'], 'worker-xref-missing')

    def test_alternative_xor_encodings_are_supported(self):
        raw = pe_fixture()
        raw[0x597] = raw[0x599] = 0x31
        self.assertTrue(detect_worker(PEImage(raw))['ok'])

    def test_worker_wrapper_reused_by_multiple_create_thread_calls_is_rejected(self):
        raw = pe_fixture()
        second = bytearray(raw[0x590:0x5a1])
        struct.pack_into('<i', second, 3, 0x1040 - (0x11a8 + 7))
        struct.pack_into('<i', second, 13, 0x35a0 - (0x11a8 + 17))
        raw[0x5a8:0x5b9] = second
        self.assertEqual(detect_worker(PEImage(raw))['reason'], 'worker-xref-ambiguous')

    def test_named_exports_and_forwarder_metadata(self):
        image = PEImage(pe_fixture(worker=False, exports=['one','two'], forwarded='two'))
        self.assertEqual(image.exports()['one'].rva, 0x1300)
        self.assertIsNone(image.exports()['one'].forwarder)
        self.assertEqual(image.exports()['two'].forwarder, 'elsewhere.two')

    def test_bad_headers_are_bounded(self):
        malformed = [b'', b'MZ', b'\0' * 64]
        for offset, value in [(60, 0xfffffffc), (0x98 + 60, 0xfffffffc),
                              (0x98 + 108, 0xfffffffc), (0x188 + 20, 0xfffffffc)]:
            raw = pe_fixture()
            struct.pack_into('<I', raw, offset, value)
            malformed.append(raw)
        raw = pe_fixture()
        struct.pack_into('<H', raw, 0x84, 0x14c)
        malformed.append(raw)
        for raw in malformed:
            with self.subTest(size=len(raw)):
                with self.assertRaises(PEError):
                    PEImage(raw)

    def test_unmapped_and_non_file_backed_rvas_are_rejected(self):
        image = PEImage(pe_fixture())
        for address in (0x2000, 0x10000000, -1):
            with self.assertRaises(PEError):
                image.rva_offset(address)
        raw = pe_fixture()
        struct.pack_into('<I', raw, 0x188 + 8, 0x800)
        with self.assertRaises(PEError):
            PEImage(raw).rva_bytes(0x1600, 4)

    def test_bad_export_ordinal_and_unterminated_string_are_rejected(self):
        raw = pe_fixture(worker=False, exports=['one'])
        struct.pack_into('<H', raw, 0x8c0, 2)
        with self.assertRaises(PEError):
            PEImage(raw).exports()
        raw = pe_fixture(worker=False, exports=['one'])
        struct.pack_into('<I', raw, 0x880, 0x37ff)
        raw[0xfff] = 65
        with self.assertRaises(PEError):
            PEImage(raw).exports()

    def test_bad_exception_directory_and_unwind_are_rejected(self):
        for offset, value in [(0x98 + 112 + 3 * 8 + 4, 13), (0x1008, 0xfffffff0)]:
            raw = pe_fixture()
            struct.pack_into('<I', raw, offset, value)
            with self.assertRaises(PEError):
                PEImage(raw).runtime_functions()

    def test_weak_or_invalid_signatures_are_rejected(self):
        for pattern in ('?? ' * 77, '42 ' * 3, 'garbage', ''):
            with self.assertRaises(ValueError):
                compile_pattern(pattern)


class ClientScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'cloudmusic.exe').write_bytes(pe_fixture(worker=False))
        (self.root/'cloudmusic.dll').write_bytes(pe_fixture())
        self.role_names = {'avcodec':'decoder-renamed.dll', 'avutil':'utility-renamed.dll',
                           'swresample':'resampler-renamed.dll'}
        for role, basename in self.role_names.items():
            (self.root/basename).write_bytes(pe_fixture(worker=False, exports=DEFAULT_PROFILE['roles'][role]['exports']))

    def test_discovers_role_exports_without_filename_or_version_pins(self):
        result = scan_client(self.root)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['modules']['avcodec'], 'decoder-renamed.dll')
        self.assertEqual(result['workerRva'], 0x1040)
        self.assertEqual(result['evidence']['abiMajors'], {'avcodec':58,'avutil':56,'swresample':3})

    def test_accepts_executable_argument(self):
        self.assertTrue(scan_client(self.root/'cloudmusic.exe')['ok'])

    def test_nonexistent_and_wrong_executable_arguments_are_rejected(self):
        self.assertEqual(scan_client(self.root/'typo.exe')['reason'], 'client-not-found')
        (self.root/'other.exe').write_bytes(pe_fixture(worker=False))
        self.assertEqual(scan_client(self.root/'other.exe')['reason'], 'invalid-client')

    def test_missing_required_abi_export_rejects_role(self):
        exports = DEFAULT_PROFILE['roles']['avutil']['exports'][:-1]
        (self.root/self.role_names['avutil']).write_bytes(pe_fixture(worker=False, exports=exports))
        result = scan_client(self.root)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'api-role-missing')
        self.assertEqual(result['role'], 'avutil')

    def test_ambiguous_provider_is_rejected(self):
        (self.root/'duplicate.dll').write_bytes((self.root/self.role_names['avcodec']).read_bytes())
        result = scan_client(self.root)
        self.assertEqual(result['reason'], 'api-role-ambiguous')

    def test_forwarded_required_export_is_rejected(self):
        exports = DEFAULT_PROFILE['roles']['avcodec']['exports']
        (self.root/self.role_names['avcodec']).write_bytes(pe_fixture(worker=False, exports=exports, forwarded=exports[0]))
        self.assertEqual(scan_client(self.root)['reason'], 'api-role-invalid-export')

    def test_non_executable_required_export_is_rejected(self):
        raw = pe_fixture(worker=False, exports=DEFAULT_PROFILE['roles']['avcodec']['exports'])
        struct.pack_into('<I', raw, 0x840, 0x3500)
        (self.root/self.role_names['avcodec']).write_bytes(raw)
        self.assertEqual(scan_client(self.root)['reason'], 'api-role-invalid-export')

    def test_unsupported_player_pattern_rejected_before_role_scan(self):
        raw = bytearray((self.root/'cloudmusic.dll').read_bytes())
        raw[0x440] ^= 1
        (self.root/'cloudmusic.dll').write_bytes(raw)
        self.assertEqual(scan_client(self.root)['reason'], 'worker-pattern-missing')

    def test_missing_player_and_bad_executable_rejected(self):
        (self.root/'cloudmusic.dll').unlink()
        self.assertEqual(scan_client(self.root)['reason'], 'player-module-not-found')
        (self.root/'cloudmusic.exe').write_bytes(b'not a PE')
        self.assertEqual(scan_client(self.root)['reason'], 'unsupported-pe')

    def test_incompatible_profile_rejected(self):
        profile = copy.deepcopy(DEFAULT_PROFILE)
        profile['architecture'] = 'x86'
        self.assertEqual(scan_client(self.root, profile)['reason'], 'invalid-profile')


if __name__ == '__main__':
    unittest.main()
