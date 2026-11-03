"""Portable, read-only NetEase Cloud Music discovery and engine verification.

No function in this module starts, stops, or modifies the music client. Resolve
and validate BEFORE any controller action that affects an existing player.
User data is separate from the installed application and the extension files.

PE specification: https://learn.microsoft.com/windows/win32/debug/pe-format
App Paths: https://learn.microsoft.com/windows/win32/shell/app-registration
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import struct
from typing import Any, Mapping

APP_NAME = 'NcmWasapiExtension'
EXECUTABLE = 'cloudmusic.exe'
RUNTIME_ARCHITECTURE = 'x64'
def find_manifest() -> Path:
    """Support both src/python/ in the repository and a flat release payload."""
    script_dir = Path(__file__).resolve().parent
    candidates = [script_dir / 'supported-clients.json']
    if script_dir.name == 'python' and script_dir.parent.name == 'src':
        candidates.append(script_dir.parent.parent / 'supported-clients.json')
    return next((p for p in candidates if p.is_file()), candidates[-1])


DEFAULT_MANIFEST = find_manifest()
_AUTOMATIC_REGISTRY = object()


def _env(env: Mapping[str, str], name: str, default: str = '') -> str:
    return next((v for k, v in env.items() if k.casefold() == name.casefold()), default)


def _expand(value: str, env: Mapping[str, str]) -> str:
    return re.sub(r'%([^%]+)%', lambda m: _env(env, m.group(1), m.group(0)), value)


def user_data_paths(env: Mapping[str, str] | None = None, create: bool = False) -> dict:
    """Return JSON-compatible absolute paths; create directories only on request."""
    env = os.environ if env is None else env
    override = _env(env, 'NCM_WASAPI_DATA_DIR')
    local = _env(env, 'LOCALAPPDATA')
    if not local and not override:
        profile = _env(env, 'USERPROFILE')
        if not profile:
            raise RuntimeError('LOCALAPPDATA or USERPROFILE is required for user data')
        local = str(Path(profile) / 'AppData' / 'Local')
    root = Path(_expand(override, env)).expanduser().resolve() if override else (
        Path(local).expanduser() / APP_NAME).resolve()
    log_override = _env(env, 'NCM_WASAPI_LOG_DIR')
    logs = Path(_expand(log_override, env)).expanduser().resolve() if log_override else root / 'logs'
    if create:
        root.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
    return {'root': str(root), 'settings': str(root / 'settings.json'),
            'logs': str(logs), 'config': str(root / 'config.json')}


def _path(value: str | os.PathLike, env: Mapping[str, str], *, directory=False) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError('Client path must be a string or path')
    text = _expand(os.fspath(value).strip(), env)
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    if not text or '\x00' in text:
        raise ValueError('Client path is empty or invalid')
    path = Path(text).expanduser()
    if directory or path.is_dir():
        path = path / EXECUTABLE
    return path.resolve()


def _icon_path(value: str) -> str | None:
    # DisplayIcon may contain a quoted executable and a resource-index suffix.
    match = re.fullmatch(r'\s*"([^"]+\.exe)"(?:\s*,\s*-?\d+)?\s*', value, re.I)
    if match:
        return match.group(1)
    match = re.fullmatch(r'\s*(.+\.exe)(?:\s*,\s*-?\d+)?\s*', value, re.I)
    return match.group(1).strip() if match else None


def _registry_candidates(registry) -> list[tuple[str, str, bool]]:
    """Read HKCU/HKLM, both views; a winreg-compatible fake can be injected."""
    if registry is None:
        return []
    found = []
    app_path = r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\cloudmusic.exe'
    uninstall = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
    views = list(dict.fromkeys((getattr(registry, 'KEY_WOW64_64KEY', 0),
                              getattr(registry, 'KEY_WOW64_32KEY', 0))))

    def read(key, name=''):
        try:
            value, _ = registry.QueryValueEx(key, name)
            return value if isinstance(value, str) else ''
        except OSError:
            return ''

    def open_key(hive, path, view):
        return registry.OpenKey(hive, path, 0, registry.KEY_READ | view)

    for hive_name in ('HKEY_CURRENT_USER', 'HKEY_LOCAL_MACHINE'):
        hive = getattr(registry, hive_name)
        for view in views:
            source = 'registry:%s:%s' % (hive_name, '32' if view == getattr(
                registry, 'KEY_WOW64_32KEY', -1) else '64')
            try:
                with open_key(hive, app_path, view) as key:
                    if value := read(key):
                        found.append((value, source + ':AppPaths', False))
            except OSError:
                pass
            try:
                with open_key(hive, uninstall, view) as parent:
                    index = 0
                    while True:
                        try:
                            child = registry.EnumKey(parent, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with open_key(hive, uninstall + '\\' + child, view) as key:
                                name = read(key, 'DisplayName')
                                compact = re.sub(r'[\s_-]', '', name.casefold())
                                matches = ('网易云音乐' in name or '云音乐' in name or
                                           'cloudmusic' in compact or
                                           'neteasecloudmusic' in compact or
                                           'cloudmusic' in child.casefold())
                                if not matches:
                                    continue
                                if value := read(key, 'InstallLocation'):
                                    found.append((value, source + ':Uninstall', True))
                                if value := _icon_path(read(key, 'DisplayIcon')):
                                    if PureWindowsPath(value).name.casefold() == EXECUTABLE:
                                        found.append((value, source + ':DisplayIcon', False))
                        except OSError:
                            continue
            except OSError:
                pass
    return found


def running_client_paths() -> list[str]:
    """Query process image paths on Windows. Never open termination/write access."""
    if os.name != 'nt':
        return []
    import ctypes as C
    from ctypes import wintypes as W
    class PROCESSENTRY32W(C.Structure):
        _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD),
                    ('th32ProcessID', W.DWORD), ('th32DefaultHeapID', C.c_size_t),
                    ('th32ModuleID', W.DWORD), ('cntThreads', W.DWORD),
                    ('th32ParentProcessID', W.DWORD), ('pcPriClassBase', W.LONG),
                    ('dwFlags', W.DWORD), ('szExeFile', W.WCHAR * 260)]
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = W.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [
        W.HANDLE, C.POINTER(PROCESSENTRY32W)]
    kernel.Process32FirstW.restype = kernel.Process32NextW.restype = W.BOOL
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD,
                                               W.LPWSTR, C.POINTER(W.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = W.BOOL
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, C.c_void_p(-1).value):
        return []
    found = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = C.sizeof(entry)
        present = kernel.Process32FirstW(snapshot, C.byref(entry))
        while present:
            if entry.szExeFile.casefold() == EXECUTABLE:
                process = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if process:
                    try:
                        buffer, length = C.create_unicode_buffer(32768), W.DWORD(32768)
                        if kernel.QueryFullProcessImageNameW(process, 0, buffer, C.byref(length)):
                            found.append(buffer.value)
                    finally:
                        kernel.CloseHandle(process)
            present = kernel.Process32NextW(snapshot, C.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return found


def discover_client(explicit=None, settings_path=None, env=None,
                    registry=_AUTOMATIC_REGISTRY, include_running=True) -> dict:
    """Ordered discovery. Explicit and saved client paths are authoritative.

    Registry/environment/process candidates are checked for existing .exe files;
    this is not build verification. Call locate_supported_client to fail closed.
    Missing settings are normal; malformed settings produce a discovery error.
    """
    env = os.environ if env is None else env
    result = {'candidates': [], 'errors': [], 'preferred': False}
    seen = set()

    def add(value, source, directory=False, authoritative=False):
        try:
            path = _path(value, env, directory=directory)
            if path.suffix.casefold() != '.exe':
                raise ValueError('Client must be an .exe file')
            key = str(path).casefold()
            if key in seen:
                return
            if not authoritative and not path.is_file():
                return
            seen.add(key)
            result['candidates'].append({'path': str(path), 'source': source})
        except (OSError, ValueError, TypeError) as error:
            if authoritative:
                result['errors'].append({'source': source, 'error': str(error)})

    if explicit is not None:
        result['preferred'] = True
        add(explicit, 'explicit', authoritative=True)
        return result
    if settings_path is None:
        try:
            settings_path = user_data_paths(env)['settings']
        except RuntimeError:
            settings_path = None
    if settings_path is not None:
        try:
            settings = json.loads(Path(settings_path).read_text(encoding='utf-8-sig'))
            if not isinstance(settings, dict):
                raise ValueError('Settings must be a JSON object')
            value = settings.get('clientPath', settings.get('executable'))
            if value is not None and value != '':
                result['preferred'] = True
                add(value, 'settings', authoritative=True)
                return result
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as error:
            result.update(preferred=True)
            result['errors'].append({'source': 'settings', 'error': str(error)})
            return result
    if registry is _AUTOMATIC_REGISTRY:
        try:
            import winreg as registry
        except ImportError:
            registry = None
    for value, source, directory in _registry_candidates(registry):
        add(value, source, directory=directory)
    if include_running:
        for value in running_client_paths():
            add(value, 'running-process')
    for variable in ('PROGRAMW6432', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        if base := _env(env, variable):
            for relative in ('NetEase/CloudMusic', 'CloudMusic',
                             'Programs/NetEase/CloudMusic', 'Programs/CloudMusic'):
                add(Path(base) / relative, 'environment:' + variable, directory=True)
    for base in _env(env, 'PATH').split(os.pathsep):
        if base.strip():
            add(Path(base.strip().strip('"')) / EXECUTABLE, 'PATH')
    return result


def pe_architecture(path) -> str:
    """Inspect PE/COFF headers without loading or executing an image."""
    with Path(path).open('rb') as stream:
        dos = stream.read(64)
        if len(dos) != 64 or dos[:2] != b'MZ':
            raise ValueError('Not a Windows PE executable')
        offset = struct.unpack_from('<I', dos, 60)[0]
        size = os.fstat(stream.fileno()).st_size
        if offset < 64 or offset > size - 26:
            raise ValueError('Invalid PE header offset')
        stream.seek(offset)
        header = stream.read(26)
        if header[:4] != b'PE\0\0':
            raise ValueError('Invalid PE signature')
        machine = struct.unpack_from('<H', header, 4)[0]
        optional_size = struct.unpack_from('<H', header, 20)[0]
        magic = struct.unpack_from('<H', header, 24)[0]
        if optional_size < 2 or offset + 24 + optional_size > size:
            raise ValueError('Invalid PE optional header size')
        if machine == 0x8664 and magic == 0x20b:
            return 'x64'
        if machine == 0x14c and magic == 0x10b:
            return 'x86'
        if machine == 0xaa64 and magic == 0x20b:
            return 'arm64'
        raise ValueError('Unsupported or inconsistent PE machine/header')


def load_manifest(manifest=DEFAULT_MANIFEST) -> list[dict]:
    """Read trusted local release pins; this never generates or updates pins."""
    data = json.loads(Path(manifest).read_text(encoding='utf-8-sig')) if isinstance(
        manifest, (str, os.PathLike)) else manifest
    clients = data.get('clients', data.get('supportedClients')) if isinstance(data, dict) else data
    if not isinstance(clients, list) or not clients:
        raise ValueError('Manifest must contain a nonempty clients list')
    valid = []
    for client in clients:
        if not isinstance(client, dict) or not isinstance(client.get('id'), str) or not client['id']:
            raise ValueError('Every manifest entry needs an id')
        if client.get('architecture') not in ('x64', 'x86', 'arm64'):
            raise ValueError('Every manifest entry needs an architecture')
        pins = client.get('sha256')
        if not isinstance(pins, dict) or not pins:
            raise ValueError('Every manifest entry needs SHA-256 file pins')
        normalized = {}
        for name, digest in pins.items():
            relative = PureWindowsPath(name) if isinstance(name, str) else None
            segments = name.replace('\\', '/').split('/') if isinstance(name, str) else []
            if (not isinstance(name, str) or not name or ':' in name or
                    relative.is_absolute() or relative.drive or relative.root or
                    any(part in ('', '.', '..') for part in segments) or
                    not isinstance(digest, str) or
                    not re.fullmatch('[0-9a-fA-F]{64}', digest)):
                raise ValueError('Invalid manifest file name or SHA-256 digest')
            key = '/'.join(relative.parts).casefold()
            if key in normalized:
                raise ValueError('Duplicate manifest file name')
            normalized[key] = digest.casefold()
        if not {'cloudmusic.exe', 'cloudmusic.dll'} <= normalized.keys():
            raise ValueError('Manifest must pin cloudmusic.exe and cloudmusic.dll')
        valid.append(dict(client, sha256=normalized))
    return valid


def _sha256(path: Path) -> str:
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError('Client changed while being verified')
    return digest


def validate_client(client_path, manifest=DEFAULT_MANIFEST, env=None) -> dict:
    """Verify a compatible engine; legacy manifests can require exact file pins."""
    env = os.environ if env is None else env
    answer: dict[str, Any] = {'ok': False, 'reason': 'invalid-client', 'sha256': {}}
    try:
        if struct.calcsize('P') != 8:
            answer['reason'] = 'unsupported-runtime-architecture'
            return answer
        path = _path(client_path, env)
        answer.update(clientPath=str(path), clientDir=str(path.parent))
        if path.suffix.casefold() != '.exe' or path.name.casefold() != EXECUTABLE:
            answer['reason'] = 'invalid-executable'
            return answer
        if not path.is_file():
            answer['reason'] = 'client-not-found'
            return answer
        try:
            architecture = pe_architecture(path)
        except (OSError, ValueError) as error:
            answer.update(reason='invalid-pe', error=str(error))
            return answer
        answer['architecture'] = architecture
        if architecture != RUNTIME_ARCHITECTURE:
            answer['reason'] = 'unsupported-architecture'
            return answer
        try:
            manifest_data = json.loads(Path(manifest).read_text(encoding='utf-8-sig')) if isinstance(
                manifest, (str, os.PathLike)) else manifest
            clients = load_manifest(manifest_data)
        except (OSError, ValueError, TypeError) as error:
            answer.update(reason='invalid-manifest', error=str(error))
            return answer
        hashes = answer['sha256']
        hashes[EXECUTABLE] = _sha256(path)
        if isinstance(manifest_data, dict) and manifest_data.get('compatibilityMode') == 'capabilities':
            from compatibility import scan_client
            compatibility = scan_client(path)
            if not compatibility.get('ok'):
                answer.update(reason='incompatible-engine', compatibility=compatibility)
                return answer
            baseline = next((c for c in clients if c['sha256'][EXECUTABLE] == hashes[EXECUTABLE]), None)
            tested_build = bool(baseline)
            if baseline:
                for name, expected in baseline['sha256'].items():
                    candidate = path.parent.joinpath(*PureWindowsPath(name).parts).resolve()
                    if not candidate.is_relative_to(path.parent) or not candidate.is_file():
                        tested_build = False
                        continue
                    if name not in hashes:
                        hashes[name] = _sha256(candidate)
                    if hashes[name] != expected:
                        tested_build = False
            answer.update(ok=True, reason='supported', compatibility=compatibility,
                compatibilityMode='capabilities', manifestId=compatibility['scheme'],
                clientVersion=baseline.get('version', '') if baseline else '',
                testedBuild=tested_build, files=sorted(set(compatibility['modules'].values())))
            return answer
        candidates = [c for c in clients if c['architecture'] == architecture and
                      c['sha256'][EXECUTABLE] == hashes[EXECUTABLE]]
        if not candidates:
            # Include the core hash in a diagnostic for maintainers, without
            # treating an observed hash as a newly trusted release pin.
            core = (path.parent / 'cloudmusic.dll').resolve()
            if core.parent == path.parent and core.is_file():
                hashes['cloudmusic.dll'] = _sha256(core)
            answer['reason'] = 'unsupported-version'
            return answer
        failures = []
        for client in candidates:
            mismatches = []
            for name, expected in client['sha256'].items():
                file_path = path if name == EXECUTABLE else path.parent.joinpath(
                    *PureWindowsPath(name).parts).resolve()
                # Relative nested modules are allowed; symlinks must not escape
                # the installation directory tree after canonical resolution.
                if not file_path.is_relative_to(path.parent):
                    mismatches.append({'file': name, 'reason': 'outside-client-directory'})
                    continue
                if not file_path.is_file():
                    mismatches.append({'file': name, 'reason': 'missing-file'})
                    continue
                if name not in hashes:
                    hashes[name] = _sha256(file_path)
                if hashes[name] != expected:
                    mismatches.append({'file': name, 'reason': 'hash-mismatch'})
            if not mismatches:
                answer.update(ok=True, reason='supported', manifestId=client['id'],
                    clientVersion=client.get('version', ''),
                    description=client.get('description', ''), files=sorted(client['sha256']))
                return answer
            failures.append({'manifestId': client['id'], 'files': mismatches})
        answer.update(reason='client-files-mismatch', mismatches=failures)
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        answer.update(reason='verification-error', error=str(error))
    return answer


def locate_supported_client(explicit=None, settings_path=None, manifest=DEFAULT_MANIFEST,
                            env=None, registry=_AUTOMATIC_REGISTRY,
                            include_running=True) -> dict:
    discovery = discover_client(explicit, settings_path, env, registry, include_running)
    if discovery['errors']:
        return {'ok': False, 'reason': 'discovery-error', **discovery}
    attempts = []
    for candidate in discovery['candidates']:
        report = validate_client(candidate['path'], manifest, env)
        report['source'] = candidate['source']
        if report['ok'] or discovery['preferred']:
            return report
        attempts.append(report)
    return {'ok': False, 'reason': 'no-supported-client' if attempts else 'client-not-found',
            'attempts': attempts}


def redact_paths(value, env=None):
    """Remove profile prefixes and executable-directory paths for shared reports."""
    env = os.environ if env is None else env
    prefixes = [(v.rstrip('\\/'), '%' + k.upper() + '%') for k, v in env.items()
                if k.upper() in ('LOCALAPPDATA', 'APPDATA', 'USERPROFILE') and v]
    def client_prefixes(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(child, str) and child and key == 'clientDir':
                    prefixes.append((child.rstrip('\\/'), '<client-directory>'))
                client_prefixes(child)
        elif isinstance(item, list):
            for child in item:
                client_prefixes(child)
    client_prefixes(value)
    prefixes.sort(key=lambda p: len(p[0]), reverse=True)
    def walk(item, key=''):
        if isinstance(item, dict):
            return {k: walk(v, k) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v, key) for v in item]
        if isinstance(item, str):
            if key in ('clientPath', 'path'):
                return '<client-directory>/' + PureWindowsPath(item).name
            if key == 'clientDir':
                return '<client-directory>'
            for prefix, replacement in prefixes:
                item = re.sub(re.escape(prefix), lambda _: replacement, item, flags=re.I)
            return item
        return item
    return walk(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', help='cloudmusic.exe or its installation directory')
    parser.add_argument('--manifest', default=str(DEFAULT_MANIFEST))
    parser.add_argument('--settings', help='User settings JSON containing clientPath')
    parser.add_argument('--json', action='store_true', help='Print compact JSON')
    parser.add_argument('--redact', action='store_true', help='Hide personal paths for a shared report')
    args = parser.parse_args(argv)
    report = locate_supported_client(args.client, args.settings, args.manifest)
    if args.redact:
        report = redact_paths(report)
    print(json.dumps(report, ensure_ascii=True, indent=None if args.json else 2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
