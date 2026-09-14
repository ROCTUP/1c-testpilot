"""Private Xvfb displays. A supervisor owns children and watches the server socket for EOF."""
import ctypes
import json
import os
from pathlib import Path
import select
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time

MAX_MESSAGE = 24 * 1024 * 1024


def _send(channel, value):
    body = json.dumps(value).encode('utf8')
    if len(body) > MAX_MESSAGE:
        raise OSError('The isolated desktop response is too large.')
    channel.sendall(struct.pack('!I', len(body)) + body)


def _receive(channel, deadline=None):
    def read(size):
        chunks = bytearray()
        while len(chunks) < size:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise TimeoutError('Isolated desktop response timed out.')
                channel.settimeout(remaining)
            part = channel.recv(size - len(chunks))
            if not part:
                raise EOFError('The isolated desktop supervisor disconnected.')
            chunks.extend(part)
        return chunks
    size, = struct.unpack('!I', read(4))
    if size > MAX_MESSAGE:
        raise OSError('The isolated desktop response is too large.')
    return json.loads(read(size))


def _authority(path, number, cookie):
    # Xauthority: FamilyLocal, hostname, display number, protocol, opaque cookie.
    fields = [socket.gethostname().encode(), str(number).encode(), b'MIT-MAGIC-COOKIE-1', cookie]
    path.write_bytes(struct.pack('>H', 256) + b''.join(struct.pack('>H', len(f)) + f for f in fields))
    path.chmod(0o600)


class IsolatedProcess:
    def __init__(self, args):
        xvfb = shutil.which('Xvfb')
        if xvfb is None:
            raise OSError('Isolated launch requires Xvfb. Install xvfb (Debian/Ubuntu: sudo apt install xvfb).')
        self.args, self.pid, self.returncode = args, None, None
        self._lock = threading.Lock()
        self._channel, child = socket.socketpair()
        self._supervisor = None
        try:
            self._supervisor = subprocess.Popen([sys.executable, str(Path(__file__)), '--supervisor', str(child.fileno())],
                pass_fds=(child.fileno(),), start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child.close()
            result = self._request(dict(action='start', args=args, xvfb=xvfb), 20)
            if not result.get('ok'):
                raise OSError(result.get('error', 'The isolated display could not be started.'))
            self.pid, self.created = result['pid'], result['created']
        except BaseException:
            self.close()
            raise
        finally:
            child.close()

    def _request(self, message, timeout):
        with self._lock:
            if self._channel is None:
                raise OSError('The isolated desktop is no longer available.')
            try:
                deadline = time.monotonic() + timeout
                self._channel.settimeout(timeout)
                _send(self._channel, message)
                return _receive(self._channel, deadline)
            except (EOFError, ValueError, OSError) as exc:
                # A broken exchange cannot be reused for a later command.
                self._channel.close()
                self._channel = None
                raise OSError('The isolated desktop supervisor did not respond.') from exc

    def poll(self):
        if self._supervisor is not None:
            self.returncode = self._supervisor.poll()
        return self.returncode

    def wait(self, timeout=20):
        if self._supervisor is not None:
            self.returncode = self._supervisor.wait(timeout=timeout)
        return self.returncode

    def close(self):
        with self._lock:
            if self._channel is not None:
                self._channel.close()
                self._channel = None
        self.wait()
        if self.returncode not in (None, 0):
            # The supervisor also exits with the client's code. Its cleanup runs in finally.
            if self.returncode == 125:
                raise OSError('Could not release all isolated desktop resources.')

    def call(self, action, request, timeout=10):
        if action != 'screenshot':
            raise OSError('This isolated desktop operation is not supported on Linux.')
        if self.poll() is not None or request['pid'] != self.pid or request['created'] != self.created:
            raise OSError('The isolated client is no longer running.')
        result = self._request(dict(action=action, request=request, timeout=timeout), timeout + 3)
        if result.get('timed_out'):
            raise subprocess.TimeoutExpired('screenshot', timeout)
        return result


def _subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'Could not supervise isolated client processes.')


def _cleanup(xvfb):
    """Only descendants of this dedicated supervisor; reap adopted orphan children too."""
    import psutil
    current = psutil.Process()
    started = time.monotonic()
    deadline = started + 5
    while time.monotonic() < deadline:
        children = current.children(recursive=True)
        for child in children:
            try:
                if xvfb is not None and child.pid == xvfb.pid and time.monotonic() - started < 1:
                    child.terminate()  # X server removes its socket and lock on normal termination.
                else:
                    child.kill()
            except psutil.NoSuchProcess:
                pass
        # Do not reap live group leaders before signalling their descendants.
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if not pid: break
            except ChildProcessError:
                return
        time.sleep(.05)
    raise OSError('Some isolated desktop processes did not stop.')


