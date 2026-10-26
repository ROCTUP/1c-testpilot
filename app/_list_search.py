"""Native list search with bounded observation of the resulting rows."""
from collections import Counter
import math
import time

import _field_batches as B
import _keyboard
import _row_search


def require(result):
    if not result.get('ok'):
        raise B.Failure(result.get('code', 'search_failed'),
                        result.get('error', 'The list search action failed.'), details={'cause': result})
    return result


def search_fields(S, objects, key):
    # The actual addition may be nested in the table's command bar. A similarly
    # named control elsewhere on the form does not establish an association.
    found = []
    for obj in objects:
        if obj.get('class') != 'Additional' or obj.get('type') != 'SearchStringRepresentation' or not obj.get('handle'):
            continue
        parent = S._collection_parent(obj.get('key', ''))
        while parent and S._key_class(parent) != 'Table':
            parent = S._collection_parent(parent)
        if parent == key:
            found.append(obj)
    return found


def fingerprint(result):
    rows, count = result.get('rows'), result.get('row_count')
    count_valid = ((count is None and result.get('truncated') is True)
                   or (type(count) is int and isinstance(rows, list) and count >= len(rows)
                       and result.get('truncated') == (count > len(rows))))
    if (not isinstance(rows, list) or not count_valid
            or type(result.get('truncated')) is not bool
            or any(not isinstance(row, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                  for k, v in row.items()) for row in rows)):
        raise B.Failure('rows_unavailable', 'The table did not return a usable row set.')
    # Selection order is not guaranteed. Preserve duplicate rows and native
    # highlighting: appearance of highlighting is itself an observable change.
    return count, result['truncated'], Counter(tuple(sorted(row.items())) for row in rows)


class Search:
    def __init__(self, S, c, key, handle, text, max_rows, settle_time):
        self.S, self.c = S, c
        self.target = dict(key=key, handle=handle)
        self.text, self.max_rows, self.settle_time = text, max_rows, settle_time
        self.stage = 'check_target'
        self.result = dict(ok=False, target=key, text=text, input_attempted=False,
                           rows_stable=False, complete=False, rows=[], row_count=None)

    def check_time(self):
        if time.monotonic() >= self.c._io_deadline:
            raise TimeoutError('List search deadline expired.')

    def call(self, action, obj, **kw):
        self.check_time()
        return require(getattr(self.S, 'tc_' + action)(key=obj['key'], handle=obj['handle'], **kw))

    def active(self):
        self.check_time()
        self.S._cell_window(self.c, self.window)

    def readable_fields(self, candidates):
        readable = []
        for candidate in candidates:
            try:
                before = self.call('get_edit_text', candidate).get('text')
            except B.Failure as exc:
                # Generated placeholders may refuse GetEditText even though the
                # explicitly placed search addition is readable.
                if exc.code in ('unsupported_element_type', 'unsupported_receiver'):
                    continue
                raise
            if not isinstance(before, str):
                raise B.Failure('search_text_unavailable', 'The current search text could not be read.')
            readable.append((candidate, before))
        return readable

    def changed_window(self, active, previous_windows):
        S, c = self.S, self.c
        key = active.get('key')
        if key and key not in previous_windows:
            self.check_time()
            forms = [o for o in S._search_objects(c, key) if o.get('class') == 'ManagedForm']
            if (len(forms) == 1 and forms[0].get('form_name') == 'UniversalListFindExtForm'
                    and S._collection_parent(forms[0]['key']) == key):
                # Only the new native search dialog belongs to this operation.
                # Never close an existing dialog or an arbitrary application form.
                self.stage = 'close_search_dialog'
                self.check_time()
                S._cell_window(c, key)
                closed = S.tc_close_window(key=key)
                self.result['dialog_closed'] = closed.get('ok') is True
                require(closed)
                self.active()
                raise B.Failure('search_dialog_required',
                                'The table opened the advanced search dialog instead of a search string. '
                                'The dialog was closed without entering text; search through this dialog is not supported.')
        raise B.Failure('search_window_changed',
                        'The active window changed while locating the search string. No text was entered; inspect the active window.',
                        details={'active_window': key})

    def external_field(self, obj, form_key):
        S, c = self.S, self.c
        self.stage = 'locate_search_field'
        self.check_time()
        objects = S._search_objects(c, form_key)
        external = [o for o in search_fields(S, objects, None) if B.owner(S, o['key']) == form_key]
        readable = self.readable_fields(external)
        if not readable:
            raise B.Failure('search_field_unavailable', 'The table has no readable standard search-string control.')
        form = S._ref_live_object(c, form_key)
        if not form or not form.get('handle'):
            raise B.Failure('form_unavailable', 'The owning form is no longer available.')
        # Fail for remote/unavailable clients before changing focus.
        try:
            _keyboard._local_process(c)
        except _keyboard.KeyboardError as exc:
            raise B.Failure(exc.code, str(exc), details=exc.details) from exc
        self.check_time()
        previous_windows = {o['key'] for o in S._read_open_windows(c)}
        S._table_activate(c, obj['key'], obj['handle'], self.window)
        self.call('send_keys', obj, keys=['CTRL', 'F'])
        self.result['search_shortcut_sent'] = True
        until = min(c._io_deadline, time.monotonic() + 3)
        while True:
            self.check_time()
            active = require(S._window(c))
            if active.get('key') != self.window:
                self.changed_window(active, previous_windows)
            focused = self.call('get_current_element', form).get('item', [])
            if len(focused) == 1:
                selected = next((o for o, _ in readable if o['key'] == focused[0].get('key')
                                 and o['handle'] == focused[0].get('handle')), None)
                if selected:
                    self.active()  # An inactive form can report its remembered focus.
                    B.live(S, c, selected)
                    current = self.readable_fields([selected])
                    if current:
                        return current[0]
                    raise B.Failure('search_text_unavailable', 'The focused search string is no longer readable.')
            if time.monotonic() >= until:
                raise B.Failure('search_field_unavailable',
                                'The table did not focus a readable external search string. No text was entered.')
            time.sleep(min(0.1, max(0, until - time.monotonic())))

    def prepare(self):
        S, c = self.S, self.c
        self.check_time()
        obj = B.live(S, c, self.target)
        if obj.get('class') != 'Table':
            raise B.Failure('invalid_table', 'Search requires a list table.')
        form = B.owner(S, obj['key'])
        self.window = S._cell_window(c)
        if not form or S._collection_parent(form) != self.window:
            raise B.Failure('inactive_window', 'Activate the window containing the list first.')
        for action, slot in (('is_visible', 'visible'), ('is_enabled', 'enabled')):
            if self.call(action, obj).get(slot) is not True:
                raise B.Failure('target_not_interactive', 'The list must be visible and enabled.')
        B.ensure_pending(S, c, None)
        B.ensure_area_pending(S, c, form)
        if S._cell_flag(c, S.G.CURRENT_MODE_IS_EDIT, obj['key'], obj['handle']) is not False:
            raise B.Failure('row_edit_pending', 'Finish or cancel row editing before searching.')
        children = S._search_objects(c, obj['key'])
        readable = self.readable_fields(search_fields(S, children, obj['key']))
        if len(readable) > 1:
            raise B.Failure('search_field_unavailable', 'The table has no unique readable standard search-string control.')
        if readable:
            self.field, before = readable[0]
            # Checks unfinished input in another table/document before focus moves.
            S._table_activate(c, obj['key'], obj['handle'], self.window)
        else:
            self.field, before = self.external_field(obj, form)
        self.result.update(text_before=before, search_field=self.field.get('name'))

    def check_text(self):
        self.active()
        B.live(self.S, self.c, self.target)
        B.live(self.S, self.c, self.field)
        actual = self.call('get_edit_text', self.field).get('text')
        if actual != self.text:
            raise B.Failure('search_text_changed', 'The search field does not contain the requested text.',
                            details={'actual_text': actual})

    def run(self):
        self.prepare()
        self.active()
        B.live(self.S, self.c, self.field)
        self.stage = 'enter_search'
        self.result['input_attempted'] = True
        # A standard addition may be hidden while still accepting native InputText.
        # Empty text uses the existing Clear action and resets this search string.
        self.call('input_text', self.field, text=self.text, finish=False)
        self.stage = 'wait_for_rows'
        previous = None
        while True:
            self.check_text()
            rows = self.call('read_rows', self.target, max_rows=self.max_rows, text_format='raw')
            self.check_text()
            current = fingerprint(rows)
            if previous == current:
                self.check_time()
                self.result.update({k: rows[k] for k in ('rows', 'row_count', 'returned_rows', 'truncated',
                                   'selection_cleared', 'cursor_repositioned') if k in rows})
                self.result.update(ok=True, rows_stable=True, complete=not rows['truncated'],
                                   scope='selectable_rows', verification='search_text_and_stable_rows',
                                   settle_time=self.settle_time)
                return self.result
            previous = current
            self.check_time()
            time.sleep(min(self.settle_time, max(0, self.c._io_deadline - time.monotonic())))


