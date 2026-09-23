"""Bounded field operations. Protocol primitives and connection ownership stay in server."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, StrictBool

LIMIT = 100
_ROW_COLUMNS = ContextVar('testpilot_row_columns', default=None)
FIELD_ACTIONS = {'read_fields': 'targets', 'set_fields': 'entries'}
PROPERTIES = ('text', 'presentation', 'edit_text', 'visible', 'enabled', 'readonly')
SCALAR_KINDS = {'InputField', 'CheckBoxField', 'RadioButtonField', 'LabelField',
                'CalendarField', 'TrackBarField', 'ProgressBarField', 'PeriodField'}


class RefTarget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ref: StrictStr


class AddressTarget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: StrictStr
    handle: StrictStr


class RefEntry(RefTarget):
    text: StrictStr


class AddressEntry(AddressTarget):
    text: StrictStr


class RefCheckEntry(RefTarget):
    checked: StrictBool


class AddressCheckEntry(AddressTarget):
    checked: StrictBool


class CellEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    column: StrictStr
    text: StrictStr


class CheckCellEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    column: StrictStr
    checked: StrictBool


Property = Literal['text', 'presentation', 'edit_text', 'visible', 'enabled', 'readonly']
CellEntries = Annotated[list[CellEntry | CheckCellEntry], Field(min_length=1, max_length=LIMIT)]


class RowEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cells: CellEntries


RowEntries = Annotated[list[RowEntry], Field(min_length=1, max_length=LIMIT)]
PropertyList = Annotated[list[Property], Field(min_length=1, max_length=len(PROPERTIES))]


def public_type(name, mode):
    model = ({'targets': RefTarget, 'entries': RefEntry | RefCheckEntry} if mode == 'id' else
             {'targets': AddressTarget, 'entries': AddressEntry | AddressCheckEntry})[name]
    return Annotated[list[model], Field(min_length=1, max_length=LIMIT)]


def plain(value):
    return value.model_dump() if isinstance(value, BaseModel) else value


def refs(action, kw):
    """Only the two declared address arrays participate in connection selection."""
    items = kw.get(FIELD_ACTIONS.get(action, ''))
    if not isinstance(items, list):
        return []
    return [item['ref'] for value in items if isinstance(item := plain(value), dict) and 'ref' in item]


def bounded(items, fields):
    if not isinstance(items, list) or not 1 <= len(items) <= LIMIT:
        raise Failure('invalid_batch', f'Supply between 1 and {LIMIT} entries.')
    out = [plain(v) for v in items]
    for i, item in enumerate(out):
        expected = set(fields)
        if isinstance(item, dict) and 'text' in expected and 'checked' in item:
            expected = (expected - {'text'}) | {'checked'}
        if (not isinstance(item, dict) or set(item) != expected
                or any(type(item[k]) is not (bool if k == 'checked' else str) for k in expected)):
            message = ('Each entry must contain ' + ', '.join(k for k in fields if k != 'text') +
                       ' and exactly one value: text (string) or checked (boolean).') if 'text' in fields else (
                       'Each entry must contain exactly: ' + ', '.join(fields) + '.')
            raise Failure('invalid_batch', message, i)
    return out


def resolve_arguments(S, action, kw):
    slot = FIELD_ACTIONS[action]
    if slot not in kw:
        return kw  # The ordinary signature check reports a missing argument.
    id_mode = S._address_mode() == 'id'
    names = ['ref'] if id_mode else ['key', 'handle']
    if action == 'set_fields': names.append('text')
    entries = bounded(kw[slot], names)
    registry = S._refs.for_client(S._need()) if id_mode else None
    resolved = []
    # Resolve every reference before live enumeration can evict entries in a small registry.
    for entry in entries:
        if id_mode:
            key, handle = registry.resolve(entry['ref'])
            entry = {'key': key, 'handle': handle, **{k: entry[k] for k in ('text', 'checked') if k in entry}}
        else:
            S._response.check_ref_args(entry)
        resolved.append(entry)
    return {**kw, slot: resolved}


class Failure(Exception):
    def __init__(self, code, message, index=None, details=None):
        super().__init__(message)
        self.code, self.index = code, index
        self.details = details or {}


def error(exc):
    if isinstance(exc, Failure):
        return {**exc.details, 'code': exc.code, 'error': str(exc), **({'index': exc.index} if exc.index is not None else {})}
    if hasattr(exc, 'result') and callable(exc.result):
        r = exc.result()
        return {k: r[k] for k in ('code', 'error', 'status_code') if k in r}
    if isinstance(exc, (TimeoutError, OSError, ConnectionError)):
        return {'code': 'batch_interrupted', 'error': 'The client response was interrupted. Inspect the connection before continuing.'}
    if hasattr(exc, 'details'):
        return {'code': exc.details.get('code', 'batch_stopped'), 'error': str(exc)}
    return {'code': 'batch_stopped', 'error': str(exc)}


def owner(S, key):
    while key and S._key_class(key) != 'ManagedForm':
        key = S._collection_parent(key)
    return key


def live(S, c, entry):
    S.tc1c.validate_address(entry['key'], entry['handle'])
    obj = S._ref_live_object(c, entry['key'])
    if not obj or not entry['handle'] or (obj.get('handle') or '').lower() != entry['handle'].lower():
        raise Failure('ref_unavailable', 'The element is no longer available. Find it again.')
    return obj


def targets(S, c, entries, writing=False):
    entries = bounded(entries, ['key', 'handle', 'text'] if writing else ['key', 'handle'])
    seen, forms, objects = set(), set(), []
    for i, item in enumerate(entries):
        S.tc1c.validate_address(item['key'], item['handle'])
        if item['key'] in seen:
            raise Failure('duplicate_target', 'Each element may appear only once.', i)
        seen.add(item['key'])
    for i, item in enumerate(entries):
        obj = live(S, c, item)
        if obj.get('class') != 'EditField':
            raise Failure('unsupported_element_type', 'This action requires form fields.', i)
        expected_kind = 'CheckBoxField' if 'checked' in item else 'InputField'
        if writing and (obj.get('type') != expected_kind or S._table_owner(item['key'])):
            raise Failure('unsupported_element_type', 'Use text for input fields or checked for checkboxes; table cells use set_row_values.', i)
        form = owner(S, item['key'])
        if not form:
            raise Failure('form_unavailable', 'The owning managed form could not be identified.', i)
        forms.add(form)
        objects.append({**obj, **item})
    if len(forms) != 1:
        raise Failure('form_mismatch', 'All fields must belong to one form.')
    return objects, next(iter(forms))


@contextmanager
def observations(c):
    track = getattr(c, '_track', None)
    c._track = None
    try:
        yield
    finally:
        c._track = track


def read_property(S, c, obj, prop):
    key, handle = obj['key'], obj['handle']
    if prop in ('visible', 'enabled', 'readonly'):
        fn = {'visible': S.tc_is_visible, 'enabled': S.tc_is_enabled, 'readonly': S.tc_is_readonly}[prop]
        result, slot = fn(key, handle), prop
    else:
        if obj.get('type') not in SCALAR_KINDS:
            raise Failure('unsupported_property', 'Use the document or specialized reading action for this field.')
        table = S._table_owner(key)
        if prop == 'text' and table:
            parent = S._ref_live_object(c, table)
            if not parent or not parent.get('handle'):
                raise Failure('ref_unavailable', 'The table is no longer available.')
            result, slot = S.tc_get_cell_text(table, obj['name'], parent['handle']), 'text'
        else:
            if table:
                parent = S._ref_live_object(c, table)
                if not parent or not parent.get('handle'):
                    raise Failure('ref_unavailable', 'The table is no longer available.')
                current = S.tc_get_current_item(table, parent['handle'])
                if [o.get('key') for o in current.get('item', [])] != [key] or not S._active_column_editor(c, key):
                    raise Failure('editor_unavailable', 'This column has no active editor. Request text to read its cell.')
            fn, slot = {'text': (S.tc_get_text, 'text'), 'presentation': (S.tc_get_data_presentation, 'presentation'),
                        'edit_text': (S.tc_get_edit_text, 'text')}[prop]
            result = fn(key, handle)
    if not result.get('ok') or result.get(slot) is None:
        raise Failure(result.get('code', 'value_unavailable'), result.get('error') or 'The requested property could not be read.')
    return result[slot]


def read_fields(S, entries, properties):
    c = S._need()
    out = {'ok': False, 'results': []}
    try:
        props = PROPERTIES[:1] if properties is None else properties
        if not isinstance(props, (list, tuple)) or not props or any(p not in PROPERTIES for p in props) or len(set(props)) != len(props):
            raise Failure('invalid_properties', 'Supply unique property names: ' + ', '.join(PROPERTIES) + '.')
        with observations(c):
            objects, form = targets(S, c, entries)
            window = S._cell_window(c)
            if S._collection_parent(form) != window:
                raise Failure('form_not_active', 'Activate the owning form before reading its fields.')
            out['results'] = [dict(index=i, name=o.get('name'), status='not_read') for i, o in enumerate(objects)]
            for obj, item in zip(objects, out['results']):
                S._cell_window(c, window)
                live(S, c, obj)
                item.update(status='read', values={})
                for prop in props:
                    try:
                        item['values'][prop] = read_property(S, c, obj, prop)
                    except (Failure, S.tc1c.OperationError) as exc:
                        item['values'][prop] = None
                        item.setdefault('unavailable', {})[prop] = error(exc)
                        item['status'] = 'partial'
                S._cell_window(c, window)
        out['ok'] = all(r['status'] == 'read' for r in out['results'])
    except Exception as exc:
        out.update(error(exc))
    return out


def form_closed(S, c, form_key):
    """Only successful enumeration can establish closure; an inactive form is still open."""
    if not form_key or not S._guid_available(c, S.G.GET_CHILD_OBJECTS):
        return False
    window = S._collection_parent(form_key)
    roots = S._read_children(c, None)
    if not roots.get('ok'):
        return False
    if not any(o.get('key') == window for o in S._coll(roots, remember=False)):
        return True
    forms = S._read_children(c, window)
    return bool(forms.get('ok') and not any(
        o.get('key') == form_key for o in S._coll(forms, window, remember=False)))


def ensure_area_pending(S, c, active_form):
    """Forget edits only when their form is confirmed closed, without changing focus."""
    closed = {}
    for key in tuple(getattr(c, '_pending_area_edits', ())):
        form_key = owner(S, key)
        if form_key not in closed:
            try:
                closed[form_key] = form_key != active_form and form_closed(S, c, form_key)
            except Exception:
                closed[form_key] = False
        if closed[form_key]:
            S._clear_pending_area_edit(c, key)
    if getattr(c, '_pending_area_edits', None):
        raise Failure('input_pending', 'Finish or cancel the existing document edit before reading table rows.')


def ensure_pending(S, c, allowed):
    pending = getattr(c, '_pending_text_input', None)
    if pending and pending != allowed:
        failure = S._check_pending_text_input(c)
        if failure:
            raise Failure(failure['code'], failure['error'], details=failure)
        # A user can resolve the dialog raised by input and explicitly move on.
        # The remembered input alone must not keep blocking the same form forever.
        form_key = owner(S, pending)
        active_window = S._cell_window(c)
        form = None
        if form_key and active_window == S._collection_parent(form_key):
            form = S._ref_live_object(c, form_key)
            current = S._input_current(c, form) if form else None
            # '' confirms no current element; None means the read was inconclusive.
            if current is not None and current != pending:
                c._pending_text_input = None
                return
        # Absence from a successfully read collection establishes closure. A failed
        # lookup, another active window or a preview alone does not establish it.
        if form is None and form_closed(S, c, form_key):
            c._pending_text_input = None
            return
        raise Failure('input_pending', 'Finish or cancel the existing field input before filling other fields.')


def check_value(S, requested, text, presentation=None):
    if requested == text or requested == presentation:
        return True, 'exact'
    if S._numeric_equivalent(requested, text) or S._numeric_equivalent(requested, presentation):
        return None, 'numeric_equivalent'
    return False, 'unconfirmed'


def field_value(S, c, obj):
    return read_property(S, c, obj, 'text'), read_property(S, c, obj, 'presentation')


def finish_known_field(S, c, obj, other, window):
    """A form can cycle Tab back to the same field. Use another requested field
    only after the underlying value confirms the input; never accept a pending reference.
    """
    with observations(c):
        S._cell_window(c, window)
        actual, presentation = field_value(S, c, obj)
        verified, _ = check_value(S, obj['text'], presentation)
        if verified is False:
            return False
        live(S, c, other)
        S._cell_ready(c, other['key'], other['handle'])
    require_result(S.tc_activate(other['key'], other['handle']))
    with observations(c):
        S._cell_window(c, window)
        form = S._ref_live_object(c, owner(S, obj['key']))
        current = S._input_current(c, form) if form else None
        if not current or current == obj['key']:
            return False
        c._pending_text_input = None
        return True


def checked(S, item, obj, actual, presentation=None):
    if 'checked' in obj:
        state = checkbox_state(actual)
        item.update(value_after=actual, checked_after=state, verified=state == obj['checked'], verification='exact')
        if state != obj['checked']:
            raise Failure('value_not_confirmed', 'The checkbox does not have the requested state.', item['index'])
        return
    verified, verification = check_value(S, obj['text'], actual, presentation)
    item.update(value_after=actual, verified=verified, verification=verification)
    if presentation is not None: item['presentation'] = presentation
    if verified is False:
        raise Failure('value_not_confirmed', 'The accepted value does not confirm the requested text.', item['index'])


CHECKED_STEP = 'testpilot:set_checked'  # Recording event only; never a protocol method.


def checkbox_state(text):
    states = {'да': True, 'нет': False, 'yes': True, 'no': False}
    value = text.strip().casefold() if isinstance(text, str) else None
    if value not in states:
        raise Failure('checkbox_state_unavailable', 'The checkbox state could not be read as Yes/No. No state is assumed.',
                      details={'value': text})
    return states[value]


def apply_checkbox(S, c, obj):
    """Set a known state and record that state, including an already satisfied request."""
    with observations(c):
        if S._kind_of(c, obj['key']) != 'CheckBoxField':
            raise Failure('unsupported_element_type', 'checked requires a checkbox field.')
        S._cell_ready(c, obj['key'], obj['handle'])
        table = S._table_owner(obj['key'])
        if table:
            parent = S._ref_live_object(c, table)
            current = S.tc_get_current_item(table, parent['handle']) if parent else {}
            if [o.get('key') for o in current.get('item', [])] != [obj['key']]:
                raise Failure('column_not_current', 'Activate the checkbox column before setting its state.')
        window = S._cell_window(c)
        before = checkbox_state(read_property(S, c, obj, 'text'))
    track = getattr(c, '_track', None)
    mark = len(track) if track is not None else None
    if before != obj['checked']:
        require_result(S.tc_set_check(obj['key'], obj['handle']))
    with observations(c):
        S._cell_window(c, window)
        actual = read_property(S, c, obj, 'text')
        if checkbox_state(actual) != obj['checked']:
            raise Failure('value_not_confirmed', 'The checkbox did not keep the requested state.')
    if mark is not None:
        track[mark:] = [(CHECKED_STEP, obj['key'], b'1' if obj['checked'] else b'0', 'action')]
    return {'ok': True, 'changed': before != obj['checked']}


def empty_cell(S, c, key, handle, obj, window):
    """Verify conditional empty text with the existing guarded editor readback."""
    current = S.tc_get_current_item(key, handle)
    items = current.get('item', [])
    if not current.get('ok') or len(items) != 1:
        raise Failure('editor_unavailable', 'The current column could not be identified for verification.')
    previous = items[0]
    require_result(S.tc_activate(obj['key'], obj['handle']))
    value_empty = S._verify_empty_cell(c, key, handle, obj, window)
    if previous['key'] != obj['key']:
        require_result(S.tc_activate(previous['key'], previous['handle']))
        after = S.tc_get_current_item(key, handle)
        if [o.get('key') for o in after.get('item', [])] != [previous['key']]:
            raise Failure('verification_cleanup_failed', 'The current column could not be restored after verification.')
    return value_empty


def require_result(result):
    if not result.get('ok'):
        details = {k: result[k] for k in ('status_code', 'suggested_action', 'message', 'readonly', 'failure_context') if k in result}
        raise Failure(result.get('code', 'input_refused'), result.get('error') or 'Input was refused.', details=details)


def begin_record(S, c, action):
    return S._native_composite_begin(c, action)


def finish_record(S, c, mark, action, out):
    if mark is None:
        return
    # Do not send recovery/recording requests after a transport failure.
    if out.get('code') == 'batch_interrupted' or getattr(c, '_pending', 0):
        S._state.setdefault('rec_native_errors', []).append(action)
        return
    try:
        end = len(c._track)
        lost = S._synth_unhandled(c._track[mark:end])
        S._native_composite_end(c, mark, action)
        S._state.setdefault('rec_native_covered', []).append((mark, end))
        if lost:
            S._state.setdefault('rec_native_errors', []).extend(lost)
            out.update(ok=False, recording_complete=False, code='recording_incomplete',
                       error='Some actions could not be recorded. Inspect lost_actions when finishing the recording.')
    except Exception as exc:
        out.update(ok=False, recording_complete=False, **error(exc))


def row_objects(S, c, key, cells, columns=None, verify_live=True):
    values = bounded(cells, ['column', 'text'])
    cols = S._table_columns(c, key) if columns is None else columns
    objects, seen = [], set()
    for i, value in enumerate(values):
        if value['column'] in seen:
            raise Failure('duplicate_column', 'Each column may appear only once.', i)
        seen.add(value['column'])
        found = [o for o in cols if o.get('name') == value['column']]
        expected_kind = 'CheckBoxField' if 'checked' in value else 'InputField'
        if len(found) != 1 or found[0].get('type') != expected_kind:
            raise Failure('invalid_column', 'Use a unique column name: text for input columns, checked for checkbox columns.', i)
        obj = {**found[0], **{k: value[k] for k in ('text', 'checked') if k in value}}
        if verify_live:
            live(S, c, obj)
        objects.append(obj)
    return objects


def add_rows(S, key, handle, rows):
    c = S._need()
    out = dict(ok=False, target=key, results=[], completed=0, added=0)
    current = None
    try:
        if not isinstance(rows, list) or not 1 <= len(rows) <= LIMIT:
            raise Failure('invalid_batch', 'Provide 1–100 rows, each with cells.')
        rows = [plain(r) for r in rows]
        if any(not isinstance(r, dict) or set(r) != {'cells'} for r in rows):
            raise Failure('invalid_batch', 'Each row must contain only cells.')
        values = []
        for i, row in enumerate(rows):
            try:
                values.append(bounded(row['cells'], ['column', 'text']))
            except Failure as exc:
                exc.details['row_index'] = i
                raise
        if sum(map(len, values)) > 1000:
            raise Failure('invalid_batch', 'At most 1000 cells may be filled in one call.')
        with observations(c):
            if S._key_class(key) != 'Table':
                raise Failure('invalid_table', 'Address a table.')
            live(S, c, dict(key=key, handle=handle))
            # Validate every row before the first mutation, including later bad columns.
            columns = S._table_columns(c, key)
            requested = {}
            for i, cells in enumerate(values):
                try:
                    requested.update((o['key'], o) for o in row_objects(S, c, key, cells, columns, False))
                except Failure as exc:
                    exc.details['row_index'] = i
                    raise
            for obj in requested.values():
                live(S, c, obj)
            ensure_pending(S, c, None)
            window = S._cell_window(c)
            form = owner(S, key)
            if not form or S._collection_parent(form) != window:
                raise Failure('form_not_active', 'Activate the owning form before adding rows.')
        out['results'] = [dict(index=i, status='not_executed', added=False) for i in range(len(rows))]
        for cells, current in zip(values, out['results']):
            with observations(c):
                S._cell_window(c, window)
                live(S, c, dict(key=key, handle=handle))
                S._cell_ready(c, key, handle)
                # Never implicitly finish a row the caller was already editing.
                mode = S._cell_flag(c, S.G.CURRENT_MODE_IS_EDIT, key, handle)
                if mode is not False:
                    raise Failure('row_edit_pending' if mode is True else 'edit_state_unavailable',
                                  'Finish or cancel the current row edit before adding rows.')
                try:
                    row_objects(S, c, key, cells, columns)
                except Failure as exc:
                    if exc.code not in ('ref_unavailable', 'invalid_column'):
                        raise
                    # Finishing the previous row may rebuild its columns too.
                    columns = S._table_columns(c, key)
                    row_objects(S, c, key, cells, columns)
            current.update(status='adding', added=None)
            require_result(S.tc_table_add_row(key, handle))
            with observations(c):
                S._cell_window(c, window)
                if S._cell_flag(c, S.G.CURRENT_MODE_IS_EDIT, key, handle) is not True:
                    raise Failure('row_add_unconfirmed',
                                  'Adding did not leave a new row in editing. Inspect the table before retrying.')
            current.update(status='filling', added=True)
            out['added'] += 1
            # Each fill owns its native recording fragment; AddRow is recorded normally.
            # Refresh after AddRow, which can rebuild dynamic columns. Reuse only for
            # this nested call, retaining its public recording/diagnostic wrappers.
            with observations(c):
                columns = S._table_columns(c, key)
            token = _ROW_COLUMNS.set((c, key, columns))
            try:
                result = S.tc_set_row_values(key, handle, cells)
            finally:
                _ROW_COLUMNS.reset(token)
            current['fill'] = result
            require_result(result)
            current['status'] = 'completed'
            out['completed'] += 1
        out['ok'] = True
    except Exception as exc:
        detail = error(exc)
        if 'index' in detail:
            detail['cell_index'] = detail.pop('index')
        out.update(detail)
        if current is not None:
            current.update(status='stopped', **detail)
            out['stopped_at'] = current['index']
    return out


def write(S, entries=None, key=None, handle=None, cells=None):
    c = S._need()
    row = key is not None
    action = 'set_row_values' if row else 'set_fields'
    out = {'ok': False, 'results': [], 'completed': 0, 'final_verified': None}
    if row:
        out['target'] = key
        out['edit_finished'] = None
    mark, current, window = None, None, None
    try:
        with observations(c):
            if row:
                if S._key_class(key) != 'Table':
                    raise Failure('invalid_table', 'Address a table.')
                live(S, c, {'key': key, 'handle': handle})
                cached = _ROW_COLUMNS.get()
                columns = cached[2] if cached and cached[0] is c and cached[1] == key else None
                objects = row_objects(S, c, key, cells, columns)
                form = owner(S, key)
                ensure_pending(S, c, None)
            else:
                objects, form = targets(S, c, entries, writing=True)
                ensure_pending(S, c, objects[0]['key'])
            window = S._cell_window(c)
            if not form or S._collection_parent(form) != window:
                raise Failure('form_not_active', 'Activate the owning form before filling it.')
            out['results'] = [dict(index=i, name=o.get('name'), status='not_executed') for i, o in enumerate(objects)]
            if row:
                S._cell_ready(c, key, handle)
                mode = S._cell_flag(c, S.G.CURRENT_MODE_IS_EDIT, key, handle)
                if mode is None:
                    raise Failure('edit_state_unavailable', 'The row edit mode could not be read.')
                out['edit_finished'] = not mode
        mark = begin_record(S, c, action)
        for obj, current in zip(objects, out['results']):
            current['status'] = 'checking'
            with observations(c):
                S._cell_window(c, window)
                live(S, c, obj)
                S._cell_ready(c, obj['key'], obj['handle'])
                if row:
                    before = S._obs_cell(c, key, handle, obj['name'])
                    if before is None:
                        raise Failure('value_unavailable', 'The current cell could not be read.')
                else:
                    before, _ = field_value(S, c, obj)
                current['value_before'] = before
                if 'checked' in obj:
                    current['checked_before'] = checkbox_state(before)
            if row:
                require_result(S.tc_activate(obj['key'], obj['handle']))
                S._cell_window(c, window)
                selected = S.tc_get_current_item(key, handle)
                mode = S._cell_flag(c, S.G.CURRENT_MODE_IS_EDIT, key, handle)
                out['edit_finished'] = None if mode is None else not mode
                if [o.get('key') for o in selected.get('item', [])] != [obj['key']]:
                    raise Failure('column_not_current', 'The form focused another column. Inspect the current editor.')
                if mode is False and current['index'] == 0:
                    out['edit_finished'] = None
                    require_result(S.tc_change_row(key, handle))
                    mode = S._cell_flag(c, S.G.CURRENT_MODE_IS_EDIT, key, handle)
                out['edit_finished'] = None if mode is None else not mode
                if mode is not True:
                    raise Failure('row_edit_interrupted', 'Row editing did not start or was interrupted. Inspect the table.')
            current['status'] = 'attempted'
            result = (apply_checkbox(S, c, obj) if 'checked' in obj else
                      S.tc_input_text(obj['key'], obj['text'], obj['handle']))
            require_result(result)
            if not row and obj.get('text') and result.get('edit_finished') is False and len(objects) > 1:
                index = current['index']
                other = objects[index + 1] if index + 1 < len(objects) else objects[index - 1]
                if finish_known_field(S, c, obj, other, window):
                    result['edit_finished'] = True
            with observations(c):
                S._cell_window(c, window)
                if row:
                    selected = S.tc_get_current_item(key, handle)
                    mode = S._cell_flag(c, S.G.CURRENT_MODE_IS_EDIT, key, handle)
                    out['edit_finished'] = None if mode is None else not mode
                    if [o.get('key') for o in selected.get('item', [])] != [obj['key']] or mode is not True:
                        raise Failure('row_edit_interrupted', 'The editor changed while entering text. Inspect the table.')
                    actual = read_property(S, c, obj, 'text' if 'checked' in obj else 'edit_text')
                    checked(S, current, obj, actual)
                else:
                    if obj.get('text') and result.get('edit_finished') is not True:
                        raise Failure('input_pending', 'Input is unfinished or unverified. Inspect the field and any selection dialog.')
                    actual, presentation = field_value(S, c, obj)
                    checked(S, current, obj, actual, presentation)
            current['status'] = 'entered' if row else 'completed'
            out['completed'] += 1
        current = None
        if row:
            out['edit_finished'] = None
            require_result(S.tc_end_edit_row(key, handle))
            with observations(c):
                S._cell_window(c, window)
                mode = S._cell_flag(c, S.G.CURRENT_MODE_IS_EDIT, key, handle)
                out['edit_finished'] = None if mode is None else not mode
                if mode is not False:
                    raise Failure('row_edit_pending', 'The row still requires validation or value selection. Finish or cancel it explicitly.')
        with observations(c):
            out['final_verified'] = True
            for obj, item in zip(objects, out['results']):
                S._cell_window(c, window)
                live(S, c, obj)
                actual, presentation = ((S._obs_cell(c, key, handle, obj['name']), None) if row else field_value(S, c, obj))
                try:
                    if row and obj.get('text') == '' and actual not in ('', None) and empty_cell(S, c, key, handle, obj, window):
                        item.update(value_after=actual, verified=True, verification='empty_value')
                    else:
                        checked(S, item, obj, actual, presentation)
                    item['status'] = 'completed'
                    if item['verified'] is None and out['final_verified'] is True: out['final_verified'] = None
                except Failure as exc:
                    if exc.code != 'value_not_confirmed':
                        raise
                    item['status'] = 'not_confirmed'
                    out['final_verified'] = False
            S._cell_window(c, window)
        out['ok'] = out['final_verified'] is not False
        if not out['ok']:
            out.update(code='final_values_not_confirmed', error='Some values changed or could not be confirmed after filling. Inspect results before continuing.')
    except Exception as exc:
        detail = error(exc)
        if row and detail.get('status_code') == 11:
            with observations(c):
                detail = S._table_edit_diagnostic(c, key, detail)
        if row and detail.get('code') == 'verification_cleanup_failed':
            out['edit_finished'] = None
        out.update(detail)
        if current is not None:
            current.update(status='stopped', **detail)
            out['stopped_at'] = current['index']
        out['ok'] = False
        out['final_verified'] = None
    finally:
        finish_record(S, c, mark, action, out)
    return out
