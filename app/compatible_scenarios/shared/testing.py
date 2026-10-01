"""Test-client object values and explicit platform-method mappings.

The adapter supplies action() and area_field(); format-specific selectors,
connection policy and assertions stay in their own executor.
"""
from dataclasses import dataclass, field
from contextvars import ContextVar
from .bsl.language import Failure, Structure, Map, value_in

# Explicit method signatures and return slots; no dynamic forwarding to Python.
METHODS = {}
for names, action, params, slot in [
    ('Connect Подключить', 'application_connect', (), None),
    ('Disconnect Отключить', 'application_disconnect', (), None),
    ('GetActiveWindow ПолучитьАктивноеОкно', 'get_active_window', (), None),
    ('GetCurrentErrorInfo ПолучитьТекущуюИнформациюОбОшибке', 'get_current_error', (), None),
    ('StartChoosing Выбрать НачатьВыбор', 'start_choosing', (), None),
    ('Choose', 'choose_row', (), None),
    ('SelectOption ВыбратьВариант', 'select_option', ('value',), None),
    ('GotoValue ПерейтиКЗначению', 'goto_value', ('percent',), None),
    ('GetCommandInterface ПолучитьКомандныйИнтерфейс', 'get_command_interface', (), None),
    ('Close Закрыть', 'close_window', (), None),
    ('FindObject НайтиОбъект', 'find_object', ('object_type', 'title', 'name', 'timeout'), None),
    ('FindObjects НайтиОбъекты', 'find_objects', ('object_type', 'title', 'name', 'timeout'), None),
    ('GetObject ПолучитьОбъект', 'get_object', ('object_type', 'title', 'name', 'timeout'), None),
    ('GetChildObjects ПолучитьПодчиненныеОбъекты ПолучитьДочерниеОбъекты', 'get_child_objects', (), 'children'),
    ('GetParent ПолучитьРодителя', 'get_parent', (), 'parent'),
    ('WaitForDropListGeneration ОжидатьФормированияВыпадающегоСписка', 'wait_for_drop_list_generation', ('timeout',), 'generated'),
    ('DeleteRow УдалитьСтроку', 'delete_rows', (), None),
    ('Activate Активизировать', 'activate', (), None),
    ('Click Нажать', 'click', (), None),
    ('InputText ВвестиТекст', 'input_text', ('text',), None),
    ('InputHTML ВвестиHTML InputDocumentHTML ВвестиHTMLДокумента', 'input_html', ('html', 'attachments'), None),
    ('GetDisplayedText ПолучитьОтображаемыйТекст ПолучитьТекст', 'get_text', (), 'text'),
    ('GetDataPresentation ПолучитьПредставлениеДанных', 'get_data_presentation', (), 'presentation'),
    ('GetAreaText ПолучитьТекстОбласти', 'get_area_text', ('area',), 'text'),
    ('GetCurrentAreaText ПолучитьТекстТекущейОбласти', 'get_current_area_text', ('area',), 'text'),
    ('SetCurrentArea УстановитьТекущуюОбласть', 'set_current_area', ('address',), None),
    ('GetCurrentAreaAddress ПолучитьАдресТекущейОбласти', 'get_current_area_address', (), 'address'),
    ('GetCurrentAreaField ПолучитьПолеТекущейОбласти', 'get_current_area_field', (), 'field'),
    ('BeginEditCurrentArea НачатьРедактированиеТекущейОбласти', 'begin_edit_current_area', (), None),
    ('EndEditCurrentArea ЗакончитьРедактированиеТекущейОбласти ЗавершитьРедактированиеТекущейОбласти', 'end_edit_current_area', ('cancel',), None),
    ('GotoFirstRow ПерейтиКПервойСтроке', 'goto_first_row', ('toggle_selection',), None),
    ('GotoLastRow ПерейтиКПоследнейСтроке', 'goto_last_row', ('toggle_selection',), None),
    ('GotoNextRow ПерейтиКСледующейСтроке', 'goto_next_row', ('toggle_selection',), None),
    ('GotoPreviousRow ПерейтиКПредыдущейСтроке', 'goto_previous_row', ('toggle_selection',), None),
    ('GotoRow ПерейтиКСтроке', 'goto_row', ('fields', 'direction'), 'found'),
    ('CurrentModeIsEdit ТекущийРежимРедактирование ТекущийРежимРедактирования', 'current_mode_is_edit', (), 'edit_mode'),
    ('GetSelectedRows ПолучитьВыделенныеСтроки', 'get_selected_rows', (), 'rows'),
    ('SelectAllRows ВыделитьВсеСтроки', 'select_all_rows', (), None),
    ('GetCellText ПолучитьТекстЯчейки', 'get_cell_text', ('column',), 'text'),
    ('AddRow ДобавитьСтроку', 'add_row', (), None),
    ('EndEditRow ЗакончитьРедактированиеСтроки ЗавершитьРедактированиеСтроки', 'end_edit_row', ('cancel',), None),
    ('ChangeRow ИзменитьСтроку', 'change_row', (), None),
    ('SetCheck УстановитьОтметку УстановитьФлажок', 'set_check', (), None),
    ('Clear Очистить', 'clear', (), None),
    ('OpenDropList ОткрытьВыпадающийСписок', 'open_drop_list', (), None),
    ('ExecuteChoiceFromDropList ВыполнитьВыборИзВыпадающегоСписка', 'choose_from_drop_list', ('value',), None),
    ('CloseDropList ЗакрытьВыпадающийСписок', 'close_drop_list', (), None),
    ('DropListIsOpen ВыпадающийСписокОткрыт', 'drop_list_is_open', (), 'open'),
    ('CurrentVisible ТекущаяВидимость', 'is_visible', (), 'visible'),
    ('CurrentEnable ТекущаяДоступность', 'is_enabled', (), 'enabled'),
    ('CurrentReadOnly ТекущееТолькоПросмотр', 'is_readonly', (), 'readonly'),
    ('Expand Развернуть', 'expand', ('row_description', 'subordinates'), None),
    ('Collapse Свернуть', 'collapse', ('row_description',), None),
    ('Expanded Развернут', 'is_expanded', ('row_description',), 'expanded'),
    ('CurrentOpened ТекущееОткрыта', 'current_opened', (), 'opened'),
    ('GoOneLevelDown ПерейтиНаУровеньВниз', 'go_one_level_down', (), None),
    ('GoOneLevelUp ПерейтиНаУровеньВверх', 'go_one_level_up', (), None),
    ('ClickFormattedStringHyperlink НажатьНаГиперссылкуВФорматированнойСтроке', 'click_formatted_string_hyperlink', ('index',), None),
    ('ExecuteChoiceFromChoiceList ВыполнитьВыборИзСпискаВыбора', 'execute_choice_from_choice_list', ('value',), None),
    ('ExecuteChoiceFromMenu ВыполнитьВыборИзМеню ExecuteChoiceFromDetailsMenu ВыполнитьВыборИзМенюРасшифровки',
     'execute_choice_from_menu', ('index',), None),
    ('GotoNextItem ПерейтиКСледующемуЭлементу', 'goto_next_item', (), None),
    ('ExecuteCommand ВыполнитьКоманду', 'execute_command', ('command',), None),
]:
    for name in names.split(): METHODS[name.casefold()] = (action, params, slot)


