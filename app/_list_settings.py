"""Read and edit standard list settings through the test client UI."""
from collections import Counter
import math
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, TypeAdapter, ValidationError

import _field_batches as B


class Filter(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: Annotated[StrictStr, Field(min_length=1)]
    comparison: Literal['eq', 'ne', 'gt', 'ge', 'lt', 'le', 'contains', 'not_contains', 'filled', 'not_filled'] = 'eq'
    value: StrictStr = ''
    enabled: StrictBool = True


class Order(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: Annotated[StrictStr, Field(min_length=1)]
    direction: Literal['asc', 'desc'] = 'asc'
    enabled: StrictBool = True


Filters = Annotated[list[Filter], Field(max_length=100)]
Orders = Annotated[list[Order], Field(max_length=100)]
Section = Literal['filters', 'orders']
COMPARISONS = {
    'eq': ('Равно', 'Equal to'), 'ne': ('Не равно', 'Not equal to'),
    'gt': ('Больше', 'Greater than'), 'ge': ('Больше или равно', 'Greater than or equal to'),
    'lt': ('Меньше', 'Less than'), 'le': ('Меньше или равно', 'Less than or equal to'),
    'contains': ('Содержит', 'Contains'), 'not_contains': ('Не содержит', 'Does not contain'),
    'filled': ('Заполнено', 'Filled'), 'not_filled': ('Не заполнено', 'Not filled'),
}


def require(result):
    if not result.get('ok'):
        raise B.Failure(result.get('code', 'list_settings_failed'),
                        result.get('error', 'The list settings action was refused.'), details={'cause': result})
    return result


def address(obj):
    return {k: obj[k] for k in ('key', 'handle')}


def fingerprint(rows):
    return Counter(tuple(sorted(row.items())) for row in rows)


class Editor:
    def __init__(self, S, c, key, handle, limit):
        self.S, self.c = S, c
        self.target = dict(key=key, handle=handle)
        self.limit = limit
        self.form = None
        self.window = None
        self.stage = 'open'
        self.changes_started = False
        self.completed = []
        self.objects = []
        self.opened = False

    def call(self, action, obj, **kw):
        return require(getattr(self.S, 'tc_' + action)(**address(obj), **kw))

    def one(self, name):
        matches = [o for o in self.objects if o.get('name') == name]
        if len(matches) != 1:
            raise B.Failure('list_settings_unavailable', f'The standard settings control is missing or ambiguous: {name}.')
        return matches[0]

    def refresh(self):
        if self.S._window(self.c)['key'] != self.window:
            raise B.Failure('window_changed', 'Another window is active. Inspect it before continuing.')
        B.live(self.S, self.c, self.form)
        self.objects = self.S._search_objects(self.c, self.form['key'])

    def exists(self, form):
        try:
            obj = self.S._ref_live_object(self.c, form['key'])
        except self.S.tc1c.OperationError as exc:
            if exc.code == 'target_unavailable': return False
            raise
        return bool(obj and obj.get('handle') == form['handle'])

    def open(self):
        target = B.live(self.S, self.c, self.target)
        if target.get('class') != 'Table':
            raise B.Failure('invalid_table', 'Address the list table.')
        remembered = getattr(self.c, '_list_settings_editor', None)
        if remembered and self.exists(remembered['form']):
            if remembered['target'] != self.target:
                raise B.Failure('list_settings_busy', 'Settings for another list are already open.')
            self.form, self.window = remembered['form'], remembered['window']
            self.refresh()
            B.ensure_pending(self.S, self.c, None)
            current = self.S._input_current(self.c, self.form)
            tablekey = current if current and self.S._key_class(current) == 'Table' else (
                self.S._table_owner(current) if current else None)
            if tablekey:
                table = next((o for o in self.objects if o['key'] == tablekey), None)
                if not table or self.S._cell_flag(self.c, self.S.G.CURRENT_MODE_IS_EDIT, **address(table)) is not False:
                    raise B.Failure('row_edit_pending', 'Finish or cancel editing the settings row before continuing.')
            return
        self.c._list_settings_editor = None
        B.ensure_pending(self.S, self.c, None)
        self.S._cell_ready(self.c, self.target['key'], self.target['handle'])
        if self.S._cell_flag(self.c, self.S.G.CURRENT_MODE_IS_EDIT, **address(target)) is not False:
            raise B.Failure('row_edit_pending', 'Finish editing the list row before opening its settings.')
        owner = B.owner(self.S, target['key'])
        if not owner or self.S._window(self.c)['key'] != owner.split('.ManagedForm[', 1)[0]:
            raise B.Failure('window_changed', 'Activate the window containing the list first.')
        objects = self.S._search_objects(self.c, owner)
        names = tuple(target.get('name', '') + suffix for suffix in ('НастроитьСписок', 'НастройкаСписка', 'ListSettings'))
        buttons = [o for o in objects if o.get('class') == 'Button' and o.get('name') in names]
        if not buttons:
            tables = [o for o in objects if o.get('class') == 'Table']
            if len(tables) != 1:
                raise B.Failure('ambiguous_list_settings', 'The form command cannot be bound to a single list table.')
            buttons = [o for o in objects if o.get('class') == 'Button'
                       and o.get('name') in ('ФормаНастройкаСписка', 'FormListSettings')]
        if len(buttons) != 1:
            raise B.Failure('list_settings_unavailable', 'The list has no unambiguous standard settings command.')
        self.call('activate', target)
        self.call('click', buttons[0])
        window = self.S._window(self.c)['key']
        objects = self.S._search_objects(self.c, window)
        finish = [o for o in objects if o.get('name') in ('ФормаЗакончитьРедактирование', 'FormFinishEditing')]
        if len(finish) != 1 or window == owner.split('.ManagedForm[', 1)[0]:
            raise B.Failure('list_settings_unavailable', 'The standard list settings form did not open.')
        formkey = B.owner(self.S, finish[0]['key'])
        forms = [o for o in objects if o['key'] == formkey]
        if len(forms) != 1:
            raise B.Failure('list_settings_unavailable', 'The settings form could not be identified.')
        self.form, self.window, self.objects = forms[0], window, objects
        self.opened = True
        self.c._list_settings_editor = dict(target=self.target, form=self.form, window=self.window)

    def section(self, section):
        self.refresh()
        suffix = 'Отбор' if section == 'filters' else 'Порядок'
        tables = [o for o in self.objects if o.get('class') == 'Table'
                  and o.get('name', '').startswith('КомпоновщикНастроекПользовательскиеНастройки')
                  and o['name'].endswith(suffix)]
        if len(tables) != 1:
            raise B.Failure('list_settings_unavailable', f'The standard {section} section is missing or ambiguous.')
        table = tables[0]
        prefix = table['name'][:-len(suffix)]
        self.call('activate', self.one(prefix))
        self.refresh()
        return self.one(table['name']), prefix

    def columns(self, table, available=False):
        suffixes = ({'field': 'Заголовок'} if available else
                    {'field': 'ЛевоеЗначение', 'comparison': 'ВидСравнения', 'value': 'ПравоеЗначение',
                     'enabled': 'Использование', 'group_type': 'ТипГруппы', 'caption': 'Представление'}
                    if table['name'].endswith('Отбор') else
                    {'field': 'Поле', 'direction': 'ТипУпорядочивания', 'enabled': 'Использование'})
        return {k: self.one(table['name'] + suffix) for k, suffix in suffixes.items()}

    def current(self, table, columns):
        result = {}
        for name, col in columns.items():
            value = self.S._obs_cell(self.c, table['key'], table['handle'], col['name'])
            if value is None:
                raise B.Failure('list_settings_unreadable', 'A settings cell could not be read.')
            result[name] = value
        return result

    def rows(self, table, available=False):
        # SelectAllRows determines the count, not the ordering. Walk exactly that
        # many rows; identical neighbouring conditions are distinct entries.
        columns = self.columns(table, available)
        # An empty standard sorting table reports one blank selected row on
        # 8.3.27; searching that synthetic row while clearing selection can hang.
        self.call('goto_first_row', table)
        if available or not table['name'].endswith('Отбор'):
            try:
                empty = self.current(table, {'field': columns['field']})['field'] == ''
            except self.S.tc1c.OperationError as exc:
                if exc.code != 'invalid_element_state': raise
                # GetCellText refuses an empty standard table. Confirm that the
                # selection has no populated row without issuing a row search.
                self.call('select_all_rows', table)
                selected = self.call('get_selected_rows', table)
                if 'rows' not in selected or any(any(row.values()) for row in selected['rows']): raise
                self.call('goto_first_row', table)
                empty = True
            if empty:
                return []
        read = self.call('read_rows', table, max_rows=self.limit)
        count = read.get('row_count')
        if type(count) is not int or read.get('truncated') or count > self.limit:
            raise B.Failure('list_settings_limit', f'The settings exceed max_rows={self.limit}.')
        result = []
        if count:
            self.call('goto_first_row', table)
            for i in range(count):
                if i: self.call('goto_next_row', table)
                result.append(self.current(table, columns))
        # Compare against the independent selection read, so missed navigation,
        # reordered rows and external edits cannot silently change the row set.
        expected = [{name: r[col['title']] for name, col in columns.items()} for r in read['rows']]
        if fingerprint(result) != fingerprint(expected):
            raise B.Failure('list_settings_changed', 'The settings changed while being read.')
        return result

    def read_section(self, section):
        table, _ = self.section(section)
        if section == 'filters':
            self.call('goto_first_row', table)
            self.call('expand', table, subordinates=True)
        return self.rows(table)

    def fields(self, section):
        _, prefix = self.section(section)
        table = self.one(prefix + 'ДоступныеПоляТаблица')
        return self.rows(table, available=True)

    def locate(self, table, wanted, available=False):
        rows = self.rows(table, available)
        indexes = [i for i, r in enumerate(rows) if r == wanted]
        if len(indexes) != 1:
            raise B.Failure('ambiguous_settings_row', 'The settings row is missing or ambiguous.')
        self.call('goto_first_row', table)
        for _ in range(indexes[0]): self.call('goto_next_row', table)
        if self.current(table, self.columns(table, available)) != wanted:
            raise B.Failure('list_settings_changed', 'The requested row is no longer current.')

    def clear(self, section):
        table, _ = self.section(section)
        rows = self.read_section(section)
        # The filter tree's first row is its non-removable root, not a condition.
        root_count = 1 if section == 'filters' else 0
        if root_count and (not rows or rows[0]['field'] or rows[0]['comparison']):
            raise B.Failure('list_settings_unreadable', 'The filter tree root could not be identified.')
        while len(rows) > root_count:
            self.call('goto_first_row', table)
            if root_count: self.call('goto_next_row', table)
            self.changes_started = True
            self.call('delete_rows', table, scope='current')
            self.refresh()
            new = self.read_section(section)
            if len(new) >= len(rows):
                raise B.Failure('list_settings_delete_failed', 'The settings row was not removed.')
            rows = new

    def set_text(self, table, column, text):
        self.changes_started = True
        result = self.call('set_row_values', table, cells=[{'column': column['name'], 'text': text}])
        if result.get('final_verified') is not True:
            raise B.Failure('list_settings_value_unconfirmed', 'The entered value was not confirmed.', details={'cause': result})
        self.refresh()

    def begin_edit(self, table):
        mark = self.S._native_composite_begin(self.c, 'list_settings_change_row')
        result = {}
        try:
            result = self.call('change_row', table)
        except Exception as exc:
            result.update(ok=False, **B.error(exc))
            raise
        finally:
            B.finish_record(self.S, self.c, mark, 'list_settings_change_row', result)
        require(result)

    def set_choice(self, table, column, text):
        if self.current(table, {'value': column})['value'] == text:
            return
        self.call('activate', column)
        current = self.call('get_current_item', table)
        if not any(o.get('key') == column['key'] for o in current.get('item', [])):
            raise B.Failure('list_settings_changed', 'The requested settings column did not become current.')
        mode = self.S._cell_flag(self.c, self.S.G.CURRENT_MODE_IS_EDIT, **address(table))
        if mode is False: self.begin_edit(table)
        elif mode is not True:
            raise B.Failure('list_settings_unreadable', 'The settings row edit mode is unknown.')
        self.call('open_drop_list', column)
        choices = self.call('get_choice_list', column).get('presentations', [])
        indexes = [i for i, value in enumerate(choices) if value == text]
        if len(indexes) != 1:
            self.call('close_drop_list', column)
            raise B.Failure('list_settings_choice_unavailable', f'The field does not offer {text}.',
                            details={'available_choices': choices})
        # InputText("Больше") also matches "Больше или равно" and can leave the
        # row in edit mode. Choose the exact item of the freshly opened list.
        self.call('choose_from_drop_list', column, value=indexes[0])
        if self.S._cell_flag(self.c, self.S.G.CURRENT_MODE_IS_EDIT, **address(table)) is True:
            self.call('end_edit_row', table)
        self.refresh()
        if (self.S._cell_flag(self.c, self.S.G.CURRENT_MODE_IS_EDIT, **address(table)) is not False
                or self.current(table, {'value': column})['value'] != text):
            raise B.Failure('list_settings_value_unconfirmed', 'The chosen value was not confirmed.')

    def add(self, section, entry):
        table, prefix = self.section(section)
        before = self.read_section(section)
        available = self.one(prefix + 'ДоступныеПоляТаблица')
        self.locate(available, {'field': entry.field}, available=True)
        # Put new filters at the root, not inside whichever group was last read.
        if section == 'filters': self.call('goto_first_row', table)
        self.changes_started = True
        self.call('click', self.one(prefix + 'ДоступныеПоляВыбрать'))
        table, _ = self.section(section)
        after = self.read_section(section)
        added = fingerprint(after) - fingerprint(before)
        if len(after) != len(before) + 1 or sum(added.values()) != 1 or fingerprint(before) - fingerprint(after):
            raise B.Failure('list_settings_add_failed', 'Adding the field did not create exactly one settings row.')
        new = dict(next(iter(added)))
        self.locate(table, new)
        columns = self.columns(table)
        if new['field'] != entry.field:
            raise B.Failure('list_settings_changed', 'Another field was added to the settings.')
        english = columns['field'].get('title') == 'Field'
        if section == 'filters':
            self.set_choice(table, columns['comparison'], COMPARISONS[entry.comparison][english])
            if entry.comparison not in ('filled', 'not_filled'):
                self.set_text(table, columns['value'], entry.value)
        else:
            direction = (('По возрастанию', 'Ascending') if entry.direction == 'asc' else ('По убыванию', 'Descending'))
            self.set_choice(table, columns['direction'], direction[english])
        enabled = columns['enabled']
        self.call('activate', enabled)
        require(B.apply_checkbox(self.S, self.c, dict(enabled, checked=entry.enabled)))
        if self.S._cell_flag(self.c, self.S.G.CURRENT_MODE_IS_EDIT, **address(table)) is True:
            self.call('end_edit_row', table)
        row = self.current(table, columns)
        if B.checkbox_state(row['enabled']) != entry.enabled:
            raise B.Failure('list_settings_value_unconfirmed', 'The enabled state was not confirmed.')
        self.completed.append(dict(section=section, index=sum(x['section'] == section for x in self.completed), row=row))

    def close(self, apply):
        self.stage = 'apply' if apply else 'close'
        self.refresh()
        name = 'ФормаЗакончитьРедактирование' if apply else 'ФормаОтменитьРедактирование'
        self.call('click', self.one(name))
        if self.exists(self.form):
            raise B.Failure('list_settings_still_open', 'The settings form is still open. Check its messages.')
        self.c._list_settings_editor = None
        self.form = None


def run(S, c, key, handle, action, *, filters=None, orders=None, replace=False,
        section='filters', max_rows=500, timeout=180):
    result = dict(ok=False, target=key)
    try:
        if type(max_rows) is not int or not 1 <= max_rows <= 10000:
            raise B.Failure('invalid_row_limit', 'max_rows must be an integer from 1 to 10000.')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise B.Failure('invalid_timeout', 'timeout must be greater than 0 and at most 3600 seconds.')
        if section not in ('filters', 'orders') or type(replace) is not bool:
            raise B.Failure('invalid_list_settings', 'Use section=filters/orders and a boolean replace.')
        filters = TypeAdapter(Filters).validate_python(filters) if filters is not None else None
        orders = TypeAdapter(Orders).validate_python(orders) if orders is not None else None
        if action == 'set' and filters is None and orders is None:
            raise B.Failure('invalid_list_settings', 'Supply filters or orders; an empty array with replace=true clears that section.')
        if filters and any(f.comparison in ('filled', 'not_filled') and f.value for f in filters):
            raise B.Failure('invalid_list_settings', 'filled and not_filled do not take a value.')
    except (B.Failure, ValidationError) as exc:
        return dict(result, **(B.error(exc) if isinstance(exc, B.Failure) else
                              dict(code='invalid_list_settings', error=str(exc))))
    editor = Editor(S, c, key, handle, max_rows)
    deadline = getattr(c, '_io_deadline', None)
    c._io_deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + timeout)
    try:
        editor.open()
        editor.stage = 'read'
        if action == 'fields':
            result.update(section=section, fields=editor.fields(section), scope='visible_available_fields')
        elif action == 'get':
            result.update(filters=editor.read_section('filters'), orders=editor.read_section('orders'),
                          filter_layout='expanded_tree_rows')
        else:
            # Resolve every requested field before the first modification.
            for name, entries in (('filters', filters), ('orders', orders)):
                if entries:
                    fields = Counter(r['field'] for r in editor.fields(name))
                    for entry in entries:
                        if fields[entry.field] != 1:
                            raise B.Failure('list_settings_field_unavailable',
                                            f'The available field is missing or ambiguous: {entry.field}.')
            for name, entries in (('filters', filters), ('orders', orders)):
                if entries is None: continue
                editor.stage = name
                if replace: editor.clear(name)
                for entry in entries: editor.add(name, entry)
            result.update(filters=editor.read_section('filters'), orders=editor.read_section('orders'),
                          filter_layout='expanded_tree_rows', completed=editor.completed)
        if action == 'set' or editor.opened:
            editor.close(apply=action == 'set')
        return dict(result, ok=True, settings_open=editor.form is not None,
                    **({'applied': True} if action == 'set' else {}))
    except Exception as exc:
        error = (dict(code='list_settings_timeout', error='Timed out while working with list settings. Inspect the remaining settings before retrying.')
                 if isinstance(exc, TimeoutError) else B.error(exc))
        return dict(result, **error, stage=editor.stage, settings_open=editor.form is not None,
                    changes_started=editor.changes_started, completed=editor.completed,
                    **({'applied': None if editor.changes_started or editor.stage == 'apply' else False} if action == 'set' else {}))
    finally:
        c._io_deadline = deadline
        # Reading the settings can evict the source table from a small registry.
        # Keep its original handle so the returned ref still checks identity.
        S._refs.for_client(c).remember(key, handle)