def run(S, c, key, handle, text, max_rows=500, timeout=180, settle_time=2, columns=None, text_format='plain'):
    empty = dict(ok=False, target=key, input_attempted=False, rows_stable=False, complete=False)
    if type(text) is not str or any(ch in text for ch in ('\0', '\r', '\n')):
        return dict(empty, code='invalid_search', error='text must be one line; empty text clears the search.')
    if type(max_rows) is not int or not 1 <= max_rows <= 10000:
        return dict(empty, code='invalid_row_limit', error='max_rows must be an integer from 1 to 10000.')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        return dict(empty, code='invalid_timeout', error='timeout must be greater than 0 and at most 3600 seconds.')
    if type(settle_time) not in (int, float) or not math.isfinite(settle_time) or not 0 < settle_time < timeout:
        return dict(empty, code='invalid_settle_time', error='settle_time must be greater than 0 and less than timeout.')
    try:
        columns = _row_search.output_options(columns, text_format)
    except _row_search.OutputError as exc:
        return dict(empty, code=exc.code, error=str(exc))
    search = Search(S, c, key, handle, text, max_rows, settle_time)
    saved = getattr(c, '_io_deadline', None)
    c._io_deadline = min(saved if saved is not None else float('inf'), time.monotonic() + timeout)
    try:
        titles = _row_search.output_titles(S, c, key, columns)
        result = search.run()
        result['rows'] = _row_search.project(result['rows'], columns, text_format, titles)
        return result
    except _row_search.OutputError as exc:
        return dict(search.result, ok=False, rows=[], code=exc.code, error=str(exc))
    except Exception as exc:
        failure = (dict(code='search_timeout', error='Search observation timed out. Inspect the list before continuing.')
                   if isinstance(exc, TimeoutError) or time.monotonic() >= c._io_deadline else B.error(exc))
        return dict(search.result, **failure, stage=search.stage)
    finally:
        c._io_deadline = saved
        S._refs.for_client(c).remember(key, handle)
