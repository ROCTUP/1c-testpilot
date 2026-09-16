"""Prepare all table selections before any final form-state reads."""


def options(include_tables, max_rows):
    if type(include_tables) is not bool:
        raise ValueError('include_tables must be a boolean.')
    if type(max_rows) is not int or not 1 <= max_rows <= 10000:
        raise ValueError('max_rows must be an integer from 1 to 10000.')
    return dict(include_tables=include_tables, max_rows=max_rows)


def target(obj):
    return {p: obj.get(p) for p in ('key', 'handle', 'name', 'title')}


def prepare(S, c, form, objects):
    import _field_batches as batches
    from _snapshots import Failure
    tables = {o['key']: dict(target(o), status='pending', rows=None, row_count=None,
                            selection_left=False, cursor_repositioned=False,
                            row_activation_possible=False) for o in objects if o.get('class') == 'Table'}
    errors = []
    visibility = {}

    def fail(t, r):
        t['status'] = 'error'
        errors.append(dict(target(t), property='rows', code=r.get('code', 'table_read_failed'),
                           error=r.get('error') or 'The table could not be prepared for ordered reading.'))

    def checked(t, action, **kw):
        r = getattr(S, 'tc_' + action)(key=t['key'], handle=t['handle'], **kw)
        if not r.get('ok'):
            fail(t, r)
            return None
        return r

    # No automatic focus changes or restoration. Finishing edits is the user's decision.
    if S._window(c).get('key') != S._collection_parent(form['key']):
        raise Failure('inactive_form', 'Activate this form before including table rows.')
    if getattr(c, '_pending_area_edits', None):
        raise Failure('other_input_pending', 'Finish or cancel pending input before including table rows.')
    # Reconcile remembered text input with the live focus, as batch writes do.
    # This only reads state; it must never finish editing or move focus itself.
    try:
        batches.ensure_pending(S, c, None)
    except (batches.Failure, S._CellEditFailure) as exc:
        raise Failure('other_input_pending', 'Finish or cancel pending input before including table rows.') from exc
    native_clear = S._guid_available(c, S.G.DESELECT_ALL_ROWS)
    # Preflight every table before changing any selection.
    for t in tables.values():
        visible = checked(t, 'is_visible')
        if visible is None:
            continue
        visibility[t['key']] = visible.get('visible')
        if visible.get('visible') is False:
            t['status'] = 'skipped'
            continue
        if visible.get('visible') is not True:
            fail(t, dict(code='visibility_unavailable', error='Table visibility could not be checked.'))
            continue
        editing = checked(t, 'current_mode_is_edit')
        if editing is None or editing.get('edit_mode') is not False:
            raise Failure('row_edit_pending', 'Finish or cancel table editing before including table rows.')

    for t in tables.values():
        if t['status'] != 'pending':
            continue
        if native_clear:
            checked(t, 'deselect_all_rows')
        else:
            selected = checked(t, 'get_selected_rows')
            if selected is None or not selected['rows']:
                continue
            t['row_activation_possible'] = True
            reduced = S.tc_goto_row(t['key'], handle=t['handle'])
            if not reduced.get('ok') and reduced.get('status_code') == 13:
                # A populated summary may have no current row. This fallback happens
                # during preparation, never after captured data has been read.
                if checked(t, 'goto_first_row') is None:
                    continue
                t['cursor_repositioned'] = True
            elif not reduced.get('ok'):
                fail(t, reduced)
                continue
            remaining = checked(t, 'get_selected_rows')
            if remaining is None:
                continue
            if len(remaining['rows']) > 1:
                fail(t, dict(code='selection_unavailable', error='Selection did not reduce to the current row.'))
            elif remaining['rows']:
                checked(t, 'goto_row', toggle_selection=True)

    # Later table handlers can modify earlier tables. Verify all selections before
    # selecting any rows; never normalize again during the final read phase.
    for t in tables.values():
        if t['key'] in visibility and type(visibility[t['key']]) is bool:
            r = checked(t, 'is_visible')
            if r is not None and r.get('visible') != visibility[t['key']]:
                raise Failure('snapshot_unstable', 'Table visibility changed during preparation. Retry.')
        if t['status'] != 'pending':
            continue
        r = checked(t, 'get_selected_rows')
        if r is not None and r['rows']:
            fail(t, dict(code='selection_changed', error='Selection changed during preparation. Retry the snapshot.'))
        mode = checked(t, 'current_mode_is_edit')
        if mode is not None and mode.get('edit_mode') is not False:
            raise Failure('row_edit_pending', 'Row editing started during preparation; it was left untouched.')
    if S._window(c).get('key') != S._collection_parent(form['key']):
        raise Failure('snapshot_unstable', 'The active window changed during table preparation.')
    for t in tables.values():
        if t['status'] == 'pending':
            # Even a failed command may have changed selection.
            t['selection_left'] = None
            checked(t, 'select_all_rows')
    return tables, errors


def read(S, tables, max_rows):
    errors = []
    for t in tables.values():
        if t['status'] != 'pending':
            continue
        r = S.tc_get_selected_rows(t['key'], t['handle'])
        if not r.get('ok') or not isinstance(r.get('rows'), list):
            t['status'] = 'error'
            errors.append(dict(target(t), property='rows', code=r.get('code', 'table_read_failed'),
                               error=r.get('error') or 'Table rows could not be read.'))
            continue
        rows = r['rows']
        t.update(rows=rows[:max_rows], row_count=len(rows), selection_left=bool(rows),
                 status='read' if len(rows) <= max_rows else 'truncated')
        if len(rows) > max_rows:
            errors.append(dict(target(t), property='rows', code='table_row_limit_exceeded',
                               error='The table exceeds max_rows; its row comparison is incomplete.'))
    return errors


def summary(tables):
    return [{k: v for k, v in t.items() if k != 'rows'} for t in tables.values()]


def difference(old, new):
    out = []
    for key, table in new.items():
        previous = old.get(key)
        if table['status'] != 'read':
            continue
        if previous is None or previous['status'] != 'read' or previous.get('handle') != table.get('handle'):
            out.append(dict(target(table), status='observed', row_count_before=previous.get('row_count') if previous else None,
                            row_count_after=table['row_count'], cells=[], rows=table['rows']))
            continue
        cells = []
        a, b = previous['rows'], table['rows']
        for index in range(max(len(a), len(b))):
            left = a[index] if index < len(a) else {}
            right = b[index] if index < len(b) else {}
            for column in dict.fromkeys((*left, *right)):
                if (column in left) != (column in right) or left.get(column) != right.get(column):
                    cells.append(dict(row=index + 1, column=column, before=left.get(column), after=right.get(column),
                                      before_present=column in left, after_present=column in right))
        if cells or len(a) != len(b):
            out.append(dict(target(table), status='changed', row_count_before=len(a),
                            row_count_after=len(b), cells=cells, rows=None))
    return out
