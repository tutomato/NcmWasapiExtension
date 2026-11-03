"""Install/remove our app-local autoload files without modifying vendor files.

The --check path is read-only. Mutations require a stopped target client, a
trusted payload identity, and either an empty target slot or intact ownership
records. Foreign/modified files are never adopted, overwritten, or deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import uuid

OWNER = '97D3C8CF-7EB4-4710-9BBA-A8266F44CDAC'
PAYLOAD_MARKER = 'NcmWasapiExtension:' + OWNER + ':v1'
RECORD = '.ncm-wasapi-autoload.json'
DLL = 'msimg32.dll'
INI = 'ncm-wasapi-autoload.ini'
OWNED_FILES = (DLL, INI)
SCHEMA = 1
VERSION = '0.7.0-preview.8'


class DeploymentError(Exception):
    def __init__(self, reason, message, **details):
        super().__init__(message)
        self.reason, self.message, self.details = reason, message, details


def _fail(reason, message, **details):
    raise DeploymentError(reason, message, **details)


def _key(path):
    return str(Path(path).resolve()).casefold()


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _strict_json(content):
    def pairs(values):
        answer = {}
        for key, value in values:
            if key in answer:
                raise ValueError('Duplicate JSON key')
            answer[key] = value
        return answer
    return json.loads(content, object_pairs_hook=pairs)


def _paths(client, root, removing=False):
    client, root = Path(client).expanduser().resolve(), Path(root).expanduser().resolve()
    if client.is_dir():
        client /= 'cloudmusic.exe'
    if client.name.casefold() != 'cloudmusic.exe':
        _fail('invalid-client', '请选择网易云的 cloudmusic.exe 或其安装目录。')
    if not client.parent.is_dir() or (not removing and not client.is_file()):
        _fail('client-not-found', '网易云安装目录不存在或程序文件缺失。')
    if root == client.parent or root in client.parent.parents or client.parent in root.parents:
        _fail('overlapping-directories', '扩展程序和网易云必须使用独立的安装目录。')
    if any(ord(char) < 32 or char == '"' for char in str(root)):
        _fail('invalid-payload-path', '扩展安装路径含不支持的字符。')
    return client, root


def client_running(client):
    """Read-only process query; inability to inspect a matching process blocks."""
    if os.name != 'nt':
        raise OSError('Windows is required for process checks')
    import ctypes as C
    from ctypes import wintypes as W
    class Entry(C.Structure):
        _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
                    ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                    ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                    ('name', W.WCHAR * 260)]
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = W.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [W.HANDLE, C.POINTER(Entry)]
    kernel.Process32FirstW.restype = kernel.Process32NextW.restype = W.BOOL
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = W.BOOL
    kernel.CloseHandle.argtypes = [W.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, C.c_void_p(-1).value):
        raise C.WinError(C.get_last_error())
    try:
        entry = Entry(); entry.size = C.sizeof(entry)
        present = kernel.Process32FirstW(snapshot, C.byref(entry))
        if not present and C.get_last_error() != 18:
            raise C.WinError(C.get_last_error())
        while present:
            if entry.name.casefold() == 'cloudmusic.exe':
                handle = kernel.OpenProcess(0x1000, False, entry.pid)
                if not handle:
                    error = C.get_last_error()
                    if error != 87:  # ERROR_INVALID_PARAMETER: process exited.
                        raise C.WinError(error)
                else:
                    try:
                        text, size = C.create_unicode_buffer(32768), W.DWORD(32768)
                        if not kernel.QueryFullProcessImageNameW(handle, 0, text, C.byref(size)):
                            raise C.WinError(C.get_last_error())
                        if _key(text.value) == _key(client):
                            return True
                    finally:
                        kernel.CloseHandle(handle)
            present = kernel.Process32NextW(snapshot, C.byref(entry))
        if C.get_last_error() not in (0, 18):
            raise C.WinError(C.get_last_error())
        return False
    finally:
        kernel.CloseHandle(snapshot)


def directory_writable(directory):
    """Check the current token's directory rights without creating a test file."""
    if os.name != 'nt':
        return os.access(directory, os.W_OK | os.X_OK)
    import ctypes as C
    from ctypes import wintypes as W
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p,
                                   W.DWORD, W.DWORD, W.HANDLE]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.CreateFileW(str(directory), 0x1 | 0x2 | 0x40, 7, None, 3, 0x02000000, None)
    if handle == C.c_void_p(-1).value:
        return False
    kernel.CloseHandle(handle)
    return True


