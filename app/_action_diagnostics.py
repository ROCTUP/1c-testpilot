"""Optional post-action observations; never change the outcome of the action itself."""
import math
import time


FAILURE_ACTIONS = frozenset({
    'click', 'activate', 'input_text', 'clear', 'choose_from_drop_list',
    'execute_choice_from_choice_list', 'set_cell_text', 'table_add_row', 'change_row',
    'end_edit_row', 'set_check', 'set_row_values',
})


def failure(S, c, key, handle, result):
    """Bounded observations after an interaction refusal, with no retry or focus change."""
    if (not c or not key or result.get('ok') is not False or
            getattr(c, '_pending', 0) or getattr(c, '_failure_diagnostic_busy', False) or
            not S._guid_available(c, S.G.GET_ACTIVE_WINDOW)):
        return result
    if result.get('status_code') not in (9, 10, 11, 12) and result.get('code') not in {
        'column_not_current', 'row_edit_interrupted', 'input_pending', 'row_edit_pending',
        'value_not_confirmed', 'final_values_not_confirmed', 'inactive_page',
    }:
        return result
    if 'failure_context' in result:
        return result
    if result.get('code') == 'readonly':
        return result  # The original handler has already confirmed the precise cause.
    out = dict(complete=True, errors=[])
    track = getattr(c, '_track', None)
    deadline = time.monotonic() + 2
    reads = 0

    def read(label, fn, *args):
        nonlocal reads
        if reads >= 24 or time.monotonic() >= deadline:
            raise RuntimeError('The diagnostic read budget was reached.')
        reads += 1
        r = fn(*args)
        if isinstance(r, dict) and r.get('ok') is False:
            out['errors'].append(dict(property=label, code=r.get('code', 'value_unavailable')))
            out['complete'] = False
        return r

    def obj(k):
        return read('object', S._ref_live_object, c, k)

    def flags(o):
        data = {k: o.get(k) for k in ('key', 'handle', 'name', 'title', 'type')}
        for prop, fn, guid in (('visible', S.tc_is_visible, S.G.CURRENT_VISIBLE),
                               ('enabled', S.tc_is_enabled, S.G.CURRENT_ENABLE)):
            if S._guid_available(c, guid):
                data[prop] = read(prop, fn, o['key'], o.get('handle')).get(prop)
                if type(data[prop]) is not bool:
                    out['complete'] = False
                    out['errors'].append(dict(property=prop, code='value_unavailable'))
        return data

    try:
        c._track = None
        c._failure_diagnostic_busy = True
        target = obj(key)
        if not target or (handle and (target.get('handle') or '').lower() != handle.lower()):
            out.update(complete=False, code='target_unavailable')
            return dict(result, failure_context=out)
        out['target'] = flags(target)
        form_key = S._batches.owner(S, key)
        window = read('window', S._window, c)
        if not form_key or window.get('key') != S._collection_parent(form_key):
            out.update(complete=False, code='active_window_changed')
            return dict(result, failure_context=out)
        form = obj(form_key)
        focus = read('focus', S.tc_get_current_element, form_key, form.get('handle')).get('item', []) if form else []
        focused = focus[0] if len(focus) == 1 else None
        table_key = key if S._key_class(key) == 'Table' else S._table_owner(key)
        if table_key:
            table = target if table_key == key else obj(table_key)
            if table:
                mode = read('editing', S.tc_current_mode_is_edit, table_key, table.get('handle')).get('edit_mode')
                if type(mode) is not bool:
                    out['complete'] = False
                    out['errors'].append(dict(property='editing', code='value_unavailable'))
                items = read('current_column', S.tc_get_current_item, table_key, table.get('handle')).get('item', [])
                column = items[0] if len(items) == 1 else None
                out['table'] = dict(key=table_key, handle=table.get('handle'), editing=mode,
                                    current_column=column)
                if focused and (focused.get('key') == table_key or S._table_owner(focused.get('key', '')) == table_key):
                    focused = column if mode is True else None
        if focused:
            focused = obj(focused['key'])
        if focused and focused.get('type') == 'InputField':
            out['focused_element'] = focused
            if S._guid_available(c, S.G.DROP_LIST_IS_OPEN):
                opened = read('drop_list_open', S.tc_drop_list_is_open, focused['key'], focused.get('handle')).get('open')
                out['drop_list_open'] = opened
                if opened is True and S._guid_available(c, S.G.GET_CHOICE_LIST):
                    choices = read('choices', S.tc_get_choice_list, focused['key'], focused.get('handle'))
                    if choices.get('status') == 'ok':
                        items = choices.get('items', [])
                        out['choices'] = [dict(index=i, text=x['text']) for i, x in enumerate(items[:100])]
                        out['choices_truncated'] = len(items) > 100
                    else:
                        out['complete'] = False
        parent = S._collection_parent(key)
        ancestors = []
        while parent and S._key_class(parent) != 'ManagedForm':
            if S._key_class(parent) == 'Group':
                group = obj(parent)
                if group:
                    ancestors.append(flags(group))
            parent = S._collection_parent(parent)
        if ancestors:
            out['ancestors'] = ancestors
        # CurrentOpened is intentionally not used to infer a collapsed parent:
        # supported platform builds have been observed to return inverted values.
        if read('window', S._window, c).get('key') != window.get('key'):
            out.update(complete=False, code='active_window_changed')
    except Exception as exc:
        out['complete'] = False
        out['errors'].append(dict(property='context', error=str(exc)))
    finally:
        c._track = track
        c._failure_diagnostic_busy = False
    return dict(result, failure_context=out)


