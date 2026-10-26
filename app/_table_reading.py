"""Read tables whose selection mode does not support SelectAllRows."""
import re
import sys
import time
from collections import Counter

import _code_execution as execution
import tc1c


SCAN_LIMIT = 10000


def helper_state(R, c, key):
    service = vars(c).get('_testpilot_service')
    if (not service or 'table_selection' not in service.get('capabilities', [])
            or R._state.get('rec_active') or vars(c).get('_service_pending')):
        return None
    match = re.search(r'\.ManagedForm\[([^]]+)\].*\.Table\[([^]]+)\]$', key)
    if not match:
        return None
    form_id, name = match.groups()
    timeout = min(30, (vars(c).get('_io_deadline') or float('inf')) - time.monotonic())
    response = execution.execute(R, c, mode='form_details', context='client',
        options=dict(form_id=form_id, items=[name]), timeout=timeout, check_permissions=False)
    R._cell_step(response, 'The table selection mode could not be read.')
    data = response.get('result')
    items = data.get('items') if isinstance(data, dict) else None
    if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
            or data.get('found') is not True or data.get('form_id') != form_id
            or items[0].get('name') != name or items[0].get('selection_mode') not in ('single', 'multiple')
            or type(items[0].get('has_current_row')) is not bool):
        raise R._CellEditFailure('The helper returned no reliable table selection state.',
                                {'code': 'selection_state_unavailable'})
    return items[0]


