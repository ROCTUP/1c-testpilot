"""Select an exact value through a field's drop-down or choice form."""
import math
import time
from typing import Annotated

from pydantic import Field, StrictStr

import _field_batches as B
from _row_search import _HIGHLIGHT

Match = Annotated[dict[StrictStr, StrictStr], Field(min_length=1, max_length=32)]


def text(value):
    return _HIGHLIGHT.sub(r'\1', value)


def require(result):
    if not result.get('ok'):
        raise B.Failure(result.get('code', 'selection_failed'),
                        result.get('error', 'The choice action failed.'), details={'cause': result})
    return result


class Selector:
    def __init__(self, S, c, key, handle, max_rows):
        self.S, self.c = S, c
        self.source = dict(key=key, handle=handle)
        self.max_rows = max_rows
        self.stage = 'check_target'
        self.selection_sent = False
        self.window = None
        self.accepted_expected = None
        self.result = dict(ok=False, target=key, verified=False)

    def call(self, action, obj, **kw):
        return require(getattr(self.S, 'tc_' + action)(key=obj['key'], handle=obj['handle'], **kw))

    def active(self, expected):
        self.S._cell_window(self.c, expected)

    def field_text(self):
        try:
            return B.read_property(self.S, self.c, self.source, 'text')
        except B.Failure as exc:
            # ListChoiceMode fields can refuse GetDisplayedText while exposing
            # their accepted value through GetDataPresentation.
            if exc.code not in ('unsupported_receiver', 'unsupported_element_type'):
                raise
            return B.read_property(self.S, self.c, self.source, 'presentation')

    def prepare(self):
        S, c = self.S, self.c
        obj = B.live(S, c, self.source)
        if obj.get('type') != 'InputField':
            raise B.Failure('unsupported_element_type', 'Select value requires an input field.')
        self.source = obj
        self.origin = self.S._cell_window(c)
        form = B.owner(S, obj['key'])
        if not form or S._collection_parent(form) != self.origin:
            raise B.Failure('inactive_window', 'Activate the field window before choosing a value.')
        B.ensure_pending(S, c, obj['key'])
        S._cell_ready(c, obj['key'], obj['handle'])
        self.table = None
        table_key = S._table_owner(obj['key'])
        if table_key:
            self.table = S._ref_live_object(c, table_key)
            if not self.table or not self.table.get('handle'):
                raise B.Failure('table_unavailable', 'The owning table is unavailable.')
            # Switching columns can accept another editor's unfinished value.
            mode = S._cell_flag(c, S.G.CURRENT_MODE_IS_EDIT, table_key, self.table['handle'])
            current = self.call('get_current_item', self.table).get('item', [])
            if mode is not False and [o.get('key') for o in current] != [obj['key']]:
                raise B.Failure('row_edit_pending', 'Finish the other table editor before selecting this cell.')
        self.result['value_before'] = (self.call('get_cell_text', self.table, column=obj['name']).get('text')
                                       if self.table else self.field_text())
        self.call('activate', obj)
        self.active(self.origin)
        if self.table:
            current = self.call('get_current_item', self.table).get('item', [])
            if [o.get('key') for o in current] != [obj['key']]:
                raise B.Failure('editor_unavailable', 'The requested table column did not become current.')
            if S._cell_flag(c, S.G.CURRENT_MODE_IS_EDIT, **self.address(self.table)) is False:
                self.call('change_row', self.table)
            if S._cell_flag(c, S.G.CURRENT_MODE_IS_EDIT, **self.address(self.table)) is not True:
                raise B.Failure('editor_unavailable', 'The table editor did not open.')

    @staticmethod
    def address(obj):
        return {k: obj[k] for k in ('key', 'handle')}

    def dropdown(self, value):
        self.stage = 'dropdown'
        self.call('open_drop_list', self.source)
        self.active(self.origin)
        opened = self.call('drop_list_is_open', self.source).get('open')
        if opened is False:
            return False
        if opened is not True:
            raise B.Failure('choice_list_unavailable', 'The drop-down state could not be determined.')
        choices = self.call('get_choice_list', self.source)
        values = choices.get('presentations')
        if not isinstance(values, list) or (values and choices.get('status') != 'ok'):
            raise B.Failure('choice_list_unavailable', 'The open choice list could not be read.')
        indexes = [i for i, v in enumerate(values) if v == value]
        if len(indexes) > 1:
            raise B.Failure('ambiguous_choice', 'Several choices have this exact text. Use match to select through the choice form.')
        if not indexes:
            self.call('close_drop_list', self.source)
            if self.call('drop_list_is_open', self.source).get('open') is not False:
                raise B.Failure('choice_list_still_open', 'The drop-down could not be closed before opening the choice form.')
            return False
        # Read again immediately before choosing; indexes are not stable identities.
        current = self.call('get_choice_list', self.source)
        if current.get('presentations') != values or current.get('items') != choices.get('items'):
            raise B.Failure('choice_list_changed', 'The choice list changed. No item was selected.')
        self.selection_sent = True
        items = choices.get('items', [])
        if len(items) == len(values):
            self.accepted_expected = items[indexes[0]].get('presentation')
        self.call('choose_from_drop_list', self.source, value=indexes[0])
        self.result.update(method='dropdown', requested=value)
        return True

    def objects(self):
        self.active(self.window)
        return self.S._search_objects(self.c, self.window)

    def visible(self, objects):
        return [o for o in objects if o.get('handle') and self.call('is_visible', o).get('visible') is True]

    def table_for(self, objects, name=None):
        tables = self.visible([o for o in objects if o.get('class') == 'Table'
                               and (name is None or o.get('name') == name)])
        if len(tables) != 1:
            raise B.Failure('choice_table_ambiguous', 'Specify choice_table as the exact name of the choice table.',
                            details={'tables': [o.get('name') for o in tables]})
        return tables[0]

    def locate(self, table, match=None, value=None, column=None):
        self.stage = 'find_choice'
        self.active(self.window)
        metadata = self.S._table_columns(self.c, table['key'])
        titles = [o.get('title') for o in metadata]
        rows_result = self.call('read_rows', table, max_rows=self.max_rows)
        rows = rows_result.get('rows')
        if (not isinstance(rows, list) or rows_result.get('truncated')
                or rows_result.get('row_count') != len(rows)):
            raise B.Failure('choice_search_incomplete', 'The choice table exceeds max_rows or could not be read completely. Narrow its filters before choosing.')
        if any(not isinstance(row, dict) or any(not isinstance(v, str) for v in row.values()) for row in rows):
            raise B.Failure('choice_rows_unavailable', 'The choice rows do not contain readable displayed text.')
        if match is None:
            if column is None:
                candidates = {k for row in rows for k, v in row.items() if text(v) == value}
                if len(candidates) > 1:
                    raise B.Failure('ambiguous_choice_column', 'The value occurs in several columns. Specify choice_column or match.')
                if not candidates:
                    raise B.Failure('choice_not_found', 'No exact value was found in the selectable rows of this choice table.')
                column = next(iter(candidates))
            match = {column: value}
        for name in match:
            if titles.count(name) > 1:
                raise B.Failure('ambiguous_column', 'Several choice columns have the same title: ' + name)
            if (rows and any(name not in row for row in rows)) or (not rows and name not in titles):
                raise B.Failure('choice_column_unavailable', 'The choice table did not return the column: ' + name)
        found = [row for row in rows if all(text(row[k]) == v for k, v in match.items())]
        if len(found) != 1:
            raise B.Failure('ambiguous_choice' if found else 'choice_not_found',
                            'Several rows match. Add distinguishing columns.' if found else
                            'No exact match in the selectable rows of this choice table.', details={'match_count': len(found)})
        # GotoRow treats * and ? as patterns. Never turn literal user input into a wildcard selection.
        if any('*' in v or '?' in v for v in match.values()):
            raise B.Failure('unsupported_choice_text', 'Choice-form positioning does not support literal * or ?. Use other distinguishing columns.')
        self.active(self.window)
        self.call('goto_first_row', table)
        position = self.call('goto_row', table, fields=match)
        if position.get('found') is not True:
            raise B.Failure('choice_changed', 'The matching row could not be positioned.')
        for title, wanted in match.items():
            cols = [o for o in metadata if o.get('title') == title]
            if len(cols) != 1 or not cols[0].get('name'):
                raise B.Failure('choice_column_unavailable', 'The positioned row cannot be checked: ' + title)
            actual = self.call('get_cell_text', table, column=cols[0]['name']).get('text')
            if not isinstance(actual, str) or text(actual) != wanted:
                raise B.Failure('choice_changed', 'The current row no longer matches the requested values.')
        return found[0]

    def form(self, value, match, choice_table, choice_column, data_type):
        self.stage = 'open_choice'
        self.call('start_choosing', self.source)
        self.window = self.S._cell_window(self.c)
        if self.window == self.origin:
            raise B.Failure('choice_not_opened', 'A separate choice window did not open.')
        objects = self.objects()
        type_tables = [o for o in objects if o.get('class') == 'Table' and o.get('name') == 'TypeTree']
        if type_tables:
            self.stage = 'choose_type'
            if not data_type:
                raise B.Failure('choice_type_required', 'Specify data_type as the displayed type name. The type dialog remains open.')
            if len(type_tables) != 1 or '.UnmanagedForm[' not in type_tables[0]['key']:
                raise B.Failure('choice_type_unavailable', 'The standard type dialog is unavailable.')
            # The native type picker is single-select and has an unnamed column.
            # SelectAllRows reads only the current row; GotoRow refuses its criteria.
            # Walk until the exact type is reached, never use a prefix match.
            table = type_tables[0]
            self.call('goto_first_row', table)
            previous = None
            available = []
            for _ in range(self.max_rows):
                self.active(self.window)
                current = self.call('get_cell_text', table, column=0).get('text')
                if current == data_type:
                    break
                if not isinstance(current, str) or current == previous:
                    raise B.Failure('choice_type_not_found', 'The requested type was not found in the type dialog.',
                                    details={'available_types': available})
                available.append(current)
                previous = current
                self.call('goto_next_row', table)
            else:
                raise B.Failure('choice_search_incomplete', 'The type search reached max_rows.')
            buttons = [o for o in objects if o.get('class') == 'Button' and o.get('name') == 'OK']
            if len(buttons) != 1:
                raise B.Failure('choice_type_unavailable', 'The type selection confirmation is missing or ambiguous.')
            self.call('click', buttons[0])
            self.window = self.S._cell_window(self.c)
            if self.window == self.origin:
                raise B.Failure('choice_not_opened', 'Type selected, but no choice window opened. Inspect the source field.')
            objects = self.objects()
            if any(o.get('name') == 'TypeTree' for o in objects):
                raise B.Failure('choice_type_unconfirmed', 'The type dialog did not close.')
        table = self.table_for(objects, choice_table)
        row = self.locate(table, match, value, choice_column)
        self.stage = 'choose_row'
        self.active(self.window)
        self.selection_sent = True
        self.call('choose_row', table)
        self.result.update(method='choice_form', matched_row=row, scope='selectable_rows')
        # Choosing a folder or a row in an unrelated list may open a card instead.
        self.active(self.origin)
        if any(o.get('key') == self.window for o in self.S._read_open_windows(self.c)):
            raise B.Failure('choice_not_completed', 'The choice window is still open. Inspect it before continuing.')

    def verify(self, value, expected):
        self.stage = 'verify'
        self.active(self.origin)
        B.live(self.S, self.c, self.source)
        if self.result.get('method') == 'dropdown' and self.call('drop_list_is_open', self.source).get('open') is not False:
            raise B.Failure('choice_not_completed', 'The drop-down is still open or its state is unknown.')
        actual = self.field_text()
        self.result['value_after'] = actual
        accepted = B.read_property(self.S, self.c, self.source, 'presentation')
        self.result['presentation'] = accepted
        accepted_expected = self.accepted_expected if self.accepted_expected is not None else actual
        if not accepted or accepted not in (accepted_expected, actual):
            raise B.Failure('value_not_confirmed', 'The accepted value does not confirm the selected field text.')
        wanted = expected if expected is not None else value
        displayed = (wanted, self.accepted_expected) if expected is None and self.accepted_expected is not None else (wanted,)
        if wanted is not None and actual not in displayed:
            raise B.Failure('value_not_confirmed', 'The field text differs from the requested value.', details={'expected': wanted})
        if wanted is None and not actual:
            raise B.Failure('value_not_confirmed', 'The selected field is still empty.')
        self.S._resolved_text_input(self.c, self.source['key'], True)
        self.result.update(ok=True, selected=True, verified=True,
                           verification=('exact_text' if actual == wanted else 'selected_item_value')
                           if wanted is not None else 'choice_closed_and_value_read',
                           changed=actual != self.result['value_before'] if self.result['value_before'] is not None else None)