def _display_files(number, pid):
    """Remember only the files created by this X server, including their inode identities."""
    lock = Path('/tmp/.X' + number + '-lock')
    sock = Path('/tmp/.X11-unix/X' + number)
    result = []
    for path, expected in ((lock, stat.S_ISREG), (sock, stat.S_ISSOCK)):
        try:
            info = path.lstat()
        except FileNotFoundError:
            # Xorg's -displayfd mode may omit the traditional lock file.
            if path == lock: continue
            raise
        if info.st_uid != os.getuid() or not expected(info.st_mode):
            raise OSError('Xvfb display ownership could not be established.')
        if path == lock and int(path.read_text().strip()) != pid:
            raise OSError('The Xvfb lock belongs to another process.')
        result.append((path, info.st_dev, info.st_ino))
    return result


def _release_display_files(files):
    for path, device, inode in files:
        try:
            info = path.lstat()
            if (info.st_dev, info.st_ino) == (device, inode):
                path.unlink()
        except FileNotFoundError:
            pass


def _supervise(channel):
    from Xlib import display
    from Xlib.ext import composite
    import psutil
    _subreaper()
    work = None; xvfb = None; desktop = None; files = []
    try:
        message = _receive(channel)
        if message.get('action') != 'start':
            raise ValueError('Expected an isolated client launch.')
        work = Path(tempfile.mkdtemp(prefix='1c-testpilot-'))
        authority = work / 'Xauthority'
        cookie = os.urandom(16)
        _authority(authority, '', cookie)
        ready, notify = os.pipe()
        try:
            xvfb = subprocess.Popen([message['xvfb'], '-displayfd', str(notify), '-screen', '0', '1920x1080x24',
                '-nolisten', 'tcp', '-auth', str(authority), '-noreset'], pass_fds=(notify,),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.close(notify); notify = None
            readable, _, _ = select.select([ready, channel], [], [], 10)
            if channel in readable:
                raise EOFError('The server ended during display startup.')
            if ready not in readable:
                raise OSError('Xvfb did not start in time.')
            number = os.read(ready, 32).decode().strip()
            if not number.isdigit():
                raise OSError('Xvfb could not create the isolated display.')
            files = _display_files(number, xvfb.pid)
        finally:
            os.close(ready)
            if notify is not None: os.close(notify)
        _authority(authority, number, cookie)
        # This process is dedicated to one client, never the MCP server's environment.
        os.environ.update(DISPLAY=':' + number, XAUTHORITY=str(authority), GDK_BACKEND='x11', QT_QPA_PLATFORM='xcb')
        for key in ('WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'):
            os.environ.pop(key, None)
        desktop = display.Display()
        if not desktop.has_extension('Composite') or not desktop.has_extension('X-Resource'):
            raise OSError('Xvfb lacks the Composite or X-Resource extension required for screenshots.')
        desktop.screen().root.composite_redirect_subwindows(composite.RedirectAutomatic)
        desktop.sync()
        client = subprocess.Popen(message['args'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        created = psutil.Process(client.pid).create_time()
        _send(channel, dict(ok=True, pid=client.pid, created=created))
        while True:
            code = client.poll()
            if code is not None:
                return code if 0 <= code < 125 else 1
            if xvfb.poll() is not None:
                raise OSError('The isolated display stopped unexpectedly.')
            if not select.select([channel], [], [], .2)[0]:
                continue
            message = _receive(channel)
            request = message.get('request', {})
            if message.get('action') != 'screenshot' or request.get('pid') != client.pid or request.get('created') != created:
                raise ValueError('The request does not belong to this isolated client.')
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).with_name('_screenshot_linux.py')), json.dumps(request)],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=min(10, message['timeout']))
                if result.returncode or len(result.stdout) > MAX_MESSAGE:
                    raise OSError('The screenshot helper failed.')
                answer = json.loads(result.stdout)
            except subprocess.TimeoutExpired:
                answer = dict(timed_out=True)
            except (ValueError, OSError):
                answer = dict(ok=False, code='screenshot_failed', error='The isolated client screenshot could not be captured.')
            _send(channel, answer)
    except EOFError:
        return 0
    except Exception as exc:
        try: _send(channel, dict(ok=False, error=str(exc)))
        except OSError: pass
        return 1
    finally:
        try:
            if desktop is not None: desktop.close()
        except Exception:
            # A failed X server has already closed this connection. Process/file cleanup
            # below remains authoritative; a disconnected X socket is not a cleanup failure.
            pass
        finally:
            _cleanup(xvfb)
            _release_display_files(files)
            if work is not None: shutil.rmtree(work)


if __name__ == '__main__':
    try:
        with socket.socket(fileno=int(sys.argv[2])) as channel:
            result = _supervise(channel)
    except Exception:
        result = 125
    sys.exit(result)
