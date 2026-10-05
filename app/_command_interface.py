"""Command-interface discovery and bounded, connection-owned menu catalogues."""
from collections import OrderedDict
from copy import deepcopy
import json
import math
import time

import guids as G

MAX_WINDOWS = 20
MAX_ELEMENTS = 10000
MAX_BYTES = 4 * 1024 * 1024


class Failure(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def is_cached(client, key=None, all_sections=False, refresh=False):
    if client is None or all_sections is not True or refresh:
        return False
    key = key or vars(client).get('_command_main_window')
    return key in vars(client).get('_command_catalogues', {})


def validate(all_sections, section, search, limit, offset, refresh, timeout):
    if type(all_sections) is not bool or type(refresh) is not bool:
        raise Failure('invalid_argument', 'all_sections and refresh must be booleans.')
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise Failure('invalid_argument', 'limit must be 1..200; offset must be a nonnegative integer.')
    if any(v is not None and (not isinstance(v, str) or not v.strip()) for v in (section, search)):
        raise Failure('invalid_argument', 'section and search must be nonempty strings.')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise Failure('invalid_argument', 'timeout must be greater than zero and at most 3600 seconds.')
    if not all_sections and (section is not None or search is not None or refresh or offset or limit != 50):
        raise Failure('invalid_argument', 'Use all_sections=true for catalogue filters, paging or refresh.')
    if all_sections and offset and section is None and search is None:
        raise Failure('invalid_argument', 'Choose section or search before paging commands.')


def _menu(items):
    return next((o for o in items if o.get('key', '').endswith('.CI.SWINFP')), None)


def _sections(items):
    return [o for o in items if o.get('class') == 'CIButton'
            and o.get('key', '').rpartition('.')[0].endswith('.CI.SubSystems')]


def _section_id(section):
    # Built-in sections can expose commands without having a navigation URL themselves.
    return section.get('url') or 'section:' + section['key'].rsplit('CIButton[', 1)[1].rstrip(']')


def _selected(items, sections):
    if not _menu(items):
        return None
    matches = [s for s in sections if any(
        s['key'].rsplit('CIButton[', 1)[1].rstrip(']') + ':' in o['key']
        for o in items if '.CI.SWINFP.' in o.get('key', ''))]
    if len(matches) != 1:
        raise Failure('menu_state_unavailable', 'Cannot identify the open section; close its menu and retry.')
    return matches[0]


def _signature(items):
    return sorted((o['key'], o.get('title') or '', o.get('url') or '') for o in items)


def _commands(items, section):
    menu = _menu(items)
    if menu is None:
        return []
    descendants = [o for o in items if o['key'].startswith(menu['key'] + '.')]
    groups = [o for o in descendants if o.get('class') == 'CIGroup']
    result = []
    for obj in descendants:
        if obj.get('class') != 'CIButton':
            continue
        parents = sorted((g for g in groups if obj['key'].startswith(g['key'] + '.')), key=lambda g: len(g['key']))
        title = obj.get('title') or ''
        result.append(dict(title=title, path=[section.get('title') or ''] +
                           [g.get('title') or '' for g in parents] + [title],
                           url=obj.get('url') or None))
    return result


class Reader:
    def __init__(self, runtime, client, window):
        self.R, self.c, self.window = runtime, client, window

    def tree(self):
        r = self.c.send_cmd(G.GET_COMMAND_INTERFACE, self.window, kind='read', middle=self.R.RC)
        if not r.get('ok'):
            raise Failure('command_interface_unavailable', 'Could not read this window command interface.')
        roots = self.R._coll(r, self.window)
        items = list(roots)
        for root in roots:
            items.extend(self.R._search_objects(self.c, root['key']))
        if len(items) > MAX_ELEMENTS:
            raise Failure('command_interface_too_large', 'The command interface exceeds 10000 elements.')
        return items

    def windows(self):
        active = self.R._window(self.c)
        if not active.get('ok') or not active.get('key'):
            raise Failure('window_state_unavailable', 'Cannot verify the active window before menu navigation.')
        return active['key'], sorted((w['key'], w.get('title') or '', w.get('url') or '')
                                     for w in self.R._read_open_windows(self.c))

    def click(self, section):
        try:
            for kind in ('action', 'commit'):
                r = self.c.send_cmd(G.CLICK_CI, section['key'], kind=kind, middle=b'', handle=section.get('handle'))
                if not r.get('ok'):
                    raise Failure('section_open_failed', 'The client refused to open the section.')
        finally:
            self.R._state['window_key'] = None

    def scan(self):
        initial = self.tree()
        sections = _sections(initial)
        selected = _selected(initial, sections)
        before = self.windows()
        catalogue = dict(sections=[dict(section=_section_id(s), title=s.get('title') or '',
                                         command_count=None, read=False) for s in sections],
                         entries=[], complete=False, restored=False)
        current_section = None
        try:
            for section, row in zip(sections, catalogue['sections']):
                current_section = section
                current = self.tree()
                opened = _selected(current, sections)
                if opened is None or opened['key'] != section['key']:
                    self.click(section)
                if self.windows() != before:
                    raise Failure('section_changed_window',
                                  'Opening this section changed application windows. Inspect the active window; no form was closed.')
                current = self.tree()
                opened = _selected(current, sections)
                if opened is None or opened['key'] != section['key']:
                    raise Failure('section_menu_unavailable', 'The section did not expose its command menu.')
                commands = _commands(current, section)
                entries = catalogue['entries'] + [dict(command=cmd, section=_section_id(section)) for cmd in commands]
                if len(entries) > MAX_ELEMENTS or len(json.dumps(entries, ensure_ascii=True).encode()) > MAX_BYTES:
                    raise Failure('command_interface_too_large', 'The menu catalogue exceeds its storage limit.')
                catalogue['entries'] = entries
                row.update(command_count=len(commands), read=True)
            catalogue['complete'] = True
        except Exception as exc:
            catalogue['scan_error'] = dict(code=getattr(exc, 'code', 'section_scan_failed'), error=str(exc),
                                           section=_section_id(current_section) if current_section else None)
        # Never send more commands after a timeout or after a section opened a form.
        try:
            if getattr(self.c, '_pending', 0) or time.monotonic() >= self.c._io_deadline:
                raise Failure('restoration_unavailable', 'No safe time remains to restore the menu.')
            if self.windows() != before:
                raise Failure('window_changed', 'Application windows changed; the new window was left open.')
            current = self.tree()
            opened = _selected(current, sections)
            if selected is None and opened is not None:
                self.click(opened)
            elif selected is not None and (opened is None or selected['key'] != opened['key']):
                self.click(selected)
            catalogue['restored'] = self.windows() == before and _signature(self.tree()) == _signature(initial)
            if not catalogue['restored']:
                raise Failure('restoration_unverified', 'The original window and command menu state could not be verified.')
        except Exception as exc:
            catalogue['restoration_error'] = dict(code=getattr(exc, 'code', 'restoration_failed'), error=str(exc))
        return catalogue


def page(catalogue, section, search, limit, offset):
    result = {k: deepcopy(v) for k, v in catalogue.items() if k != 'entries'}
    result.update(ok=True, total_commands=len(catalogue['entries']))
    if section is None and search is None:
        return result
    entries = catalogue['entries']
    if section is not None:
        matches = [s for s in catalogue['sections'] if s['section'] == section or s['title'] == section]
        if len(matches) != 1:
            raise Failure('section_not_found' if not matches else 'ambiguous_section',
                          'Use the section identifier returned in sections.')
        entries = [e for e in entries if e['section'] == matches[0]['section']]
        result['section_read'] = matches[0]['read']
    if search is not None:
        entries = [e for e in entries if search.casefold() in ' / '.join(e['command']['path']).casefold()]
    commands = [deepcopy(e['command']) for e in entries[offset:offset + limit]]
    end = offset + len(commands)
    result.pop('sections', None)
    result.update(commands=commands, total=len(entries), offset=offset, returned=len(commands),
                  has_more=end < len(entries), next_offset=end if end < len(entries) else None)
    return result


def get(R, c, key, all_sections, section, search, limit, offset, refresh, timeout):
    try:
        validate(all_sections, section, search, limit, offset, refresh, timeout)
        cache = vars(c).setdefault('_command_catalogues', OrderedDict())
        saved_key = key or vars(c).get('_command_main_window')
        if all_sections and not refresh and saved_key in cache:
            cache.move_to_end(saved_key)
            return dict(page(cache[saved_key], section, search, limit, offset), cached=True)
        if all_sections and not refresh and (section is not None or search is not None):
            raise Failure('command_catalogue_unavailable',
                          'First call with all_sections=true and no filters to collect sections, or request refresh=true.')
        old_deadline = getattr(c, '_io_deadline', None)
        c._io_deadline = min(old_deadline or float('inf'), time.monotonic() + timeout)
        try:
            if key is not None:
                window = R._winkey(c, key)
            else:
                mains = [w for w in R._read_open_windows(c) if w.get('class') == 'MainFrame'
                         and not R._code_execution.protected(c, w.get('key'))]
                if len(mains) != 1:
                    raise Failure('main_window_unavailable', 'Specify the application window explicitly.')
                window = mains[0]['key']
            reader = Reader(R, c, window)
            if not all_sections:
                return dict(ok=True, commands=reader.tree())
            # A failed refresh must not silently leave an older catalogue usable.
            cache.pop(window, None)
            catalogue = reader.scan()
            cache[window] = catalogue
            while len(cache) > MAX_WINDOWS:
                cache.popitem(last=False)
            if key is None:
                c._command_main_window = window
            return dict(page(catalogue, section, search, limit, offset), cached=False)
        finally:
            c._io_deadline = old_deadline
    except Failure as exc:
        return dict(ok=False, code=exc.code, error=str(exc))
