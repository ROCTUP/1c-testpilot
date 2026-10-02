"""Test-client object values and explicit platform-method mappings.

The adapter supplies action() and area_field(); format-specific selectors,
connection policy and assertions stay in their own executor.
"""
from dataclasses import dataclass, field
from datetime import datetime
from contextvars import ContextVar
from .bsl.language import Failure, Structure, Map, value_in

# Explicit method signatures and return slots; no dynamic forwarding to Python.
METHODS = {}
for names, action, params, slot in [
    ('ChooseUserMessage ВыбратьСообщениеПользователю', 'choose_user_message', ('text',), None),
    ('CloseUserMessagesPanel ЗакрытьПанельСообщенийПользователю', 'close_user_messages_panel', (), None),
    ('GetUserMessageTexts ПолучитьТекстыСообщенийПользователю', 'get_user_message_texts', (), 'messages'),
    ('GotoStartPage ПерейтиКНачальнойСтранице', 'goto_start_page', (), None),
    ('GotoPreviousWindow ПерейтиКПредыдущемуОкну', 'goto_previous_window', (), None),
    ('GotoNextWindow ПерейтиКСледующемуОкну', 'goto_next_window', (), None),
    ('WriteContentToFile ЗаписатьСодержимоеВФайл', 'write_content_to_file', ('save_as',), None),
    ('ClickViewStatusItem НажатьНаЭлементСостоянияПросмотра', 'click_view_status_item', ('index',), None),
    ('DeleteViewStatusItem УдалитьЭлементСостоянияПросмотра', 'delete_view_status_item', ('index',), None),
    ('GetViewStatusItemTexts ПолучитьТекстыЭлементовСостоянияПросмотра', 'get_view_status_item_texts', (), 'items'),
    ('CurrentCheck ТекущееПометка', 'current_check', (), 'checked'),
    ('GetLinkedWindow ПолучитьСвязанноеОкно', 'get_linked_window', (), 'window'),
    ('GetCurrentPage ПолучитьТекущуюСтраницу', 'get_current_page', (), 'page'),
    ('GetChoiceListPresentation ПолучитьПредставлениеСпискаВыбора', 'get_choice_list', (), 'items'),
    ('GetStatePresentation ПолучитьОтображениеСостояния', 'get_state_presentation', (), 'presentation'),
    ('GetDocumentHTML ПолучитьHTMLДокумента', 'get_html', ('html', 'attachments'), None),
    ('StartUILogRecording НачатьЗаписьЖурналаДействийПользователя', 'record_start', (), None),
    ('FinishUILogRecording ЗавершитьЗаписьЖурналаДействийПользователя', 'record_finish', (), 'uilog'),
    ('CancelUILogRecording ПрерватьЗаписьЖурналаДействийПользователя', 'record_cancel', (), None),
    ('PauseUILogRecording ПриостановитьЗаписьЖурналаДействийПользователя', 'record_pause', (), None),
    ('ResumeUILogRecording ПродолжитьЗаписьЖурналаДействийПользователя', 'record_resume', (), None),
    ('GetMaxActionExecutionTime ПолучитьМаксимальноеВремяВыполненияДействия', 'get_max_action_time', (), 'max_action_time'),
    ('SetMaxActionExecutionTime УстановитьМаксимальноеВремяВыполненияДействия', 'set_max_action_time', ('seconds',), None),
    ('GetAccumulatedPerformanceIndicators ПолучитьНакопленныеПоказателиПроизводительности', 'get_performance', ('clear',), 'indicators'),
    ('ClearAccumulatedPerformanceIndicators ОчиститьНакопленныеПоказателиПроизводительности', 'clear_performance', (), None),
    ('SetFileDialogResult УстановитьРезультатДиалогаВыбораФайла', 'set_file_dialog_result', ('result', 'filename', 'filter_index'), None),
    ('ClearFileDialogResult ОчиститьРезультатДиалогаВыбораФайла', 'clear_file_dialog_result', (), None),
    ('WaitForClosing ОжидатьЗакрытие', 'wait_for_closing', ('timeout',), None),
    ('WaitForObjectDisplayed ОжидатьОтображениеОбъекта', 'wait_for_object_displayed', ('object_type', 'title', 'name', 'timeout'), None),
    ('WaitForCondition ОжидатьСостояния', 'wait_for_condition', ('callback', 'timeout'), None),
    ('Connect УстановитьСоединение', 'application_connect', (), None),
    ('Disconnect РазорватьСоединение', 'application_disconnect', (), None),
    ('GetActiveWindow ПолучитьАктивноеОкно', 'get_active_window', (), None),
    ('GetCurrentErrorInfo ПолучитьТекущуюИнформациюОбОшибке', 'get_current_error', (), None),
    ('StartChoosing Выбрать', 'start_choosing', (), None),
    ('Choose', 'choose_row', (), None),
    ('StartChoosingFromChoiceList ВыбратьИзСпискаВыбора', 'start_choosing_from_choice_list', (), None),
    ('Open Открыть', 'open_field', (), None),
    ('Create Создать', 'create', (), None),
    ('CancelEdit ОтменитьРедактирование', 'cancel_edit', (), None),
    ('IncreaseValue УвеличитьЗначение', 'increase_value', (), None),
    ('DecreaseValue УменьшитьЗначение', 'decrease_value', (), None),
    ('SelectOption ВыбратьВариант', 'select_option', ('value',), None),
    ('GotoValue ПерейтиКЗначению', 'goto_value', ('percent',), None),
    ('GotoDate ПерейтиКДате', 'goto_date', ('date',), None),
    ('GotoNextMonth ПерейтиНаМесяцВперед', 'calendar_next_month', (), None),
    ('GotoPreviousMonth ПерейтиНаМесяцНазад', 'calendar_previous_month', (), None),
    ('GotoNextYear ПерейтиНаГодВперед', 'calendar_next_year', (), None),
    ('GotoPreviousYear ПерейтиНаГодНазад', 'calendar_previous_year', (), None),
    ('GetCommandInterface ПолучитьКомандныйИнтерфейс', 'get_command_interface', (), None),
    ('Close Закрыть', 'close_window', (), None),
    ('FindObject НайтиОбъект', 'find_object', ('object_type', 'title', 'name', 'timeout'), None),
    ('FindObjects НайтиОбъекты', 'find_objects', ('object_type', 'title', 'name', 'timeout'), None),
    ('GetObject ПолучитьОбъект', 'get_object', ('object_type', 'title', 'name', 'timeout'), None),
    ('GetChildObjects ПолучитьПодчиненныеОбъекты', 'get_child_objects', (), 'children'),
    ('GetParent ПолучитьРодителя', 'get_parent', (), 'parent'),
    ('WaitForDropListGeneration ОжидатьФормированияВыпадающегоСписка', 'wait_for_drop_list_generation', ('timeout',), 'generated'),
    ('DeleteRow УдалитьСтроку', 'delete_rows', (), None),
    ('Activate Активизировать', 'activate', (), None),
    ('Click Нажать', 'click', (), None),
    ('InputText ВвестиТекст', 'input_text', ('text',), None),
    ('InputDocumentHTML ВвестиHTMLДокумента', 'input_html', ('html', 'attachments'), None),
    ('GetDisplayedText ПолучитьОтображаемыйТекст', 'get_text', (), 'text'),
    ('GetEditText ПолучитьТекстРедактирования', 'get_edit_text', (), 'text'),
    ('GetDataPresentation ПолучитьПредставлениеДанных', 'get_data_presentation', (), 'presentation'),
    ('GetAreaText ПолучитьТекстОбласти', 'get_area_text', ('area',), 'text'),
    ('GetCurrentAreaText ПолучитьТекстТекущейОбласти', 'get_current_area_text', ('area',), 'text'),
    ('SetCurrentArea УстановитьТекущуюОбласть', 'set_current_area', ('address',), None),
    ('GetCurrentAreaAddress ПолучитьАдресТекущейОбласти', 'get_current_area_address', (), 'address'),
    ('GetCurrentAreaField ПолучитьПолеТекущейОбласти', 'get_current_area_field', (), 'field'),
    ('GetDocumentDataAreaVerticalSize ПолучитьРазмерОбластиДанныхДокументаПоВертикали', 'get_doc_area_vertical_size', (), 'size'),
    ('GetDocumentDataAreaHorizontalSize ПолучитьРазмерОбластиДанныхДокументаПоГоризонтали', 'get_doc_area_horizontal_size', (), 'size'),
    ('IncludedInMergedArea ВходитВОбъединеннуюОбласть', 'included_in_merged_area', ('address',), 'merged_area'),
    ('TextIsWithinAreaBounds ТекстВПределахГраницОбласти', 'text_within_area_bounds', ('area',), 'fits'),
    ('BeginEditCurrentArea НачатьРедактированиеТекущейОбласти', 'begin_edit_current_area', (), None),
    ('EndEditCurrentArea ЗакончитьРедактированиеТекущейОбласти', 'end_edit_current_area', ('cancel',), None),
    ('GotoFirstRow ПерейтиКПервойСтроке', 'goto_first_row', ('toggle_selection',), None),
    ('GotoLastRow ПерейтиКПоследнейСтроке', 'goto_last_row', ('toggle_selection',), None),
    ('GotoNextRow ПерейтиКСледующейСтроке', 'goto_next_row', ('toggle_selection',), None),
    ('GotoPreviousRow ПерейтиКПредыдущейСтроке', 'goto_previous_row', ('toggle_selection',), None),
    ('GotoRow ПерейтиКСтроке', 'goto_row', ('fields', 'direction'), 'found'),
    ('CurrentModeIsEdit ТекущийРежимРедактирование', 'current_mode_is_edit', (), 'edit_mode'),
    ('GetSelectedRows ПолучитьВыделенныеСтроки', 'get_selected_rows', (), 'rows'),
    ('SelectAllRows ВыделитьВсеСтроки', 'select_all_rows', (), None),
    ('GetCellText ПолучитьТекстЯчейки', 'get_cell_text', ('column',), 'text'),
    ('AddRow ДобавитьСтроку', 'table_add_row', (), None),
    ('CopyRow СкопироватьСтроку', 'copy_row', (), None),
    ('SwitchRowDeleteMark ПереключитьПометкуУдаленияСтроки', 'switch_row_delete_mark', (), None),
    ('SetOrder УстановитьПорядок', 'set_order', ('column',), None),
    ('EndEditRow ЗакончитьРедактированиеСтроки', 'end_edit_row', ('cancel',), None),
    ('ChangeRow ИзменитьСтроку', 'change_row', (), None),
    ('SetCheck УстановитьОтметку', 'set_check', (), None),
    ('Clear Очистить', 'clear', (), None),
    ('OpenDropList ОткрытьВыпадающийСписок', 'open_drop_list', (), None),
    ('ExecuteChoiceFromDropList ВыполнитьВыборИзВыпадающегоСписка', 'choose_from_drop_list', ('value',), None),
    ('CloseDropList ЗакрытьВыпадающийСписок', 'close_drop_list', (), None),
    ('DropListIsOpen ВыпадающийСписокОткрыт', 'drop_list_is_open', (), 'open'),
    ('CurrentVisible ТекущаяВидимость', 'is_visible', (), 'visible'),
    ('CurrentEnable ТекущаяДоступность', 'is_enabled', (), 'enabled'),
    ('CurrentReadOnly ТекущееТолькоПросмотр', 'is_readonly', (), 'readonly'),
    ('TitleIsShown ЗаголовокОтображается', 'title_is_shown', (), 'title_shown'),
    ('GetToolTipText ПолучитьТекстПодсказки', 'get_tooltip', (), 'tooltip'),
    ('GetCommandBar ПолучитьКоманднуюПанель', 'get_command_bar', (), 'commandbar'),
    ('GetContextMenu ПолучитьКонтекстноеМеню', 'get_context_menu', (), 'menu'),
    ('Expand Развернуть', 'expand', ('row_description', 'subordinates'), None),
    ('Collapse Свернуть', 'collapse', ('row_description',), None),
    ('Expanded Развернут', 'is_expanded', ('row_description',), 'expanded'),
    ('CanBeExpanded ВозможноРазвернуть', 'can_be_expanded', ('row_description',), 'can_expand'),
    ('CurrentOpened ТекущееОткрыта', 'current_opened', (), 'opened'),
    ('GoOneLevelDown ПерейтиНаУровеньВниз', 'go_one_level_down', (), None),
    ('GoOneLevelUp ПерейтиНаУровеньВверх', 'go_one_level_up', (), None),
    ('ClickFormattedStringHyperlink НажатьНаГиперссылкуВФорматированнойСтроке', 'click_formatted_string_hyperlink', ('index',), None),
    ('GetFormattedStringHyperlinksPresentations ПолучитьПредставленияГиперссылокФорматированнойСтроки',
     'get_formatted_string_hyperlinks', (), 'hyperlinks'),
    ('ClickHTMLDocumentHyperlink НажатьНаГиперссылкуВHTMLДокументе', 'click_html_hyperlink', ('index',), None),
    ('ClickFormattedDocumentHyperlink НажатьНаГиперссылкуВФорматированномДокументе', 'click_formatted_doc_hyperlink', ('index',), None),
    ('ExecuteChoiceFromChoiceList ВыполнитьВыборИзСпискаВыбора', 'execute_choice_from_choice_list', ('value',), None),
    ('ExecuteChoiceFromMenu ВыполнитьВыборИзМеню ExecuteChoiceFromDetailsMenu ВыполнитьВыборИзМенюРасшифровки',
     'execute_choice_from_menu', ('index',), None),
    ('ExecuteChoiceFromList ВыполнитьВыборИзСписка', 'execute_choice_from_list', ('index',), None),
    ('GotoNextItem ПерейтиКСледующемуЭлементу', 'goto_next_item', (), None),
    ('GotoPreviousItem ПерейтиКПредыдущемуЭлементу', 'goto_previous_item', (), None),
    ('GetCurrentItem ПолучитьТекущийЭлемент', 'get_current_item', (), 'item'),
    ('FindDefaultButton НайтиКнопкуПоУмолчанию', 'find_default_button', (), 'button'),
    ('CurrentModified ТекущаяМодифицированность', 'current_modified', (), 'modified'),
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


# The platform gives a managed form and a table methods of the same name: the form moves between
# its elements, the table between its columns. The receiver's class selects the Testpilot action.
FORM_ACTIONS = {'goto_next_item': 'goto_next_element', 'goto_previous_item': 'goto_previous_element',
                'get_current_item': 'get_current_element'}
# Actions answering with tested objects; the value is the result key that holds them.
OBJECT_RESULTS = {'get_current_item': 'item', 'get_current_element': 'item', 'get_command_bar': 'commandbar',
                  'get_context_menu': 'menu', 'find_default_button': 'button',
                  'get_linked_window': 'window', 'get_current_page': 'page'}


def invoke_element(adapter, obj, name, args, action, params, slot):
    if obj.data.get('class') == 'ManagedForm': action = FORM_ACTIONS.get(action, action)
    # The platform calls two different methods Выбрать: TestedFormTable.Choose (double click, Enter)
    # and StartChoosing of an input or calendar field (the field's Select button).
    if action == 'start_choosing' and name == 'выбрать' and obj.data.get('class') == 'Table':
        action = 'choose_row'
    minimum = {'goto_first_row': 0, 'goto_last_row': 0, 'goto_next_row': 0, 'goto_previous_row': 0,
               'goto_row': 1, 'end_edit_row': 0, 'end_edit_current_area': 0,
               'get_area_text': 0, 'get_current_area_text': 0, 'input_html': 1,
               'text_within_area_bounds': 0, 'write_content_to_file': 0}.get(action, len(params))
    if action in ('expand', 'collapse', 'is_expanded', 'can_be_expanded'): minimum = 0
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
    if action == 'write_content_to_file':
        from .bsl.language import boolean
        kw['save_as'] = boolean(args[0]) if args and args[0] is not None else False
    if action == 'input_html' and 'attachments' in kw:
        import base64
        from .bsl.pictures import Picture
        if not isinstance(kw['attachments'], (Map, Structure)):
            raise Failure('scenario_failed', 'HTML attachments require a Map.')
        kw['attachments'] = {name: base64.b64encode(value.data).decode('ascii') if isinstance(value, Picture) else value
                             for name, value in kw['attachments'].items()}
    if action == 'delete_rows': kw.update(scope='current', confirm=None)
    # Like DeleteRow, the confirmation question stays for the following steps of the scenario.
    if action == 'switch_row_delete_mark': kw['confirm'] = None
    if action == 'goto_date':
        if type(args[0]) is not datetime: raise Failure('scenario_failed', 'GotoDate expects a Date.')
        kw = dict(year=args[0].year, month=args[0].month, day=args[0].day)
    result = adapter.action(action, obj, **kw)
    if action == 'get_child_objects': return [UIObject(item, obj) for item in result['children']]
    if action == 'get_parent': return UIObject(result['parent'][0]) if result['parent'] else None
    if action in OBJECT_RESULTS:
        found = result[OBJECT_RESULTS[action]]
        if not found and result.get('current_status') == 'unavailable':
            raise Failure('state_unavailable', 'The current form element could not be read.')
        return UIObject(found[0], obj) if found else None
    # The platform returns a string; Testpilot reports an empty tooltip as None.
    if action == 'get_tooltip' and result[slot] is None: return ''
    if action == 'get_view_status_item_texts':
        from .bsl.language import FixedArray
        return FixedArray(value_in(result[slot]))
    return value_in(result[slot]) if slot else None
