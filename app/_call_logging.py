"""Incremental MCP call journals. No protocol commands and no replayable scenarios."""
from contextvars import ContextVar
from datetime import datetime
import html
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid


ACTIONS = frozenset({'start_logging', 'stop_logging', 'get_logging_status'})
MODES = ('off', 'actions', 'all')
CURRENT = ContextVar('testpilot_logged_call', default=None)
_SECRET_KEYS = {'password', 'pwd', 'secret', 'token', 'authorization', 'auth'}


def enabled(name, default='true'):
    return os.environ.get(name, default).strip().lower() not in ('false', '0', 'no', 'off')


def now():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def clean(value):
    if isinstance(value, dict):
        return {str(k): ('***' if str(k).lower() in _SECRET_KEYS or 'password' in str(k).lower()
                         else clean(v)) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    if isinstance(value, bytes):
        return {'binary_bytes': len(value)}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def encode(value):
    return (json.dumps(clean(value), ensure_ascii=False) + '\n').encode('utf-8')


def observe(result, picture=None):
    call = CURRENT.get()
    if call is not None:
        call['result'] = clean(result)
        call['picture'] = picture


_HTML = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>1C Testpilot — журнал</title>
<style>body{font:16px system-ui;margin:24px auto;max-width:1100px;padding:0 16px;color:#182332}
header{position:sticky;top:0;z-index:2;background:white;padding:20px 0 16px;border-bottom:1px solid #dce2ea}
header h1{font-size:26px;line-height:1.3;margin:0 0 18px;letter-spacing:-.4px}
.toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:12px}
.fold-controls{display:flex;align-items:center;gap:8px}
.fold-controls button{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;padding:10px;color:#526071;border:1px solid #cbd5e1;border-radius:8px;background:#f8fafc;cursor:pointer}
.fold-controls button:hover{background:#edf2f7;color:#182332}.fold-controls button:focus-visible{outline:2px solid #2563eb;outline-offset:2px}
.fold-controls svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.search-box{display:flex;align-items:center;flex:1 1 280px;gap:10px;min-width:0;padding:0 12px;border:1px solid #cbd5e1;border-radius:8px;background:#f8fafc}
.search-box svg{width:18px;height:18px;flex:none;color:#64748b}
.search-box input{width:100%;min-width:0;height:42px;border:0;outline:0;background:transparent;font:inherit;color:inherit}
.search-box input::placeholder{color:#64748b}.search-box:focus-within{border-color:#2563eb;box-shadow:0 0 0 3px #2563eb20}
.error-filter{display:flex;align-items:center;gap:9px;min-height:42px;padding:0 12px;border:1px solid #dce2ea;border-radius:8px;cursor:pointer;white-space:nowrap}
.error-filter input{width:16px;height:16px;margin:0;accent-color:#b42318}.error-filter:has(input:checked){background:#fff1f0;border-color:#f0aaa3;color:#9d271c}
.refresh{font:inherit;font-weight:600;color:#334155;background:#f8fafc;border:1px solid #cbd5e1;border-radius:8px;padding:10px 16px;cursor:pointer}
.refresh:hover{background:#edf2f7}.refresh:focus-visible,.error-filter:focus-within{outline:2px solid #2563eb;outline-offset:2px}
.header-note{display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px 20px;margin-top:12px;font-size:13px;color:#64748b}
@media(max-width:600px){header h1{font-size:22px}.search-box{flex-basis:100%}.error-filter{flex:1}.header-note{flex-direction:column}}
article{border:1px solid #ccd3dd;border-radius:8px;padding:16px;margin:16px 0}
article.failed{border-left:5px solid #b42318}summary{cursor:pointer}
.call-title{font-size:18px}.call-meta{display:block;margin-top:10px}.call-body{margin-top:12px}
pre{white-space:pre-wrap;word-break:normal;overflow-wrap:anywhere;font-size:13px}
.json-line{display:block}
mark.search-hit{background:#fde68a;color:#182332;border-radius:2px}
img{max-width:100%;max-height:420px}small{color:#526071}.notice{color:#9d271c}
.screenshot-link{cursor:zoom-in}
#image-viewer{width:94vw;height:92vh;max-width:none;max-height:none;padding:0;border:1px solid #475569;border-radius:12px;background:#111827;color:#f1f5f9;overflow:hidden}
#image-viewer::backdrop{background:#0f172acc;backdrop-filter:blur(3px)}
.viewer-shell{display:flex;flex-direction:column;width:100%;height:100%;background:#111827}
.viewer-shell:fullscreen{width:100vw;height:100vh}
.viewer-bar{display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:12px 16px;border-bottom:1px solid #334155}
#image-title{flex:1;min-width:120px;font-size:15px;overflow-wrap:anywhere}
.viewer-bar button{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;flex:none;color:#cbd5e1;border:0;border-radius:6px;background:transparent;padding:7px;cursor:pointer}
.viewer-bar button svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
#image-fullscreen .icon-collapse{display:none}#image-fullscreen[aria-pressed="true"] .icon-expand{display:none}#image-fullscreen[aria-pressed="true"] .icon-collapse{display:block}
.viewer-bar button:hover:not(:disabled){background:#334155}.viewer-bar button:focus-visible{outline:2px solid #93c5fd;outline-offset:2px}
.viewer-bar button:disabled{opacity:.3;cursor:default}
#image-position{min-width:5ch;text-align:center;color:#cbd5e1;font-size:13px;font-variant-numeric:tabular-nums}
.viewer-stage{position:relative;flex:1;min-height:0;padding:12px;overflow:auto}
.image-nav{position:absolute;top:50%;transform:translateY(-50%);display:flex;align-items:center;justify-content:center;width:52px;height:52px;padding:8px;border:1px solid #ffffff50;border-radius:50%;background:#0f172acc;color:white;box-shadow:0 2px 12px #0006;cursor:pointer}
.image-nav svg{width:32px;height:32px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.image-nav:hover:not(:disabled){background:#0f172a}.image-nav:focus-visible{outline:3px solid #93c5fd;outline-offset:3px}
.image-nav:disabled{opacity:.3;cursor:default}
#image-previous{left:24px}#image-next{right:24px}
@media(max-width:600px){.image-nav{width:44px;height:44px}#image-previous{left:16px}#image-next{right:16px}}
#viewer-image{display:block;width:100%;height:100%;max-width:none;max-height:none;object-fit:contain}
#image-viewer.expanded{width:100vw;height:100vh;margin:0;border:0;border-radius:0}
#viewer-note{margin:0;padding:8px 16px;color:#cbd5e1;font-size:13px}
</style>
<header><h1>1C Testpilot — журнал вызовов</h1>
<div class="toolbar">
<label class="search-box">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>
<input id="search" type="search" aria-label="Поиск по действию или тексту" placeholder="Поиск по действию или тексту" oninput="filter()">
</label>
<div class="fold-controls" role="group" aria-label="Отображение блоков">
<button type="button" id="collapse-all" onclick="setAllExpanded(false)" title="Свернуть всё" aria-label="Свернуть всё"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 11 6-6 6 6m-12 8 6-6 6 6"/></svg></button>
<button type="button" id="expand-all" onclick="setAllExpanded(true)" title="Развернуть всё" aria-label="Развернуть всё"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 5 6 6 6-6m-12 8 6 6 6-6"/></svg></button>
</div>
<label class="error-filter"><input id="errors" type="checkbox" onchange="filter()"> Только ошибки</label>
<button class="refresh" type="button" onclick="location.reload()">Обновить</button>
</div>
<div class="header-note"><span>Обновите отчёт, чтобы увидеть новые вызовы.</span><span>Время — в часовом поясе MCP-сервера.</span></div></header>
<dialog id="image-viewer" aria-labelledby="image-title">
<div class="viewer-shell" id="viewer-shell">
<div class="viewer-bar"><strong id="image-title" aria-live="polite">Снимок экрана</strong>
<span id="image-position"></span>
<button type="button" id="image-fullscreen" onclick="toggleImageFullscreen()" title="Во весь экран" aria-label="Во весь экран" aria-pressed="false">
<svg class="icon-expand" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/></svg>
<svg class="icon-collapse" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 8h5V3m8 0v5h5M8 21v-5H3m18 0h-5v5"/></svg></button>
<button type="button" onclick="closeImageViewer()" autofocus title="Закрыть" aria-label="Закрыть просмотр изображения"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div>
<div class="viewer-stage"><img id="viewer-image" alt="Снимок окна 1С">
<button type="button" class="image-nav" id="image-previous" onclick="moveImage(-1)" title="Предыдущий снимок (←)" aria-label="Предыдущий снимок"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 6-6 6 6 6"/></svg></button>
<button type="button" class="image-nav" id="image-next" onclick="moveImage(1)" title="Следующий снимок (→)" aria-label="Следующий снимок"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m10 6 6 6-6 6"/></svg></button>
</div>
<p id="viewer-note" role="status" hidden></p>
</div></dialog>
<script>function highlightMatches(element,matches){
const walker=document.createTreeWalker(element,NodeFilter.SHOW_TEXT),nodes=[];
let node,offset=0,first=0;
while(node=walker.nextNode())nodes.push(node);
for(const node of nodes){
const text=node.data,end=offset+text.length;
while(first<matches.length&&matches[first].index+matches[first][0].length<=offset)first++;
const fragment=document.createDocumentFragment();let used=0;
for(let i=first;i<matches.length&&matches[i].index<end;i++){
const start=Math.max(0,matches[i].index-offset),stop=Math.min(text.length,matches[i].index+matches[i][0].length-offset);
fragment.append(document.createTextNode(text.slice(used,start)));
const mark=document.createElement('mark');mark.className='search-hit';mark.textContent=text.slice(start,stop);
fragment.append(mark);used=stop;
}
if(used){fragment.append(document.createTextNode(text.slice(used)));node.replaceWith(fragment);}
offset=end;
}
}
function filter(){
const query=document.getElementById('search').value;
const escaped=Array.from(query,c=>('.*+?^${}()|[]'.includes(c)||c.charCodeAt(0)===92)?String.fromCharCode(92)+c:c).join('');
const pattern=query?new RegExp(escaped,'giu'):null;
const err=document.getElementById('errors').checked;
document.querySelectorAll('article').forEach(e=>{
e.querySelectorAll('mark.search-hit').forEach(mark=>{const parent=mark.parentNode;mark.replaceWith(document.createTextNode(mark.textContent));parent.normalize();});
const matches=pattern?[...e.textContent.matchAll(pattern)]:[];
e.hidden=(err&&!e.classList.contains('failed'))||(pattern!==null&&matches.length===0);
if(!e.hidden&&matches.length)highlightMatches(e,matches);
});
}
function setAllExpanded(expanded){document.querySelectorAll('article details').forEach(e=>{e.open=expanded;});}
const imageViewer=document.getElementById('image-viewer'), viewerShell=document.getElementById('viewer-shell');
const viewerImage=document.getElementById('viewer-image'), viewerNote=document.getElementById('viewer-note');
let previousOverflow='';
let imageLinks=[],imageIndex=-1;
function showImage(index){
if(index<0||index>=imageLinks.length)return;
imageIndex=index;const link=imageLinks[index];viewerImage.src=link.href;
const title=link.closest('article').querySelector('.call-title').textContent;
document.getElementById('image-title').textContent=title;viewerImage.alt=title;
document.getElementById('image-position').textContent=`${index+1} / ${imageLinks.length}`;
document.getElementById('image-previous').disabled=index===0;
document.getElementById('image-next').disabled=index===imageLinks.length-1;
document.querySelector('.viewer-stage').scrollTo(0,0);
}
function moveImage(offset){if(imageViewer.open)showImage(imageIndex+offset);}
document.addEventListener('keydown',event=>{
if(!imageViewer.open||event.altKey||event.ctrlKey||event.metaKey||event.shiftKey)return;
if(event.key==='ArrowLeft'||event.key==='ArrowRight'){
event.preventDefault();moveImage(event.key==='ArrowLeft'?-1:1);
}
});
document.addEventListener('click',event=>{
const link=event.target.closest('a.screenshot-link');
if(!link||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey||event.button!==0)return;
if(typeof imageViewer.showModal!=='function')return;
event.preventDefault();
imageLinks=[...document.querySelectorAll('article:not([hidden]) a.screenshot-link')];
showImage(imageLinks.indexOf(link));
viewerNote.hidden=true;imageViewer.classList.remove('expanded');
previousOverflow=document.documentElement.style.overflow;document.documentElement.style.overflow='hidden';
imageViewer.showModal();updateFullscreenButton();
});
async function closeImageViewer(){
if(document.fullscreenElement===viewerShell){try{await document.exitFullscreen();}catch(e){}}
imageViewer.close();
}
imageViewer.addEventListener('cancel',event=>{event.preventDefault();closeImageViewer();});
imageViewer.addEventListener('click',event=>{if(event.target===imageViewer)closeImageViewer();});
imageViewer.addEventListener('close',()=>{
document.documentElement.style.overflow=previousOverflow;viewerImage.removeAttribute('src');
imageLinks=[];imageIndex=-1;
imageViewer.classList.remove('expanded');viewerNote.hidden=true;updateFullscreenButton();
});
function updateFullscreenButton(){
const full=document.fullscreenElement===viewerShell||imageViewer.classList.contains('expanded');
const button=document.getElementById('image-fullscreen'),label=full?'Свернуть':'Во весь экран';
button.title=label;button.setAttribute('aria-label',label);
button.setAttribute('aria-pressed',String(full));
}
async function toggleImageFullscreen(){
try{
if(document.fullscreenElement===viewerShell)await document.exitFullscreen();
else if(imageViewer.classList.contains('expanded'))imageViewer.classList.remove('expanded');
else if(document.fullscreenEnabled&&viewerShell.requestFullscreen)await viewerShell.requestFullscreen();
else throw new Error('Fullscreen unavailable');
viewerNote.hidden=true;
}catch(e){
imageViewer.classList.add('expanded');viewerNote.textContent='Полноэкранный режим недоступен. Изображение развёрнуто в окне браузера.';viewerNote.hidden=false;
}
updateFullscreenButton();
}
document.addEventListener('fullscreenchange',updateFullscreenButton);
</script>
'''


def json_block(value):
    lines = []
    for line in json.dumps(value, ensure_ascii=False, indent=2).splitlines():
        # Continue below the value, leaving the property names at the left edge.
        key = re.match(r'^\s*"(?:[^"\\]|\\.)*":\s*', line)
        leading = len(line) - len(line.lstrip(' '))
        indent = max(leading + 8, key.end() + 1 if key else 0)
        content = re.sub(r'([.\-\u2013\u2014])', r'\1<wbr>', html.escape(line, quote=True))
        lines.append(f'<span class="json-line" style="padding-left:{indent}ch;text-indent:-{indent}ch">'
                     + content + '</span>')
    return '<pre>' + ''.join(lines) + '</pre>'


def card(event):
    esc = lambda v: html.escape(str(v), quote=True)
    result = event['result']
    failed = isinstance(result, dict) and (result.get('ok') is False or 'exception' in result)
    text = (f'<article class="{"failed" if failed else "passed"}"><details class="call" open>'
            f'<summary><strong class="call-title">#{event["call_id"]} '
            f'{esc(event["tool"])} / {esc(event["action"])}</strong><small class="call-meta">'
            f'{esc(event["time"])} · {esc(event["connection_id"])} · '
            f'{event["duration_ms"]} мс · {"Ошибка" if failed else "Ответ получен"}</small></summary>'
            '<div class="call-body">')
    for label, key in [('Параметры', 'arguments'), ('Адреса элементов', 'targets'), ('Результат', 'result')]:
        text += f'<details><summary>{label}</summary>{json_block(event.get(key, {}))}</details>'
    shot = event.get('screenshot', {})
    if shot.get('path'):
        path = esc(shot['path'])
        text += f'<p><a class="screenshot-link" href="{path}" target="_blank"><img loading="lazy" src="{path}" alt="Состояние после вызова"></a></p>'
    if shot:
        text += '<details><summary>Захват экрана</summary>' + json_block(shot) + '</details>'
    return (text + '</div></details></article>\n').encode('utf-8')


class Journal:
    def __init__(self, store, directory, connection_id, mode):
        self.store, self.directory = store, directory
        self.connection_id, self.mode = connection_id, mode
        self.active = True
        self.reason = None
        self.calls = self.completed = self.bytes = self.screenshot_errors = 0
        self.started_at = now()

    def status(self):
        return dict(ok=True, active=self.active, logging_id=self.directory.name,
                    connection_id=self.connection_id, screenshot_mode=self.mode,
                    started_at=self.started_at, calls=self.calls, completed=self.completed,
                    bytes=self.bytes, screenshot_errors=self.screenshot_errors,
                    directory=str(self.directory), journal=str(self.directory / 'events.jsonl'),
                    report=str(self.directory / 'report.html'), reason=self.reason)

    def pause(self, reason):
        self.active = False
        self.reason = reason
        logging.getLogger(__name__).warning('Call logging paused (%s): %s', self.directory.name, reason)

    def begin(self, tool, action, arguments, targets):
        if not self.active:
            return None
        self.calls += 1
        call = dict(event='start', call_id=self.calls, time=now(), connection_id=self.connection_id,
                    tool=tool, action=action, arguments=clean(arguments), targets=clean(targets))
        if not self.store.append(self, 'events.jsonl', encode(call)):
            return None
        return dict(call, clock=time.monotonic(), result=None, picture=None)

    def finish(self, call, capture=None):
        if call is None or not self.active:
            return
        event = {k: v for k, v in call.items() if k not in ('clock', 'picture')}
        event.update(event='finish', time=now(), duration_ms=round((time.monotonic() - call['clock']) * 1000))
        # Persist the outcome BEFORE capture: a helper crash must not hide a performed action.
        if not self.store.append(self, 'events.jsonl', encode(event)):
            return
        self.completed += 1
        if capture is not None or (self.mode != 'off' and call['picture'] is not None):
            shot = {'captured_at': now()}
            try:
                picture = call['picture'] if call['picture'] is not None else capture()
                path = f'screenshots/{call["call_id"]:06d}.png'
                if self.store.append(self, path, picture.png):
                    shot.update(path=path, metadata=clean(picture.metadata))
                else:
                    shot.update(code=self.reason, error='Screenshot was not saved.')
            except Exception as exc:
                self.screenshot_errors += 1
                shot.update(code=getattr(exc, 'code', 'screenshot_failed'), error=str(exc))
            shot['captured_at'] = now()
            event['screenshot'] = shot
            if not self.store.append(self, 'events.jsonl', encode(dict(event='screenshot', call_id=call['call_id'], **shot))):
                return
        self.store.append(self, 'report.html', card(event))

    def stop(self, reason='stopped'):
        if self.active:
            self.store.append(self, 'events.jsonl', encode(dict(event='stop', time=now(), reason=reason)))
        self.active = False
        self.reason = self.reason or reason
        # A completion marker allows retention to remove only our finished journals.
        self.store.complete(self)
        return self.status()


class Store:
    def __init__(self, root=None, max_bytes=None, screenshots=None):
        self.root = Path(root or os.environ.get('TC1C_LOG_DIR', 'logs')).expanduser().absolute()
        try:
            self.max_bytes = max_bytes if max_bytes is not None else int(os.environ.get('TC1C_LOG_MAX_MB', '1024')) * 1024 * 1024
        except ValueError:
            raise ValueError('TC1C_LOG_MAX_MB must be a positive integer') from None
        if not isinstance(self.max_bytes, int) or isinstance(self.max_bytes, bool) or self.max_bytes <= 0:
            raise ValueError('TC1C_LOG_MAX_MB must be a positive integer')
        self.screenshots = enabled('TC1C_LOG_SCREENSHOTS') if screenshots is None else screenshots
        self.lock = threading.RLock()
        self.active = set()
        self.total = None

    def owned(self):
        """Only exact, marked directories; never follow links during retention."""
        if not self.root.exists():
            return []
        found = []
        for directory in self.root.glob('session-*'):
            if directory.is_symlink() or not directory.is_dir() or (hasattr(directory, 'is_junction') and directory.is_junction()):
                continue
            try:
                if directory.resolve().parent != self.root.resolve():
                    continue
                marker = directory / 'session.json'
                if marker.is_symlink() or json.loads(marker.read_text('utf-8')).get('format') != 'testpilot-call-log-1':
                    continue
                paths = list(directory.iterdir())
                shots = directory / 'screenshots'
                if shots.exists() and not shots.is_symlink():
                    paths.extend(shots.iterdir())
                if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in paths):
                    continue
                if any(p.is_dir() and p != shots for p in paths):
                    continue
                if any(p.is_dir() for p in paths if p.parent == shots):
                    continue
                size = sum(p.stat().st_size for p in paths if p.is_file())
                found.append((directory, size, paths))
            except (OSError, ValueError):
                continue
        return sorted(found, key=lambda row: row[0].name)

    def room(self, count):
        if count > self.max_bytes:
            return False
        if self.total is None:
            self.total = sum(size for _, size, _ in self.owned())
        if self.total + count <= self.max_bytes:
            return True
        for directory, size, paths in self.owned():
            if directory in self.active or not (directory / 'completed').is_file():
                continue
            # All targets above were checked to stay below this exact journal directory.
            for path in paths:
                if path.is_file():
                    path.unlink()
            shots = directory / 'screenshots'
            if shots.exists():
                shots.rmdir()
            directory.rmdir()
            self.total -= size
            if self.total + count <= self.max_bytes:
                return True
        return False

    def append(self, journal, filename, data):
        with self.lock:
            try:
                if not journal.active:
                    return False
                if not self.room(len(data)):
                    journal.pause('log_limit_exceeded')
                    return False
                path = journal.directory / filename
                with path.open('ab') as stream:
                    stream.write(data)
                    stream.flush()
                journal.bytes += len(data)
                self.total += len(data)
                return True
            except Exception:
                # Disk errors must never replace the tool's real outcome.
                self.total = None
                journal.pause('log_write_failed')
                return False

    def start(self, connection_id, mode=None):
        mode = mode or ('actions' if self.screenshots else 'off')
        if mode not in MODES:
            return {'ok': False, 'code': 'invalid_logging_mode', 'error': 'screenshot_mode must be off, actions or all.'}
        if mode != 'off' and not self.screenshots:
            return {'ok': False, 'code': 'log_screenshots_disabled', 'error': 'Screenshot logging is disabled on this server; use screenshot_mode="off".'}
        with self.lock:
            directory = self.root / ('session-' + datetime.now().astimezone().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:12])
            journal = Journal(self, directory, connection_id, mode)
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                initial = [('session.json', encode(dict(format='testpilot-call-log-1', connection_id=connection_id, started_at=journal.started_at))),
                           ('report.html', _HTML.encode('utf-8')),
                           ('events.jsonl', encode(dict(event='session_start', time=now(), connection_id=connection_id, screenshot_mode=mode)))]
                if not self.room(sum(len(data) for _, data in initial)):
                    return {'ok': False, 'code': 'log_limit_exceeded', 'error': 'No room for a new call journal.'}
                directory.mkdir(mode=0o700 if os.name != 'nt' else 0o777)
                (directory / 'screenshots').mkdir()
                self.active.add(directory)
                for filename, data in initial:
                    if not self.append(journal, filename, data):
                        self.complete(journal)
                        return {'ok': False, 'code': journal.reason, 'error': 'Could not start call logging.'}
                return journal
            except Exception:
                self.active.discard(directory)
                return {'ok': False, 'code': 'log_write_failed', 'error': 'Could not create the call journal in TC1C_LOG_DIR.'}

    def complete(self, journal):
        with self.lock:
            try:
                (journal.directory / 'completed').touch()
            except OSError:
                pass
            self.active.discard(journal.directory)
