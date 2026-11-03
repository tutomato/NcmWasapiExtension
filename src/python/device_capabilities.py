"""Read-only Windows render endpoint catalog and exclusive format capabilities.

Uses EnumAudioEndpoints, the property store, and IsFormatSupported(EXCLUSIVE).
It never calls IAudioClient.Initialize or Start, reserves no stream, and changes
no player/device settings. Format support does not guarantee a subsequent open:
exclusive policy, a busy device, or disconnection can still prevent Initialize.

DeviceCatalog.refresh() is safe to call from a controller worker. COM pointers
are local to each call; only ordinary Python data is cached between calls.
Reference: https://learn.microsoft.com/windows/win32/api/audioclient/
nf-audioclient-iaudioclient-isformatsupported
"""
import argparse
import copy
import ctypes as C
from ctypes import wintypes as W
import json
import threading
import time
import uuid


HRESULT = C.c_int32
S_OK = 0
UNSUPPORTED_FORMAT = 0x88890008
DEVICE_IN_USE = 0x8889000a
RPC_E_CHANGED_MODE = 0x80010106
RATES = (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 64000,
         88200, 96000, 176400, 192000, 352800, 384000, 705600, 768000)
CHANNELS = (2, 1)
# The order is the native output converter's preference, within a rate/channel.
KINDS = (('s24in32', 32, 24, False), ('s24', 24, 24, False),
         ('s32', 32, 32, False), ('s16', 16, 16, False),
         ('f32', 32, 32, True))
_FATAL_PROBE_CODES = {DEVICE_IN_USE, 0x88890004, 0x8889000e, 0x88890010,
                      0x88890026, 0x80070490, 0x80070005}


