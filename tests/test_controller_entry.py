"""Read-only controller entry tests; enabling live audio is never exercised."""
import builtins
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'python'
spec = importlib.util.spec_from_file_location('release_controller', SOURCE / 'controller.py')
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


class ControllerEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = self.root / 'client' / 'cloudmusic.exe'
        self.client.parent.mkdir()
        pe = bytearray(512)
        pe[:2] = b'MZ'
        struct.pack_into('<I', pe, 60, 128)
        pe[128:132] = b'PE\0\0'
        struct.pack_into('<H', pe, 132, 0x8664)
        struct.pack_into('<H', pe, 148, 240)
        struct.pack_into('<H', pe, 152, 0x20b)
        self.client.write_bytes(pe)
        (self.client.parent / 'cloudmusic.dll').write_bytes(b'pinned core')
        pins = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                for f in self.client.parent.iterdir()}
        self.manifest = self.root / 'supported-clients.json'
        self.manifest.write_text(json.dumps({'clients': [
            {'id': 'fixture-release-x64', 'architecture': 'x64', 'sha256': pins}]}), encoding='utf-8')
        self.data = self.root / 'data-not-created'
        self.logs = self.root / 'logs-not-created'

    def invoke_safely(self, args):
        output = io.StringIO()
        original_import = builtins.__import__
        def reject_runtime(name, *values, **options):
            if name.split('.')[0] in ('frida', 'websocket', 'job_process',
                                     'cdp_bridge', 'device_capabilities', 'ctypes'):
                raise AssertionError('Runtime dependency imported during a safe check: ' + name)
            return original_import(name, *values, **options)
        env = {'NCM_WASAPI_DATA_DIR': str(self.data), 'NCM_WASAPI_LOG_DIR': str(self.logs)}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(controller, 'find_manifest', return_value=self.manifest), \
                mock.patch.object(builtins, '__import__', side_effect=reject_runtime), \
                mock.patch.object(controller, '_run_enabled', side_effect=AssertionError('No live audio allowed')), \
                contextlib.redirect_stdout(output):
            status = controller.main(args)
        self.assertFalse(self.data.exists(), 'Check or failed enable created settings/log directories')
        self.assertFalse(self.logs.exists(), 'Check or failed enable created a log directory')
        return status, json.loads(output.getvalue())

    def test_check_success_is_json_and_does_not_load_runtime_or_create_data(self):
        status, result = self.invoke_safely(['--check', '--client', str(self.client)])
        self.assertEqual(status, 0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['manifestId'], 'fixture-release-x64')

    def test_invalid_enable_exits_before_runtime_and_side_effects(self):
        self.client.write_bytes(self.client.read_bytes() + b'unknown-update')
        before = self.client.read_bytes()
        status, result = self.invoke_safely(['--enable', '--client', str(self.client)])
        self.assertEqual(status, 2)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'unsupported-version')
        self.assertEqual(self.client.read_bytes(), before)

    def test_missing_client_enable_has_readable_json_failure(self):
        status, result = self.invoke_safely(['--enable', '--client', str(self.root / 'absent/cloudmusic.exe')])
        self.assertEqual(status, 2)
        self.assertEqual(result['reason'], 'client-not-found')
        self.assertTrue(result['message'])

    def test_source_enable_requires_built_payload_resources(self):
        status, result = self.invoke_safely(['--enable', '--client', str(self.client)])
        self.assertEqual(status, 2)
        self.assertEqual(result['reason'], 'runtime-resources-missing')

    def test_settings_save_is_atomic_preserves_other_preferences(self):
        settings = self.root / 'state' / 'settings.json'
        settings.parent.mkdir()
        settings.write_text(json.dumps({'theme': 'dark'}), encoding='utf-8')
        controller.save_client_path(self.client, settings)
        saved = json.loads(settings.read_text(encoding='utf-8'))
        self.assertEqual(saved, {'theme': 'dark', 'clientPath': str(self.client)})
        self.assertEqual(list(settings.parent.iterdir()), [settings])


if __name__ == '__main__':
    unittest.main()
