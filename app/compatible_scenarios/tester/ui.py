"""Tester UI semantics over the shared Testpilot handlers.

Behavior references: Tester CommonModules/Extender, Fields, Forms and
DataProcessors/Assertions. Unsupported object kinds fail explicitly.
"""
from dataclasses import dataclass
from compatible_scenarios.shared.testing import METHODS, OBJECT_OWNER, UIObject, MainWindow, Application, TESTED_CLASSES, invoke_element
from decimal import Decimal, InvalidOperation
from datetime import datetime
import operator
import re
import time
from compatible_scenarios.shared.bsl.language import Failure, Structure, Map, TypeValue, NULL, boolean, string, value_in
from compatible_scenarios.shared.bsl.language import COLLECTIONS, ValueList, value_equal


def aliases(rows):
    result = {}
    for row in rows:
        canonical, *names = row.split()
        for name in (canonical, *names): result[name.casefold()] = canonical
    return result


FUNCTIONS = aliases([
    'get Получить', 'fetch Взять', 'set Установить', 'click Нажать', 'activate Фокус',
    'clear Очистить', 'choose Выбрать', 'pick Подобрать', 'with Здесь', 'findform НайтиФорму',
    'commando Коммандос', 'close Закрыть', 'closeall ЗакрытьВсе ЗакрытьВсё',
    'check Проверить', 'assert Заявить', 'checkerrors ПроверитьОшибки',
    'getmessages ПолучитьСообщения', 'connect Подключить',
    'systemvariable ПеременнаяСреды',
    'put Ввести', 'entervalue ВнестиЗначение', 'findmessages НайтиСообщения',
    'gettablecontent ПолучитьСодержимоеТаблицы',
    'openmenu Меню', 'getwindow ПолучитьОкно', 'getlinks ПолучитьСсылки', 'getmainmenu ПолучитьГлавноеМеню',
    'getspreadsheetcontent ПолучитьСодержимоеТабличногоДокумента',
    'getactivewindowcontrols ПолучитьЭлементыАктивногоОкна',
    'getactivewindowchanges ПолучитьИзмененияОкна',
    'screenshot Снимок', 'getscreenshot ПолучитьСнимок',
    'gotorow КСтроке',
    'gotofirstrow ПерейтиКПервойСтроке', 'gotolastrow ПерейтиКПоследнейСтроке',
    'gotonextrow ПерейтиКСледующейСтроке', 'gotopreviousrow ПерейтиКПредыдущейСтроке',
    'next Далее', 'currenttab ТекущаяВкладка', 'waiting Дождаться',
    'openvalueininputfield ОткрытьЗначениеВПолеВвода',
    'checkstate ПроверитьСтатус', 'checktable ПроверитьТаблицу',
    'expandtreerow РаскрытьСтрокуДерева', 'collapsetreerow СвернутьСтрокуДерева',
    'gooneleveldown ПерейтиНаУровеньНиже', 'goonelevelup ПерейтиНаУровеньВыше',
])
ASSERTIONS = aliases([
    'not_ Не_', 'istrue ЭтоИстина', 'isfalse ЭтоЛожь', 'equal Равно', 'notequal НеРавно',
    'greater Больше', 'greaterorequal БольшеИлиРавно', 'less Меньше', 'lessorequal МеньшеИлиРавно',
    'isundefined ЭтоНеопределено',
    'isnull ЭтоNull ЕстьNull',
    'filled Заполнено', 'empty Пусто', 'exists Существует', 'between Между',
    'contains Содержит', 'has ИмеетДлину Вмещает', 'that',
])


@dataclass
class Assertion:
    value: object
    details: str = ''
    negate: bool = False


