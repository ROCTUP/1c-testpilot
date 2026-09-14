"""X11/XWayland window capture. Runs only in the bounded screenshot child process."""
import base64
import json
import os
import re
import sys
import time

from _screenshots import CaptureError, validate_options
from _screenshot_image import MAX_PIXELS, transform

MAX_WINDOWS = 2048
SETTLE_TIMEOUT = 2.5


def select_windows(windows, focus=None):
    """Choose one main window/dialog and its popup descendants, in stacking order."""
    visible = [w for w in windows if w['mapped'] and not w['hidden']]
    mains = [w for w in visible if not w['override']]
    by_id = {w['id']: w for w in visible}
    anchor = by_id.get(focus)
    seen = set()
    while anchor and anchor['override'] and anchor['id'] not in seen:
        seen.add(anchor['id'])
        anchor = by_id.get(anchor['transient'])
    if not anchor or anchor['override']:
        # A preview/dialog can be above its parent while another application has focus.
        parents = {w['transient'] for w in mains}
        leaves = [w for w in mains if w['id'] not in parents]
        if len(leaves) != 1:
            code = ('screenshot_window_minimized' if not mains and any(w['hidden'] for w in windows)
                    else 'screenshot_window_ambiguous' if mains else 'screenshot_window_unavailable')
            message = {
                'screenshot_window_minimized': 'The client window is minimized or hidden. Restore it before taking a screenshot.',
                'screenshot_window_ambiguous': 'Several client windows are open. Activate the required window and retry.',
                'screenshot_window_unavailable': 'No visible window for this client was found. Check its desktop session and the server DISPLAY/XAUTHORITY.',
            }[code]
            raise CaptureError(code, message)
        anchor = leaves[0]
    selected = [anchor]
    for w in visible:
        if not w['override'] or w['z'] < anchor['z']:
            continue
        owner = w['transient']
        visited = set()
        while owner in by_id and by_id[owner]['override'] and owner not in visited:
            visited.add(owner)
            owner = by_id[owner]['transient']
        # Some toolkit popups lack WM_TRANSIENT_FOR; only accept them with one main.
        if owner == anchor['id'] or (not owner and len(mains) == 1):
            selected.append(w)
    return sorted(selected, key=lambda w: w['z'])


def signature(windows):
    return [(w['id'], tuple(w['rect']), w['hidden'], w['mapped'], w['transient'], w['override'],
             w.get('visual'), w.get('depth'), w.get('title')) for w in windows]


def almost_same(previous, current):
    """Allow only a tiny changing area such as a blinking caret."""
    from PIL import ImageChops
    if previous.size != current.size:
        return False
    box = ImageChops.difference(previous, current).getbbox()
    return box is None or (box[2] - box[0]) * (box[3] - box[1]) <= 64