def validate_options(*, value=None, match=None, choice_table=None, choice_column=None,
                     data_type=None, expected=None, max_rows=500, timeout=180):
    out = dict(ok=False, selected=False, verified=False)
    if (value is None) == (match is None) or (value is not None and (type(value) is not str or not value)):
        return dict(out, code='invalid_selection', error='Supply either a nonempty value or match={column title: exact text}.')
    if match is not None and (not isinstance(match, dict) or not 1 <= len(match) <= 32
                             or any(type(k) is not str or not k or type(v) is not str for k, v in match.items())):
        return dict(out, code='invalid_selection', error='match requires 1 to 32 column titles and string values.')
    if any(v is not None and (type(v) is not str or not v) for v in (choice_table, choice_column, data_type, expected)) or (match is not None and choice_column is not None):
        return dict(out, code='invalid_selection', error='Optional names and expected must be nonempty text; choice_column applies only to value.')
    if type(max_rows) is not int or not 1 <= max_rows <= 10000:
        return dict(out, code='invalid_row_limit', error='max_rows must be an integer from 1 to 10000.')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        return dict(out, code='invalid_timeout', error='timeout must be greater than 0 and at most 3600 seconds.')


def run(S, c, key, handle, *, value=None, match=None, choice_table=None, choice_column=None,
        data_type=None, expected=None, max_rows=500, timeout=180):
    failure = validate_options(value=value, match=match, choice_table=choice_table,
                               choice_column=choice_column, data_type=data_type, expected=expected,
                               max_rows=max_rows, timeout=timeout)
    if failure:
        return dict(failure, target=key)
    selector = Selector(S, c, key, handle, max_rows)
    deadline = getattr(c, '_io_deadline', None)
    c._io_deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + timeout)
    try:
        selector.prepare()
        if match is not None or choice_table or choice_column or data_type or not selector.dropdown(value):
            selector.form(value, match, choice_table, choice_column, data_type)
        selector.verify(value, expected)
        return selector.result
    except Exception as exc:
        failure = (dict(code='selection_timeout', error='Selection timed out. Inspect the field and open choice window before retrying.')
                   if isinstance(exc, TimeoutError) else B.error(exc))
        return dict(selector.result, **failure, ok=False, selected=None if selector.selection_sent else False,
                    stage=selector.stage, selection_sent=selector.selection_sent)
    finally:
        c._io_deadline = deadline
        S._refs.for_client(c).remember(key, handle)
