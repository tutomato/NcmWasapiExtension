"""Mock PE metadata only: no vendor images, DLL loading or process changes."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/python'))
import compatibility


class AutoloadEntryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = self.root/'client';self.client.mkdir()
        for name in ('cloudmusic.exe','cloudmusic.dll','libcef.dll'):
            (self.client/name).write_bytes(b'fixture-metadata-only')
        self.rows = {
            'cloudmusic.exe': {'imports': {}, 'exports': {}},
            'cloudmusic.dll': {'imports': {
                0x1200: ('MSIMG32.DLL','AlphaBlend'),
                0x1210: ('LIBCEF.DLL','cef_initialize'),
                0x1218: ('libcef.dll','cef_execute_process')}, 'exports': {}},
            'libcef.dll': {'imports': {
                0x2200: ('KERNEL32.DLL','GetCommandLineW'),
                0x2208: ('kernel32.dll','GetCommandLineA')}, 'exports': {
                'cef_initialize': compatibility.Export('cef_initialize',0x3010),
                'cef_execute_process': compatibility.Export('cef_execute_process',0x3020)}}}
        self.invalid_image = None
        self.non_executable = set()
        self.opened = []

    def scan(self, target=None):
        outer = self
        class FakePEImage:
            def __init__(self,path):
                self.name=Path(path).name;outer.opened.append(Path(path))
                if self.name==outer.invalid_image:
                    raise compatibility.PEError('Unsupported architecture or corrupt image')
            def imports(self):return copy.deepcopy(outer.rows[self.name]['imports'])
            def exports(self):return copy.deepcopy(outer.rows[self.name]['exports'])
            def executable(self,rva,size=1):return rva not in outer.non_executable
        with mock.patch.object(compatibility,'PEImage',FakePEImage):
            return compatibility.scan_autoload_entry(target or self.client/'cloudmusic.exe')

    def test_valid_direct_import_contract_accepts_without_local_msimg_file(self):
        result=self.scan()
        self.assertTrue(result['ok']);self.assertFalse((self.client/'msimg32.dll').exists())
        self.assertEqual(result['evidence']['importSlots']['cef_initialize'],0x1210)
        self.assertEqual(result['evidence']['msimg32Imports'],['AlphaBlend'])
        self.assertIs(result['runtimeLoadOrderVerified'],False)
        self.assertEqual({path.name for path in self.opened},set(self.rows))

    def test_directory_input_is_supported(self):self.assertTrue(self.scan(self.client)['ok'])

    def test_missing_module_rejects_before_reading_it(self):
        (self.client/'libcef.dll').unlink()
        result=self.scan();self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'autoload-entry-module-missing')
        self.assertEqual(result['module'],'libcef.dll')

    def test_related_images_must_all_be_x64_valid_pe(self):
        for name in self.rows:
            with self.subTest(name=name):
                self.invalid_image=name;result=self.scan()
                self.assertFalse(result['ok']);self.assertEqual(result['reason'],'autoload-entry-invalid-pe')

    def test_msimg_must_be_a_nonempty_direct_import(self):
        del self.rows['cloudmusic.dll']['imports'][0x1200]
        result=self.scan();self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'autoload-entry-msimg32-import-missing')

    def test_msimg_symbols_must_be_supplied_by_the_proxy(self):
        self.rows['cloudmusic.dll']['imports'][0x1200]=('msimg32.dll','FutureUnknownFunction')
        result=self.scan();self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'autoload-entry-msimg32-import-unsupported')

    def test_supported_msimg_ordinal_is_accepted(self):
        self.rows['cloudmusic.dll']['imports'][0x1200]=('msimg32.dll','#2')
        self.assertTrue(self.scan()['ok'])

    def test_both_cef_initialize_and_execute_imports_are_required(self):
        original=copy.deepcopy(self.rows['cloudmusic.dll']['imports'])
        for slot in (0x1210,0x1218):
            with self.subTest(slot=slot):
                self.rows['cloudmusic.dll']['imports']=copy.deepcopy(original)
                del self.rows['cloudmusic.dll']['imports'][slot]
                result=self.scan();self.assertFalse(result['ok'])
                self.assertEqual(result['reason'],'autoload-entry-import-missing')

    def test_both_commandline_imports_must_come_from_kernel32(self):
        original=copy.deepcopy(self.rows['libcef.dll']['imports'])
        for slot in (0x2200,0x2208):
            with self.subTest(slot=slot):
                self.rows['libcef.dll']['imports']=copy.deepcopy(original)
                symbol=self.rows['libcef.dll']['imports'][slot][1]
                self.rows['libcef.dll']['imports'][slot]=('other-provider.dll',symbol)
                result=self.scan();self.assertFalse(result['ok'])
                self.assertEqual(result['reason'],'autoload-entry-import-missing')

    def test_duplicate_hook_import_slot_is_rejected(self):
        self.rows['cloudmusic.dll']['imports'][0x1220]=('libcef.dll','cef_initialize')
        result=self.scan();self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'autoload-entry-import-ambiguous')

    def test_cef_exports_must_be_present_direct_and_executable(self):
        for name in ('cef_initialize','cef_execute_process'):
            original=copy.deepcopy(self.rows['libcef.dll']['exports'])
            for kind in ('missing','forwarder','non-executable'):
                with self.subTest(name=name,kind=kind):
                    self.rows['libcef.dll']['exports']=copy.deepcopy(original)
                    item=original[name];self.non_executable=set()
                    if kind=='missing':del self.rows['libcef.dll']['exports'][name]
                    elif kind=='forwarder':self.rows['libcef.dll']['exports'][name]=compatibility.Export(name,item.rva,'another.'+name)
                    else:self.non_executable.add(item.rva)
                    result=self.scan();self.assertFalse(result['ok'])
                    self.assertIn(result['reason'],('autoload-entry-export-missing','autoload-entry-export-invalid'))
            self.rows['libcef.dll']['exports']=original

    def test_resolved_module_cannot_escape_client_directory(self):
        outside=self.root/'outside';outside.mkdir();(outside/'libcef.dll').write_bytes(b'other')
        original=Path.resolve
        def resolve(path,*args,**kwargs):
            if path.name=='libcef.dll':return outside/'libcef.dll'
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'resolve',resolve):result=self.scan()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'autoload-entry-module-outside-client')

    def test_unknown_executable_name_is_not_accepted(self):
        other=self.client/'other.exe';other.write_bytes(b'other')
        self.assertFalse(self.scan(other)['ok']);self.assertEqual(self.opened,[])


if __name__=='__main__':unittest.main()