def _payload(root):
    try:
        if (root/'.ncm-wasapi-install').read_text(encoding='utf-8-sig') != PAYLOAD_MARKER:
            raise ValueError('Wrong payload identity')
        source = root/'native'/DLL
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError('Loader source escapes payload')
        content = source.read_bytes()
        if len(content) < 64 or content[:2] != b'MZ':
            raise ValueError('Loader is not a PE image')
        offset = struct.unpack_from('<I', content, 60)[0]
        if offset < 64 or offset + 26 > len(content) or content[offset:offset+4] != b'PE\0\0':
            raise ValueError('Invalid loader PE header')
        if struct.unpack_from('<H', content, offset+4)[0] != 0x8664 or struct.unpack_from('<H', content, offset+24)[0] != 0x20b:
            raise ValueError('Loader must be x64')
        for required in ('controller.py', 'runtime/python.exe'):
            candidate = root/required
            if not candidate.is_file() or not candidate.resolve().is_relative_to(root):
                raise ValueError('Missing payload runtime: ' + required)
        return content
    except (OSError, ValueError, struct.error) as error:
        _fail('invalid-payload', '扩展运行文件缺失或身份校验失败，请重新安装。', error=str(error))


def _ownership(client, root):
    directory = client.parent
    existing = {}
    for name in (*OWNED_FILES, RECORD):
        target = directory/name
        if target.is_symlink() or target.resolve().parent != directory:
            _fail('unsafe-target', '目标插件文件是链接，未修改任何文件。', file=name)
        if target.exists():
            if not target.is_file():
                _fail('target-not-file', '目标插件路径已被其他内容占用。', file=name)
            existing[name] = target.read_bytes()
    if RECORD not in existing:
        if existing:
            _fail('foreign-loader-conflict', '已有其他插件或未知自动加载文件，不能覆盖。请先处理现有插件。', files=sorted(existing))
        return existing, None
    try:
        record = _strict_json(existing[RECORD].decode('utf-8-sig'))
        if (not isinstance(record, dict) or record.get('schema') != SCHEMA or
                record.get('owner') != OWNER or _key(record.get('installationRoot', '')) != _key(root) or
                _key(record.get('clientPath', '')) != _key(client) or
                not isinstance(record.get('files'), dict) or set(record['files']) != set(OWNED_FILES)):
            raise ValueError('Wrong ownership identity or installation root')
        for name in OWNED_FILES:
            digest = record['files'][name]
            if not isinstance(digest, str) or len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
                raise ValueError('Invalid ownership checksum')
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        _fail('foreign-ownership-record', '自动加载记录不属于此安装，未覆盖或删除文件。', error=str(error))
    modified = [name for name in OWNED_FILES if name in existing and _sha(existing[name]) != record['files'][name]]
    if modified:
        _fail('owned-file-modified', '插件文件已被其他程序或用户修改，未覆盖或删除文件。', files=modified)
    return existing, record


def _engine_check(client, root):
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from compatibility import scan_client, scan_autoload_entry
    audio = scan_client(client)
    if not audio.get('ok'):
        return audio
    entry = scan_autoload_entry(client)
    if not entry.get('ok'):
        return entry
    return dict(audio, autoloadEntry=entry)


def _preflight(client, root, removing, process_check, engine_check, permission_check, check_payload):
    client, root = _paths(client, root, removing)
    existing, record = _ownership(client, root)
    try:
        if process_check(client):
            _fail('client-running', '请完全退出网易云后重试；正在运行时不会替换插件文件。')
    except DeploymentError:
        raise
    except Exception as error:
        _fail('process-check-failed', '无法确认网易云是否已退出，未修改文件。', error=str(error))
    if not permission_check(client.parent):
        _fail('permission-denied', '没有网易云安装目录的写入权限，请以管理员身份运行安装程序。')
    source, engine = None, None
    if not removing:
        if check_payload:
            source = _payload(root)
        try:
            engine = engine_check(client, root)
        except Exception as error:
            _fail('engine-check-failed', '无法检查网易云音频结构，未修改文件。', error=str(error))
        if not isinstance(engine, dict) or not engine.get('ok'):
            _fail('incompatible-engine', '没有识别到兼容的网易云音频结构，未安装自动加载插件。', compatibility=engine)
    return client, root, existing, record, source, engine


def _write(path, content):
    with path.open('xb') as stream:
        stream.write(content); stream.flush(); os.fsync(stream.fileno())


def _clean_transaction(transaction):
    # No recursive delete: remove only the exact files this transaction created.
    for subdir in ('new', 'backup'):
        directory = transaction/subdir
        for name in (*OWNED_FILES, RECORD):
            candidate = directory/name
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink()
        if directory.exists():
            directory.rmdir()
    transaction.rmdir()


