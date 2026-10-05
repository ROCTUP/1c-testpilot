"""Addressed local shortcuts. The caller verifies the application's resulting state."""
import json
import subprocess
import sys
import time
from pathlib import Path

from _screenshots import CaptureError, resolve_process


class KeyboardError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def combination(keys):
    """Modifiers followed by one named key; no text or arbitrary OS command."""
    if not isinstance(keys, (tuple, list)) or not 1 <= len(keys) <= 4:
        raise KeyboardError('invalid_shortcut', 'Use modifiers followed by one key.')
    if any(not isinstance(k, str) for k in keys):
        raise KeyboardError('invalid_shortcut', 'Shortcut keys must be strings.')
    keys = tuple(k.upper() for k in keys)
    allowed = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') | {'ENTER', 'ESC', 'TAB', 'SPACE'}
    allowed |= {f'F{i}' for i in range(1, 13)}
    if (keys[-1] not in allowed or any(k not in ('CTRL', 'SHIFT', 'ALT') for k in keys[:-1])
            or len(set(keys)) != len(keys)):
        raise KeyboardError('invalid_shortcut', 'Unsupported key or duplicate modifier.')
    return keys


def send_shortcut(runtime, client, window_key, keys):
    keys = combination(keys)
    if sys.platform not in ('win32', 'linux'):
        raise KeyboardError('keyboard_platform_unsupported', 'Local shortcuts require Windows or Linux with X11.')
    try:
        pid, created = resolve_process(client)
    except CaptureError as exc:
        code = exc.code.replace('screenshot_', 'keyboard_', 1)
        message = ('Sending a shortcut requires access to the client desktop on the same computer.'
                   if code == 'keyboard_remote_client' else
                   'The connected local 1C process could not be identified. No shortcut was sent.')
        raise KeyboardError(code, message) from exc
    active = runtime._window(client)
    if not active.get('ok') or active.get('key') != window_key:
        raise KeyboardError('keyboard_window_not_active', 'Activate the addressed 1C window before sending a shortcut.')
    request = dict(pid=pid, created=created, keys=keys, title=active.get('title') or '')
    remaining = 5.0
    deadline = getattr(client, '_io_deadline', None)
    if deadline is not None:
        remaining = min(remaining, deadline - time.monotonic())
    if remaining < 1:
        raise KeyboardError('keyboard_deadline_exceeded', 'No time remains to send and release the shortcut keys.')
    isolated = vars(client).get('_isolated_process')
    try:
        if isolated is not None:
            out = isolated.call('keyboard', request, timeout=remaining)
        else:
            result = subprocess.run([sys.executable, str(Path(__file__)), json.dumps(request)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=remaining,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode or len(result.stdout) > 16384:
                raise KeyboardError('keyboard_failed', 'The shortcut helper failed; its effect is unknown.')
            out = json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise KeyboardError('keyboard_timeout', 'The shortcut helper timed out; its effect is unknown.') from exc
    except (OSError, ValueError) as exc:
        raise KeyboardError('keyboard_desktop_unavailable', 'Cannot access the client desktop to send the shortcut.') from exc
    if out.get('timed_out'):
        raise KeyboardError('keyboard_timeout', 'The shortcut helper timed out; its effect is unknown.')
    if not out.get('ok'):
        raise KeyboardError(out.get('code', 'keyboard_failed'), out.get('error', 'Could not send the shortcut.'))
    return out


def dispatch(request):
    import psutil
    try:
        request = dict(request, keys=combination(request['keys']))
        process = psutil.Process(request['pid'])
        names = ('1cv8', '1cv8c') if sys.platform == 'linux' else ('1cv8.exe', '1cv8c.exe')
        if process.create_time() != request['created'] or process.name().lower() not in names:
            raise KeyboardError('keyboard_client_changed', 'The connected client process changed.')
        if sys.platform == 'win32':
            from _keyboard_windows import send
        else:
            from _keyboard_linux import send
        send(request)
        return {'ok': True}
    except KeyboardError as exc:
        return dict(ok=False, code=exc.code, error=str(exc))
    except Exception:
        return dict(ok=False, code='keyboard_desktop_unavailable',
                    error='The client window or desktop is unavailable for the shortcut.')


if __name__ == '__main__':
    sys.modules['_keyboard'] = sys.modules[__name__]
    print(json.dumps(dispatch(json.loads(sys.argv[1]))))
