"""Create a Windows process suspended so its job can be assigned before it runs.

Usage::

    process = SuspendedProcess([executable], cwd=directory)
    # Assign process._handle to the configured job, checking the BOOL result.
    process.resume()

The owner must terminate a process if job assignment or startup fails. close()
only closes this object's handles; it never detaches or terminates a live player.
"""

from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import math
import os
import subprocess


class STARTUPINFOW(C.Structure):
    _fields_ = [
        ('cb', W.DWORD),
        ('lpReserved', W.LPWSTR),
        ('lpDesktop', W.LPWSTR),
        ('lpTitle', W.LPWSTR),
        ('dwX', W.DWORD),
        ('dwY', W.DWORD),
        ('dwXSize', W.DWORD),
        ('dwYSize', W.DWORD),
        ('dwXCountChars', W.DWORD),
        ('dwYCountChars', W.DWORD),
        ('dwFillAttribute', W.DWORD),
        ('dwFlags', W.DWORD),
        ('wShowWindow', W.WORD),
        ('cbReserved2', W.WORD),
        ('lpReserved2', C.POINTER(W.BYTE)),
        ('hStdInput', W.HANDLE),
        ('hStdOutput', W.HANDLE),
        ('hStdError', W.HANDLE),
    ]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [
        ('hProcess', W.HANDLE),
        ('hThread', W.HANDLE),
        ('dwProcessId', W.DWORD),
        ('dwThreadId', W.DWORD),
    ]


CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
ERROR_ACCESS_DENIED = 5

_kernel32 = C.WinDLL('kernel32', use_last_error=True)
_kernel32.CreateProcessW.argtypes = [
    W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD,
    C.c_void_p, W.LPCWSTR, C.POINTER(STARTUPINFOW),
    C.POINTER(PROCESS_INFORMATION),
]
_kernel32.CreateProcessW.restype = W.BOOL
_kernel32.ResumeThread.argtypes = [W.HANDLE]
_kernel32.ResumeThread.restype = W.DWORD
_kernel32.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
_kernel32.GetExitCodeProcess.restype = W.BOOL
_kernel32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
_kernel32.WaitForSingleObject.restype = W.DWORD
_kernel32.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
_kernel32.TerminateProcess.restype = W.BOOL
_kernel32.CloseHandle.argtypes = [W.HANDLE]
_kernel32.CloseHandle.restype = W.BOOL


def _winerror():
    return C.WinError(C.get_last_error())


class SuspendedProcess:
    """Small Popen-compatible process owner with an explicit resume operation.

    All handles are non-inheritable. Environment is inherited. The new process
    cannot execute user code or spawn children before resume() is called, so the
    caller can first assign its process handle to a KILL_ON_JOB_CLOSE job.
    """

    def __init__(self, args, *, cwd=None):
        self._handle = 0
        self._thread_handle = 0
        self.pid = None
        self.returncode = None
        self.resumed = False
        if isinstance(args, (str, bytes, os.PathLike)):
            args = [os.fsdecode(args)]
        else:
            args = [os.fsdecode(arg) for arg in args]
        if not args or not args[0]:
            raise ValueError('An executable path is required')
        self.args = args
        executable = os.path.abspath(args[0])
        if not os.path.isfile(executable):
            raise FileNotFoundError(executable)
        args = [executable, *args[1:]]
        command_line = C.create_unicode_buffer(subprocess.list2cmdline(args))
        startup = STARTUPINFOW()
        startup.cb = C.sizeof(startup)
        info = PROCESS_INFORMATION()
        if not _kernel32.CreateProcessW(
            executable, command_line, None, None, False,
            CREATE_SUSPENDED | CREATE_NO_WINDOW, None,
            os.fsdecode(cwd) if cwd is not None else None,
            C.byref(startup), C.byref(info),
        ):
            raise _winerror()
        self._handle = int(info.hProcess)
        self._thread_handle = int(info.hThread)
        self.pid = int(info.dwProcessId)

    def _require_handle(self):
        if not self._handle:
            raise ValueError('Process handle has been closed')
        return self._handle

    def resume(self):
        if self.resumed:
            return
        self._require_handle()
        if not self._thread_handle:
            raise ValueError('Primary thread handle has been closed')
        previous = _kernel32.ResumeThread(self._thread_handle)
        if previous == WAIT_FAILED:
            raise _winerror()
        if previous > 1:
            # Do not override suspension requested by another actor. The caller
            # should terminate the process in its startup-failure cleanup path.
            raise RuntimeError('Primary thread remains suspended')
        self.resumed = True
        handle, self._thread_handle = self._thread_handle, 0
        if not _kernel32.CloseHandle(handle):
            raise _winerror()

    def _read_returncode(self):
        code = W.DWORD()
        if not _kernel32.GetExitCodeProcess(self._require_handle(), C.byref(code)):
            raise _winerror()
        self.returncode = int(code.value)
        return self.returncode

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        result = _kernel32.WaitForSingleObject(self._require_handle(), 0)
        if result == WAIT_OBJECT_0:
            return self._read_returncode()
        if result == WAIT_TIMEOUT:
            return None
        if result == WAIT_FAILED:
            raise _winerror()
        raise OSError('Unexpected process wait result: %r' % result)

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
        result = _kernel32.WaitForSingleObject(self._require_handle(), milliseconds)
        if result == WAIT_OBJECT_0:
            return self._read_returncode()
        if result == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if result == WAIT_FAILED:
            raise _winerror()
        raise OSError('Unexpected process wait result: %r' % result)

    def terminate(self):
        if self.poll() is not None:
            return
        if not _kernel32.TerminateProcess(self._require_handle(), 1):
            code = C.get_last_error()
            # The process can exit between poll and TerminateProcess. Windows
            # reports ACCESS_DENIED for an already terminated process handle.
            if code == ERROR_ACCESS_DENIED and self.poll() is not None:
                return
            raise C.WinError(code)

    def kill(self):
        self.terminate()

    def close(self):
        # Attempt both closes even if one fails. Clear first for idempotence and
        # to prevent accidental use of a recycled numeric handle.
        first_error = None
        for attr in ('_thread_handle', '_handle'):
            handle = getattr(self, attr, 0)
            setattr(self, attr, 0)
            if handle and not _kernel32.CloseHandle(handle) and first_error is None:
                first_error = _winerror()
        if first_error is not None:
            raise first_error

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def create_suspended(executable, args=None, *, cwd=None):
    return SuspendedProcess([os.fsdecode(executable), *(args or [])], cwd=cwd)