def deploy(operation, client, root, *, process_check=None, engine_check=None, permission_check=None, for_remove=False):
    """operation is check/install/remove. Inject checks only in isolated tests."""
    process_check = process_check or client_running
    engine_check = engine_check or _engine_check
    permission_check = permission_check or directory_writable
    transaction = None
    applied = []
    try:
        if operation not in ('check', 'install', 'remove'):
            _fail('invalid-operation', '不支持的操作。')
        removing = operation == 'remove' or (operation == 'check' and for_remove)
        client, root, existing, record, source, engine = _preflight(
            client, root, removing, process_check, engine_check, permission_check, operation == 'install')
        answer = dict(ok=True, operation=operation, clientPath=str(client), installationRoot=str(root),
                      installed=bool(record), compatibility=engine)
        if operation == 'check':
            return dict(answer, reason='ready', message='自动加载安装检查通过。', writable=True)
        if removing and not record:
            return dict(answer, reason='not-installed', message='此客户端未安装本扩展的自动加载模块。', installed=False)
        transaction = client.parent/('.ncm-wasapi-transaction-'+uuid.uuid4().hex)
        transaction.mkdir()
        (transaction/'new').mkdir(); (transaction/'backup').mkdir()
        for name, content in existing.items():
            _write(transaction/'backup'/name, content)
        replacements = {}
        if not removing:
            ini = b'\xff\xfe'+('[NcmWasapiExtension]\r\nSchema=1\r\nPayloadRoot='+str(root)+'\r\n').encode('utf-16-le')
            replacements = {DLL: source, INI: ini}
            record = {'schema': SCHEMA, 'owner': OWNER, 'version': VERSION,
                'installationRoot': str(root), 'clientPath': str(client),
                'files': {name: _sha(content) for name, content in replacements.items()}}
            replacements[RECORD] = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode('utf-8')
            for name, content in replacements.items():
                _write(transaction/'new'/name, content)
        # Recheck process and bytes immediately before the first target mutation.
        if process_check(client):
            _fail('client-running', '网易云刚被启动，未替换插件文件。请退出后重试。')
        latest, _ = _ownership(client, root)
        if latest != existing:
            _fail('target-changed', '插件文件在检查后发生变化，未继续安装。')
        if removing:
            for name in (*OWNED_FILES, RECORD):
                if name in existing:
                    # Rename into a separate slot; backups remain untouched for rollback.
                    os.replace(client.parent/name, transaction/'new'/name)
                    applied.append(name)
        else:
            # Commit the ownership record last; no vendor executable is renamed.
            for name in (*OWNED_FILES, RECORD):
                os.replace(transaction/'new'/name, client.parent/name)
                applied.append(name)
        try:
            _clean_transaction(transaction)
            transaction = None
        except OSError:
            # Files are committed. A cleanup issue does not undo a complete install.
            answer['retainedTransaction'] = str(transaction)
            transaction = None
        return dict(answer, reason='removed' if removing else 'installed', installed=not removing,
                    message='已移除自动加载模块。' if removing else '已安装自动加载模块，以后可直接启动原网易云。')
    except Exception as error:
        rollback_errors = []
        if transaction is not None:
            for name in reversed(applied):
                try:
                    target = client.parent/name
                    if target.exists():
                        if removing or target.read_bytes() != replacements[name]:
                            raise OSError('Target changed during rollback; unknown content preserved')
                    if name in existing:
                        os.replace(transaction/'backup'/name, target)
                    elif target.exists():
                        target.unlink()
                except OSError as rollback_error:
                    rollback_errors.append({'file': name, 'error': str(rollback_error)})
            if not rollback_errors:
                try: _clean_transaction(transaction)
                except OSError: pass
        answer = {'ok': False, 'operation': operation, 'reason': getattr(error, 'reason', 'file-operation-failed'),
                  'message': getattr(error, 'message', '插件文件无法替换，原文件已尝试恢复。请退出网易云后重试。'),
                  'rollbackComplete': not rollback_errors}
        if isinstance(error, DeploymentError): answer.update(error.details)
        else: answer['error'] = str(error)
        if rollback_errors:
            answer.update(reason='rollback-incomplete', message='文件仍被占用，备份已保留。请关闭网易云，勿删除恢复目录。',
                          rollbackErrors=rollback_errors, recoveryDirectory=str(transaction))
        return answer


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='网易云输出扩展自动加载模块安装工具')
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ('check', 'install', 'remove'): mode.add_argument('--'+name, action='store_true')
    parser.add_argument('--client', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--for-remove', action='store_true', help='只读检查卸载条件，不检查引擎兼容性')
    parser.add_argument('--report-file', help='将 JSON 结果另存到安装器指定的报告文件')
    args = parser.parse_args(argv)
    operation = next(name for name in ('check','install','remove') if getattr(args,name))
    if args.for_remove and operation != 'check':
        parser.error('--for-remove can only be combined with --check')
    report = deploy(operation, args.client, args.root, for_remove=args.for_remove)
    if args.report_file:
        destination = Path(args.report_file).resolve()
        temporary = destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write(temporary, (json.dumps(report, ensure_ascii=False)+'\n').encode('utf-8'))
            os.replace(temporary, destination)
        except OSError as error:
            report['reportFileError'] = str(error)
        finally:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
