"""Portable discovery/verification tests. Only temporary fixtures are written."""
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

MODULE = Path(__file__).resolve().parents[1] / 'src' / 'python' / 'installation.py'
spec = importlib.util.spec_from_file_location('installation', MODULE)
installation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installation)


class FakeKey:
    def __init__(self, registry, hive, path, view):
        self.registry, self.hive, self.path, self.view = registry, hive, path, view
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass


class FakeRegistry:
    HKEY_CURRENT_USER = 'HKCU'
    HKEY_LOCAL_MACHINE = 'HKLM'
    KEY_READ = 0x20019
    KEY_WOW64_64KEY = 0x100
    KEY_WOW64_32KEY = 0x200
    def __init__(self):
        self.rows = {}
    def add(self, hive, path, values, view=0x100):
        self.rows[(hive, path.casefold(), view)] = values
    def OpenKey(self, hive, path, reserved, access):
        view, path = access & 0x300, path.casefold()
        if ((hive, path, view) not in self.rows and not any(
                h == hive and v == view and p.startswith(path + '\\')
                for h, p, v in self.rows)):
            raise FileNotFoundError(path)
        return FakeKey(self, hive, path, view)
    def QueryValueEx(self, key, name):
        values = self.rows.get((key.hive, key.path, key.view), {})
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1
    def EnumKey(self, key, index):
        prefix = key.path + '\\'
        names = sorted({path[len(prefix):].split('\\')[0]
                        for hive, path, view in self.rows
                        if hive == key.hive and view == key.view and path.startswith(prefix)})
        if index >= len(names):
            raise OSError('No more items')
        return names[index]


