"""Addressed local shortcuts. The caller verifies the application's resulting state."""
import json
import subprocess
import sys
import time
from pathlib import Path

from _screenshots import CaptureError, resolve_process

NAMED_KEYS = (
    'ENTER', 'ESC', 'TAB', 'SPACE', 'BACKSPACE', 'INSERT', 'DELETE',
    'HOME', 'END', 'PAGEUP', 'PAGEDOWN', 'LEFT', 'RIGHT', 'UP', 'DOWN',
    'BACKQUOTE', 'MINUS', 'EQUALS', 'LBRACKET', 'RBRACKET', 'BACKSLASH',
    'SEMICOLON', 'QUOTE', 'COMMA', 'PERIOD', 'SLASH',
    'NUMPAD_ADD', 'NUMPAD_SUBTRACT', 'NUMPAD_MULTIPLY', 'NUMPAD_DIVIDE',
    'NUMPAD_DECIMAL', 'NUMPAD_ENTER',
)
SUPPORTED_KEYS = tuple('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') + tuple(
    f'F{i}' for i in range(1, 13)) + NAMED_KEYS + tuple(f'NUMPAD_{i}' for i in range(10))


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
    if (keys[-1] not in SUPPORTED_KEYS or any(k not in ('CTRL', 'SHIFT', 'ALT') for k in keys[:-1])
            or len(set(keys)) != len(keys)):
        raise KeyboardError('invalid_shortcut', 'Unsupported key or duplicate modifier.')
    return keys


def _local_process(client):
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
    return pid, created


def activate_target(runtime, client, key, handle=None):
    """Activate an addressed window/control and verify actual, not remembered, focus."""
    _local_process(client)  # Reject remote clients before changing their UI.
    cls = runtime._key_class(key)
    windows = ('MainFrame', 'SecondaryFrame', 'HomePage')
    if cls not in (*windows, 'ManagedForm', 'EditField', 'Table'):
        raise KeyboardError('keyboard_target_unsupported',
                            'Address a window, managed form, input field or table.')
    ancestors = []
    parent = runtime._collection_parent(key)
    while parent:
        ancestors.append(parent)
        parent = runtime._collection_parent(parent)
    if any(runtime._key_class(p) == 'Table' for p in ancestors):
        raise KeyboardError('keyboard_target_unsupported',
                            'Address the table itself; activate its column separately before sending keys.')
    target = runtime._ref_live_object(client, key)
    if target is None or (handle is not None and target.get('handle') != handle):
        raise KeyboardError('keyboard_target_unavailable', 'The addressed object is no longer available. Find it again.')
    window_key = key if cls in windows else next(
        (p for p in ancestors if runtime._key_class(p) in windows), None)
    form_key = key if cls == 'ManagedForm' else next(
        (p for p in ancestors if runtime._key_class(p) == 'ManagedForm'), None)
    form = runtime._ref_live_object(client, form_key) if form_key else None
    control = cls in ('EditField', 'Table')
    if not window_key or (cls not in windows and (not form or not form.get('handle'))):
        raise KeyboardError('keyboard_target_unavailable', 'The owning form or window is unavailable.')
    guids = (runtime.G.ACTIVATE, runtime.G.FORM_GET_CURRENT_ITEM) if control else (runtime.G.ACTIVATE,)
    if not all(runtime._guid_available(client, g) for g in guids):
        raise KeyboardError('unsupported_platform_version', 'This platform cannot activate and verify the shortcut target.')

    def activate(address, object_handle=None):
        for kind in ('action', 'commit'):
            r = client.send_cmd(runtime.G.ACTIVATE, address, kind=kind, middle=b'', handle=object_handle)
            if not r.get('ok'):
                raise KeyboardError('keyboard_activation_failed', 'The shortcut target could not be activated. No keys were sent.')

    def verify_window():
        active = runtime._window(client)
        if not active.get('ok') or active.get('key') != window_key:
            raise KeyboardError('keyboard_window_not_active',
                                'The addressed window did not become active. No keys were sent; inspect the active window.')

    activate(window_key)
    verify_window()
    if cls == 'ManagedForm':
        activate(key, target['handle'])
        verify_window()
    elif control:
        activate(key, target['handle'])
        verify_window()
        if runtime._input_current(client, form) != key:
            raise KeyboardError('keyboard_focus_not_confirmed',
                                'The addressed control did not receive focus. No keys were sent; inspect the current element.')
        verify_window()  # An inactive form can report its remembered current element.
    return window_key


def send_shortcut(runtime, client, window_key, keys):
    keys = combination(keys)
    pid, created = _local_process(client)
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