class GUID(C.Structure):
    _fields_ = [('Data1', W.DWORD), ('Data2', W.WORD), ('Data3', W.WORD),
                ('Data4', C.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class WAVEFORMATEX(C.Structure):
    _pack_ = 1
    _fields_ = [('wFormatTag', W.WORD), ('nChannels', W.WORD),
                ('nSamplesPerSec', W.DWORD), ('nAvgBytesPerSec', W.DWORD),
                ('nBlockAlign', W.WORD), ('wBitsPerSample', W.WORD),
                ('cbSize', W.WORD)]


class WAVEFORMATEXTENSIBLE(C.Structure):
    _pack_ = 1
    _fields_ = [('Format', WAVEFORMATEX), ('validBits', W.WORD),
                ('channelMask', W.DWORD), ('SubFormat', GUID)]


class PROPERTYKEY(C.Structure):
    _fields_ = [('fmtid', GUID), ('pid', W.DWORD)]


class BLOB(C.Structure):
    _fields_ = [('cbSize', W.ULONG), ('pBlobData', C.c_void_p)]


class _PROPUNION(C.Union):
    _fields_ = [('pointer', C.c_void_p), ('blob', BLOB), ('scalar', C.c_int64)]


class PROPVARIANT(C.Structure):
    _anonymous_ = ('value',)
    _fields_ = [('vt', W.WORD), ('reserved1', W.WORD), ('reserved2', W.WORD),
                ('reserved3', W.WORD), ('value', _PROPUNION)]


def hr_hex(hr):
    return '0x%08x' % (hr & 0xffffffff)


class ComError(RuntimeError):
    def __init__(self, operation, hr):
        self.operation, self.hr = operation, hr & 0xffffffff
        super().__init__('%s: %s' % (operation, hr_hex(hr)))


def require(hr, operation):
    if hr < 0:
        raise ComError(operation, hr)


def method(obj, index, result_type, *arg_types):
    table = C.cast(obj, C.POINTER(C.POINTER(C.c_void_p))).contents
    return C.WINFUNCTYPE(result_type, C.c_void_p, *arg_types)(table[index])


def release(obj):
    if obj and obj.value:
        method(obj, 2, W.ULONG)(obj)
        obj.value = None


def make_format(rate, channels, kind, extensible=True):
    """Return (ctypes descriptor, JSON descriptor); no COM/device access."""
    if channels not in (1, 2) or not 0 < rate <= 768000:
        raise ValueError('Unsupported rate or channel count')
    match = next((v for v in KINDS if v[0] == kind), None)
    if match is None:
        raise ValueError('Unknown sample kind: %s' % kind)
    _, bits, valid, is_float = match
    if not extensible and bits != valid:
        raise ValueError('Padded PCM requires WAVEFORMATEXTENSIBLE')
    value = WAVEFORMATEXTENSIBLE() if extensible else WAVEFORMATEX()
    fmt = value.Format if extensible else value
    fmt.wFormatTag = 65534 if extensible else (3 if is_float else 1)
    fmt.nChannels, fmt.nSamplesPerSec = channels, rate
    fmt.nBlockAlign = channels * (bits // 8)
    fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign
    fmt.wBitsPerSample, fmt.cbSize = bits, 22 if extensible else 0
    if extensible:
        value.validBits, value.channelMask = valid, 3 if channels == 2 else 4
        value.SubFormat = GUID.parse(
            '00000003-0000-0010-8000-00aa00389b71' if is_float else
            '00000001-0000-0010-8000-00aa00389b71')
    return value, {'rate': rate, 'channels': channels, 'kind': kind,
                   'bits': bits, 'validBits': valid,
                   'blockAlign': fmt.nBlockAlign, 'formatTag': fmt.wFormatTag,
                   'channelMask': (3 if channels == 2 else 4) if extensible else 0,
                   'extensible': extensible, 'legacy': not extensible}


def select_format(device, rate, channels=2, allow_channel_fallback=False):
    """Pick a supported native-rate format; never silently change sample rate."""
    ranks = {kind[0]: i for i, kind in enumerate(KINDS)}
    choices = [f for f in device.get('formats', []) if f['rate'] == rate and
               (f['channels'] == channels or allow_channel_fallback)]
    return copy.deepcopy(min(choices, key=lambda f:
        (f['channels'] != channels, ranks.get(f['kind'], 999)))
        ) if choices else None


class _WindowsBackend:
    """One-call COM owner. Do not retain its pointers outside the context."""
    def __enter__(self):
        if not hasattr(C, 'WinDLL'):
            raise RuntimeError('Windows Core Audio is required')
        if C.sizeof(WAVEFORMATEX) != 18 or C.sizeof(WAVEFORMATEXTENSIBLE) != 40:
            raise RuntimeError('Unexpected audio format ABI')
        self.ole = C.WinDLL('ole32')
        self.ole.CoInitializeEx.argtypes = [C.c_void_p, W.DWORD]
        self.ole.CoInitializeEx.restype = HRESULT
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        self.ole.CoCreateInstance.argtypes = [C.POINTER(GUID), C.c_void_p,
            W.DWORD, C.POINTER(GUID), C.POINTER(C.c_void_p)]
        self.ole.CoCreateInstance.restype = HRESULT
        self.ole.CoTaskMemFree.argtypes = [C.c_void_p]
        self.ole.CoTaskMemFree.restype = None
        self.ole.PropVariantClear.argtypes = [C.POINTER(PROPVARIANT)]
        self.ole.PropVariantClear.restype = HRESULT
        self.enumerator = C.c_void_p()
        hr = self.ole.CoInitializeEx(None, 2)  # STA on a fresh controller worker.
        self.uninitialize = hr >= 0
        if (hr & 0xffffffff) != RPC_E_CHANGED_MODE:
            require(hr, 'CoInitializeEx')
        try:
            clsid = GUID.parse('bcde0395-e52f-467c-8e3d-c4579291692e')
            iid = GUID.parse('a95664d2-9614-4f35-a746-de8db63617e6')
            require(self.ole.CoCreateInstance(C.byref(clsid), None, 23,
                C.byref(iid), C.byref(self.enumerator)), 'CoCreateInstance')
        except BaseException:
            if self.uninitialize:
                self.ole.CoUninitialize()
            raise
        return self

    def __exit__(self, *_):
        release(self.enumerator)
        if self.uninitialize:
            self.ole.CoUninitialize()

    def _id(self, endpoint):
        value = C.c_void_p()
        require(method(endpoint, 5, HRESULT, C.POINTER(C.c_void_p))(
            endpoint, C.byref(value)), 'IMMDevice.GetId')
        try:
            return C.wstring_at(value)
        finally:
            self.ole.CoTaskMemFree(value)

    def _property(self, store, fmtid, pid):
        key = PROPERTYKEY(GUID.parse(fmtid), pid)
        value = PROPVARIANT()
        try:
            require(method(store, 5, HRESULT, C.POINTER(PROPERTYKEY),
                C.POINTER(PROPVARIANT))(store, C.byref(key), C.byref(value)),
                'IPropertyStore.GetValue')
            if value.vt == 31 and value.pointer:  # VT_LPWSTR
                return C.wstring_at(value.pointer)
            if value.vt == 65 and value.blob.pBlobData:  # VT_BLOB
                if value.blob.cbSize <= 65536:
                    return C.string_at(value.blob.pBlobData, value.blob.cbSize).hex()
            return None
        finally:
            self.ole.PropVariantClear(C.byref(value))

    def _metadata(self, endpoint):
        device_id = self._id(endpoint)
        store = C.c_void_p()
        record = {'deviceId': device_id, 'name': device_id, 'deviceFormat': None}
        try:
            require(method(endpoint, 4, HRESULT, W.DWORD,
                C.POINTER(C.c_void_p))(endpoint, 0, C.byref(store)),
                'IMMDevice.OpenPropertyStore')
            record['name'] = self._property(store,
                'a45c254e-df1c-4efd-8020-67d146a850e0', 14) or device_id
            record['deviceFormat'] = self._property(store,
                'f19f064d-082c-4e27-bc73-6882a1bb8e4c', 0)
        except Exception as error:
            record['metadataError'] = str(error)
        finally:
            release(store)
        return record

    def enumerate(self):
        collection, default = C.c_void_p(), C.c_void_p()
        errors, records, default_id = [], [], None
        try:
            hr = method(self.enumerator, 4, HRESULT, C.c_int, C.c_int,
                C.POINTER(C.c_void_p))(self.enumerator, 0, 1, C.byref(default))
            if hr >= 0:
                default_id = self._id(default)
            elif (hr & 0xffffffff) != 0x80070490:  # No default endpoint is valid.
                errors.append({'operation': 'GetDefaultAudioEndpoint', 'hr': hr_hex(hr)})
            require(method(self.enumerator, 3, HRESULT, C.c_int, W.DWORD,
                C.POINTER(C.c_void_p))(self.enumerator, 0, 1,
                C.byref(collection)), 'EnumAudioEndpoints')
            count = W.UINT()
            require(method(collection, 3, HRESULT, C.POINTER(W.UINT))(
                collection, C.byref(count)), 'IMMDeviceCollection.GetCount')
            for index in range(count.value):
                endpoint = C.c_void_p()
                try:
                    require(method(collection, 4, HRESULT, W.UINT,
                        C.POINTER(C.c_void_p))(collection, index,
                        C.byref(endpoint)), 'IMMDeviceCollection.Item')
                    records.append(self._metadata(endpoint))
                except Exception as error:
                    errors.append({'operation': 'ReadEndpoint', 'error': str(error)})
                finally:
                    release(endpoint)
        finally:
            release(default)
            release(collection)
        return records, default_id, errors

    def probe(self, device_id, rates, channels):
        endpoint, client = C.c_void_p(), C.c_void_p()
        formats, errors = [], {}
        calls, rejected = 0, 0
        def note(hr, operation):
            key = (hr_hex(hr), operation)
            errors[key] = errors.get(key, 0) + 1
        try:
            require(method(self.enumerator, 5, HRESULT, W.LPCWSTR,
                C.POINTER(C.c_void_p))(self.enumerator, device_id,
                C.byref(endpoint)), 'IMMDeviceEnumerator.GetDevice')
            iid = GUID.parse('1cb9ad4c-dbfa-4c32-b178-c2f568a703b2')
            require(method(endpoint, 3, HRESULT, C.POINTER(GUID), W.DWORD,
                C.c_void_p, C.POINTER(C.c_void_p))(endpoint, C.byref(iid),
                23, None, C.byref(client)), 'IMMDevice.Activate')
            supported = method(client, 7, HRESULT, C.c_int, C.c_void_p, C.c_void_p)
            abort = False
            for rate in rates:
                for channel_count in channels:
                    for kind, bits, valid, _ in KINDS:
                        # Older drivers sometimes accept WAVEFORMATEX only.
                        for extensible in ((True, False) if bits == valid else (True,)):
                            value, descriptor = make_format(rate, channel_count, kind, extensible)
                            hr = supported(client, 1, C.byref(value), None)
                            calls += 1
                            code = hr & 0xffffffff
                            if hr == S_OK:
                                formats.append(descriptor)
                                break
                            if code == UNSUPPORTED_FORMAT:
                                rejected += 1
                            else:
                                note(hr, 'IsFormatSupported')
                            if code in _FATAL_PROBE_CODES:
                                abort = True
                                break
                        if abort:
                            break
                    if abort:
                        break
                if abort:
                    break
        except ComError as error:
            note(error.hr, error.operation)
        except Exception as error:
            errors[(str(error), 'probe')] = 1
        finally:
            release(client)
            release(endpoint)
        error_list = [{'hr': hr, 'operation': op, 'count': count}
                      for (hr, op), count in errors.items()]
        known = True if formats else (None if errors else False)
        return {'formats': formats, 'exclusiveSupported': known,
                'probeState': ('partial' if formats else 'error') if errors else 'complete',
                'probeErrors': error_list, 'probeCalls': calls,
                'unsupportedDescriptors': rejected,
                'formatMask': sorted({f['kind'] for f in formats}),
                'capabilityOnly': True}


class DeviceCatalog:
    def __init__(self, poll_interval=2.0, capability_ttl=60.0, error_ttl=2.0,
                 rates=RATES, channels=CHANNELS, backend_factory=None, clock=None):
        self.poll_interval = max(0, float(poll_interval))
        self.capability_ttl = max(0, float(capability_ttl))
        self.error_ttl = max(0, float(error_ttl))
        self.rates, self.channels = tuple(dict.fromkeys(rates)), tuple(dict.fromkeys(channels))
        if not self.rates or not self.channels:
            raise ValueError('At least one rate and channel count are required')
        self._factory, self._clock = backend_factory or _WindowsBackend, clock or time.monotonic
        self._cache, self._lock = {}, threading.RLock()
        self._last_poll = float('-inf')
        self._snapshot = {'devices': [], 'defaultDeviceId': None, 'generation': 0, 'ok': True,
                          'errors': []}
        self._signature = None

    def refresh(self, force=False):
        """Poll active endpoints/default; probe only changed or expired entries.

        force=True also bypasses capability caches. A generation changes only
        when visible devices/default/capabilities/errors change. Returned data
        is detached from the cache and may safely be passed across threads.
        """
        with self._lock:
            now = self._clock()
            if not force and now - self._last_poll < self.poll_interval:
                return copy.deepcopy(self._snapshot)
            self._last_poll = now
            try:
                with self._factory() as backend:
                    metadata, default_id, errors = backend.enumerate()
                    live_ids = {v['deviceId'] for v in metadata}
                    self._cache = {k: v for k, v in self._cache.items() if k[0] in live_ids}
                    devices = []
                    for item in metadata:
                        key = (item['deviceId'], item['name'], item.get('deviceFormat'),
                               self.rates, self.channels, KINDS)
                        old = self._cache.get(key)
                        ttl = (self.error_ttl if old and old[1].get('probeErrors')
                               else self.capability_ttl)
                        if force or old is None or now - old[0] >= ttl:
                            capabilities = backend.probe(item['deviceId'], self.rates, self.channels)
                            if (old and old[1].get('exclusiveSupported') is True and
                                    capabilities.get('exclusiveSupported') is None):
                                # A busy/service failure cannot revoke previously
                                # verified support for this same endpoint signature.
                                capabilities = dict(capabilities,
                                    formats=copy.deepcopy(old[1]['formats']),
                                    exclusiveSupported=True,
                                    formatMask=list(old[1]['formatMask']), stale=True,
                                    probeState='busy' if any(
                                        v.get('hr') == hr_hex(DEVICE_IN_USE)
                                        for v in capabilities.get('probeErrors', []))
                                    else 'stale')
                            self._cache[key] = (now, capabilities)
                        else:
                            capabilities = old[1]
                        devices.append(dict(item, **capabilities,
                                            isDefault=item['deviceId'] == default_id))
                    devices.sort(key=lambda v: v['deviceId'])
                    answer = {'devices': devices, 'defaultDeviceId': default_id,
                              'ok': not errors, 'errors': errors}
            except Exception as error:
                # Keep last known endpoints on an enumeration/service error.
                answer = {k: copy.deepcopy(v) for k, v in self._snapshot.items()
                          if k != 'generation'}
                answer.update(ok=False, errors=[{'operation': 'refresh', 'error': str(error)}])
            signature = json.dumps(answer, sort_keys=True, ensure_ascii=True)
            generation = self._snapshot['generation'] + (signature != self._signature)
            self._signature = signature
            self._snapshot = dict(answer, generation=generation)
            return copy.deepcopy(self._snapshot)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compact', action='store_true')
    parser.add_argument('--watch', action='store_true', help='Print only catalog changes, polling every 2 seconds')
    args = parser.parse_args()
    catalog, previous = DeviceCatalog(), None
    try:
        while True:
            snapshot = catalog.refresh()
            if snapshot['generation'] != previous:
                print(json.dumps(snapshot, ensure_ascii=True,
                                 indent=None if args.compact else 2), flush=True)
                previous = snapshot['generation']
            if not args.watch:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        pass