class Adapter:
    def __init__(self, host):
        self.host, self.current = host, None
        self.window_baseline = None
        self.window_tooltips = {}

    def window_close_revision(self, key):
        return self.host.window_close_revisions.get((id(self.host.client), key), 0)

    def remember_closed_window(self, key):
        key = key.split('.ManagedForm[', 1)[0]
        # A close invalidates contexts saved before it, not later forms at the same address.
        self.host.window_close_revisions[id(self.host.client), key] = self.window_close_revision(key) + 1

    def check_owner(self, obj):
        owner = getattr(obj, 'owner', None)
        if owner is not None and owner is not self.host.client:
            raise Failure('connection_mismatch', 'This object belongs to another client. Switch back or find an object in the current client.')

    def action(self, action, obj=None, **kw):
        if action == 'get_user_message_texts':
            # Native reads must not reopen messages already handled by the scenario.
            kw['open_if_closed'] = False
        if obj is not None:
            self.check_owner(obj)
            if not isinstance(obj, UIObject): raise Failure('scenario_failed', 'A tested UI object was expected.')
            # Do not silently redirect a saved object to a replacement with the same key.
            live = self.host.live(obj.data['key'])
            if not live or live.get('handle') != obj.data.get('handle'):
                raise Failure('element_unavailable', 'The scenario element is no longer available.')
            if action != 'find_objects':
                kw.update(key=obj.data['key'])
                if action not in ('close_window', 'get_child_objects'): kw['handle'] = obj.data.get('handle')
        result = self.host.action(action, **kw)
        if action == 'close_window' and result.get('ok') is True:
            closed = result.get('closed') or kw.get('key')
            if closed: self.remember_closed_window(closed)
        return result

    def form(self, name=None):
        if isinstance(name, UIObject):
            self.check_owner(name)
            return name
        if name is None:
            window = self.action('get_active_window')
            # get_active_window returns the window itself, not an envelope.
            root = window.get('key')
            result = self.action('find_objects', cls='ManagedForm', root_key=root)
        else:
            from compatible_scenarios.tester.windows import find_window
            window = find_window(self, name)
            if window is not None:
                if window.data.get('class') == 'MainFrame':
                    raise Failure('element_unavailable', 'The main window is not a working form.')
                result = self.action('find_objects', cls='ManagedForm', root_key=window.data['key'])
            else:
                result = self.action('find_objects', cls='ManagedForm', title=name, scope='application')
        objects = result['objects']
        if not objects: raise Failure('element_unavailable', f'Form {name!r} was not found.')
        return UIObject(objects[0])

    def focus(self, obj):
        key = obj.data['key']
        if '.CI.' in key or key.endswith('.CI'): return
        table = self.table(obj) if obj.data.get('class') != 'Additional' else None
        if table is not None:
            rows = self.action('get_selected_rows', table).get('rows')
            if not isinstance(rows, list): raise Failure('state_unavailable', 'The table selection is unknown.')
            if not rows: self.action('goto_first_row', table, toggle_selection=False)
        text_field = obj.data.get('type') == 'InputField'
        opened = self.action('drop_list_is_open', obj).get('open') if text_field else None
        if text_field and type(opened) is not bool:
            raise Failure('state_unavailable', 'The drop-list state is unknown.')
        match = re.match(r'^.+?\.ManagedForm\[[^\]]+\]', key)
        current = []
        if match and key != match[0]:
            owner = self.host.live(match[0])
            if owner:
                current = self.action('get_current_element', UIObject(owner)).get('item', [])
                if table is not None and any(x.get('key') == table.data['key'] for x in current):
                    current = self.action('get_current_item', table).get('item', [])
        if not any(x.get('key') == key and x.get('handle') == obj.data.get('handle') for x in current):
            self.action('activate', obj)
        if opened is False:
            try:
                self.action('close_drop_list', obj)
            except Failure as exc:
                if not (self.platform_refusal(exc) or exc.result.get('code') in (
                        'target_unavailable', 'target_hidden', 'client_busy',
                        'action_failed', 'element_unavailable')):
                    raise

    def field(self, name, source=None, kind=None, focus=False):
        if isinstance(name, UIObject):
            if focus: self.focus(name)
            return name
        if not isinstance(name, str): raise Failure('scenario_failed', 'Field name must be a string.')
        root = self.form(source) if source is not None else (self.current or self.form())
        kinds = {'field': 'EditField', 'поле': 'EditField', 'table': 'Table', 'таблица': 'Table',
                 'button': 'Button', 'кнопка': 'Button', 'group': 'Group', 'группа': 'Group',
                 'decoration': 'Decoration', 'декорация': 'Decoration'}
        cls = None if kind is None else kinds.get(str(kind).casefold())
        if kind is not None and cls is None: raise Failure('unsupported_scenario', f'Unsupported field type {kind!r}.')
        for part in re.split(r'(?<!\\)/', name):
            part = part.strip().replace('\\/', '/')
            area = None
            if '[' in part or ']' in part:
                match = re.fullmatch(r'(.+?)\s*\[([^\[\]]+)\]', part)
                if match is None: raise Failure('scenario_failed', f'Invalid field selector {part!r}.')
                part, area = match[1].rstrip(), match[2].strip()
            params = {'name': part[1:]} if part.startswith(('!', '#')) else {'title': part}
            found = self.action('find_objects', root, root_key=root.data['key'], **params, **({'cls': cls} if cls else {}))
            if 'title' in params and len(found['objects']) > 1:
                matches = ', '.join(f"{o.get('name')!r} ({o.get('class')}"
                                    + (f" / {o['type']}" if o.get('type') else '') + ')'
                                    for o in found['objects'])
                self.host.message(f"Ambiguous field selector {part!r}: {len(found['objects'])} matches: {matches}. "
                                  f"Using the first: {found['objects'][0].get('name')!r}. Specify !name or a group path.")
            if not found['objects'] and 'title' in params:
                found = self.action('find_objects', root, root_key=root.data['key'], name=part, **({'cls': cls} if cls else {}))
            if not found['objects']: raise Failure('element_unavailable', f'Field {part!r} was not found.')
            root = UIObject(found['objects'][0], root, area)
            if area is not None and root.data.get('type') not in ('SpreadsheetDocumentField', 'LabelField') and root.data.get('class') != 'Decoration':
                table = self.table(root)
                if table is None: raise Failure('scenario_failed', 'A row selector requires a table column.')
                self.locate_row(table, area)
            if focus: self.focus(root)
            if focus and area is not None and root.data.get('type') == 'SpreadsheetDocumentField':
                self.action('set_current_area', root, address=area)
        return root

    def locate_row(self, table, row):
        if not re.fullmatch(r'[1-9]\d*', row): raise Failure('scenario_failed', 'Table row numbers start at 1.')
        special = self.host.special_fields()
        if not isinstance(special, Structure) or special.actual('LineNo') not in special:
            raise Failure('scenario_failed', 'SpecialFields must contain LineNo.')
        column = special['LineNo']
        if not isinstance(column, str) or not column.strip():
            raise Failure('scenario_failed', 'SpecialFields.LineNo must be a nonempty column title.')
        editing = self.action('current_mode_is_edit', table)['edit_mode']
        if editing is None: raise Failure('edit_state_unavailable', 'The row edit mode is unknown.')
        if editing: self.action('end_edit_row', table)
        else: self.action('activate', table)
        self.action('goto_first_row', table, toggle_selection=False)
        columns = self.action('find_objects', table, root_key=table.data['key'], title=column)['objects']
        if columns and self.action('is_visible', UIObject(columns[0], table))['visible']:
            found = self.action('goto_row', table, fields={column: row}, direction='down').get('found')
            if found is not True:
                raise Failure('row_not_found' if found is False else 'state_unavailable',
                              f'Could not locate row {row} in column {column!r}.')
        else:
            for _ in range(int(row)-1): self.action('goto_next_row', table, toggle_selection=False)
        if editing: self.action('change_row', table)

    def goto_row(self, name, column, value, from_start=True, source=None):
        table = self.field(name, source, 'table')
        editing = self.action('current_mode_is_edit', table)['edit_mode']
        if editing is None: raise Failure('edit_state_unavailable', 'The row edit mode is unknown.')
        if editing: self.action('end_edit_row', table)
        if from_start is None or boolean(from_start): self.action('goto_first_row', table)
        if not isinstance(column, str): raise Failure('scenario_failed', 'The column must be a name or title.')
        field = self.field(column, table)
        title = field.data['title'] if column.startswith(('!', '#')) else column
        error = None
        try:
            found = self.action('goto_row', table, fields={title: value})['found']
            if found is True: return True
        except Failure as exc:
            if exc.result['code'] in ('scenario_timeout', 'connection_closed', 'scenario_limit'): raise
            error = exc
        try: return self.fetch(field) == value
        except Failure:
            if error is not None: raise error
            raise

    def table(self, obj):
        if isinstance(obj.parent, UIObject) and obj.parent.data.get('class') == 'Table': return obj.parent
        key = obj.data['key']
        if '.Table[' in key and obj.data.get('class') != 'Table':
            table_key = key[:key.index(']', key.index('.Table[')) + 1]
            data = self.host.live(table_key)
            if data: return UIObject(data)
        return None

    def fetch(self, obj):
        kind = obj.data.get('type')
        if kind == 'SearchStringRepresentation': return self.action('get_edit_text', obj)['text']
        table = self.table(obj)
        if table:
            from compatible_scenarios.tester.tables import plain_cell_text
            return plain_cell_text(self.action('get_cell_text', table, column=obj.data['name'])['text'])
        if kind == 'SpreadsheetDocumentField': return self.action('get_area_text', obj, area=obj.area)['text']
        if kind == 'CheckBoxField':
            try:
                text = self.action('get_data_presentation', obj)['presentation']
                value = str(text).strip().casefold()
                if value in ('да', 'yes', 'true', 'истина'): return True
                if value in ('нет', 'no', 'false', 'ложь'): return False
            except Failure as exc:
                if not self.operation_refusal(exc): raise
            try: return self.action('get_text', obj)['text']
            except Failure as exc:
                if not self.operation_refusal(exc): raise
                return ''
        try:
            if kind in ('InputField', 'RadioButtonField', 'LabelField') or obj.data.get('class') == 'Decoration':
                return self.action('get_text', obj)['text']
            return self.action('get_data_presentation', obj)['presentation']
        except Failure as exc:
            if not self.operation_refusal(exc): raise
            return ''

    def input_value(self, obj, value, choose=False, test=False):
        if type(value) is datetime:
            from compatible_scenarios.tester.regional import for_host
            text = for_host(self.host, value, for_input=True)
        else: text = string(value)
        try: self.action('input_text', obj, text=text, finish=False)
        except Failure as exc:
            if not self.operation_refusal(exc): raise
            readonly = self.action('is_readonly', obj).get('readonly')
            if readonly is not False: raise exc
            opened = self.action('drop_list_is_open', obj).get('open')
            if type(opened) is not bool: raise Failure('state_unavailable', 'The drop-list state is unknown.')
            if not opened: self.action('open_drop_list', obj)
            self.action('execute_choice_from_choice_list', obj, value=text)
            return
        if not choose: return
        try: generated = self.action('wait_for_drop_list_generation', obj).get('generated')
        except Failure as exc:
            # A field without autocomplete can refuse the wait. Transport failures
            # must still stop the scenario; an unreceived answer is not an empty list.
            if not self.operation_refusal(exc): raise
            return
        if type(generated) is not bool: raise Failure('state_unavailable', 'The autocomplete state is unknown.')
        if not generated: return
        opened = self.action('drop_list_is_open', obj).get('open')
        if type(opened) is not bool: raise Failure('state_unavailable', 'The drop-list state is unknown.')
        if not opened: return
        self.action('execute_choice_from_choice_list', obj, value=0)
        if test:
            actual = self.fetch(obj)
            if not isinstance(actual, str): raise Failure('state_unavailable', 'The selected field value could not be read.')
            if actual.lower() != text.lower():
                self.action('clear', obj)
                raise Failure('assertion_failed', f'Expected {text!r} after autocomplete, got {actual!r}.',
                              expected=text, actual=actual)

    def set(self, obj, value, choose=False, test=False):
        kind = obj.data.get('type')
        if kind == 'SearchStringRepresentation':
            delay = self.host.globals['testerdynamiclistsearchwaittime']
            if not isinstance(delay, Decimal) or not delay.is_finite() or delay < 0:
                raise Failure('invalid_timeout', 'TesterDynamicListSearchWaitTime must be a finite nonnegative number.')
            self.action('input_text', obj, text=string(value), finish=False)
            self.host.operation('wait_search_results', {'seconds': float(delay)}, lambda: self.host.pause(delay))
            return obj
        table = self.table(obj)
        started_edit = False
        if table and kind in ('InputField', 'CheckBoxField'):
            mode = self.action('current_mode_is_edit', table)['edit_mode']
            if mode is None: raise Failure('edit_state_unavailable', 'The row edit mode is unknown.')
            if mode is False:
                self.action('change_row', table); started_edit = True
        if kind == 'CheckBoxField':
            wanted = boolean(value)
            current = self.fetch(obj)
            # Fetch exposes table cells as text; only setting needs a boolean state.
            if table and isinstance(current, str):
                presentation = current.strip().casefold()
                if presentation in ('да', 'yes', 'true', 'истина'): current = True
                elif presentation in ('нет', 'no', 'false', 'ложь'): current = False
            if type(current) is not bool:
                raise Failure('unsupported_scenario', f'Unrecognized checkbox presentation {current!r}.')
            if current != wanted: self.action('set_check', obj)
        elif kind == 'RadioButtonField': self.action('select_option', obj, value=value)
        elif kind == 'TrackBarField': self.action('goto_value', obj, percent=value)
        elif kind == 'FormattedDocumentField': self.action('input_html', obj, html=value)
        elif kind == 'InputField': self.input_value(obj, value, choose, test)
        elif kind == 'SpreadsheetDocumentField':
            self.action('begin_edit_current_area', obj)
            self.input_value(obj, value, choose, test)
            self.action('end_edit_current_area', obj)
        else: self.action('input_text', obj, text=string(value), finish=False)
        if started_edit and self.action('current_mode_is_edit', table)['edit_mode']:
            self.action('end_edit_row', table)
        return obj

    def call(self, name, args):
        name = FUNCTIONS[name]
        if name == 'systemvariable':
            import os
            variable = args[0]
            if not isinstance(variable, str) or '\x00' in variable:
                raise Failure('scenario_failed', 'SystemVariable expects an environment variable name without NUL characters.')
            return os.environ.get(variable, '')
        if name in ('getactivewindowcontrols', 'getactivewindowchanges'):
            from compatible_scenarios.tester.context import run
            return run(self, name == 'getactivewindowchanges')
        if name in ('openmenu','getwindow','getlinks','getmainmenu'):
            import compatible_scenarios.tester.windows as windows
            result = {'openmenu':windows.open_menu,'getwindow':windows.window,
                    'getlinks':windows.links,'getmainmenu':windows.main_menu}[name](self,*args)
            if name == 'openmenu': self.window_baseline = None
            return result
        if name in ('put', 'entervalue'):
            obj = self.field(args[0], args[2] if len(args)>2 else None,
                             args[3] if len(args)>3 else None, focus=True)
            test = name == 'entervalue' or (len(args)>4 and boolean(args[4]))
            return self.set(obj, args[1], choose=True, test=test)
        if name == 'gettablecontent':
            from compatible_scenarios.tester.tables import get_table_content
            return get_table_content(self, *args)
        if name == 'waiting': return self.waiting(*args)
        if name == 'next':
            if self.current is None or self.current.data.get('class') not in ('ManagedForm', 'Table'):
                raise Failure('scenario_failed', 'Next requires a form or table selected with With.')
            self.action('goto_next_element' if self.current.data['class'] == 'ManagedForm' else 'goto_next_item', self.current)
            return None
        if name in ('gotofirstrow', 'gotolastrow', 'gotonextrow', 'gotopreviousrow'):
            obj = self.field(args[0], args[1] if len(args)>1 else None, 'table')
            self.finish_row(obj)
            action = {'gotofirstrow':'goto_first_row', 'gotolastrow':'goto_last_row',
                      'gotonextrow':'goto_next_row', 'gotopreviousrow':'goto_previous_row'}[name]
            self.action(action, obj, toggle_selection=False)
            return None
        if name == 'currenttab':
            obj = self.field(*args)
            pages = self.action('get_current_page', obj)['page']
            return UIObject(pages[0], obj) if pages else None
        if name == 'openvalueininputfield':
            obj = self.field(args[0], args[1] if len(args)>1 else None, 'field', focus=True)
            if obj.data.get('type') != 'InputField':
                raise Failure('unsupported_element_type', 'OpenValueInInputField requires an input field.')
            editing = self.begin_row(obj)
            self.action('open_field', obj)
            self.finish_owned_row(editing)
            return None
        if name == 'clear':
            source = self.form(args[1]) if len(args)>1 and args[1] is not None else (self.current or self.form())
            kind = args[2] if len(args)>2 else None
            names = args[0]
            if isinstance(names, str): names = [part.strip() for part in names.split(',') if part]
            elif isinstance(names, UIObject): names = [names]
            else: raise Failure('scenario_failed', 'Clear requires field names separated by commas.')
            if not names or any(isinstance(part,str) and not part for part in names):
                raise Failure('scenario_failed', 'Clear requires at least one field name.')
            for field in names:
                obj = self.field(field, source, kind)
                self.action('activate', obj)
                editing = self.begin_row(obj)
                if obj.data.get('type') == 'ViewStatusRepresentation':
                    items = self.action('get_view_status_item_texts', obj)['items']
                    for index in reversed(range(len(items))): self.action('delete_view_status_item', obj, index=index)
                else: self.action('clear', obj)
                self.finish_owned_row(editing)
            return obj
        if name == 'connect':
            clear = args[0] if args else None
            if clear is not None and type(clear) is not bool:
                raise Failure('scenario_failed', 'Connect ClearErrors must be Boolean or Undefined.')
            port, computer = (args[1:] + [None, None])[:2]
            self.host.connect(port, computer)
            ui = self.host.ui
            if clear is not False:
                error = ui.action('get_current_error').get('error')
                if error is None:
                    try: ui.call('checkerrors', [])
                    except Failure as exc:
                        if exc.result.get('code') != 'application_error': raise
                    else: return None
                ui.call('closeall', [])
            return None
        if name == 'assert': return Assertion(*args)
        if name == 'checktable':
            from compatible_scenarios.tester.tables import check_table
            return check_table(self, *args)
        if name == 'checkstate':
            names, state = args[:2]
            wanted = True if len(args) < 3 or args[2] is None else boolean(args[2])
            source = args[3] if len(args) > 3 else None
            kind = args[4] if len(args) > 4 else None
            states = {'visible':('is_visible','visible'), 'видимость':('is_visible','visible'),
                      'enable':('is_enabled','enabled'), 'доступность':('is_enabled','enabled'),
                      'readonly':('is_readonly','readonly'), 'толькочтение':('is_readonly','readonly')}
            if not isinstance(names, str) or not names.strip() or not isinstance(state,str) or state.casefold() not in states:
                raise Failure('scenario_failed', 'CheckState requires field names and Visible, Enable or ReadOnly.')
            action, slot = states[state.casefold()]
            for field in names.split(','):
                obj = self.field(field.strip(), source, kind)
                actual = self.action(action, obj).get(slot)
                if type(actual) is not bool: raise Failure('state_unavailable', f'The {state} state of {field!r} is unavailable.')
                if actual != wanted:
                    raise Failure('assertion_failed', f'{field.strip()}: {state} expected {wanted}, got {actual}.',
                                  field=field.strip(), state=state, expected=wanted, actual=actual)
            return None
        if name in ('expandtreerow', 'collapsetreerow', 'gooneleveldown', 'goonelevelup'):
            obj = self.field(args[0], args[1] if len(args)>1 else None, 'table')
            self.finish_row(obj)
            if name in ('expandtreerow', 'collapsetreerow'):
                expanded = self.action('is_expanded', obj).get('expanded')
                if type(expanded) is not bool: raise Failure('state_unavailable', 'The table row expansion state is unavailable.')
                wanted = name == 'expandtreerow'
                if expanded != wanted: self.action('expand' if wanted else 'collapse', obj)
            else: self.action('go_one_level_down' if name=='gooneleveldown' else 'go_one_level_up', obj)
            return None
        if name == 'gotorow': return self.goto_row(*args)
        if name == 'commando':
            if not 1 <= len(args) <= 2: raise Failure('scenario_failed', 'Commando expects a link and optional Activate.')
            self.action('execute_command', command=args[0])
            if len(args) == 1 or boolean(args[1]): self.window_baseline = None
            return self.call('with', []) if len(args) == 1 or boolean(args[1]) else None
        if name in ('with', 'findform'):
            obj = self.form(args[0] if args else None)
            if name == 'with':
                if len(args) < 2 or boolean(args[1]): self.action('activate', obj)
                self.current = obj
                from compatible_scenarios.tester.context import reset
                reset(self, obj)
                self.host.globals['currentsource'] = self.host.globals['текущийобъект'] = obj
            return obj
        if name == 'close':
            obj = self.form(args[0]) if args else (self.current or self.form())
            key = obj.data['key'].split('.ManagedForm[', 1)[0]
            self.action('close_window', key=key); self.current = None
            self.window_baseline = None
            return None
        if name == 'closeall':
            from compatible_scenarios.tester.windows import close_all
            result = close_all(self)
            self.window_baseline = None
            return result
        if name in ('screenshot', 'getscreenshot', 'getspreadsheetcontent'):
            from compatible_scenarios.tester.files import screenshot, spreadsheet
            return spreadsheet(self, args) if name == 'getspreadsheetcontent' else screenshot(self, args, name == 'getscreenshot')
        if name in ('checkerrors', 'getmessages', 'findmessages'):
            try: messages = self.action('get_user_message_texts')['messages']
            except Failure as exc:
                if exc.result.get('code') != 'user_messages_unavailable': raise
                messages = []
            if not messages:
                for field in ('Message', 'ErrorInfo'):
                    errors = self.action('find_objects', name=field)['objects']
                    messages = [o['title'] for o in errors if o.get('title') and o.get('name') == field]
                    if messages: break
            if name == 'findmessages':
                from compatible_scenarios.tester.tables import find_messages
                return find_messages(self, args[0], messages)
            if name == 'checkerrors' and messages: raise Failure('application_error', messages[0])
            return messages if name == 'getmessages' else None
        if not args: raise Failure('scenario_failed', f'{name} requires a field.')
        takes_value = name in ('set', 'check', 'pick')
        start = 2 if takes_value else 1
        if len(args) < start or len(args) > start+2: raise Failure('scenario_failed', f'Invalid arguments for {name}.')
        obj = self.field(args[0], args[start] if len(args) > start else None,
                         args[start+1] if len(args) > start+1 else None,
                         focus=name not in ('get', 'fetch', 'check'))
        if name == 'get' and obj.area is not None and obj.data.get('type') == 'SpreadsheetDocumentField':
            self.action('set_current_area', obj, address=obj.area)
            return self.area_field(obj)
        if name in ('get', 'activate'): return obj
        if name == 'fetch': return self.fetch(obj)
        if name == 'set': return self.set(obj, args[1])
        if name == 'check':
            actual, expected = self.fetch(obj), args[1]
            if type(expected) is datetime:
                from compatible_scenarios.shared.bsl.dates import read_presentation
                from compatible_scenarios.tester.regional import format_for
                date_format = format_for(self.host)
                try: actual = read_presentation(actual, date_format)
                except Failure as exc:
                    raise Failure('assertion_failed', 'The field does not contain the expected date.',
                                  expected=string(expected), actual=actual, reason=exc.result['error']) from exc
            if isinstance(expected, Decimal):
                try: actual = Decimal(str(actual).replace('\u00a0', '').replace(' ', '').replace(',', '.') or '0')
                except ArithmeticError: pass
            if actual != expected: raise Failure('assertion_failed', f'Field {args[0]!r}: expected {expected!r}, got {actual!r}.')
            return None
        if name == 'pick':
            editing = self.begin_row(obj)
            opened = self.action('drop_list_is_open', obj).get('open')
            if type(opened) is not bool: raise Failure('state_unavailable', 'The drop-list state is unknown.')
            if not opened: self.action('open_drop_list', obj)
            self.action('execute_choice_from_choice_list', obj, value=args[1])
            self.finish_owned_row(editing)
            return None
        if name == 'choose':
            if obj.data.get('type') == 'SpreadsheetDocumentField':
                self.action('begin_edit_current_area', obj)
            elif obj.data.get('class') != 'Table': self.begin_row(obj)
            if obj.data.get('class') == 'Table': self.action('choose', obj)
            else:
                try: self.action('start_choosing', obj)
                except Failure as exc:
                    # The command completed; a separate choice window is optional.
                    if exc.result.get('code') != 'choice_not_opened': raise
            return obj
        if name == 'click':
            if obj.data.get('class') == 'Group':
                try: self.action('expand', obj)
                except Failure as exc:
                    if not self.platform_refusal(exc): raise
                    try: self.action('collapse', obj)
                    except Failure as collapse_error:
                        if not self.platform_refusal(collapse_error): raise
                return obj
            if obj.data.get('type') == 'LabelField' or obj.data.get('class') == 'Decoration':
                index = 0 if obj.area is None else self.link_index(obj.area)
                try: self.action('click_formatted_string_hyperlink', obj, index=index)
                except Failure as exc:
                    if obj.area is not None or not self.platform_refusal(exc): raise
                    self.action('click', obj)
                return obj
            try:
                self.action('set_check' if obj.data.get('type') == 'CheckBoxField' else 'click', obj)
            except Failure as exc:
                refused = self.platform_refusal(exc) or exc.result.get('code') in (
                    'target_unavailable', 'target_hidden', 'client_busy', 'action_failed')
                if obj.data.get('class') != 'Button' or not refused or not self.button_enabled(obj):
                    raise
            return obj
        raise Failure('unsupported_scenario', f'Unsupported Tester function {name}.')

    def button_enabled(self, obj):
        """Tester Click ignores a refusal only when the button hierarchy is usable."""
        visited = set()
        while True:
            identity = (obj.data.get('key'), obj.data.get('handle'))
            if identity in visited:
                raise Failure('state_unavailable', 'The button parent hierarchy contains a cycle.')
            visited.add(identity)
            enabled = self.action('is_enabled', obj).get('enabled')
            if type(enabled) is not bool:
                raise Failure('state_unavailable', 'The button hierarchy availability is unknown.')
            if not enabled: return False
            if obj.data.get('class') == 'ManagedForm': return True
            visible = self.action('is_visible', obj).get('visible')
            if type(visible) is not bool:
                raise Failure('state_unavailable', 'The button hierarchy visibility is unknown.')
            if not visible: return False
            parents = self.action('get_parent', obj).get('parent')
            if not isinstance(parents, list) or len(parents) != 1:
                raise Failure('state_unavailable', 'The button parent could not be determined.')
            obj = UIObject(parents[0])

    @staticmethod
    def platform_refusal(exc):
        return exc.result.get('code') in ('target_not_interactive', 'invalid_element_state',
            'unsupported_element_type', 'unsupported_receiver', 'value_not_found', 'client_operation_rejected')

    @classmethod
    def operation_refusal(cls, exc):
        """A refused UI operation, distinct from transport and execution failures."""
        return cls.platform_refusal(exc) or exc.result.get('code') in (
            'target_unavailable', 'target_hidden', 'client_busy', 'action_failed')

    @staticmethod
    def link_index(area):
        try: number = Decimal(area)
        except InvalidOperation: return area
        if not number.is_finite() or number != int(number) or number < 1:
            raise Failure('scenario_failed', 'Hyperlink numbers in selectors start at 1.')
        return int(number)-1

    def finish_row(self, obj):
        if obj.data.get('class') != 'Table': raise Failure('unsupported_element_type', 'A table is required.')
        editing = self.action('current_mode_is_edit', obj).get('edit_mode')
        if type(editing) is not bool: raise Failure('edit_state_unavailable', 'The row edit mode is unknown.')
        if editing: self.action('end_edit_row', obj)

    def begin_row(self, obj):
        table = self.table(obj)
        if table is None: return None
        editing = self.action('current_mode_is_edit', table).get('edit_mode')
        if type(editing) is not bool: raise Failure('edit_state_unavailable', 'The row edit mode is unknown.')
        if editing: return None
        self.action('change_row', table)
        return table

    def finish_owned_row(self, table):
        if table is not None: self.finish_row(table)

    def waiting(self, name, timeout=Decimal(3), object_type=None):
        if not isinstance(name, str): raise Failure('scenario_failed', 'Waiting requires an object name or title.')
        if object_type is None: object_type = TypeValue('testedform')
        if not isinstance(object_type, TypeValue) or object_type.name not in TESTED_CLASSES:
            raise Failure('unsupported_scenario', 'Waiting requires a supported tested-object Type.')
        if timeout is None: timeout = Decimal(3)
        if not isinstance(timeout, Decimal) or not timeout.is_finite() or timeout < 0:
            raise Failure('invalid_timeout', 'Waiting timeout must be a finite nonnegative number.')
        deadline = time.monotonic() + min(float(timeout), max(0,self.host.deadline-time.monotonic()))
        criteria = dict(cls=TESTED_CLASSES[object_type.name], scope='application', timeout=0)
        criteria.update({'name':name[1:]} if name.startswith(('!','#')) else {'title':name})
        while True:
            objects = self.action('find_objects', **criteria)['objects']
            if TESTED_CLASSES[object_type.name] is None:
                from compatible_scenarios.tester.windows import object_type as window_type
                objects = [o for o in objects if window_type(o)==object_type.name]
            self.host.tick(self.host.location['path'], self.host.location['line'])
            if objects: return True
            if time.monotonic() >= deadline: return False
            self.host.pause(min(.1,max(0,deadline-time.monotonic())))

    def attribute(self, obj, name):
        self.check_owner(obj)
        if isinstance(obj, MainWindow):
            from compatible_scenarios.tester.windows import main_window
            obj = main_window(self)
        if not isinstance(obj, UIObject): raise Failure('unsupported_scenario', f'Unsupported property {name}.')
        fields = {'name': 'name', 'имя': 'name', 'titletext': 'title', 'текстзаголовка': 'title',
                  'caption': 'title', 'заголовок': 'title', 'formname': 'form_name', 'имяформы': 'form_name',
                  'type': 'type', 'тип': 'type', 'url':'url', 'навигационнаяссылка':'url',
                  'ismain':'is_main', 'главное':'is_main', 'этоглавное':'is_main',
                  'homepage':'home_page', 'начальнаястраница':'home_page'}
        if name not in fields: raise Failure('unsupported_scenario', f'Unsupported UI property {name}.')
        live = self.host.live(obj.data['key'])
        if not live or live.get('handle') != obj.data.get('handle'):
            raise Failure('element_unavailable', 'The scenario element is no longer available.')
        return live.get(fields[name])

    def area_field(self, obj):
        fields = self.action('get_current_area_field', obj)['field']
        if not fields: raise Failure('element_unavailable', 'The current area has no field.')
        return UIObject(fields[0], obj)

    def search(self, obj, action, args):
        if len(args) > 4: raise Failure('scenario_failed', 'Object search takes at most four arguments.')
        object_type, title, name, timeout = (args + [None]*4)[:4]
        classes = TESTED_CLASSES
        if object_type is not None and (not isinstance(object_type, TypeValue) or object_type.name not in classes):
            raise Failure('unsupported_scenario', 'Search requires a supported tested-object Type or Undefined.')
        if any(value is not None and not isinstance(value, str) for value in (title, name)):
            raise Failure('scenario_failed', 'Search title and name must be strings or Undefined.')
        if timeout is None: timeout = Decimal(0)
        if not isinstance(timeout, Decimal) or not timeout.is_finite() or timeout < 0:
            raise Failure('invalid_timeout', 'Search timeout must be a finite nonnegative number.')
        seconds = min(float(timeout), max(0, self.host.deadline-time.monotonic()))
        deadline = time.monotonic() + seconds
        application = isinstance(obj, Application)
        criteria = dict(scope='application', timeout=0) if application else dict(root_key=obj.data['key'], timeout=0)
        if object_type is not None and classes[object_type.name]: criteria['cls'] = classes[object_type.name]
        if title is not None: criteria['title'] = title
        if name is not None: criteria['name'] = name
        while True:
            objects = self.action('find_objects', None if application else obj, **criteria)['objects']
            if object_type is not None and classes[object_type.name] is None:
                from compatible_scenarios.tester.windows import object_type as window_type
                objects = [o for o in objects if window_type(o)==object_type.name]
            if objects or time.monotonic() >= deadline: break
            self.host.pause(min(.1, max(0, deadline-time.monotonic())))
        self.host.tick(self.host.location['path'], self.host.location['line'])
        if action == 'find_objects': return [UIObject(item, obj) for item in objects]
        if objects: return UIObject(objects[0], obj)
        if action == 'get_object': raise Failure('element_unavailable', 'The requested child object was not found.')
        return None

    def method(self, obj, name, args):
        if isinstance(obj, Application) and name in ('connect', 'установитьсоединение', 'disconnect', 'разорватьсоединение'):
            if args: raise Failure('scenario_failed', f'{name} takes no arguments.')
            return self.host.operation('application_' + ('connect' if name in ('connect', 'установитьсоединение') else 'disconnect'), {},
                                       lambda: self.host.connections.application_method(obj, name))
        self.check_owner(obj)
        from compatible_scenarios.tester.files import BinaryData, binary_method
        if isinstance(obj, BinaryData): return binary_method(self.host, obj, name, args)
        from compatible_scenarios.shared.bsl.pictures import Picture
        if isinstance(obj, Picture):
            if name in ('getbinarydata', 'получитьдвоичныеданные') and not args:
                return BinaryData(obj.data)
            raise Failure('unsupported_scenario', f'Unsupported picture method {name}.')
        if isinstance(obj, Assertion):
            op = ASSERTIONS.get(name)
            if op == 'that' and 1 <= len(args) <= 2:
                obj.value, obj.details, obj.negate = args[0], args[1] if len(args)>1 else '', False
                return obj
            if op == 'not_' and not args: obj.negate = True; return obj
            if op in ('istrue', 'isfalse', 'isundefined', 'isnull') and not args:
                result = obj.value is {'istrue': True, 'isfalse': False, 'isundefined': None, 'isnull': NULL}[op]
            elif op in ('equal', 'notequal', 'greater', 'greaterorequal', 'less', 'lessorequal') and len(args) == 1:
                result = {'equal': operator.eq, 'notequal': operator.ne, 'greater': operator.gt,
                          'greaterorequal': operator.ge, 'less': operator.lt, 'lessorequal': operator.le}[op](obj.value, args[0])
            elif op in ('filled', 'empty') and not args:
                value = obj.value
                filled = (bool(value.strip()) if isinstance(value,str) else bool(len(value)) if isinstance(value,COLLECTIONS)
                          else value != 0 if isinstance(value,Decimal) else value != datetime(1,1,1) if type(value) is datetime else value is not None and value is not NULL)
                result = filled if op == 'filled' else not filled
            elif op == 'exists' and not args: result = obj.value is not None and obj.value is not NULL
            elif op == 'between' and len(args)==2: result = args[0] <= obj.value <= args[1]
            elif op == 'contains' and len(args)==1:
                values = [v for _,v in obj.value.items()] if isinstance(obj.value,(Structure,Map)) else obj.value
                if isinstance(values, ValueList): values = [item.value for item in values]
                result = any(value_equal(v, args[0]) for v in values) if isinstance(values,list) else args[0] in string(values)
            elif op == 'has' and len(args)==1:
                result = len(obj.value if isinstance(obj.value,COLLECTIONS) else string(obj.value)) == args[0]
            else: raise Failure('unsupported_scenario', f'Unsupported assertion {name} or argument count.')
            if result == obj.negate:
                raise Failure('assertion_failed', obj.details or f'Assertion {name} failed: {obj.value!r}, {args!r}.')
            obj.negate = False
            return obj
        if isinstance(obj, MainWindow) and name in ('executecommand', 'выполнитькоманду') and len(args) == 1:
            self.action('execute_command', command=args[0]); return None
        if isinstance(obj, MainWindow):
            from compatible_scenarios.tester.windows import main_window
            obj = main_window(self)
        from compatible_scenarios.tester import platform_methods
        if name in METHODS and METHODS[name][0] in platform_methods.SPECIAL:
            return platform_methods.invoke(self, obj, name, args)
        if isinstance(obj, Application):
            action = METHODS.get(name, (None,))[0]
            if action == 'get_current_error':
                if args: raise Failure('scenario_failed', f'{name} takes no arguments.')
                from compatible_scenarios.shared.bsl.builtins import ErrorInfo
                result = self.action(action)
                return ErrorInfo(result['error'], tuple(result.get('details') or ())) if result.get('error') else None
            if action in ('find_object', 'find_objects', 'get_object'):
                return self.search(obj, action, args)
            if action in ('get_active_window', 'get_child_objects'):
                if args: raise Failure('scenario_failed', f'{name} takes no arguments.')
                if action == 'get_child_objects':
                    return [UIObject(item, obj) for item in self.action(action, scope='application')['children']]
                active = self.action(action)
                data = self.host.live(active['key']) if active.get('key') else None
                if not data: raise Failure('element_unavailable', 'The active window is unavailable.')
                return UIObject(data, obj)
            raise Failure('unsupported_scenario', f'Unsupported application method {name}.')
        if not isinstance(obj, UIObject) or name not in METHODS:
            raise Failure('unsupported_scenario', f'Unsupported method {name} for this value.')
        action, params, slot = METHODS[name]
        if action in ('application_connect', 'application_disconnect'):
            raise Failure('unsupported_scenario', f'{name} requires App.')
        if action in ('get_active_window', 'get_current_error'):
            raise Failure('unsupported_scenario', f'{name} requires App.')
        if action == 'get_command_interface':
            if args: raise Failure('scenario_failed', 'GetCommandInterface takes no arguments.')
            from compatible_scenarios.tester.windows import links
            return links(self, obj)
        if action == 'close_window':
            if args or obj.data.get('class') not in ('MainFrame','SecondaryFrame','HomePage'):
                raise Failure('unsupported_scenario', 'Close takes no arguments and requires a window.')
            self.action('close_window', obj)
            return None
        if action == 'get_object' and not args and obj.data.get('class') in ('MainFrame','SecondaryFrame','HomePage'):
            return self.search(obj, 'get_object', [TypeValue('testedform')])
        if action in ('find_object', 'find_objects', 'get_object'):
            return self.search(obj, action, args)
        if action == 'execute_command':
            raise Failure('unsupported_scenario', 'ExecuteCommand is supported on MainWindow only.')
        return invoke_element(self, obj, name, args, action, params, slot)