class Reader:
    def __init__(self, R, c, key, handle, window, result, *, deadline=None, restore_deadline=None):
        self.R, self.c, self.key, self.handle = R, c, key, handle
        self.window, self.result = window, result
        self.deadline, self.restore_deadline = deadline, restore_deadline
        self.complete = True

    def check_time(self):
        deadline = self.deadline if self.deadline is not None else vars(self.c).get('_io_deadline')
        if deadline is not None and time.monotonic() >= deadline:
            self.fail('row_read_timeout', 'The table reading deadline expired.')

    def call(self, name, **kw):
        self.check_time()
        return self.R._cell_step(getattr(self.R, 'tc_' + name)(self.key, handle=self.handle, **kw),
                                 'Table reading failed: ' + name)

    def fail(self, code, message):
        raise self.R._CellEditFailure(message, {'code': code})

    def rows(self):
        return self.call('get_selected_rows')['rows']

    def guard(self):
        self.check_time()
        self.R._cell_window(self.c, self.window)
        self.check_time()
        if self.R._cell_flag(self.c, self.R.G.CURRENT_MODE_IS_EDIT, self.key, self.handle) is not False:
            self.fail('row_edit_pending', 'Row editing started during table reading.')

    def clear_current(self):
        if self.R._guid_available(self.c, self.R.G.DESELECT_ALL_ROWS):
            self.call('deselect_all_rows')
        else:
            self.call('goto_row', toggle_selection=True)
        if self.rows():
            self.fail('selection_cleanup_failed', 'The current row selection was not cleared.')

    def current_exists(self, state):
        self.check_time()
        if state is not None:
            return state['has_current_row']
        if self.R._guid_available(self.c, self.R.G.GET_CURRENT_ROW):
            return bool(self.call('get_current_row')['row'])
        # A successful, non-null cell read proves a current row, including empty text.
        columns = self.R._table_columns(self.c, self.key)
        names = Counter(column['name'] for column in columns)
        for column in columns:
            self.check_time()
            if names[column['name']] != 1:
                self.fail('ambiguous_column', 'Use a column element name returned by find_objects for this table.')
            try:
                value = self.R._read_cell_text(self.c, self.key, self.handle, column['name'], resolved=[column])
            except tc1c.OperationError as exc:
                if exc.status not in (11, 13):
                    raise
                continue
            if value.get('ok') and value.get('text') is not None:
                return True
            if not value.get('ok') and value.get('status_code') not in (11, 13):
                self.R._cell_step(value, 'The current table row could not be checked.')
        return False

    def bulk_rows(self):
        self.call('select_all_rows')
        # On Linux a newly opened dynamic list can initially select only the two
        # endpoints. Repeat selection before fetching the rows; no intermediate
        # read is needed, so even a large list is transferred only once.
        # Do not infer the client's OS from the host running this Python process.
        self.call('select_all_rows')
        return self.rows()

    def read(self, state, *, allow_sequential=True, selection_verified_empty=False, max_rows=None):
        if state is None or state['selection_mode'] == 'multiple':
            rows = self.bulk_rows()
            if len(rows) > 1 or state is not None or (selection_verified_empty and rows):
                return rows
        # Never address an unavailable current row with an unqualified GotoRow.
        if not allow_sequential:
            self.result['row_activation_possible'] = True
        had_current = self.current_exists(state)
        if had_current:
            self.call('goto_row')
        else:
            self.guard()
            self.call('goto_first_row', toggle_selection=True)
        current = self.rows()
        if not current:
            if had_current:
                self.fail('selection_state_unavailable', 'The existing current row could not be selected.')
            return []
        if not had_current:
            self.result['cursor_repositioned'] = True
            if len(current) > 1:
                # GotoFirstRow with selection can extend an existing range. Rows now
                # prove that navigation reached a populated table; collapse the range
                # before probing bulk selection, without treating it as the full table.
                self.call('goto_row')
                current = self.rows()
        if len(current) != 1:
            self.fail('selection_state_unavailable', 'A single current row could not be selected.')
        if state is None:
            self.clear_current()
            rows = self.bulk_rows()
            if rows:
                return rows
            self.call('goto_row')
        if not allow_sequential:
            self.fail('table_requires_sequential_read',
                      'This table requires sequential row reading. Use read_rows; its rows cannot be included in a stable form snapshot.')
        return self.walk(current[0], max_rows=max_rows)

    def walk(self, original, *, max_rows=None):
        # Selection marks movement, not row contents: identical rows are distinct.
        offset = 0
        at_first = False
        self.result['cursor_restored'] = False
        try:
            while True:
                self.guard()
                self.clear_current()
                self.call('goto_previous_row')
                rows = self.rows()
                if not rows:
                    at_first = True
                    break
                if len(rows) != 1:
                    self.fail('selection_state_unavailable', 'Single-row navigation selected multiple rows.')
                offset += 1
                if offset >= SCAN_LIMIT:
                    self.fail('row_scan_limit', 'Sequential table reading exceeds 10000 rows.')
            self.call('goto_row')
            result = []
            while True:
                rows = self.rows()
                if not rows:
                    return result
                if len(rows) != 1:
                    self.fail('selection_state_unavailable', 'Single-row navigation selected multiple rows.')
                if max_rows is not None and len(result) >= max_rows:
                    # This additional row proves truncation without counting the
                    # rest of the list or inventing an exact total.
                    self.complete = False
                    return result
                if len(result) >= SCAN_LIMIT:
                    self.fail('row_scan_limit', 'Sequential table reading exceeds 10000 rows.')
                result.extend(rows)
                self.guard()
                self.clear_current()
                self.call('goto_next_row')
        finally:
            failed = sys.exc_info()[0] is not None
            # Only restore when the original ordinal has been established. A timeout
            # with an unread answer makes further navigation unsafe.
            if at_first and not vars(self.c).get('_pending'):
                try:
                    self.deadline = self.restore_deadline
                    self.guard()
                    self.call('goto_first_row')
                    for _ in range(offset):
                        self.call('goto_next_row')
                    self.call('goto_row')
                    if self.rows() != [original]:
                        self.fail('table_changed', 'The original row changed during table reading.')
                    self.result['cursor_restored'] = True
                except Exception as exc:
                    self.result['cursor_restored'] = False
                    self.result['cursor_restore_error'] = str(exc)
                    if not failed:
                        raise
