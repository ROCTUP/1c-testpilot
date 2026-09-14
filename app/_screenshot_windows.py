"""Bounded child process for Windows window capture; never activates or restores windows."""
import base64
import ctypes
from ctypes import wintypes
import json
import sys

from _screenshots import CaptureError, validate_options
from _screenshot_image import MAX_PIXELS, transform


def intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def plan_layers(windows, anchor):
    layers = [w for w in windows if w['pid'] == anchor['pid'] and w['visible']
              and w['z'] < anchor['z'] and w['rect'][2] - w['rect'][0] >= 16
              and w['rect'][3] - w['rect'][1] >= 16 and not w['cloaked']]
    # Include all relevant popups, also ones extending beyond the base window.
    layers = [w for w in layers if w['root'] == anchor['root'] or intersects(w['rect'], anchor['rect'])]
    return sorted(layers, key=lambda w: w['z'], reverse=True)


def screen_unobstructed(window, windows, desktop):
    r = window['rect']
    if r[0] < desktop[0] or r[1] < desktop[1] or r[2] > desktop[2] or r[3] > desktop[3]:
        return False
    return not any(w['pid'] != window['pid'] and w['visible'] and not w['cloaked']
                   and w['z'] < window['z'] and intersects(w['rect'], r) for w in windows)


