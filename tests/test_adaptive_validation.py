"""Changing client hashes alone never disables a compatible engine."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/python'))
import compatibility
import installation
from test_installation import fake_pe


class AdaptiveValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.exe = self.root/'cloudmusic.exe'
        self.exe.write_bytes(fake_pe(marker=b'an-unlisted-update'))
        self.manifest = {'compatibilityMode': 'capabilities', 'clients': [{
            'id': 'old-baseline', 'version': '1.0.0', 'architecture': 'x64',
            'sha256': {'cloudmusic.exe': 'a'*64, 'cloudmusic.dll': 'b'*64}}]}
        self.detected = {'ok': True, 'scheme': 'ncm-ffmpeg58-thread-v1',
            'architecture': 'x64', 'workerRva': 0x7654,
            'modules': {'player': 'cloudmusic.dll', 'avcodec': 'codec.dll',
                        'avutil': 'util.dll', 'swresample': 'resample.dll'}}

    def test_unlisted_client_passes_when_its_engine_contract_passes(self):
        with mock.patch.object(compatibility, 'scan_client', return_value=self.detected) as scan:
            result = installation.validate_client(self.exe, self.manifest)
        self.assertTrue(result['ok'])
        self.assertFalse(result['testedBuild'])
        self.assertEqual(result['compatibility']['workerRva'], 0x7654)
        scan.assert_called_once_with(self.exe)

    def test_unknown_engine_does_not_pass_even_if_hash_was_a_baseline(self):
        self.manifest['clients'][0]['sha256']['cloudmusic.exe'] = hashlib.sha256(self.exe.read_bytes()).hexdigest()
        with mock.patch.object(compatibility, 'scan_client', return_value={'ok': False, 'reason': 'ambiguous-worker'}):
            result = installation.validate_client(self.exe, self.manifest)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'incompatible-engine')
        self.assertEqual(result['compatibility']['reason'], 'ambiguous-worker')

    def test_client_update_does_not_mutate_the_regression_baseline(self):
        before = json.dumps(self.manifest, sort_keys=True)
        with mock.patch.object(compatibility, 'scan_client', return_value=self.detected):
            installation.validate_client(self.exe, self.manifest)
        self.assertEqual(json.dumps(self.manifest, sort_keys=True), before)


if __name__ == '__main__':
    unittest.main()
