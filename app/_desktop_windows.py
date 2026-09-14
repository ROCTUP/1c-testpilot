"""Owned inactive Windows desktops and bounded helpers. Imported lazily on Windows."""
import ctypes
from ctypes import wintypes
from functools import wraps
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


def _win32_errors(method):
    """Expose OS failures consistently; pywintypes.error is not an OSError."""
    @wraps(method)
    def call(*args, **kwargs):
        import pywintypes
        try:
            return method(*args, **kwargs)
        except pywintypes.error as exc:
            raise OSError(exc.winerror, f'{exc.funcname}: {exc.strerror}') from exc
    return call


def _create_process(args, desktop, job):
    """Assign the job atomically at process creation, including on parent failure."""
    class StartupInfo(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('reserved', wintypes.LPWSTR), ('desktop', wintypes.LPWSTR),
                    ('title', wintypes.LPWSTR), ('x', wintypes.DWORD), ('y', wintypes.DWORD),
                    ('xsize', wintypes.DWORD), ('ysize', wintypes.DWORD), ('xchars', wintypes.DWORD),
                    ('ychars', wintypes.DWORD), ('fill', wintypes.DWORD), ('flags', wintypes.DWORD),
                    ('show', wintypes.WORD), ('reserved_size', wintypes.WORD), ('reserved_ptr', ctypes.c_void_p),
                    ('stdin', wintypes.HANDLE), ('stdout', wintypes.HANDLE), ('stderr', wintypes.HANDLE)]
    class StartupInfoEx(ctypes.Structure):
        _fields_ = [('startup', StartupInfo), ('attributes', ctypes.c_void_p)]
    class ProcessInfo(ctypes.Structure):
        _fields_ = [('process', wintypes.HANDLE), ('thread', wintypes.HANDLE),
                    ('pid', wintypes.DWORD), ('tid', wintypes.DWORD)]
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    k.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    k.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                                           ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    k.UpdateProcThreadAttribute.restype = wintypes.BOOL
    k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k.DeleteProcThreadAttributeList.restype = None
    k.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                 wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                 ctypes.POINTER(StartupInfoEx), ctypes.POINTER(ProcessInfo)]
    k.CreateProcessW.restype = wintypes.BOOL
    size = ctypes.c_size_t()
    k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = ctypes.create_string_buffer(size.value)
    if not k.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        jobs = (wintypes.HANDLE * 1)(int(job))
        # PROC_THREAD_ATTRIBUTE_JOB_LIST, available on Windows 10 and newer.
        if not k.UpdateProcThreadAttribute(attributes, 0, 0x0002000d, jobs, ctypes.sizeof(jobs), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        info = StartupInfoEx()
        info.startup.cb = ctypes.sizeof(info)
        info.startup.desktop = 'WinSta0\\' + desktop
        info.attributes = ctypes.cast(attributes, ctypes.c_void_p)
        process = ProcessInfo()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        if not k.CreateProcessW(args[0], command, None, None, False, 0x00080000 | 0x08000000,
                                None, None, ctypes.byref(info), ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        return process.process, process.thread, process.pid
    finally:
        k.DeleteProcThreadAttributeList(attributes)


class IsolatedProcess:
    """Own the desktop and kill-on-close job, rather than relying on a reusable PID."""
    @_win32_errors
    def __init__(self, args):
        import win32api
        import win32job
        import win32service
        self.args, self.pid, self.returncode = args, None, None
        self.process = self.job = self.desktop = None
        self.name = 'Testpilot_' + uuid.uuid4().hex
        try:
            self.desktop = win32service.CreateDesktop(self.name, 0, 0x01ff, None)
            self.job = win32job.CreateJobObject(None, '')
            info = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            info['BasicLimitInformation']['LimitFlags'] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, info)
            self.process, thread, self.pid = _create_process(args, self.name, self.job)
            win32api.CloseHandle(thread)
        except BaseException as exc:
            try:
                self.close()
            except Exception as cleanup_error:
                raise exc from cleanup_error
            raise

    @_win32_errors
    def poll(self):
        import win32event
        import win32process
        if self.process is not None and win32event.WaitForSingleObject(self.process, 0) == 0:
            self.returncode = win32process.GetExitCodeProcess(self.process)
        return self.returncode

    @_win32_errors
    def wait(self, timeout=10):
        import win32event
        if self.process is not None and win32event.WaitForSingleObject(self.process, max(0, int(timeout * 1000))) != 0:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.poll()

    @_win32_errors
    def close(self):
        import win32api
        import win32job
        if self.job is not None:
            win32job.TerminateJobObject(self.job, 1)
            self.wait()
            deadline = time.monotonic() + 10
            while win32job.QueryInformationJobObject(self.job, win32job.JobObjectBasicAccountingInformation)['ActiveProcesses']:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(self.args, 10)
                time.sleep(.02)
            self.job.Close()
            self.job = None
        if self.process is not None:
            win32api.CloseHandle(self.process)
            self.process = None
        if self.desktop is not None:
            self.desktop.CloseDesktop()
            self.desktop = None

    @_win32_errors
    def call(self, action, request, timeout=10):
        """A helper waits for stdin until assigned to the same owned job."""
        import win32job
        if self.job is None or self.poll() is not None or request['pid'] != self.pid:
            raise OSError('The isolated client is no longer running.')
        worker = subprocess.Popen([sys.executable, str(Path(__file__))], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        try:
            win32job.AssignProcessToJobObject(self.job, int(worker._handle))
            body = json.dumps(dict(desktop=self.name, action=action, request=request)).encode('utf8')
            stdout, stderr = worker.communicate(body, timeout=timeout)
            if worker.returncode:
                raise OSError('The isolated desktop helper failed.')
            return json.loads(stdout)
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.communicate()


def _worker(message):
    import psutil
    import win32service
    name = message['desktop']
    if not name.startswith('Testpilot_') or not name.removeprefix('Testpilot_').isalnum():
        raise ValueError('Invalid isolated desktop.')
    desktop = win32service.OpenDesktop(name, 0, False, 0x01ff)
    # This disposable thread has no windows or hooks. Never switch the input desktop.
    desktop.SetThreadDesktop()
    request = message['request']
    process = psutil.Process(request['pid'])
    if process.create_time() != request['created'] or process.name().lower() not in ('1cv8.exe', '1cv8c.exe'):
        raise ValueError('The isolated client process changed.')
    if message['action'] == 'screenshot':
        from _screenshot_windows import capture_windows
        return capture_windows(dict(request, isolated=True))
    from _native_window import active_secondary_window, close_secondary_window
    if message['action'] == 'active_window':
        return {'window': active_secondary_window(request['pid'])}
    if message['action'] == 'close_window':
        window = request['window']
        if window['pid'] != request['pid']:
            raise ValueError('The window belongs to another client.')
        return {'closed': close_secondary_window(window)}
    raise ValueError('Unknown desktop operation.')


if __name__ == '__main__':
    try:
        result = _worker(json.loads(sys.stdin.buffer.read()))
    except Exception as exc:
        result = {'ok': False, 'code': getattr(exc, 'code', 'isolated_desktop_unavailable'), 'error': str(exc)}
    print(json.dumps(result, ensure_ascii=True))
