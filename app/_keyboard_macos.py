"""macOS shortcuts posted to the verified client process, without switching the foreground app."""
import ctypes
import time

from _keyboard import KeyboardError

# ANSI virtual key codes (HIToolbox Events.h). No INSERT: the 1C client ignored the Help key
# that a PC Insert key produces on macOS, with or without its Help/Fn flags.
KEYS = {
    'A': 0, 'S': 1, 'D': 2, 'F': 3, 'H': 4, 'G': 5, 'Z': 6, 'X': 7, 'C': 8, 'V': 9,
    'B': 11, 'Q': 12, 'W': 13, 'E': 14, 'R': 15, 'Y': 16, 'T': 17, '1': 18, '2': 19,
    '3': 20, '4': 21, '6': 22, '5': 23, 'EQUALS': 24, '9': 25, '7': 26, 'MINUS': 27,
    '8': 28, '0': 29, 'RBRACKET': 30, 'O': 31, 'U': 32, 'LBRACKET': 33, 'I': 34, 'P': 35,
    'ENTER': 36, 'L': 37, 'J': 38, 'QUOTE': 39, 'K': 40, 'SEMICOLON': 41, 'BACKSLASH': 42,
    'COMMA': 43, 'SLASH': 44, 'N': 45, 'M': 46, 'PERIOD': 47, 'TAB': 48, 'SPACE': 49,
    'BACKQUOTE': 50, 'BACKSPACE': 51, 'ESC': 53,
    'NUMPAD_DECIMAL': 65, 'NUMPAD_MULTIPLY': 67, 'NUMPAD_ADD': 69, 'NUMPAD_DIVIDE': 75,
    'NUMPAD_ENTER': 76, 'NUMPAD_SUBTRACT': 78, 'NUMPAD_0': 82, 'NUMPAD_1': 83, 'NUMPAD_2': 84,
    'NUMPAD_3': 85, 'NUMPAD_4': 86, 'NUMPAD_5': 87, 'NUMPAD_6': 88, 'NUMPAD_7': 89,
    'NUMPAD_8': 91, 'NUMPAD_9': 92,
    'F5': 96, 'F6': 97, 'F7': 98, 'F3': 99, 'F8': 100, 'F9': 101, 'F11': 103, 'F10': 109,
    'F12': 111, 'HOME': 115, 'PAGEUP': 116, 'DELETE': 117, 'F4': 118,
    'END': 119, 'F2': 120, 'PAGEDOWN': 121, 'F1': 122, 'LEFT': 123, 'RIGHT': 124,
    'DOWN': 125, 'UP': 126,
}
# The macOS 1C client binds its Ctrl shortcuts to Command (Ctrl+Shift+Z opens nothing,
# Command+Shift+Z opens the message panel), and Alt to Option.
MODIFIERS = {'CTRL': 0x100000, 'SHIFT': 0x20000, 'ALT': 0x80000}
HELD = 0x20000 | 0x40000 | 0x80000 | 0x100000  # Shift, Control, Option, Command
PRIVATE_STATE, HID_STATE = -1, 1


def send(request):
    code = KEYS.get(request['keys'][-1])
    if code is None:
        raise KeyboardError('keyboard_key_unavailable', 'The macOS 1C client does not receive this key; use the form command instead.')
    cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    cg.CGPreflightPostEventAccess.argtypes = []
    cg.CGPreflightPostEventAccess.restype = ctypes.c_bool
    cg.CGEventSourceFlagsState.argtypes = [ctypes.c_int32]
    cg.CGEventSourceFlagsState.restype = ctypes.c_uint64
    cg.CGEventSourceCreate.argtypes = [ctypes.c_int32]
    cg.CGEventSourceCreate.restype = ctypes.c_void_p
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventGetFlags.argtypes = [ctypes.c_void_p]
    cg.CGEventGetFlags.restype = ctypes.c_uint64
    cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    cg.CGEventSetFlags.restype = None
    cg.CGEventPostToPid.argtypes = [ctypes.c_int, ctypes.c_void_p]
    cg.CGEventPostToPid.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    # Only check: CGRequestPostEventAccess would open a system prompt from a helper process.
    if not cg.CGPreflightPostEventAccess():
        raise KeyboardError('keyboard_permission_required',
                            'Allow Accessibility for the application running the MCP server '
                            '(System Settings > Privacy & Security > Accessibility). No shortcut was sent.')
    if cg.CGEventSourceFlagsState(HID_STATE) & HELD:
        raise KeyboardError('keyboard_keys_pressed', 'Release the keyboard modifiers before retrying the shortcut.')
    # Events go to the client process, never to whichever app is in front, so the foreground
    # is not required. The caller verified the addressed 1C window through the testing protocol.
    flags = sum(MODIFIERS[k] for k in request['keys'][:-1])
    source = cg.CGEventSourceCreate(PRIVATE_STATE)  # Not combined with physical modifier state.
    if not source:
        raise KeyboardError('keyboard_desktop_unavailable', 'Cannot create a keyboard event source.')

    events = []
    try:
        # Prepare both events before sending: allocation failure must not leave a key down.
        for down in (True, False):
            event = cg.CGEventCreateKeyboardEvent(source, code, down)
            if not event:
                raise KeyboardError('keyboard_failed', 'Cannot create the keyboard event. No shortcut was sent.')
            events.append(event)
            # Keep the keypad/function flags macOS sets for the key; replace only modifiers.
            cg.CGEventSetFlags(event, (cg.CGEventGetFlags(event) & ~HELD) | flags)
        try:
            cg.CGEventPostToPid(request['pid'], events[0])
            time.sleep(.05)
        finally:
            cg.CGEventPostToPid(request['pid'], events[1])
            time.sleep(.05)
    finally:
        for event in reversed(events):
            cf.CFRelease(event)
        cf.CFRelease(source)