def capture_windows(request):
    import psutil
    import win32gui as gui
    import win32api
    import win32process
    from PIL import Image
    validate_options(request['scale'], request['grid'], request['region'])
    process = psutil.Process(request['pid'])
    if process.create_time() != request['created'] or process.name().lower() not in ('1cv8.exe', '1cv8c.exe'):
        raise CaptureError('screenshot_client_changed', 'The 1C process changed before capture.')
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    desktop_handle = user32.OpenInputDesktop(0, False, 1)
    if not desktop_handle:
        raise CaptureError('screenshot_desktop_unavailable', 'The interactive desktop is unavailable. Unlock the client desktop session.')
    user32.CloseDesktop(desktop_handle)
    # All rectangles and captured pixels use the same physical coordinate system.
    user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    user32.SetProcessDpiAwarenessContext(wintypes.HANDLE(-4))
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi = ctypes.WinDLL('gdi32', use_last_error=True)
    for name, args, result in [
        ('CreateCompatibleDC', [wintypes.HDC], wintypes.HDC),
        ('SelectObject', [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
        ('DeleteObject', [wintypes.HANDLE], wintypes.BOOL),
        ('DeleteDC', [wintypes.HDC], wintypes.BOOL),
        ('CreateDIBSection', [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                              ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD], wintypes.HANDLE),
        ('BitBlt', [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD], wintypes.BOOL),
    ]:
        fn = getattr(gdi, name); fn.argtypes = args; fn.restype = result
    class BitmapInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('width', wintypes.LONG), ('height', wintypes.LONG),
                    ('planes', wintypes.WORD), ('bits', wintypes.WORD), ('compression', wintypes.DWORD),
                    ('image_size', wintypes.DWORD), ('xppm', wintypes.LONG), ('yppm', wintypes.LONG),
                    ('used', wintypes.DWORD), ('important', wintypes.DWORD)]
    dwm = ctypes.WinDLL('dwmapi')
    dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    windows = []
    def collect(hwnd, _):
        tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        cloaked = wintypes.DWORD()
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        windows.append(dict(hwnd=hwnd, tid=tid, pid=pid, z=len(windows), rect=gui.GetWindowRect(hwnd),
                            visible=bool(gui.IsWindowVisible(hwnd)), cloaked=bool(cloaked.value),
                            root=gui.GetAncestor(hwnd, 3)))
    gui.EnumWindows(collect, None)
    own = [w for w in windows if w['pid'] == request['pid'] and w['visible'] and not w['cloaked']]
    if not own:
        raise CaptureError('screenshot_window_unavailable', 'No visible 1C window is available in this desktop session.')
    class ThreadInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD),
                    ('active', wintypes.HWND), ('focus', wintypes.HWND), ('capture', wintypes.HWND),
                    ('menu', wintypes.HWND), ('move', wintypes.HWND), ('caret', wintypes.HWND), ('rect', wintypes.RECT)]
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(ThreadInfo)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    active = set()
    fg = gui.GetForegroundWindow()
    for w in own:
        info = ThreadInfo(cbSize=ctypes.sizeof(ThreadInfo))
        if user32.GetGUIThreadInfo(w['tid'], ctypes.byref(info)) and info.active:
            root = gui.GetAncestor(info.active, 3)
            if any(o['hwnd'] == root for o in own):
                active.add(root)
    foreground = next((w for w in own if w['hwnd'] == fg), None)
    if foreground:
        active = {foreground['root']}
    candidates = [w for w in own if w['hwnd'] in active]
    if len(candidates) != 1:
        candidates = [w for w in own if w['hwnd'] == w['root']
                      and gui.GetWindowLong(w['hwnd'], -16) & 0x00c00000 == 0x00c00000]
    if len(candidates) != 1:
        raise CaptureError('screenshot_window_ambiguous', 'The active 1C window could not be identified. Select the intended window in 1C.')
    anchor = candidates[0]
    if gui.IsIconic(anchor['hwnd']):
        raise CaptureError('screenshot_window_minimized', 'The 1C window is minimized. Restore it before taking a screenshot.')
    layers = plan_layers(windows, anchor)
    rects = [anchor['rect']] + [w['rect'] for w in layers]
    frame = (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))
    width, height = frame[2] - frame[0], frame[3] - frame[1]
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise CaptureError('screenshot_too_large', 'The 1C windows exceed the capture size limit. Move them closer together or reduce their size.')
    desktop = (win32api.GetSystemMetrics(76), win32api.GetSystemMetrics(77),
               win32api.GetSystemMetrics(76) + win32api.GetSystemMetrics(78), win32api.GetSystemMetrics(77) + win32api.GetSystemMetrics(79))
    def read_window(w):
        rect = w['rect']
        rw, rh = rect[2] - rect[0], rect[3] - rect[1]
        if rw * rh > MAX_PIXELS:
            return None
        screen = user32.GetDC(None)
        dst = gdi.CreateCompatibleDC(screen)
        bitmap, old = None, None
        try:
            if not screen or not dst:
                return None
            bits = ctypes.c_void_p()
            info = BitmapInfo(size=ctypes.sizeof(BitmapInfo), width=rw, height=-rh, planes=1, bits=32)
            bitmap = gdi.CreateDIBSection(screen, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
            if not bitmap or not bits.value:
                return None
            old = gdi.SelectObject(dst, bitmap)
            if not old or old == ctypes.c_void_p(-1).value:
                return None
            # Prefer actual visible pixels: this also includes GPU-rendered choice lists.
            if screen_unobstructed(w, windows, desktop):
                if not gdi.BitBlt(dst, 0, 0, rw, rh, screen, rect[0], rect[1], 0x00CC0020 | 0x40000000):
                    return None
                source = 'screen'
            elif user32.PrintWindow(w['hwnd'], dst, 2):
                source = 'window'
            else:
                return None
            img = Image.frombytes('RGB', (rw, rh), ctypes.string_at(bits, rw * rh * 4), 'raw', 'BGRX', 0, 1)
            if all(lo == hi for lo, hi in img.getextrema()):
                return None
            return img, source
        finally:
            if old and old != ctypes.c_void_p(-1).value:
                gdi.SelectObject(dst, old)
            if bitmap: gdi.DeleteObject(bitmap)
            if dst: gdi.DeleteDC(dst)
            if screen: user32.ReleaseDC(None, screen)
    canvas = Image.new('RGB', (width, height), '#404040')
    missing, sources = [], set()
    for w in [anchor] + layers:
        if not gui.IsWindow(w['hwnd']) or not gui.IsWindowVisible(w['hwnd']) or gui.GetWindowRect(w['hwnd']) != w['rect']:
            raise CaptureError('screenshot_window_changed', 'A 1C window moved or closed during capture. Take another screenshot.')
        captured = read_window(w)
        if captured is None:
            if w is anchor:
                raise CaptureError('screenshot_unavailable', 'Windows could not provide the 1C window image. Check its desktop session.')
            missing.append(gui.GetWindowText(w['hwnd']) or 'Popup')
            continue
        img, source = captured
        sources.add(source)
        canvas.paste(img, (w['rect'][0] - frame[0], w['rect'][1] - frame[1]))
    # A moved foreground window can invalidate the screen-source occlusion check.
    before = [(w['hwnd'], w['rect'], w['visible'], w['cloaked']) for w in windows if w['visible']]
    windows.clear()
    gui.EnumWindows(collect, None)
    after = [(w['hwnd'], w['rect'], w['visible'], w['cloaked']) for w in windows if w['visible']]
    if before != after:
        raise CaptureError('screenshot_window_changed', 'Windows changed during capture. Take another screenshot.')
    if process.create_time() != request['created']:
        raise CaptureError('screenshot_client_changed', 'The 1C process changed during capture.')
    png, metadata = transform(canvas, request['scale'], request['grid'], request['region'])
    metadata.update(ok=True, capture_complete=not missing, title=gui.GetWindowText(anchor['hwnd']),
                    overlays=len(layers), source='screen' if sources == {'screen'} else 'composite')
    if missing:
        metadata['warning'] = 'Some popup windows could not be captured. The image is incomplete.'
        metadata['missing_windows'] = missing
    return dict(metadata, png=base64.b64encode(png).decode('ascii'))


if __name__ == '__main__':
    try:
        result = capture_windows(json.loads(sys.argv[1]))
    except CaptureError as exc:
        result = dict(ok=False, code=exc.code, error=str(exc))
    except Exception:
        result = dict(ok=False, code='screenshot_failed', error='Windows could not complete screenshot capture.')
    print(json.dumps(result, ensure_ascii=True))