def fake_pe(machine=0x8664, marker=b'fixture-version-1'):
    content = bytearray(512)
    content[:2] = b'MZ'
    struct.pack_into('<I', content, 60, 128)
    content[128:132] = b'PE\0\0'
    struct.pack_into('<H', content, 132, machine)
    struct.pack_into('<H', content, 148, 224 if machine == 0x14c else 240)
    struct.pack_into('<H', content, 152, 0x10b if machine == 0x14c else 0x20b)
    content[420:420 + len(marker)] = marker
    return content


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows TEMP may use an 8.3 alias; match the runtime's canonical paths.
        self.root = Path(self.temp.name).resolve()
        self.client = self.make_client('arbitrary drive and folder')
        self.manifest = self.pins(self.client)
        self.env = {'LOCALAPPDATA': str(self.root / 'profile' / 'local'),
                    'USERPROFILE': str(self.root / 'profile')}
    def make_client(self, relative, machine=0x8664, marker=b'version-1'):
        folder = self.root / relative
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'cloudmusic.exe').write_bytes(fake_pe(machine, marker))
        (folder / 'cloudmusic.dll').write_bytes(b'pinned core module')
        return folder / 'cloudmusic.exe'
    def pins(self, path, extra=None):
        files = ['cloudmusic.exe', 'cloudmusic.dll', *(extra or [])]
        return [{'id': 'fixture-x64', 'architecture': 'x64', 'description': 'Test build',
                 'sha256': {name: hashlib.sha256((path.parent / name).read_bytes()).hexdigest()
                            for name in files}}]
    def locate(self, **kwargs):
        return installation.locate_supported_client(manifest=self.manifest,
            env=self.env, registry=None, include_running=False, **kwargs)

    def test_supported_build_at_arbitrary_installation_path(self):
        report = self.locate(explicit=self.client)
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['clientPath'], str(self.client.resolve()))
        self.assertEqual(report['manifestId'], 'fixture-x64')
        self.assertEqual(report['architecture'], 'x64')
        self.assertEqual(report['files'], ['cloudmusic.dll', 'cloudmusic.exe'])

    def test_installation_directory_and_quoted_executable_are_supported(self):
        for value in (self.client.parent, '"' + str(self.client) + '"'):
            with self.subTest(value=value):
                self.assertTrue(self.locate(explicit=value)['ok'])

    def test_version_is_reported_only_from_a_verified_manifest_entry(self):
        self.manifest[0]['version'] = '3.1.42.205594'
        report = self.locate(explicit=self.client)
        self.assertEqual(report['clientVersion'], '3.1.42.205594')
        (self.client.parent / 'cloudmusic.dll').write_bytes(b'incompatible update')
        failed = self.locate(explicit=self.client)
        self.assertFalse(failed['ok'])
        self.assertNotIn('clientVersion', failed)

    def test_unknown_version_fails_closed_without_modifying_fixture(self):
        self.client.write_bytes(fake_pe(marker=b'unknown-version'))
        before = {p.name: p.read_bytes() for p in self.client.parent.iterdir()}
        report = self.locate(explicit=self.client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['reason'], 'unsupported-version')
        self.assertEqual(set(report['sha256']), {'cloudmusic.exe', 'cloudmusic.dll'})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.client.parent.iterdir()})

    def test_core_dll_and_additional_pins_are_required(self):
        (self.client.parent / 'cloudmusic.dll').write_bytes(b'updated incompatible core')
        report = self.locate(explicit=self.client)
        self.assertEqual(report['reason'], 'client-files-mismatch')
        self.assertEqual(report['mismatches'][0]['files'][0]['reason'], 'hash-mismatch')
        self.manifest = self.pins(self.client)
        core = self.client.parent / 'avcodec-example.dll'
        core.write_bytes(b'additional ABI-sensitive module')
        self.manifest = self.pins(self.client, [core.name])
        core.unlink()
        report = self.locate(explicit=self.client)
        self.assertEqual(report['reason'], 'client-files-mismatch')
        self.assertEqual(report['mismatches'][0]['files'][0]['file'], core.name)

    def test_missing_core_dll_is_not_supported(self):
        (self.client.parent / 'cloudmusic.dll').unlink()
        report = self.locate(explicit=self.client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['mismatches'][0]['files'][0]['reason'], 'missing-file')

    def test_x86_and_arm64_are_rejected_by_x64_runtime(self):
        for machine, expected in ((0x14c, 'x86'), (0xaa64, 'arm64')):
            with self.subTest(machine=machine):
                self.client.write_bytes(fake_pe(machine))
                report = self.locate(explicit=self.client)
                self.assertEqual(report['architecture'], expected)
                self.assertEqual(report['reason'], 'unsupported-architecture')

    def test_32bit_python_runtime_cannot_approve_x64_injection(self):
        with mock.patch.object(installation.struct, 'calcsize', return_value=4):
            self.assertEqual(self.locate(explicit=self.client)['reason'],
                             'unsupported-runtime-architecture')

    def test_non_pe_and_truncated_headers_are_rejected(self):
        for content in (b'not an executable', b'MZ' + bytes(80), fake_pe()[:155]):
            with self.subTest(size=len(content)):
                self.client.write_bytes(content)
                self.assertEqual(self.locate(explicit=self.client)['reason'], 'invalid-pe')

    def test_non_executable_and_renamed_program_cannot_be_approved(self):
        self.assertFalse(self.locate(explicit=self.client.parent / 'cloudmusic.dll')['ok'])
        wrong = self.client.parent / 'other.exe'
        wrong.write_bytes(self.client.read_bytes())
        self.assertEqual(self.locate(explicit=wrong)['reason'], 'invalid-executable')

    def test_manifest_rejects_traversal_duplicate_names_and_unpinned_core(self):
        bad = [
            {'../outside.dll': 'a' * 64},
            {'cloudmusic.exe': 'a' * 64},
            {'cloudmusic.exe': 'a' * 64, 'CLOUDMUSIC.EXE': 'b' * 64,
             'cloudmusic.dll': 'c' * 64},
            {'cloudmusic.exe': 'not-a-hash', 'cloudmusic.dll': 'b' * 64},
        ]
        for pins in bad:
            with self.subTest(pins=pins):
                manifest = [{'id': 'bad', 'architecture': 'x64', 'sha256': pins}]
                report = installation.validate_client(self.client, manifest)
                self.assertEqual(report['reason'], 'invalid-manifest')

    def test_manifest_file_list_and_clients_wrapper(self):
        for data in (self.manifest, {'clients': self.manifest}):
            file = self.root / 'supported-clients.json'
            file.write_text(json.dumps(data), encoding='utf-8-sig')
            self.assertTrue(installation.validate_client(self.client, file)['ok'])

    def test_nested_module_pins_remain_inside_installation(self):
        nested = self.client.parent / 'plugins' / 'codec.dll'
        nested.parent.mkdir()
        nested.write_bytes(b'nested ABI-sensitive module')
        manifest = self.pins(self.client, ['plugins/codec.dll'])
        self.assertTrue(installation.validate_client(self.client, manifest)['ok'])
        # Both separators are accepted, with one stable manifest key format.
        digest = manifest[0]['sha256'].pop('plugins/codec.dll')
        manifest[0]['sha256']['plugins\\codec.dll'] = digest
        report = installation.validate_client(self.client, manifest)
        self.assertTrue(report['ok'])
        self.assertIn('plugins/codec.dll', report['files'])
        for bad in ('../codec.dll', 'sub/../../codec.dll', 'C:\\codec.dll',
                    '\\\\host\\share\\codec.dll', 'sub/file.dll:stream', '/codec.dll'):
            entry = dict(self.manifest[0], sha256=dict(self.manifest[0]['sha256'], **{bad: digest}))
            self.assertEqual(installation.validate_client(self.client, [entry])['reason'],
                             'invalid-manifest')

    def test_explicit_path_wins_over_saved_path_and_known_installation(self):
        settings = self.root / 'settings.json'
        settings.write_text(json.dumps({'clientPath': str(self.client)}), encoding='utf-8')
        report = self.locate(explicit=self.root / 'missing' / 'cloudmusic.exe', settings_path=settings)
        self.assertEqual(report['reason'], 'client-not-found')
        self.assertEqual(report['source'], 'explicit')

    def test_saved_path_is_authoritative_and_supports_environment_expansion(self):
        self.env['CUSTOM_LOCATION'] = str(self.client.parent)
        settings = self.root / 'settings.json'
        settings.write_text(json.dumps({'clientPath': '%custom_location%/cloudmusic.exe'}), encoding='utf-8')
        report = self.locate(settings_path=settings)
        self.assertTrue(report['ok'])
        self.assertEqual(report['source'], 'settings')
        self.client.unlink()
        self.assertEqual(self.locate(settings_path=settings)['reason'], 'client-not-found')

    def test_malformed_settings_do_not_silently_use_another_client(self):
        settings = self.root / 'settings.json'
        settings.write_text('{broken', encoding='utf-8')
        report = self.locate(settings_path=settings)
        self.assertEqual(report['reason'], 'discovery-error')

    def test_registry_app_paths_and_32bit_uninstall_arbitrary_location(self):
        app = r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\cloudmusic.exe'
        uninstall = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\CloudMusic'
        registry = FakeRegistry()
        registry.add('HKCU', app, {'': '"' + str(self.client) + '"'})
        result = installation.discover_client(env=self.env, registry=registry, include_running=False)
        self.assertEqual(result['candidates'][0]['path'], str(self.client.resolve()))
        self.assertIn('AppPaths', result['candidates'][0]['source'])
        registry = FakeRegistry()
        registry.add('HKLM', uninstall, {'DisplayName': '网易云音乐',
            'InstallLocation': str(self.client.parent)}, view=0x200)
        result = installation.discover_client(env=self.env, registry=registry, include_running=False)
        self.assertIn(':32:Uninstall', result['candidates'][0]['source'])

    def test_display_icon_suffix_and_case_insensitive_environment(self):
        registry = FakeRegistry()
        uninstall = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\NCM'
        registry.add('HKCU', uninstall, {'DisplayName': 'NetEase Cloud Music',
            'DisplayIcon': '"' + str(self.client) + '",0'})
        result = installation.discover_client(env={}, registry=registry, include_running=False)
        self.assertEqual(result['candidates'][0]['path'], str(self.client.resolve()))
        paths = installation.user_data_paths({'localappdata': str(self.root / 'lowercase-env')})
        self.assertIn('lowercase-env', paths['root'])

    def test_path_environment_and_running_process_discovery_are_deduplicated(self):
        env = dict(self.env, PATH=str(self.client.parent))
        with mock.patch.object(installation, 'running_client_paths', return_value=[str(self.client)]):
            result = installation.discover_client(env=env, registry=None)
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidates'][0]['source'], 'running-process')

    def test_per_user_paths_are_separate_and_creation_is_explicit(self):
        paths = installation.user_data_paths(self.env)
        self.assertFalse(Path(paths['root']).exists())
        self.assertEqual(Path(paths['settings']).parent, Path(paths['root']))
        self.assertNotEqual(Path(paths['root']), self.client.parent)
        installation.user_data_paths(self.env, create=True)
        self.assertTrue(Path(paths['logs']).is_dir())
        self.assertFalse(Path(paths['settings']).exists())

    def test_data_and_log_environment_overrides_do_not_write_during_lookup(self):
        env = {'NCM_WASAPI_DATA_DIR': str(self.root / 'custom-data'),
               'NCM_WASAPI_LOG_DIR': str(self.root / 'separate-logs')}
        paths = installation.user_data_paths(env)
        self.assertEqual(paths['root'], str((self.root / 'custom-data').resolve()))
        self.assertEqual(paths['logs'], str((self.root / 'separate-logs').resolve()))
        self.assertFalse(Path(paths['root']).exists())
        self.assertFalse(Path(paths['logs']).exists())
        installation.user_data_paths(env, create=True)
        self.assertTrue(Path(paths['root']).is_dir())
        self.assertTrue(Path(paths['logs']).is_dir())

    def test_manifest_location_supports_source_and_payload_layout(self):
        project = self.root / 'repo'
        (project / 'src' / 'python').mkdir(parents=True)
        (project / 'supported-clients.json').write_text('[]', encoding='utf-8')
        with mock.patch.object(installation, '__file__', str(project / 'src/python/installation.py')):
            self.assertEqual(installation.find_manifest(), project / 'supported-clients.json')
        payload = self.root / 'payload'
        payload.mkdir()
        (payload / 'supported-clients.json').write_text('[]', encoding='utf-8')
        with mock.patch.object(installation, '__file__', str(payload / 'installation.py')):
            self.assertEqual(installation.find_manifest(), payload / 'supported-clients.json')

    def test_redacted_cli_report_and_failure_exit_status(self):
        manifest = self.root / 'supported-clients.json'
        manifest.write_text(json.dumps(self.manifest), encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = installation.main(['--client', str(self.client), '--manifest', str(manifest),
                                        '--json', '--redact'])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(report['clientDir'], '<client-directory>')
        self.assertNotIn(str(self.root), output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            status = installation.main(['--client', str(self.root / 'absent.exe'),
                                        '--manifest', str(manifest)])
        self.assertEqual(status, 1)

    def test_diagnostic_errors_redact_arbitrary_client_directory(self):
        report = {'clientDir': str(self.client.parent), 'error':
                  'Cannot read ' + str(self.client.parent / 'cloudmusic.dll')}
        result = installation.redact_paths(report, env={})
        self.assertNotIn(str(self.client.parent), result['error'])


if __name__ == '__main__':
    unittest.main()
