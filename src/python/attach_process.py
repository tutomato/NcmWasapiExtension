"""Identity-bound handles for a naturally launched player; never starts a process.

OpenProcess establishes one kernel object identity before validating its image,
session and creation time. The same handle is retained through job assignment,
waiting and teardown, so a recycled PID is never adopted accidentally.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import math
import ntpath
import subprocess

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
WAIT_FAILED = 0xffffffff
INFINITE = 0xffffffff
PROCESS_RIGHTS = 0x00100000 | 0x1000 | 0x0100 | 0x0001


def _bind_kernel():
    kernel = C.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'OpenProcess': ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        'CloseHandle': ([W.HANDLE], W.BOOL),
        'QueryFullProcessImageNameW': ([W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
        'GetProcessTimes': ([W.HANDLE, C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME)], W.BOOL),
        'GetCurrentProcessId': ([], W.DWORD),
        'ProcessIdToSessionId': ([W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
        'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
        'GetExitCodeProcess': ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        'TerminateProcess': ([W.HANDLE, W.UINT], W.BOOL),
        'OpenEventW': ([W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
        'SetEvent': ([W.HANDLE], W.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def _failure(message):
    code = C.get_last_error() if hasattr(C, 'get_last_error') else 0
    return OSError(code, message)


def normalize_image(value):
    text = str(value).replace('/', '\\')
    if text.startswith('\\\\?\\UNC\\'):
        text = '\\\\' + text[8:]
    elif text.startswith('\\\\?\\'):
        text = text[4:]
    if not ntpath.isabs(text):
        raise ValueError('Expected an absolute process image path')
    return ntpath.normcase(ntpath.normpath(text))


def validate_identity(pid, expected_image, expected_creation_time, observed):
    """Pure policy decision, shared by actual identity checks and fixture tests."""
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 0xffffffff:
        raise ValueError('Invalid target PID')
    if (isinstance(expected_creation_time, bool) or
            not isinstance(expected_creation_time, int) or
            not 0 < expected_creation_time < (1 << 64)):
        raise ValueError('Invalid target creation time')
    if observed['creationTime'] != expected_creation_time:
        raise ValueError('Target process creation time does not match')
    if observed['sessionId'] != observed['controllerSessionId']:
        raise ValueError('Target process belongs to another Windows session')
    if normalize_image(observed['image']) != normalize_image(expected_image):
        raise ValueError('Target process image does not match the verified client')
    return True


class AttachedProcess:
    def __init__(self, pid, expected_image, creation_time, *, kernel=None):
        self._kernel = kernel if kernel is not None else _bind_kernel()
        self._handle = 0
        self.pid = int(pid)
        self.args = [str(expected_image)]
        self.returncode = None
        # Validate types before OpenProcess, including bool-as-int.
        validate_identity(pid, expected_image, creation_time, {
            'creationTime': creation_time, 'sessionId': 0, 'controllerSessionId': 0,
            'image': expected_image})
        handle = self._kernel.OpenProcess(PROCESS_RIGHTS, False, pid)
        if not handle:
            raise _failure('Cannot open target process')
        self._handle = int(handle)
        try:
            observed = self._identity()
            validate_identity(pid, expected_image, creation_time, observed)
            if self.poll() is not None:
                raise ValueError('Target process has already exited')
            self.identity = observed
        except BaseException:
            self.close()
            raise

    def _identity(self):
        image = C.create_unicode_buffer(32768)
        length = W.DWORD(len(image))
        if not self._kernel.QueryFullProcessImageNameW(self._handle, 0, image, C.byref(length)):
            raise _failure('Cannot read target process image')
        times = [W.FILETIME() for _ in range(4)]
        if not self._kernel.GetProcessTimes(self._handle, *(C.byref(t) for t in times)):
            raise _failure('Cannot read target process creation time')
        sessions = []
        for pid in (self.pid, self._kernel.GetCurrentProcessId()):
            value = W.DWORD()
            if not self._kernel.ProcessIdToSessionId(pid, C.byref(value)):
                raise _failure('Cannot read Windows process session')
            sessions.append(value.value)
        return {'image': image.value, 'creationTime': (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
                'sessionId': sessions[0], 'controllerSessionId': sessions[1]}

    def _require_handle(self):
        if not self._handle:
            raise ValueError('Target process handle is closed')
        return self._handle

    def _read_code(self):
        value = W.DWORD()
        if not self._kernel.GetExitCodeProcess(self._require_handle(), C.byref(value)):
            raise _failure('Cannot read target exit code')
        self.returncode = int(value.value)
        return self.returncode

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        result = self._kernel.WaitForSingleObject(self._require_handle(), 0)
        if result == WAIT_OBJECT_0:
            return self._read_code()
        if result == WAIT_TIMEOUT:
            return None
        raise _failure('Cannot wait for target process')

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if timeout is None:
            milliseconds = INFINITE
        else:
            seconds = float(timeout)
            if not math.isfinite(seconds):
                raise ValueError('Timeout must be finite or None')
            milliseconds = min(INFINITE - 1, max(0, math.ceil(seconds * 1000)))
        result = self._kernel.WaitForSingleObject(self._require_handle(), milliseconds)
        if result == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if result != WAIT_OBJECT_0:
            raise _failure('Cannot wait for target process')
        return self._read_code()

    def terminate(self):
        if self.poll() is not None:
            return
        if not self._kernel.TerminateProcess(self._require_handle(), 1):
            if self.poll() is None:
                raise _failure('Cannot terminate target process safely')

    kill = terminate

    def close(self):
        handle, self._handle = self._handle, 0
        if handle and not self._kernel.CloseHandle(handle):
            raise _failure('Cannot close target process handle')

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def event_names(pid, creation_time):
    suffix = str(pid) + '.' + str(creation_time)
    return ('Local\\NcmWasapiExtension.Ready.' + suffix,
            'Local\\NcmWasapiExtension.Stop.' + suffix)


class StartupSignals:
    def __init__(self, pid, creation_time, ready_name, stop_name, *, kernel=None):
        if (ready_name, stop_name) != event_names(pid, creation_time):
            raise ValueError('Startup events do not belong to this process identity')
        self._kernel = kernel if kernel is not None else _bind_kernel()
        self.ready = self.stop = 0
        self.ready = int(self._kernel.OpenEventW(2, False, ready_name) or 0)
        if not self.ready:
            raise _failure('Startup ready event is missing')
        self.stop = int(self._kernel.OpenEventW(0x00100000, False, stop_name) or 0)
        if not self.stop:
            self.close()
            raise _failure('Startup stop event is missing')

    def signal_ready(self):
        if not self.ready or not self._kernel.SetEvent(self.ready):
            raise _failure('Cannot acknowledge native audio startup')

    def stop_requested(self):
        result = self._kernel.WaitForSingleObject(self.stop, 0)
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        raise _failure('Cannot read native stop event')

    def close(self):
        for attribute in ('ready', 'stop'):
            handle, current = getattr(self, attribute, 0), 0
            setattr(self, attribute, current)
            if handle:
                self._kernel.CloseHandle(handle)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