OBJECT_OWNER = ContextVar('scenario_object_owner', default=None)


@dataclass
class UIObject:
    data: dict
    parent: object = None
    area: str = None
    owner: object = field(default_factory=OBJECT_OWNER.get, repr=False, compare=False)


class MainWindow:
    """The launch-selected application's main window, not another connection."""
    def __init__(self): self.owner = OBJECT_OWNER.get()


class Application:
    """The scenario's already connected application."""
    def __init__(self): self.owner = OBJECT_OWNER.get()


TESTED_CLASSES = {'testedform': 'ManagedForm', 'testedformtable': 'Table', 'testedformfield': 'EditField',
                  'testedformbutton': 'Button', 'testedformgroup': 'Group', 'testedformdecoration': 'Decoration'}
TESTED_CLASSES.update(dict.fromkeys(('testedclientapplicationwindow', 'testedcommandinterface',
                                    'testedcommandinterfacebutton', 'testedcommandinterfacegroup')))


def invoke_element(adapter, obj, name, args, action, params, slot):
    if action == 'goto_next_item' and obj.data.get('class') == 'ManagedForm':
        action = 'goto_next_element'
    # The platform calls two different methods Выбрать: TestedFormTable.Choose (double click, Enter)
    # and StartChoosing of an input or calendar field (the field's Select button).
    if action == 'start_choosing' and name == 'выбрать' and obj.data.get('class') == 'Table':
        action = 'choose_row'
    minimum = {'goto_first_row': 0, 'goto_last_row': 0, 'goto_next_row': 0, 'goto_previous_row': 0,
               'goto_row': 1, 'end_edit_row': 0, 'end_edit_current_area': 0,
               'get_area_text': 0, 'get_current_area_text': 0, 'input_html': 1}.get(action, len(params))
    if action in ('expand', 'collapse', 'is_expanded'): minimum = 0
    if action == 'wait_for_drop_list_generation': minimum = 0
    if not minimum <= len(args) <= len(params): raise Failure('unsupported_scenario', f'{name} expects {minimum}..{len(params)} arguments.')
    if action in ('expand', 'collapse') and obj.data.get('class') == 'Group' and args:
        raise Failure('unsupported_scenario', 'Group Expand and Collapse take no arguments.')
    kw = {k: v for k, v in zip(params, args) if v is not None}
    if 'row_description' in kw:
        description = kw.pop('row_description')
        if not isinstance(description, (Map, Structure)) or len(description) != 1:
            raise Failure('unsupported_scenario', 'Tree row descriptions currently require one column/value pair.')
        kw['row_column'], kw['row_value'] = next(iter(description.items()))
    if action == 'get_current_area_field': return adapter.area_field(obj)
    if action == 'input_text': kw['finish'] = False
    if action == 'delete_rows': kw.update(scope='current', confirm=None)
    result = adapter.action(action, obj, **kw)
    if action == 'get_child_objects': return [UIObject(item, obj) for item in result['children']]
    if action == 'get_parent': return UIObject(result['parent'][0]) if result['parent'] else None
    return value_in(result[slot]) if slot else None