class Desktop:
    def __init__(self, pid):
        from Xlib import X, display
        from Xlib.ext import res
        self.X, self.res, self.pid = X, res, pid
        try:
            self.d = display.Display()
        except Exception as exc:
            raise CaptureError('screenshot_desktop_unavailable', 'Cannot access the client desktop. Set DISPLAY and XAUTHORITY for its graphical session.') from exc
        self.root = self.d.screen().root
        if not self.d.has_extension('Composite') or not self.d.has_extension('X-Resource'):
            raise CaptureError('screenshot_capture_unavailable', 'This display does not support identifying and capturing individual client windows.')
        v = self.d.composite_query_version()
        r = self.d.res_query_version()
        if (v.major_version, v.minor_version) < (0, 2) or (r.server_major, r.server_minor) < (1, 2):
            raise CaptureError('screenshot_capture_unavailable', 'The display extensions are too old for individual client window capture.')
        self.atoms = {}
        self.visuals = {v.visual_id: v for depth in self.d.screen().allowed_depths for v in depth.visuals}

    def prop(self, window, name):
        atom = self.atoms.setdefault(name, self.d.intern_atom(name))
        p = window.get_full_property(atom, self.X.AnyPropertyType)
        return None if p is None else p.value

    def snapshot(self):
        from Xlib.error import BadWindow
        found, scanned = [], 0
        # Recurse through window-manager frames, but not through a 1C window's controls.
        def walk(parent, depth=0):
            nonlocal scanned
            if depth > 16:
                raise CaptureError('screenshot_window_unavailable', 'The desktop window hierarchy is too deep to inspect.')
            for w in parent.query_tree().children:
                scanned += 1
                if scanned > MAX_WINDOWS:
                    raise CaptureError('screenshot_window_unavailable', 'Too many desktop windows to identify the client reliably.')
                try:
                    ids = self.d.res_query_client_ids([dict(client=w.id, mask=self.res.LocalClientPIDMask)]).ids
                    ours = any(list(i.value) == [self.pid] for i in ids)
                    a = w.get_attributes()
                    if not ours:
                        if a.map_state == self.X.IsViewable:
                            walk(w, depth + 1)
                        continue
                    g = w.get_geometry()
                    if g.width < 16 or g.height < 16 or g.depth == 0:
                        continue
                    xy = self.root.translate_coords(w, 0, 0)
                    wm = self.prop(w, 'WM_STATE')
                    states = self.prop(w, '_NET_WM_STATE')
                    hidden_atom = self.d.intern_atom('_NET_WM_STATE_HIDDEN')
                    transient = self.prop(w, 'WM_TRANSIENT_FOR')
                    title = self.prop(w, '_NET_WM_NAME')
                    if title is None: title = w.get_wm_name() or ''
                    if isinstance(title, bytes): title = title.decode('utf8', errors='replace')
                    found.append(dict(id=w.id, z=scanned, rect=[xy.x, xy.y, g.width, g.height],
                        depth=g.depth, visual=a.visual, override=bool(a.override_redirect),
                        mapped=a.map_state == self.X.IsViewable,
                        hidden=bool((wm is not None and len(wm) and wm[0] == 3)
                                    or (states is not None and hidden_atom in states)),
                        transient=int(transient[0]) if transient is not None and len(transient) else None,
                        title=str(title)))
                except BadWindow:
                    continue
        walk(self.root)
        focus = self.d.get_input_focus().focus
        focus_id = getattr(focus, 'id', None)
        by_id = {w['id'] for w in found}
        for _ in range(32):
            if focus_id in by_id or focus_id in (None, self.root.id): break
            try:
                focus = self.d.create_resource_object('window', focus_id).query_tree().parent
                focus_id = focus.id
            except BadWindow:
                focus_id = None
                break
        return select_windows(found, focus_id)

    def pixels(self, item):
        from PIL import Image
        visual = self.visuals.get(item['visual'])
        if visual is None or (visual.red_mask, visual.green_mask, visual.blue_mask) != (0xff0000, 0xff00, 0xff):
            raise CaptureError('screenshot_pixel_format_unsupported', 'The client display pixel format is unsupported.')
        w = self.d.create_resource_object('window', item['id'])
        pixmap = w.composite_name_window_pixmap()
        try:
            g = pixmap.get_geometry()
            if [g.width, g.height] != item['rect'][2:]:
                raise CaptureError('screenshot_window_changed', 'The client window changed size during capture. Retry the screenshot.')
            if g.width * g.height > MAX_PIXELS:
                raise CaptureError('screenshot_too_large', 'The client window is too large to capture.')
            fmt = next(f for f in self.d.display.info.pixmap_formats if f.depth == g.depth)
            if g.depth not in (24, 32) or fmt.bits_per_pixel not in (24, 32):
                raise CaptureError('screenshot_pixel_format_unsupported', 'The client display pixel format is unsupported.')
            r = pixmap.get_image(0, 0, g.width, g.height, self.X.ZPixmap, 0xffffffff)
            stride = ((g.width * fmt.bits_per_pixel + fmt.scanline_pad - 1) // fmt.scanline_pad) * fmt.scanline_pad // 8
            little = self.d.display.info.image_byte_order == self.X.LSBFirst
            if g.depth == 32:
                im = Image.frombytes('RGBA', (g.width, g.height), r.data, 'raw', 'BGRA' if little else 'ARGB', stride)
                # XRender buffers contain premultiplied alpha.
                return Image.frombytes('RGBa', im.size, im.tobytes()).convert('RGBA')
            mode = ('BGRX' if little else 'XRGB') if fmt.bits_per_pixel == 32 else ('BGR' if little else 'RGB')
            return Image.frombytes('RGB', (g.width, g.height), r.data, 'raw', mode, stride)
        finally:
            pixmap.free()

    def render(self, windows):
        from PIL import Image
        left = min(w['rect'][0] for w in windows)
        top = min(w['rect'][1] for w in windows)
        width = max(w['rect'][0] + w['rect'][2] for w in windows) - left
        height = max(w['rect'][1] + w['rect'][3] for w in windows) - top
        if width * height > MAX_PIXELS:
            raise CaptureError('screenshot_too_large', 'The client windows cover too large an area to capture.')
        canvas = Image.new('RGB', (width, height), '#eeeeee')
        missing = []
        for i, w in enumerate(windows):
            try:
                im = self.pixels(w)
            except Exception as exc:
                if i == 0:
                    if isinstance(exc, CaptureError): raise
                    raise CaptureError('screenshot_window_unavailable', 'The window image is unavailable. Keep the client window open and retry.') from exc
                missing.append(w['id'])
                continue
            canvas.paste(im, (w['rect'][0] - left, w['rect'][1] - top), im if im.mode == 'RGBA' else None)
        return canvas, missing


def capture_linux(request):
    import psutil
    validate_options(request['scale'], request['grid'], request['region'])
    if not os.environ.get('DISPLAY'):
        raise CaptureError('screenshot_desktop_unavailable', 'Set DISPLAY and XAUTHORITY for the client graphical session. Native Wayland capture is not supported.')
    if not re.fullmatch(r'(?:unix/?)?:\d+(?:\.\d+)?', os.environ['DISPLAY']):
        raise CaptureError('screenshot_desktop_unavailable', 'Use the local client DISPLAY (for example :0). Forwarded or remote X displays cannot identify the local client reliably.')
    process = psutil.Process(request['pid'])
    if process.create_time() != request['created'] or process.name() not in ('1cv8', '1cv8c'):
        raise CaptureError('screenshot_client_changed', 'The connected 1C process changed before capture.')
    desktop = Desktop(process.pid)
    try:
        start = time.monotonic()
        # Protocol command completion can precede asynchronous GTK window creation.
        time.sleep(.15)
        previous, previous_sig, stable_since = None, None, time.monotonic()
        while time.monotonic() - start < SETTLE_TIMEOUT:
            windows = desktop.snapshot()
            canvas, missing = desktop.render(windows)
            after = desktop.snapshot()
            sig = (signature(windows), missing)
            unchanged = signature(windows) == signature(after)
            if not unchanged or sig != previous_sig or previous is None or not almost_same(previous, canvas):
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= .2:
                break
            previous, previous_sig = canvas, sig
            time.sleep(.08)
        else:
            raise CaptureError('screenshot_window_changing', 'The client window is still changing. Wait for the current operation to finish and retry.')
        if process.create_time() != request['created'] or not process.is_running():
            raise CaptureError('screenshot_client_changed', 'The connected 1C process changed during capture.')
        png, meta = transform(canvas, request['scale'], request['grid'], request['region'])
        # Check again after PNG encoding; do not return a newly hidden or replaced window.
        if signature(desktop.snapshot()) != signature(windows):
            raise CaptureError('screenshot_window_changed', 'The client window changed during capture. Retry the screenshot.')
        meta.update(ok=True, title=windows[0]['title'], overlays=len(windows)-1,
                    source='xcomposite', capture_complete=not missing)
        if missing:
            meta.update(warning='Some popup windows could not be captured. The image is incomplete.', missing_windows=missing)
        return dict(meta, png=base64.b64encode(png).decode('ascii'))
    finally:
        desktop.d.close()


if __name__ == '__main__':
    try:
        result = capture_linux(json.loads(sys.argv[1]))
    except CaptureError as exc:
        result = dict(ok=False, code=exc.code, error=str(exc))
    except ImportError:
        result = dict(ok=False, code='screenshot_dependency_unavailable', error='Install the project dependencies, including python-xlib and Pillow.')
    except Exception:
        result = dict(ok=False, code='screenshot_failed', error='Linux could not complete screenshot capture.')
    print(json.dumps(result, ensure_ascii=True))
