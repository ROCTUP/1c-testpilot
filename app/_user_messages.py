"""Read and reopen a window's message panel, preserving its initial open state."""
import time

import guids as G
import tc1c
import _keyboard
from tool_versions import TOOL_MIN_VERSION


def read(client, key):
    try:
        response = client.send_cmd(G.GET_USER_MESSAGE_TEXTS, key, kind='read', middle=tc1c.RES_COLLECTION)
    except tc1c.OperationError as exc:
        if exc.status != 17:
            raise
        return dict(exc.result(), code='user_messages_unavailable', messages=None,
                    error='The user-message panel is closed or unavailable in this window.')
    messages = tc1c.decode_user_messages(response['raw']) if response.get('ok') else None
    if messages is None:
        return dict(ok=False, code='user_messages_unavailable', messages=None,
                    error='The message list could not be decoded; this does not establish that it is empty.')
    return dict(ok=True, messages=messages)


def panel(runtime, client, key, *, keep_open=False):
    initial = read(client, key)
    if initial.get('ok'):
        return dict(ok=True, panel_open=True, opened=False) if keep_open else initial
    if initial.get('status_code') != 17:
        return initial  # A malformed response is not evidence of a closed panel.
    if not keep_open:
        minimum = TOOL_MIN_VERSION['tc_close_user_messages_panel']
        version = runtime._conn_ver(client)
        if runtime._vt(version) and runtime._vt(version) < runtime._vt(minimum):
            return dict(initial, reason='unsupported_platform_version',
                        available_since=minimum, connected_version=version,
                        message='Automatic opening requires platform %s+ to restore the closed panel. '
                                'Open the panel manually, then retry reading.' % minimum)
    previous = getattr(client, '_io_deadline', None)
    deadline = time.monotonic() + 10
    client._io_deadline = min(previous, deadline) if previous is not None else deadline
    try:
        try:
            _keyboard.send_shortcut(runtime, client, key, ('CTRL', 'SHIFT', 'Z'))
        except _keyboard.KeyboardError as exc:
            return dict(initial, reason=exc.code, error=str(exc),
                        message='Open the panel in the addressed 1C window with Ctrl+Shift+Z, then retry. '
                                'Unreadable messages do not mean that the action had no validation errors.')
        # Only read while waiting. A second shortcut could close a panel that opened late.
        until = min(client._io_deadline, time.monotonic() + 2)
        while True:
            current = runtime._window(client)
            if current.get('key') != key:
                return dict(initial, reason='keyboard_window_changed',
                            error='The active window changed while opening the message panel.', panel_open=None)
            result = read(client, key)
            if result.get('ok'):
                break
            if result.get('status_code') != 17 or time.monotonic() >= until:
                failure = dict(result, reason='message_panel_not_opened', panel_open=None,
                               error='The panel did not become readable after the shortcut; its state is unknown.')
                if result.get('status_code') == 17:
                    failure['message'] = ('The window may not have produced any user messages yet, '
                                          'so there may be no panel to open. '
                                          'The absence of messages could not be confirmed.')
                return failure
            time.sleep(.05)
        if keep_open:
            return dict(ok=True, panel_open=True, opened=True)
        # Closing is addressed through the protocol, not another keyboard toggle.
        try:
            for kind in ('action', 'commit'):
                closed = client.send_cmd(G.CLOSE_USER_MESSAGES_PANEL, key, kind=kind, middle=b'')
                if not closed.get('ok'):
                    raise RuntimeError('The client refused to close the message panel.')
            check = read(client, key)
            if check.get('status_code') != 17:
                raise RuntimeError('The message panel could not be confirmed closed.')
        except (OSError, RuntimeError, ValueError) as exc:
            return dict(result, ok=False, code='message_panel_restore_failed', panel_restored=False,
                        error='Messages were read, but the panel could not be restored to its closed state: ' + str(exc))
        return dict(result, panel_restored=True)
    finally:
        client._io_deadline = previous
