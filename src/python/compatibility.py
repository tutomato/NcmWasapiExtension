"""Read-only structural compatibility checks for the NCM x64 audio engine.

This module never loads a DLL, creates a player, writes files, or accepts an
observed binary hash as a new compatibility rule. A profile describes the
particular engine contract supported by our hooks, independent of product
version and absolute/RVA placement. Runtime validation must still check the
loaded FFmpeg ABI majors before installing any audio hooks.

PE layout: https://learn.microsoft.com/windows/win32/debug/pe-format
FFmpeg ABI: https://www.ffmpeg.org/doxygen/trunk/index.html
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
import re
import struct
from typing import Mapping

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TABLE_ENTRIES = 1024 * 1024
EXECUTE = 0x20000000

DEFAULT_PROFILE = {
    'scheme': 'ncm-ffmpeg58-thread-v1',
    'architecture': 'x64',
    'player': 'cloudmusic.dll',
    'worker': {
        'pattern': ('48 89 4c 24 08 55 57 48 81 ec 08 01 00 00 48 8d 6c 24 20 '
                    '48 8d 0d ?? ?? ?? ?? e8 ?? ?? ?? ?? 48 8b 85 00 01 00 00 '
                    '48 89 45 08 48 83 7d 08 00 74 10 48 8b 45 08 48 8b 00 '
                    '48 8b 4d 08 ff 50 08 eb 02 33 c0 48 8d a5 e8 00 00 00 '
                    '5f 5d c3'),
        'minFunctionSize': 77,
        'maxFunctionSize': 77,
        'requireCreateThreadXref': True,
        'maxCreateThreadRefs': 1,
    },
    'roles': {
        'avcodec': {'abiMajor': 58, 'exports': [
            'avcodec_version', 'avcodec_open2', 'avcodec_receive_frame']},
        'avutil': {'abiMajor': 56, 'exports': [
            'avutil_version', 'av_opt_get_int', 'av_frame_get_sample_rate']},
        'swresample': {'abiMajor': 3, 'exports': [
            'swresample_version', 'swr_alloc_set_opts']},
    },
}


class PEError(ValueError):
    pass


@dataclass(frozen=True)
class Section:
    name: str
    rva: int
    virtual_size: int
    raw_offset: int
    raw_size: int
    flags: int

    @property
    def span(self):
        return max(self.virtual_size, self.raw_size)

    @property
    def executable(self):
        return bool(self.flags & EXECUTE)


@dataclass(frozen=True)
class Export:
    name: str
    rva: int
    forwarder: str | None = None


@dataclass(frozen=True)
class RuntimeFunction:
    begin: int
    end: int
    unwind: int


class PEImage:
    """A bounds-checked, file-backed AMD64 PE32+ reader. No image loading."""
    def __init__(self, path_or_bytes):
        if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
            self.data = bytes(path_or_bytes)
        else:
            path = Path(path_or_bytes)
            if path.stat().st_size > MAX_FILE_BYTES:
                raise PEError('Image exceeds supported file size')
            self.data = path.read_bytes()
        if not 64 <= len(self.data) <= MAX_FILE_BYTES or self.data[:2] != b'MZ':
            raise PEError('Not a supported PE image')
        offset = self.u32(60)
        if offset < 64 or self.take(offset, 4) != b'PE\0\0':
            raise PEError('Invalid PE signature')
        file_header = offset + 4
        machine = self.u16(file_header)
        section_count = self.u16(file_header + 2)
        optional_size = self.u16(file_header + 16)
        if machine != 0x8664 or not 1 <= section_count <= 96:
            raise PEError('Only AMD64 PE images with a bounded section table are supported')
        optional = file_header + 20
        self.take(optional, optional_size)
        if optional_size < 112 or self.u16(optional) != 0x20b:
            raise PEError('Only PE32+ is supported')
        self.image_size = self.u32(optional + 56)
        self.header_size = self.u32(optional + 60)
        if (not 0 < self.image_size <= 0x80000000 or
                not optional + optional_size <= self.header_size <= len(self.data)):
            raise PEError('Invalid image/header size')
        directory_count = self.u32(optional + 108)
        if directory_count > (optional_size - 112) // 8:
            raise PEError('Data-directory count exceeds optional header')
        self.directories = [struct.unpack('<II', self.take(optional + 112 + i * 8, 8))
                            for i in range(min(directory_count, 16))]
        section_table = optional + optional_size
        if section_table + section_count * 40 > self.header_size:
            raise PEError('Section table exceeds headers')
        sections = []
        for index in range(section_count):
            base = section_table + index * 40
            entry = self.take(base, 40)
            name = entry[:8].split(b'\0', 1)[0].decode('ascii', errors='replace')
            virtual_size, rva, raw_size, raw_offset = struct.unpack_from('<IIII', entry, 8)
            flags = struct.unpack_from('<I', entry, 36)[0]
            section = Section(name, rva, virtual_size, raw_offset, raw_size, flags)
            if section.span and (rva < self.header_size or rva + section.span > self.image_size):
                raise PEError('Section lies outside image')
            if raw_size:
                if raw_offset < self.header_size:
                    raise PEError('Section overlaps headers')
                self.take(raw_offset, raw_size)
            sections.append(section)
        by_rva = sorted((s for s in sections if s.span), key=lambda s: s.rva)
        for left, right in zip(by_rva, by_rva[1:]):
            if left.rva + left.span > right.rva:
                raise PEError('Overlapping section virtual ranges')
        by_raw = sorted((s for s in sections if s.raw_size), key=lambda s: s.raw_offset)
        for left, right in zip(by_raw, by_raw[1:]):
            if left.raw_offset + left.raw_size > right.raw_offset:
                raise PEError('Overlapping section raw ranges')
        self.sections = tuple(sections)
        self.architecture = 'x64'

    def take(self, offset, size):
        if (not isinstance(offset, int) or not isinstance(size, int) or
                offset < 0 or size < 0 or offset > len(self.data) - size):
            raise PEError('Read outside image file')
        return self.data[offset:offset + size]

    def u16(self, offset):
        return struct.unpack('<H', self.take(offset, 2))[0]

    def u32(self, offset):
        return struct.unpack('<I', self.take(offset, 4))[0]

    def directory(self, index):
        rva, size = self.directories[index] if index < len(self.directories) else (0, 0)
        if bool(rva) != bool(size):
            raise PEError('Inconsistent data directory')
        return rva, size

    def section_at(self, rva, size=1):
        for section in self.sections:
            if section.rva <= rva and size >= 0 and rva + size <= section.rva + section.span:
                return section
        raise PEError('RVA outside sections')

    def rva_offset(self, rva, size=1):
        if not isinstance(rva, int) or rva < 0 or size < 0:
            raise PEError('Invalid RVA')
        if rva < self.header_size and rva + size <= self.header_size:
            self.take(rva, size)
            return rva
        section = self.section_at(rva, size)
        delta = rva - section.rva
        if delta + size > section.raw_size:
            raise PEError('RVA has no file-backed data')
        offset = section.raw_offset + delta
        self.take(offset, size)
        return offset

    def rva_bytes(self, rva, size):
        return self.take(self.rva_offset(rva, size), size)

    def c_string(self, rva, limit=4096):
        offset = self.rva_offset(rva)
        if rva < self.header_size:
            available = self.header_size - rva
        else:
            section = self.section_at(rva)
            available = section.raw_size - (rva - section.rva)
        raw = self.take(offset, min(limit, available))
        end = raw.find(b'\0')
        if end < 0:
            raise PEError('Unterminated PE string')
        try:
            return raw[:end].decode('ascii')
        except UnicodeDecodeError as error:
            raise PEError('Non-ASCII PE symbol') from error

    def executable(self, rva, size=1):
        try:
            return self.section_at(rva, size).executable
        except PEError:
            return False

    def exports(self):
        rva, size = self.directory(0)
        if not rva:
            return {}
        if size < 40:
            raise PEError('Truncated export directory')
        self.rva_offset(rva, size)
        header = self.rva_bytes(rva, 40)
        function_count, name_count, functions_rva, names_rva, ordinals_rva = struct.unpack_from('<IIIII', header, 20)
        if function_count > MAX_TABLE_ENTRIES or name_count > MAX_TABLE_ENTRIES:
            raise PEError('Invalid export table count')
        functions = self.rva_bytes(functions_rva, function_count * 4)
        names = self.rva_bytes(names_rva, name_count * 4)
        ordinals = self.rva_bytes(ordinals_rva, name_count * 2)
        result = {}
        for index in range(name_count):
            ordinal = struct.unpack_from('<H', ordinals, index * 2)[0]
            if ordinal >= function_count:
                raise PEError('Export ordinal outside function table')
            name = self.c_string(struct.unpack_from('<I', names, index * 4)[0])
            target = struct.unpack_from('<I', functions, ordinal * 4)[0]
            if not name or name in result:
                raise PEError('Invalid or duplicate export name')
            forwarder = self.c_string(target) if rva <= target < rva + size else None
            result[name] = Export(name, target, forwarder)
        return result

    def imports(self):
        rva, size = self.directory(1)
        if not rva:
            return {}
        if size < 20:
            raise PEError('Truncated import directory')
        self.rva_offset(rva, size)
        result = {}
        for index in range(min(size // 20, 4096)):
            descriptor = struct.unpack('<IIIII', self.rva_bytes(rva + index * 20, 20))
            if not any(descriptor):
                return result
            original, _, _, module_rva, iat = descriptor
            module = self.c_string(module_rva).casefold()
            if not module or not iat:
                raise PEError('Invalid import descriptor')
            thunk = original or iat
            for entry in range(65536):
                value = struct.unpack('<Q', self.rva_bytes(thunk + entry * 8, 8))[0]
                if not value:
                    break
                slot = iat + entry * 8
                self.rva_offset(slot, 8)
                if value & (1 << 63):
                    name = '#' + str(value & 0xffff)
                else:
                    if value > 0xffffffff:
                        raise PEError('Unsupported bound import without name table')
                    name = self.c_string(value + 2)
                if slot in result:
                    raise PEError('Duplicate import slot')
                result[slot] = (module, name)
            else:
                raise PEError('Unterminated import-name table')
        raise PEError('Unterminated import descriptors')

    def runtime_functions(self):
        rva, size = self.directory(3)
        if not rva:
            return []
        if size % 12 or size // 12 > MAX_TABLE_ENTRIES:
            raise PEError('Invalid exception directory size')
        table = self.rva_bytes(rva, size)
        result = []
        begins = set()
        for begin, end, unwind in struct.iter_unpack('<III', table):
            if not (begin or end or unwind):
                continue
            if (not 0 < begin < end <= self.image_size or not unwind or
                    begin in begins or not self.executable(begin, end - begin)):
                raise PEError('Invalid runtime-function entry')
            self.rva_offset(unwind, 1)
            begins.add(begin)
            result.append(RuntimeFunction(begin, end, unwind))
        return sorted(result, key=lambda item: item.begin)


def compile_pattern(text):
    if not isinstance(text, str):
        raise ValueError('Pattern must be text')
    tokens = text.split()
    if not tokens or len(tokens) > 4096:
        raise ValueError('Invalid pattern size')
    values, masks = [], []
    for token in tokens:
        if not re.fullmatch('[0-9a-fA-F?]{2}', token):
            raise ValueError('Invalid byte pattern token')
        value, mask = 0, 0
        for nibble in token:
            value = (value << 4) | (0 if nibble == '?' else int(nibble, 16))
            mask = (mask << 4) | (0 if nibble == '?' else 15)
        values.append(value)
        masks.append(mask)
    if sum(mask.bit_count() for mask in masks) < 128:
        raise ValueError('Pattern must retain at least 128 fixed bits')
    return bytes(values), bytes(masks)


def pattern_matches(data, values, masks):
    return len(data) == len(values) and all((byte & mask) == value for byte, value, mask in zip(data, values, masks))


def find_create_thread_refs(image: PEImage, worker_rva, functions=None):
    imports = image.imports()
    functions = image.runtime_functions() if functions is None else functions
    starts = [function.begin for function in functions]
    result = []
    for section in image.sections:
        if not section.executable or section.raw_size < 17:
            continue
        code = image.take(section.raw_offset, section.raw_size)
        offset = 0
        while True:
            offset = code.find(b'\x4c\x8d\x05', offset)
            if offset < 0:
                break
            base = section.rva + offset
            raw = code[offset:offset + 17]
            offset += 1
            if (len(raw) != 17 or raw[7:9] not in (b'\x33\xd2', b'\x31\xd2') or
                    raw[9:11] not in (b'\x33\xc9', b'\x31\xc9') or raw[11:13] != b'\xff\x15'):
                continue
            if base + 7 + struct.unpack_from('<i', raw, 3)[0] != worker_rva:
                continue
            containing = bisect_right(starts, base) - 1
            if containing < 0 or base + 17 > functions[containing].end:
                continue
            slot = base + 17 + struct.unpack_from('<i', raw, 13)[0]
            module, name = imports.get(slot, ('', ''))
            if module not in ('kernel32.dll', 'kernelbase.dll') or name != 'CreateThread':
                continue
            result.append({'rva': base, 'iatRva': slot, 'module': module, 'name': name})
    return result


def detect_worker(image: PEImage, profile=DEFAULT_PROFILE):
    worker = profile['worker']
    values, masks = compile_pattern(worker['pattern'])
    minimum, maximum = worker['minFunctionSize'], worker['maxFunctionSize']
    if not isinstance(minimum, int) or not isinstance(maximum, int) or not 0 < minimum <= maximum <= 65536:
        raise ValueError('Invalid worker-function size constraints')
    functions = image.runtime_functions()
    matches = []
    for function in functions:
        size = function.end - function.begin
        if minimum <= size <= maximum and len(values) <= size and pattern_matches(
                image.rva_bytes(function.begin, len(values)), values, masks):
            matches.append(function)
    if len(matches) != 1:
        return {'ok': False, 'reason': 'worker-pattern-missing' if not matches else 'worker-pattern-ambiguous',
                'matchCount': len(matches)}
    function = matches[0]
    xrefs = find_create_thread_refs(image, function.begin, functions)
    if worker.get('requireCreateThreadXref', True) and not xrefs:
        return {'ok': False, 'reason': 'worker-xref-missing', 'matchCount': 1}
    max_refs = worker.get('maxCreateThreadRefs', 1)
    if not isinstance(max_refs, int) or max_refs < 1:
        raise ValueError('Invalid CreateThread reference limit')
    if len(xrefs) > max_refs:
        return {'ok': False, 'reason': 'worker-xref-ambiguous', 'matchCount': 1,
                'createThreadRefCount': len(xrefs)}
    return {'ok': True, 'workerRva': function.begin, 'functionSize': function.end - function.begin,
            'matchCount': 1, 'createThreadRefs': xrefs}


def scan_client(client, profile=DEFAULT_PROFILE):
    """Return structural evidence or a rejection. No ProductVersion/hash gates.

    Only direct DLL files in the selected installation directory are examined.
    A symlink resolving outside that directory cannot provide an API role.
    Required API roles must be unique, direct executable exports; forwarded
    exports and ambiguous providers are rejected. ABI integers are checked in
    the target process before hooks, rather than loading untrusted DLLs here.
    """
    result = {'ok': False, 'reason': 'invalid-client', 'scheme': profile.get('scheme'),
              'architecture': 'x64', 'features': {}, 'modules': {}}
    try:
        if profile.get('architecture') != 'x64' or not isinstance(profile.get('scheme'), str):
            raise ValueError('Only named x64 compatibility profiles are supported')
        path = Path(client).expanduser().resolve()
        if not path.exists():
            return dict(result, reason='client-not-found')
        if not path.is_dir() and path.name.casefold() != 'cloudmusic.exe':
            return dict(result, reason='invalid-client')
        directory = path if path.is_dir() else path.parent
        executable = (directory / 'cloudmusic.exe').resolve()
        if executable.parent != directory or not executable.is_file():
            return dict(result, reason='client-not-found')
        PEImage(executable)
        player_name = profile['player']
        if not isinstance(player_name, str) or Path(player_name).name != player_name:
            raise ValueError('Player module must be a flat filename')
        player_path = (directory / player_name).resolve()
        if player_path.parent != directory or not player_path.is_file():
            return dict(result, reason='player-module-not-found')
        player = PEImage(player_path)
        worker = detect_worker(player, profile)
        if not worker['ok']:
            return dict(result, reason=worker['reason'], evidence={'worker': worker})
        roles = profile['roles']
        if not isinstance(roles, Mapping) or not roles:
            raise ValueError('Profile must declare API roles')
        for contract in roles.values():
            major = contract['abiMajor']
            if not isinstance(major, int) or isinstance(major, bool) or not 0 < major < 256:
                raise ValueError('Invalid API-role ABI major')
        providers = {role: [] for role in roles}
        rejected = {role: [] for role in roles}
        scanned = []
        for candidate in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if candidate.suffix.casefold() != '.dll' or not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if resolved.parent != directory:
                continue
            try:
                image = player if resolved == player_path else PEImage(resolved)
                exports = image.exports()
            except (OSError, PEError):
                continue
            scanned.append(candidate.name)
            for role, contract in roles.items():
                required = contract['exports']
                if (not isinstance(required, list) or not required or
                        not all(isinstance(name, str) and name for name in required)):
                    raise ValueError('Invalid API role exports')
                if all(name in exports for name in required):
                    invalid = [name for name in required if exports[name].forwarder or
                               not image.executable(exports[name].rva)]
                    if invalid:
                        rejected[role].append({'module': candidate.name, 'exports': invalid})
                    else:
                        providers[role].append(candidate.name)
        for role, matches in providers.items():
            if len(matches) != 1:
                reason = 'api-role-missing' if not matches else 'api-role-ambiguous'
                if not matches and rejected[role]:
                    reason = 'api-role-invalid-export'
                return dict(result, reason=reason, role=role,
                            evidence={'providers': matches, 'rejected': rejected[role]})
        modules = {'player': player_name, **{role: matches[0] for role, matches in providers.items()}}
        result.update(ok=True, reason='compatible-engine', modules=modules,
                      outputWorkerRva=worker['workerRva'], workerRva=worker['workerRva'],
                      features={'nativeSourceRate': True, 'wasapiExclusive': True,
                                'frontendRuntimeRequired': True},
                      evidence={'worker': worker, 'abiMajors': {role: contract['abiMajor']
                                for role, contract in roles.items()},
                                'scannedModules': scanned})
    except (OSError, PEError) as error:
        result.update(reason='unsupported-pe', error=str(error))
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        result.update(reason='invalid-profile', error=str(error))
    return result


def scan_autoload_entry(client):
    """Read-only import/export contract for our app-local loader.

    This checks the actual direct-import slots our native loader replaces,
    without loading any image or changing the installation. It does not prove
    runtime dependency initialization order; natural-startup regression remains
    necessary. The future app-local msimg32 proxy is supplied by our installer,
    so no existing msimg32.dll is required or adopted by this check.
    """
    result = {'ok': False, 'reason': 'invalid-client',
              'scheme': 'ncm-app-local-cef-imports-v1', 'architecture': 'x64'}
    try:
        path = Path(client).expanduser().resolve()
        if not path.exists():
            return dict(result, reason='client-not-found')
        if not path.is_dir() and path.name.casefold() != 'cloudmusic.exe':
            return result
        directory = path if path.is_dir() else path.parent
        images = {}
        for name in ('cloudmusic.exe', 'cloudmusic.dll', 'libcef.dll'):
            candidate = (directory / name).resolve()
            if candidate.parent != directory:
                return dict(result, reason='autoload-entry-module-outside-client', module=name)
            if not candidate.is_file():
                return dict(result, reason='autoload-entry-module-missing', module=name)
            # PEImage accepts AMD64 PE32+ only, including the executable itself.
            images[name] = PEImage(candidate)

        def imports_from(image, module):
            return {slot: symbol for slot, (owner, symbol) in image.imports().items()
                    if owner.casefold() == module}

        msimg = imports_from(images['cloudmusic.dll'], 'msimg32.dll')
        if not msimg:
            return dict(result, reason='autoload-entry-msimg32-import-missing')
        supported_msimg = {'AlphaBlend', 'DllInitialize', 'GradientFill',
                           'TransparentBlt', 'vSetDdrawflag', '#1', '#2', '#3', '#4', '#5'}
        unsupported = sorted(set(msimg.values()) - supported_msimg)
        if unsupported:
            return dict(result, reason='autoload-entry-msimg32-import-unsupported',
                        symbols=unsupported)

        cef_imports = imports_from(images['cloudmusic.dll'], 'libcef.dll')
        command_imports = imports_from(images['libcef.dll'], 'kernel32.dll')
        slots = {}
        for importer, imported, names in (
                ('cloudmusic.dll', cef_imports, ('cef_initialize', 'cef_execute_process')),
                ('libcef.dll', command_imports, ('GetCommandLineW', 'GetCommandLineA'))):
            for name in names:
                matches = [slot for slot, symbol in imported.items() if symbol == name]
                if len(matches) != 1:
                    return dict(result, reason='autoload-entry-import-missing' if not matches
                                else 'autoload-entry-import-ambiguous', importer=importer,
                                symbol=name, matchCount=len(matches))
                slots[name] = matches[0]

        cef = images['libcef.dll']
        exports = cef.exports()
        export_rvas = {}
        for name in ('cef_initialize', 'cef_execute_process'):
            item = exports.get(name)
            if item is None:
                return dict(result, reason='autoload-entry-export-missing', symbol=name)
            if item.forwarder or not cef.executable(item.rva):
                return dict(result, reason='autoload-entry-export-invalid', symbol=name)
            export_rvas[name] = item.rva
        return dict(result, ok=True, reason='compatible-autoload-entry',
                    modules={'player': 'cloudmusic.dll', 'cef': 'libcef.dll'},
                    evidence={'msimg32Imports': sorted(set(msimg.values())),
                              'importSlots': slots, 'cefExportRvas': export_rvas},
                    runtimeLoadOrderVerified=False)
    except (OSError, PEError) as error:
        return dict(result, reason='autoload-entry-invalid-pe', error=str(error))
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        return dict(result, reason='autoload-entry-invalid-contract', error=str(error))
