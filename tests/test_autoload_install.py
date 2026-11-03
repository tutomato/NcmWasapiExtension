"""Only isolated temporary directories are mutated. No player or vendor files."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parent
if not (SOURCE/'autoload_install.py').is_file():
    SOURCE = SOURCE.parent/'src/python'
sys.path.insert(0, str(SOURCE))
import autoload_install as tool


def fake_pe(marker=b'original'):
    content = bytearray(512)
    content[:2] = b'MZ'
    struct.pack_into('<I', content, 60, 128)
    content[128:132] = b'PE\0\0'
    struct.pack_into('<H', content, 132, 0x8664)
    struct.pack_into('<H', content, 152, 0x20b)
    return bytes(content)+marker


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows TEMP may use an 8.3 alias; fault-injection paths must match it.
        self.root = Path(self.temp.name).resolve()
        self.client_dir = self.root/'网易云 with spaces'
        self.client_dir.mkdir()
        self.client = self.client_dir/'cloudmusic.exe'
        self.vendor = b'unchanged-vendor-client'
        self.client.write_bytes(self.vendor)
        (self.client_dir/'cloudmusic.dll').write_bytes(b'unchanged-vendor-core')
        self.payload = self.root/'程序 payload with spaces'
        (self.payload/'native').mkdir(parents=True)
        (self.payload/'runtime').mkdir()
        self.source_dll = self.payload/'native/msimg32.dll'
        self.source_dll.write_bytes(fake_pe())
        (self.payload/'.ncm-wasapi-install').write_text(tool.PAYLOAD_MARKER,encoding='utf-8')
        (self.payload/'runtime/python.exe').write_bytes(b'fixture-runtime')
        (self.payload/'controller.py').write_text('# fixture',encoding='utf-8')
        self.checks = dict(process_check=lambda client:False,
                           engine_check=lambda client,root:{'ok':True,'scheme':'fixture-engine'},
                           permission_check=lambda directory:True)

    def run_tool(self, operation, **changes):
        options = dict(self.checks, **changes)
        answer = tool.deploy(operation, self.client, self.payload, **options)
        self.assertEqual(self.client.read_bytes(),self.vendor)
        self.assertEqual((self.client_dir/'cloudmusic.dll').read_bytes(), b'unchanged-vendor-core')
        return answer

    def snapshot(self):
        return {path.relative_to(self.root).as_posix():path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def install(self):
        result = self.run_tool('install')
        self.assertTrue(result['ok'],result)
        return result

    def test_check_has_no_filesystem_writes(self):
        before = self.snapshot()
        answer = self.run_tool('check')
        self.assertTrue(answer['ok'])
        self.assertEqual(before,self.snapshot())
        self.assertEqual(set(self.client_dir.iterdir()),{self.client,self.client_dir/'cloudmusic.dll'})

    def test_preflight_supports_destination_root_not_created_yet(self):
        future = self.root/'not created payload'
        before = self.snapshot()
        answer = tool.deploy('check',self.client,future,**self.checks)
        self.assertTrue(answer['ok'],answer)
        self.assertFalse(future.exists())
        self.assertEqual(before,self.snapshot())

    def test_install_unicode_ini_and_ownership(self):
        self.install()
        self.assertEqual((self.client_dir/tool.DLL).read_bytes(),self.source_dll.read_bytes())
        raw = (self.client_dir/tool.INI).read_bytes()
        self.assertTrue(raw.startswith(b'\xff\xfe'))
        ini = raw.decode('utf-16')
        self.assertIn('[NcmWasapiExtension]\r\nSchema=1',ini)
        self.assertIn('PayloadRoot='+str(self.payload),ini)
        record = json.loads((self.client_dir/tool.RECORD).read_text(encoding='utf-8'))
        self.assertEqual(record['owner'],tool.OWNER)
        self.assertEqual(record['installationRoot'],str(self.payload))
        self.assertEqual(record['files'][tool.DLL],hashlib.sha256(self.source_dll.read_bytes()).hexdigest())
        self.assertFalse(list(self.client_dir.glob('.ncm-wasapi-transaction-*')))

    def test_update_only_replaces_intact_own_files(self):
        self.install()
        self.source_dll.write_bytes(fake_pe(b'new version'))
        self.install()
        self.assertEqual((self.client_dir/tool.DLL).read_bytes(),self.source_dll.read_bytes())

    def test_uninstall_removes_only_our_three_files(self):
        self.install()
        extra = self.client_dir/'user-file.txt'
        extra.write_text('leave it',encoding='utf-8')
        result = self.run_tool('remove')
        self.assertTrue(result['ok'],result)
        for name in (*tool.OWNED_FILES,tool.RECORD): self.assertFalse((self.client_dir/name).exists())
        self.assertEqual(extra.read_text(),'leave it')

    def test_remove_does_not_need_compatible_engine_or_source_dll(self):
        self.install()
        self.source_dll.unlink()
        result = self.run_tool('remove',engine_check=lambda *_:self.fail('Remove must not inspect engine'))
        self.assertTrue(result['ok'],result)

    def test_uninstall_check_skips_engine_compatibility(self):
        self.install()
        before = self.snapshot()
        result = self.run_tool('check',for_remove=True,engine_check=lambda *_:self.fail('Remove check engine'))
        self.assertTrue(result['ok'])
        self.assertEqual(before,self.snapshot())

    def test_foreign_dll_is_never_overwritten_or_removed(self):
        (self.client_dir/tool.DLL).write_bytes(b'BetterNCM or another plugin')
        before = self.snapshot()
        for operation in ('check','install','remove'):
            result = self.run_tool(operation)
            self.assertFalse(result['ok'])
            self.assertEqual(result['reason'],'foreign-loader-conflict')
            self.assertEqual(before,self.snapshot())

    def test_foreign_ini_is_never_adopted(self):
        (self.client_dir/tool.INI).write_text('foreign')
        result = self.run_tool('install')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'foreign-loader-conflict')

    def test_wrong_marker_is_preserved(self):
        (self.client_dir/tool.RECORD).write_text('{"owner":"different"}')
        before = self.snapshot()
        self.assertEqual(self.run_tool('remove')['reason'],'foreign-ownership-record')
        self.assertEqual(before,self.snapshot())

    def test_modified_own_dll_and_ini_are_preserved(self):
        for name in tool.OWNED_FILES:
            with self.subTest(name=name):
                self.install()
                original = (self.client_dir/name).read_bytes()
                (self.client_dir/name).write_bytes(b'new-unknown-content')
                before = self.snapshot()
                for operation in ('install','remove'):
                    self.assertEqual(self.run_tool(operation)['reason'],'owned-file-modified')
                    self.assertEqual(before,self.snapshot())
                (self.client_dir/name).write_bytes(original)

    def test_wrong_installation_root_cannot_remove_another_install(self):
        self.install()
        before = self.snapshot()
        result = tool.deploy('remove',self.client,self.root/'other-root',**self.checks)
        self.assertEqual(result['reason'],'foreign-ownership-record')
        self.assertEqual(before,self.snapshot())

    def test_running_client_blocks_all_operations_before_writes(self):
        before = self.snapshot()
        for operation in ('check','install','remove'):
            result = self.run_tool(operation,process_check=lambda _:True)
            self.assertEqual(result['reason'],'client-running')
            self.assertEqual(before,self.snapshot())

    def test_new_process_after_preflight_aborts_and_cleans_staging(self):
        before = self.snapshot()
        process = mock.Mock(side_effect=[False,True])
        result = self.run_tool('install',process_check=process)
        self.assertEqual(result['reason'],'client-running')
        self.assertEqual(before,self.snapshot())
        self.assertFalse(list(self.client_dir.glob('.ncm-wasapi-transaction-*')))

    def test_process_query_failure_is_fail_closed(self):
        result = self.run_tool('install',process_check=mock.Mock(side_effect=OSError('query denied')))
        self.assertEqual(result['reason'],'process-check-failed')
        self.assertFalse((self.client_dir/tool.DLL).exists())

    def test_permission_denied_is_read_only(self):
        before = self.snapshot()
        result = self.run_tool('install',permission_check=lambda _:False)
        self.assertEqual(result['reason'],'permission-denied')
        self.assertEqual(before,self.snapshot())

    def test_incompatible_engine_is_read_only(self):
        before = self.snapshot()
        result = self.run_tool('install',engine_check=lambda *_:{'ok':False})
        self.assertEqual(result['reason'],'incompatible-engine')
        self.assertEqual(before,self.snapshot())

    def test_payload_identity_and_architecture_checked(self):
        (self.payload/'.ncm-wasapi-install').write_text('not this project')
        self.assertEqual(self.run_tool('install')['reason'],'invalid-payload')
        (self.payload/'.ncm-wasapi-install').write_text(tool.PAYLOAD_MARKER)
        self.source_dll.write_bytes(b'not a DLL')
        self.assertEqual(self.run_tool('install')['reason'],'invalid-payload')

    def test_failed_update_rolls_back_all_original_bytes(self):
        self.install()
        before = self.snapshot()
        self.source_dll.write_bytes(fake_pe(b'new version'))
        expected = self.snapshot()
        original = tool.os.replace
        failed = False
        def replace(source,target):
            nonlocal failed
            if Path(target)==self.client_dir/tool.INI and not failed:
                failed=True
                raise PermissionError('simulated locked ini')
            return original(source,target)
        with mock.patch.object(tool.os,'replace',side_effect=replace):
            result = self.run_tool('install')
        self.assertFalse(result['ok'])
        self.assertTrue(result['rollbackComplete'])
        self.assertEqual(expected,self.snapshot())
        self.assertEqual((self.client_dir/tool.DLL).read_bytes(),before['网易云 with spaces/msimg32.dll'])

    def test_failed_fresh_install_leaves_no_partial_plugin(self):
        before = self.snapshot()
        original = tool.os.replace
        def replace(source,target):
            if Path(target)==self.client_dir/tool.RECORD:
                raise PermissionError('simulated marker write denial')
            return original(source,target)
        with mock.patch.object(tool.os,'replace',side_effect=replace): result=self.run_tool('install')
        self.assertFalse(result['ok'])
        self.assertTrue(result['rollbackComplete'])
        self.assertEqual(before,self.snapshot())

    def test_failed_remove_rolls_back_all_original_bytes(self):
        self.install()
        before = self.snapshot()
        original = tool.os.replace
        def replace(source,target):
            if Path(source)==self.client_dir/tool.INI:
                raise PermissionError('simulated locked ini')
            return original(source,target)
        with mock.patch.object(tool.os,'replace',side_effect=replace): result=self.run_tool('remove')
        self.assertFalse(result['ok'])
        self.assertTrue(result['rollbackComplete'])
        self.assertEqual(before,self.snapshot())

    def test_unknown_changes_during_rollback_are_not_overwritten(self):
        self.install()
        self.source_dll.write_bytes(fake_pe(b'new version'))
        original = tool.os.replace
        def replace(source,target):
            if Path(target)==self.client_dir/tool.INI:
                (self.client_dir/tool.DLL).write_bytes(b'unknown concurrent writer')
                raise PermissionError('simulated lock')
            return original(source,target)
        with mock.patch.object(tool.os,'replace',side_effect=replace): result=self.run_tool('install')
        self.assertEqual(result['reason'],'rollback-incomplete')
        self.assertFalse(result['rollbackComplete'])
        self.assertEqual((self.client_dir/tool.DLL).read_bytes(),b'unknown concurrent writer')
        self.assertTrue(Path(result['recoveryDirectory']).is_dir())
        self.assertTrue((Path(result['recoveryDirectory'])/'backup'/tool.DLL).is_file())

    def test_missing_owned_file_can_be_repaired_and_removed(self):
        self.install()
        (self.client_dir/tool.DLL).unlink()
        self.install()
        (self.client_dir/tool.INI).unlink()
        self.assertTrue(self.run_tool('remove')['ok'])

    def test_ownership_json_cannot_add_paths_or_duplicate_keys(self):
        self.install()
        record = self.client_dir/tool.RECORD
        original = record.read_bytes()
        value = json.loads(original)
        value['files']['../unrelated.txt']='a'*64
        record.write_text(json.dumps(value))
        self.assertEqual(self.run_tool('remove')['reason'],'foreign-ownership-record')
        record.write_text('{"owner":"x","owner":"'+tool.OWNER+'"}')
        self.assertEqual(self.run_tool('remove')['reason'],'foreign-ownership-record')
        record.write_bytes(original)

    def test_no_install_uninstall_is_idempotent(self):
        before = self.snapshot()
        result = self.run_tool('remove')
        self.assertTrue(result['ok'])
        self.assertEqual(result['reason'],'not-installed')
        self.assertEqual(before,self.snapshot())

    @unittest.skipUnless(os.name=='nt','Windows sharing-lock behavior')
    def test_real_file_sharing_lock_rolls_back_update_and_remove(self):
        import ctypes as C
        from ctypes import wintypes as W
        self.install()
        self.source_dll.write_bytes(fake_pe(b'new locked upgrade'))
        before = self.snapshot()
        kernel = C.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
        kernel.CreateFileW.restype=W.HANDLE
        kernel.CloseHandle.argtypes=[W.HANDLE]
        handle = kernel.CreateFileW(str(self.client_dir/tool.INI),0x80000000,1,None,3,0,None)
        self.assertNotEqual(handle,C.c_void_p(-1).value)
        try:
            for operation in ('install','remove'):
                result = self.run_tool(operation)
                self.assertFalse(result['ok'],result)
                self.assertTrue(result['rollbackComplete'],result)
                self.assertEqual(before,self.snapshot())
        finally:
            kernel.CloseHandle(handle)
        self.assertTrue(self.run_tool('remove')['ok'])


if __name__=='__main__':
    unittest.main()