def validate(enabled, wait):
    if type(enabled) is not bool:
        return 'diagnostics must be a boolean.'
    if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= 60:
        return 'diagnostics_wait must be a number from 0 to 60 seconds.'
    return None


def collect(S, c, enabled=False, wait=2.0):
    start = time.monotonic()
    deadline = start + wait
    track = getattr(c, '_track', None)
    out = {}
    diagnostic = dict(status='unavailable', messages=None, messages_scope='window', timed_out=False)

    def finish():
        if enabled:
            diagnostic['seconds'] = round(time.monotonic() - start, 4)
            out['diagnostics'] = diagnostic
        return out

    try:
        c._track = None
        if not S._guid_available(c, S.G.GET_ACTIVE_WINDOW):
            out['window'] = dict(ok=False, key=None, title=None, addressable=False,
                code='unsupported_platform_version', message='Active-window reading requires platform 8.3.3 or newer.')
            diagnostic.update(code='unsupported_platform_version', message=out['window']['message'])
            return finish()
        while True:
            window = S._window(c)
            out['window'] = window
            if not enabled:
                return out
            diagnostic.update(status='unavailable', messages=None)
            diagnostic.pop('code', None)
            diagnostic.pop('message', None)
            if not window.get('ok') or not window.get('key'):
                diagnostic.update(code=window.get('code', 'active_window_unavailable'),
                    message='Messages cannot be read from the active window.')
                # Preview/native windows have no address for this protocol operation.
                if window.get('native'):
                    return finish()
            elif not S._guid_available(c, S.G.GET_USER_MESSAGE_TEXTS):
                diagnostic.update(code='unsupported_platform_version',
                    message='User-message reading is unavailable on this platform.')
                return finish()
            else:
                try:
                    reply = c.send_cmd(S.G.GET_USER_MESSAGE_TEXTS, window['key'], kind='read', middle=S.RC)
                    parsed = S._user_messages_result(reply)
                    if parsed.get('ok'):
                        diagnostic.update(status='read', messages=parsed['messages'])
                    else:
                        diagnostic.update(code='user_messages_unavailable', message='The message panel could not be read.')
                except S.tc1c.OperationError as exc:
                    if exc.status != 17:
                        raise
                    diagnostic.update(code='user_messages_unavailable',
                        message='The message panel is closed or unavailable.')
                # The message list belongs to the addressed window, not necessarily the
                # window that is active by the time the query returns.
                current = S._window(c)
                out['window'] = current
                if current.get('ok') and current.get('key') == window['key']:
                    if diagnostic['messages']:
                        return finish()
                else:
                    diagnostic.update(status='unavailable', messages=None, code='active_window_changed',
                        message='The active window changed while reading its messages.')
            if wait == 0:
                return finish()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                diagnostic['timed_out'] = True
                return finish()
            time.sleep(min(.15, remaining))
            if time.monotonic() >= deadline:
                diagnostic['timed_out'] = True
                return finish()
    except Exception as exc:
        # An additional read cannot undo the action that has already been sent. Do not
        # shorten socket deadlines either: normal action timeouts govern each query.
        S._state['window_key'] = None
        error = dict(code='diagnostics_read_failed', message=str(exc))
        if isinstance(exc, S.tc1c.OperationError):
            detail = exc.result()
            error.update(code=detail['code'], message=detail['error'])
        out['window'] = dict(ok=False, key=None, title=None, addressable=False, **error)
        diagnostic.update(status='failed', messages=None, timed_out=isinstance(exc, TimeoutError), **error)
        return finish()
    finally:
        c._track = track
