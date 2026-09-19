"""Read a form overview and current interaction without changing focus or editing."""
import time

import _snapshots as snapshots


def interaction(S, c, form, elements, before):
    out = dict(current_key=None, input=None, choices=[], tables=[], documents=[], pages=[], errors=[])

    def error(obj, prop, result):
        out['errors'].append(dict(key=obj['key'], handle=obj.get('handle'), title=obj.get('title'),
            property=prop, code=result.get('code', 'value_unavailable'),
            error=result.get('error') or result.get('message') or 'The property could not be read.'))

    def read(obj, prop, fn, field, valid):
        try:
            result = fn(obj['key'], obj.get('handle'))
        except S.tc1c.OperationError as exc:
            result = exc.result()
        value = result.get(field)
        if result.get('ok') and valid(value):
            return value
        error(obj, prop, result)
        return None

    def related(obj, prop, fn, field):
        items = read(obj, prop, fn, field, lambda v: isinstance(v, list))
        if items is not None and len(items) == 1 and items[0].get('key') in elements:
            return elements[items[0]['key']]
        if items is not None:
            error(obj, prop, {'message': 'The current element could not be identified in this form.'})
        return None

    for obj in elements.values():
        if obj.get('type') == 'Pages' and obj.get('visible') is True:
            page = related(obj, 'current_page', S.tc_get_current_page, 'page')
            out['pages'].append(dict(key=obj['key'], handle=obj.get('handle'),
                                     current_page_key=page['key'] if page else None))

    focus = before[1]
    if not focus:
        return out
    if len(focus) != 1 or focus[0] not in elements:
        error(form, 'current_element', {'message': 'The focused element is outside the returned form elements.'})
        return out
    obj = elements[focus[0]]
    out['current_key'] = obj['key']
    table_key = obj['key'] if obj.get('class') == 'Table' else S._table_owner(obj['key'])
    if table_key:
        table = elements.get(table_key)
        if table is None:
            error(obj, 'table', {'message': 'The focused table could not be identified.'})
            return out
        column = related(table, 'current_column', S.tc_get_current_item, 'item')
        editing = read(table, 'editing', S.tc_current_mode_is_edit, 'edit_mode', lambda v: type(v) is bool)
        out['tables'].append(dict(key=table_key, handle=table.get('handle'),
            current_column_key=column['key'] if column else None, editing=editing))
        if column:
            obj = column
            out['current_key'] = obj['key']
        else:
            return out
        # Outside row editing, the column is not an active input editor.
        if editing is not True:
            return out
    kind = obj.get('type')
    if kind == 'InputField':
        text = read(obj, 'edit_text', S.tc_get_edit_text, 'text', lambda v: isinstance(v, str))
        opened = read(obj, 'drop_list_open', S.tc_drop_list_is_open, 'open', lambda v: type(v) is bool)
        out['input'] = dict(key=obj['key'], handle=obj.get('handle'), edit_text=text, drop_list_open=opened)
        if opened is True:
            result = S.tc_get_choice_list(obj['key'], obj.get('handle'))
            if result.get('ok') and result.get('status') != 'unknown' and isinstance(result.get('items'), list):
                out['choices'] = [dict(field_key=obj['key'], index=i, text=item['text'])
                                  for i, item in enumerate(result['items'])]
            else:
                error(obj, 'choices', result)
    elif kind == 'SpreadsheetDocumentField':
        address = read(obj, 'current_area', S.tc_get_current_area_address, 'address', lambda v: isinstance(v, str))
        editing = read(obj, 'editing', S.tc_current_mode_is_edit, 'edit_mode', lambda v: type(v) is bool)
        out['documents'].append(dict(key=obj['key'], handle=obj.get('handle'), current_area=address, editing=editing))
    return out


def run(S, key=None, save_as_snapshot=False, include_tables=False, max_rows=500):
    start = time.perf_counter()
    c, error = S._need_ver('8.3.3')
    if error:
        return error
    if type(save_as_snapshot) is not bool:
        return dict(ok=False, code='invalid_argument', error='save_as_snapshot must be a boolean.')
    try:
        options = snapshots.table_rows.options(include_tables, max_rows)
    except ValueError as exc:
        return dict(ok=False, code='invalid_argument', error=str(exc))
    if include_tables:
        _, error = S._need_ver('8.3.6')
        if error:
            return error
    track = getattr(c, '_track', None)
    try:
        window = S._window(c)
        if not window.get('key'):
            return dict(ok=False, code='active_window_unavailable', error='The active form cannot be read.')
        form = snapshots.form_object(S, c, key)
        if S._collection_parent(form['key']) != window['key']:
            return dict(ok=False, code='inactive_form', error='Activate this form before reading its current context.')
        current = snapshots.capture(S, c, form,
            inspect=lambda elements, before: interaction(S, c, form, elements, before), **options)
        context = current.pop('context')
        modified = current.pop('modified')
        if S._window(c).get('key') != window['key']:
            raise snapshots.Failure('context_unstable', 'The active window changed while reading the form. Retry.')
        for table in context['tables']:
            result = S.tc_get_current_item(table['key'], table.get('handle'))
            items = result.get('item', [])
            if (table['current_column_key'] is not None and
                    (not result.get('ok') or [o.get('key') for o in items] != [table['current_column_key']])):
                raise snapshots.Failure('context_unstable', 'The current table column changed while reading. Retry.')
        rows = []
        for obj in current['elements'].values():
            rows.append({**{k: v for k, v in obj.items() if k != '_status'},
                         'parent_key': S._collection_parent(obj['key'])})
        # An absent key and an empty value must not both turn into "" during TOON normalization.
        columns = list(dict.fromkeys(k for row in rows for k in row))
        rows = [{k: row.get(k) for k in columns} for row in rows]
        errors = current['errors'] + context.pop('errors')
        result = dict(ok=True, form={**form, 'modified': modified}, elements=rows,
                      **context, complete=not errors, errors=errors)
        if include_tables:
            result['table_rows'] = list(current['tables'].values())
        if S._address_mode() == 'id':
            registry = S._refs.for_client(c)
            if len(S._refs.context_pairs(result, registry)) > registry.limit:
                raise S._refs.RefError('ref_limit_exceeded',
                    'The form exceeds TC1C_REF_LIMIT; increase it to read the complete form. No snapshot was stored.')
        if save_as_snapshot:
            owner, generation, connection_id = snapshots.identity(S)
            try:
                saved = S._snapshot_storage().add(owner, generation, connection_id, form, current)
                result.update(snapshot_id=saved['snapshot_id'], snapshot_complete=current['complete'])
            except snapshots.Failure as exc:
                result['snapshot_error'] = dict(code=exc.code, message=str(exc))
        result['seconds'] = round(time.perf_counter() - start, 4)
        return result
    except snapshots.Failure as exc:
        code = exc.code.replace('snapshot_', 'context_')
        message = str(exc) if code != 'context_form_unavailable' else 'The form is no longer available. Find the form again.'
        return dict(ok=False, code=code, error=message)
    except S._refs.RefError as exc:
        return dict(ok=False, code=exc.code, error=str(exc))
    except (OSError, RuntimeError) as exc:
        return dict(ok=False, code='context_read_failed', error=str(exc))
    finally:
        c._track = track
