"""X11 shortcuts on the verified client focus, with balanced modifier events."""
import os
import re

from _keyboard import KeyboardError


def send(request):
    from Xlib import X, XK, display
    from Xlib.ext import res, xtest
    if not re.fullmatch(r'(?:unix/?)?:\d+(?:\.\d+)?', os.environ.get('DISPLAY', '')):
        raise KeyboardError('keyboard_desktop_unavailable', 'Set DISPLAY and XAUTHORITY for the local client X11 session.')
    d = display.Display()
    try:
        if not d.has_extension('X-Resource'):
            raise KeyboardError('keyboard_desktop_unavailable', 'X11 cannot verify the owner of the focused window.')
        version = d.res_query_version()
        if (version.server_major, version.server_minor) < (1, 2):
            raise KeyboardError('keyboard_desktop_unavailable', 'X-Resource 1.2 is required to verify the client window.')
        root = d.screen().root
        focus = d.get_input_focus().focus
        if not getattr(focus, 'id', None) or focus.id == root.id:
            raise KeyboardError('keyboard_window_not_foreground', 'Focus the addressed 1C window and retry.')
        ids = d.res_query_client_ids([dict(client=focus.id, mask=res.LocalClientPIDMask)]).ids
        if not any(list(i.value) == [request['pid']] for i in ids):
            raise KeyboardError('keyboard_window_not_foreground', 'The focused X11 window does not belong to this client.')
        window = focus
        title = None
        for _ in range(32):
            value = window.get_full_property(d.intern_atom('_NET_WM_NAME'), X.AnyPropertyType)
            title = value.value if value is not None else window.get_wm_name()
            if title:
                break
            window = window.query_tree().parent
            if window.id == root.id:
                break
        if isinstance(title, bytes):
            title = title.decode('utf8', errors='replace')
        # A form tab lives in the main native window; it does not have its own OS caption.
        transient = window.get_full_property(d.intern_atom('WM_TRANSIENT_FOR'), X.AnyPropertyType)
        attributes = window.get_attributes()
        main = transient is None and not attributes.override_redirect
        if not main and (not request['title'] or title != request['title']):
            raise KeyboardError('keyboard_window_changed', 'The focused window is not the addressed 1C window.')
        if attributes.map_state != X.IsViewable:
            raise KeyboardError('keyboard_window_unavailable', 'The client window is not visible.')
        names = {'ENTER': 'Return', 'ESC': 'Escape', 'TAB': 'Tab', 'SPACE': 'space'}
        key = request['keys'][-1]
        keysym = XK.string_to_keysym(names.get(key, key.lower() if len(key) == 1 else key))
        code = d.keysym_to_keycode(keysym)
        if not code:
            raise KeyboardError('keyboard_key_unavailable', 'The shortcut key is unavailable in the X11 keyboard map.')
        if not d.has_extension('XTEST'):
            raise KeyboardError('keyboard_desktop_unavailable', 'XTest is required to send shortcuts to this client.')
        modifiers = {'CTRL': 'Control_L', 'SHIFT': 'Shift_L', 'ALT': 'Alt_L'}
        codes = [d.keysym_to_keycode(XK.string_to_keysym(modifiers[k])) for k in request['keys'][:-1]] + [code]
        if not all(codes):
            raise KeyboardError('keyboard_key_unavailable', 'A shortcut modifier is absent from the X11 keyboard map.')
        # Keep the WM/other X clients from changing focus between the check and injection.
        # No sleeping or waiting for 1C while the server is grabbed.
        d.grab_server()
        try:
            if d.get_input_focus().focus != focus:
                raise KeyboardError('keyboard_window_changed', 'The focus changed before the shortcut was sent.')
            if any(d.query_keymap()):
                raise KeyboardError('keyboard_keys_pressed', 'Release the keyboard keys before retrying the shortcut.')
            pressed = []
            try:
                for code in codes:
                    xtest.fake_input(d, X.KeyPress, code)
                    pressed.append(code)
            finally:
                for code in reversed(pressed):
                    xtest.fake_input(d, X.KeyRelease, code)
                d.sync()
        finally:
            d.ungrab_server()
            d.sync()
    finally:
        d.close()
