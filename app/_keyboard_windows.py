"""Deliver a shortcut to a verified 1C input queue without switching desktops."""
import ctypes
from ctypes import wintypes
import time

from _keyboard import KeyboardError


def send(request):
    import win32api
    import win32con as C
    import win32gui as gui
    import win32process
    from _native_window import _GUIThreadInfo
    u = ctypes.WinDLL('user32', use_last_error=True)
    u.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUIThreadInfo)]
    u.GetGUIThreadInfo.restype = wintypes.BOOL
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.AttachThreadInput.restype = wintypes.BOOL
    u.GetKeyboardState.argtypes = [ctypes.c_void_p]
    u.SetKeyboardState.argtypes = [ctypes.c_void_p]
    targets = set()
    def inspect(hwnd, _):
        tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid != request['pid'] or not gui.IsWindowVisible(hwnd) or gui.IsIconic(hwnd):
            return
        info = _GUIThreadInfo(cbSize=ctypes.sizeof(_GUIThreadInfo))
        if not u.GetGUIThreadInfo(tid, ctypes.byref(info)) or info.hwndActive != hwnd or not info.hwndFocus:
            return
        if not gui.IsWindowEnabled(hwnd) or win32process.GetWindowThreadProcessId(info.hwndFocus)[1] != pid:
            return
        # Tabbed 1C forms share the native main frame, whose caption is the database name.
        # The caller checked the exact active form key through the testing protocol.
        cls = gui.GetClassName(hwnd)
        main = (cls == 'V8TopLevelFrameSDI' or
                (cls.startswith('V8Window0.') and not gui.GetWindow(hwnd, C.GW_OWNER)
                 and gui.GetWindowLong(hwnd, C.GWL_STYLE) & C.WS_CAPTION == C.WS_CAPTION))
        if not main and (not request['title'] or gui.GetWindowText(hwnd) != request['title']):
            return
        targets.add((tid, hwnd, int(info.hwndFocus)))
    gui.EnumWindows(inspect, None)
    if len(targets) != 1:
        raise KeyboardError('keyboard_window_unavailable', 'Activate the addressed client window; its native window could not be identified uniquely.')
    tid, hwnd, focus = targets.pop()
    if not request.get('isolated') and gui.GetForegroundWindow() != hwnd:
        raise KeyboardError('keyboard_window_not_foreground', 'Bring the addressed 1C window to the foreground and retry.')
    modifiers = {'CTRL': (C.VK_CONTROL, C.VK_LCONTROL), 'SHIFT': (C.VK_SHIFT, C.VK_LSHIFT),
                 'ALT': (C.VK_MENU, C.VK_LMENU)}
    special = {'ENTER': 13, 'ESC': 27, 'TAB': 9, 'SPACE': 32, 'BACKSPACE': 8,
               'INSERT': 45, 'DELETE': 46, 'HOME': 36, 'END': 35, 'PAGEUP': 33,
               'PAGEDOWN': 34, 'LEFT': 37, 'UP': 38, 'RIGHT': 39, 'DOWN': 40,
               'BACKQUOTE': 0xC0, 'MINUS': 0xBD, 'EQUALS': 0xBB, 'LBRACKET': 0xDB,
               'RBRACKET': 0xDD, 'BACKSLASH': 0xDC, 'SEMICOLON': 0xBA,
               'QUOTE': 0xDE, 'COMMA': 0xBC, 'PERIOD': 0xBE, 'SLASH': 0xBF,
               'NUMPAD_ADD': 107, 'NUMPAD_SUBTRACT': 109, 'NUMPAD_MULTIPLY': 106,
               'NUMPAD_DIVIDE': 111, 'NUMPAD_DECIMAL': 110, 'NUMPAD_ENTER': 13,
               **{f'NUMPAD_{i}': 96+i for i in range(10)}}
    key = request['keys'][-1]
    vk = special.get(key) or (C.VK_F1 + int(key[1:]) - 1 if len(key) > 1 and key.startswith('F') else ord(key))
    if not request.get('isolated') and any(u.GetAsyncKeyState(k) & 0x8000 for k in
            (C.VK_CONTROL, C.VK_SHIFT, C.VK_MENU, vk)):
        raise KeyboardError('keyboard_keys_pressed', 'Release the keyboard modifiers and shortcut key before retrying.')
    own_tid = win32api.GetCurrentThreadId()
    gui.PeekMessage(None, 0, 0, C.PM_NOREMOVE)
    if not u.AttachThreadInput(own_tid, tid, True):
        raise KeyboardError('keyboard_desktop_unavailable', 'Cannot access the client input queue.')
    old = (ctypes.c_ubyte * 256)()
    captured = False
    pressed = False
    scan = win32api.MapVirtualKey(vk, 0)
    alt = 'ALT' in request['keys'][:-1]
    # F10 also uses system-key messages, even without Alt (Win32 WM_SYSKEYDOWN).
    system = alt or key == 'F10'
    down = C.WM_SYSKEYDOWN if system else C.WM_KEYDOWN
    up = C.WM_SYSKEYUP if system else C.WM_KEYUP
    extended = key in ('INSERT', 'DELETE', 'HOME', 'END', 'PAGEUP', 'PAGEDOWN',
                       'LEFT', 'RIGHT', 'UP', 'DOWN', 'NUMPAD_DIVIDE', 'NUMPAD_ENTER')
    flags = 1 | (scan << 16) | (int(extended) << 24) | ((1 << 29) if alt else 0)
    try:
        captured = bool(u.GetKeyboardState(old))
        if not captured:
            raise KeyboardError('keyboard_failed', 'Cannot read the client keyboard state.')
        info = _GUIThreadInfo(cbSize=ctypes.sizeof(_GUIThreadInfo))
        if not u.GetGUIThreadInfo(tid, ctypes.byref(info)) or info.hwndActive != hwnd or info.hwndFocus != focus:
            raise KeyboardError('keyboard_window_changed', 'The client focus changed before the shortcut was sent.')
        state = (ctypes.c_ubyte * 256).from_buffer_copy(old)
        for modifier in request['keys'][:-1]:
            for code in modifiers[modifier]:
                state[code] = 0x80
        if not u.SetKeyboardState(state):
            raise KeyboardError('keyboard_failed', 'Cannot set the client keyboard state.')
        gui.PostMessage(focus, down, vk, flags)
        pressed = True
        time.sleep(.25)
    finally:
        try:
            if pressed:
                gui.PostMessage(focus, up, vk, flags | (3 << 30))
                time.sleep(.25)
        finally:
            if captured:
                u.SetKeyboardState(old)
            u.AttachThreadInput(own_tid, tid, False)
