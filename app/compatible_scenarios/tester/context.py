"""Tester window descriptions with optional live properties from Testpilot.epf."""
import copy
import json
import time
import _form_details

from compatible_scenarios.shared.bsl.language import Failure, value_in

TYPES = {'ManagedForm': 'TestedForm', 'EditField': 'TestedFormField', 'Group': 'TestedFormGroup',
         'Table': 'TestedFormTable', 'Button': 'TestedFormButton', 'Decoration': 'TestedFormDecoration',
         'Additional': 'TestedFormItemAddition', 'CIButton': 'TestedCommandInterfaceButton',
         'CI': 'TestedWindowCommandInterface', 'MainFrame': 'TestedClientApplicationWindow',
         'SecondaryFrame': 'TestedClientApplicationWindow'}

# String(Control) differs from String(TypeOf(Control)). Tester uses the former.
RUSSIAN_TYPES = {
    'TestedForm': 'ТестируемаяФорма',
    'TestedFormField': 'ТестируемоеПолеФормы',
    'TestedFormGroup': 'ТестируемаяГруппаФормы',
    'TestedFormTable': 'ТестируемаяТаблицаФормы',
    'TestedFormButton': 'ТестируемаяКнопкаФормы',
    'TestedFormDecoration': 'ТестируемаяДекорацияФормы',
    'TestedFormItemAddition': 'ТестируемоеДополнениеЭлементаФормы',
    'TestedCommandInterfaceButton': 'ТестируемаяКнопкаКомандногоИнтерфейса',
    'TestedWindowCommandInterface': 'ТестируемыйКомандныйИнтерфейсОкна',
    'TestedClientApplicationWindow': 'ТестируемоеОкноКлиентскогоПриложения',
}

# String(Form*Type.*): platform presentations, not BSL enumeration identifiers.
TEXT = {
    'CalendarField': ('Поле календаря', 'Calendar field'),
    'ChartField': ('Поле диаграммы', 'Chart field'),
    'CheckBoxField': ('Поле флажка', 'Checkbox'),
    'DendrogramField': ('Поле дендрограммы', 'Dendrogram field'),
    'FormattedDocumentField': ('Поле форматированного документа', 'Formatted document field'),
    'GanttChartField': ('Поле диаграммы Ганта', 'Gantt chart field'),
    'GeographicalSchemaField': ('Поле географической схемы', 'Geographical schema field'),
    'GraphicalSchemaField': ('Поле графической схемы', 'Graphical schema field'),
    'HTMLDocumentField': ('Поле HTML документа', 'HTML document field'),
    'InputField': ('Поле ввода', 'Input field'),
    'LabelField': ('Поле надписи', 'Text box'),
    'PDFDocumentField': ('Поле PDF документа', 'PDF document field'),
    'PeriodField': ('Поле периода', 'Period field'),
    'PictureField': ('Поле картинки', 'Image field'),
    'PlannerField': ('Поле данных планировщика', 'Planner field'),
    'ProgressBarField': ('Поле индикатора', 'Progress bar'),
    'RadioButtonField': ('Поле переключателя', 'Radio buttons'),
    'SpreadsheetDocumentField': ('Поле табличного документа', 'Spreadsheet document field'),
    'TextDocumentField': ('Поле текстового документа', 'Text document field'),
    'TrackBarField': ('Поле полосы регулирования', 'Slider field'),
    'ButtonGroup': ('Группа кнопок', 'Button group'),
    'ColumnGroup': ('Группа колонок', 'Column group'),
    'CommandBar': ('Командная панель', 'Command bar'),
    'ContextMenu': ('Контекстное меню', 'Context menu'),
    'Page': ('Страница', 'Page'),
    'Pages': ('Страницы', 'Pages'),
    'Popup': ('Подменю', 'Submenu'),
    'UsualGroup': ('Обычная группа', 'Regular group'),
    'Label': ('Надпись', 'Label'),
    'Picture': ('Картинка', 'Picture'),
}


def spreadsheet_hint(name, english):
    if english: return f'Use GetSpreadsheetContent( "!{name}" ) to get the xlsx file of this spreadsheet'
    return f'Используйте GetSpreadsheetContent ( "!{name}" ) для получения xlsx-файла этого табличного документа'


def english_script(host):
    variant = host.globals.get('testerscriptvariant')
    if variant is None or variant == '': return False
    if isinstance(variant, str):
        variant = variant.casefold()
        if variant in ('ru', 'russian'): return False
        if variant in ('en', 'english'): return True
    raise Failure('scenario_failed', 'TesterScriptVariant must be ru or en (Russian or English).')


def optional_refusal(adapter, error):
    return error.get('code') == 'value_unavailable' or adapter.platform_refusal(Failure(
        error.get('code', ''), error.get('error', 'Property unavailable.')))


def reset(adapter, form=None):
    saved = adapter.window_baseline
    if form is None or (saved and saved[0] != identity(form.data)):
        adapter.window_baseline = None


def identity(form): return form['key'], form.get('handle')


def helper_properties(adapter, form, objects):
    host = adapter.host
    service = vars(host.client).get('_testpilot_service')
    if not service or 'form_context' not in service.get('capabilities', []):
        return None
    form_id = form['key'].rsplit('.ManagedForm[', 1)[-1].removesuffix(']')
    names = list(dict.fromkeys(o['name'] for o in objects if o.get('name') and o['key'] != form['key']))
    requested = set(names)
    def fetch():
        import _code_execution as E
        remaining = host.deadline - time.monotonic()
        if remaining <= 0: raise Failure('scenario_timeout', 'Scenario deadline expired.')
        return E.execute(host.R, host.client, mode='form_context', context='client',
                         options=dict(form_id=form_id, items=names),
                         timeout=min(180, remaining), check_permissions=False)
    with host.connections.bound():
        response = host.operation('read_form_properties', {'form': form['key']}, fetch)
    data = response.get('result')
    if not isinstance(data, dict) or data.get('form_id') != form_id:
        raise Failure('helper_protocol_error', 'The helper returned properties of a different form.')
    if data.get('found') is False:
        raise Failure('context_unstable', 'The described form is no longer open.')
    if data.get('found') is not True or not isinstance(data.get('type'), str) or not isinstance(data.get('items'), list):
        raise Failure('helper_protocol_error', 'Invalid form properties response.')
    by_name = {}
    for item in data['items']:
        if (not isinstance(item, dict) or not isinstance(item.get('name'), str)
                or item['name'] not in requested or item['name'] in by_name
                or not isinstance(item.get('type'), str)
                or any(k in item and not isinstance(item[k], str) for k in ('control_type', 'input_hint'))
                or 'text_edit' in item and type(item['text_edit']) is not bool):
            raise Failure('helper_protocol_error', 'Invalid element properties response.')
        by_name[item['name']] = item
    return dict(type=data['type'], items=by_name)


def read(adapter):
    from compatible_scenarios.tester.ui import UIObject
    window = adapter.action('get_active_window')
    children = adapter.action('get_child_objects', key=window['key'])['children']
    forms = [obj for obj in children if obj.get('class') == 'ManagedForm']
    if not forms: return None
    if len(forms) != 1: raise Failure('ambiguous_form', 'The active window contains several managed forms.')
    form = forms[0]
    before_focus = adapter.action('get_current_element', UIObject(form))
    if before_focus.get('current_status') == 'unavailable':
        raise Failure('state_unavailable', 'The focused element is unknown.')
    with _form_details.suppress():
        context = adapter.action('get_context', key=form['key'], include_tables=False, result_mode='full')
    # Tester permits unavailable field presentations; the appropriate getter is
    # called below. Transport failures and disappearing targets remain errors.
    errors = context.get('errors', [])
    def optional(error):
        prop = error.get('property')
        return prop in ('presentation', 'visible', 'enabled', 'readonly') and optional_refusal(adapter, error)
    blocking = [e for e in errors if not optional(e)]
    if context.get('complete') is not True and (blocking or not errors):
        raise Failure('window_description_incomplete', 'The window could not be read completely.', errors=context.get('errors', []))
    if identity(context['form']) != identity(form):
        raise Failure('context_unstable', 'The form was replaced during the read.')
    items = context['elements']
    unknown_visibility = {e.get('key') for e in errors
                          if e.get('property') in ('visible', 'enabled') and optional(e)}
    unknown_readonly = {e.get('key') for e in errors if e.get('property') == 'readonly'}
    if len(items) > 10000: raise Failure('scenario_limit', 'The window exceeds 10000 elements.')
    elements = {obj['key']: obj for obj in items}
    if form['key'] not in elements: elements = {form['key']: form, **elements}
    nested = {}
    for obj in elements.values():
        if obj['key'] == form['key']: continue
        parent = obj.get('parent_key') or adapter.host.R._collection_parent(obj['key'])
        nested.setdefault(parent, []).append(obj)
    current = elements.get(context.get('current_key'))
    editing = context.get('input') or {}
    from compatible_scenarios.tester.tables import number_separators
    separators = number_separators(adapter)
    from compatible_scenarios.tester.regional import scenario_language
    english = scenario_language(adapter.host).replace('-', '_').split('_')[0].casefold() != 'ru'
    script_english = english_script(adapter.host)
    extra = helper_properties(adapter, form, elements.values())

    def title(value): return TEXT[value][english] if value in TEXT else value

    def action(name, obj, field):
        result = adapter.action(name, UIObject(obj))
        if field not in result:
            raise Failure('window_description_incomplete', f'{name} did not return {field}.')
        return result[field]

    def info(obj):
        result = {'ID': obj.get('name') or '', 'Title': obj.get('title') or ''}
        if obj['key'] == editing.get('key') and editing.get('edit_text') is not None:
            result['EditingText'] = editing['edit_text']
        return result

    def describe(obj, column=False, depth=0):
        adapter.host.tick(**adapter.host.location)
        if depth > 100: raise Failure('scenario_limit', 'The window hierarchy is too deep.')
        cls, kind = obj.get('class'), obj.get('type')
        if obj['key'] not in unknown_visibility and (obj.get('visible') is False or obj.get('enabled') is False): return None
        if cls not in TYPES:
            raise Failure('unsupported_scenario', f'Window description is not implemented for {cls!r}.')
        entry = {'ID': obj.get('form_name') if cls == 'ManagedForm' else obj.get('name') or '',
                 'TitleText': obj.get('title') or '',
                 'Type': TYPES[cls] if script_english else RUSSIAN_TYPES[TYPES[cls]]}
        properties = extra['items'].get(obj.get('name')) if extra else None
        if extra and cls == 'ManagedForm': entry['Type'] = extra['type']
        elif properties:
            entry['Type'] = properties.get('control_type') or properties['type']
        if cls == 'ManagedForm':
            if current: entry['CurrentControl'] = info(current)
        else:
            tooltip_key = (form.get('form_name'), obj.get('name'))
            if tooltip_key in adapter.window_tooltips:
                tooltip = adapter.window_tooltips[tooltip_key]
            else:
                try:
                    tooltip = action('get_tooltip', obj, 'tooltip')
                    # Tester caches tooltips per form/name; keep this cache within the test.
                    adapter.window_tooltips[tooltip_key] = tooltip
                except Failure as exc:
                    if not optional_refusal(adapter, exc.result): raise
                    tooltip = None
            caption, name = entry['TitleText'], entry['ID']
            if (tooltip and tooltip.strip() and tooltip.casefold() != caption.casefold()
                    and tooltip.replace(' ', '').casefold() != name.casefold()):
                entry['ToolTip'] = tooltip
        if cls == 'Group': entry['GroupType'] = title(kind)
        elif cls == 'Decoration': entry['DecorationType'] = title(kind)
        elif cls == 'Additional':
            if kind == 'SearchStringRepresentation': entry['SearchString'] = True
        elif cls == 'Button':
            # Some buttons do not implement the getter; a transport failure is not optional.
            try:
                if action('current_check', obj, 'checked') is True: entry['PressedOrChecked'] = True
            except Failure as exc:
                if not optional_refusal(adapter, exc.result): raise
        elif cls == 'Table':
            focused = action('get_current_item', obj, 'item')
            if len(focused) == 1: entry['CurrentColumn'] = info(focused[0])
            is_edit = action('current_mode_is_edit', obj, 'edit_mode')
            if type(is_edit) is not bool: raise Failure('state_unavailable', 'The row editing state is unknown.')
            if is_edit: entry['TableRowIsEditingNow'] = True
        elif cls == 'EditField':
            entry['FieldType'] = title(kind)
            if obj['key'] not in unknown_readonly and obj.get('readonly') is True:
                entry['ReadOnly'] = True
            if properties:
                if properties.get('text_edit') is False and entry.get('ReadOnly') is not True:
                    entry['AllowsTextInput'] = False
                if properties.get('input_hint', '').strip(): entry['InputHint'] = properties['input_hint']
            if not column:
                if kind == 'SpreadsheetDocumentField':
                    rows = action('get_doc_area_vertical_size', obj, 'size')
                    columns = action('get_doc_area_horizontal_size', obj, 'size')
                    data = {'RowCount': rows, 'ColumnCount': columns,
                            'CurrentState': action('get_state_presentation', obj, 'presentation')}
                    if type(rows) is not int or type(columns) is not int or rows < 0 or columns < 0:
                        raise Failure('state_unavailable', 'The spreadsheet dimensions are unknown.')
                    if rows + columns > 0: data['Hint'] = spreadsheet_hint(entry['ID'], english)
                else:
                    prop = 'text' if kind in ('InputField', 'RadioButtonField', 'LabelField') else 'presentation'
                    try:
                        data = action('get_text' if prop == 'text' else 'get_data_presentation', obj, prop)
                    except Failure as exc:
                        if not optional_refusal(adapter, exc.result): raise
                        data = ''
                        # Fields.getDisplayedText tries displayed text after a
                        # checkbox presentation refusal; other fields stay empty.
                        if kind == 'CheckBoxField':
                            try: data = action('get_text', obj, 'text')
                            except Failure as fallback:
                                if not optional_refusal(adapter, fallback.result): raise
                    if kind == 'CheckBoxField':
                        value = str(data).strip().casefold()
                        if value in ('да', 'yes', 'true', 'истина'): data = True
                        elif value in ('нет', 'no', 'false', 'ложь'): data = False
                    if isinstance(data, str) and separators is not None:
                        from compatible_scenarios.tester.tables import number_value
                        # NormalizeNumber in Tester returns normalized text, not a number.
                        from compatible_scenarios.shared.bsl.language import string
                        data = string(number_value(data, separators))
                if data not in ('', None) or kind == 'InputField': entry['Value'] = data
            if obj['key'] == editing.get('key') and editing.get('drop_list_open') is True:
                entry['DropList'] = [{'Index': c['index'], 'Value': c['text']} for c in context.get('choices', [])
                                     if c['field_key'] == obj['key']]
            if kind == 'RadioButtonField':
                values = action('get_choice_list', obj, 'items')
                entry['AvailableValues'] = [{'Presentation': i['presentation'], 'DisplayedText': i['text']} for i in values]
        branches = []
        for child in nested.get(obj['key'], []):
            description = describe(child, cls == 'Table' or kind == 'ColumnGroup', depth + 1)
            if description is not None: branches.append(description)
        if branches: entry['Items'] = branches
        return entry

    result = [describe(elements[form['key']])]
    # Recheck the form after the extra getters; never store a partial/mixed baseline.
    after = adapter.action('get_active_window')
    live = adapter.host.live(form['key'])
    focus = adapter.action('get_current_element', UIObject(form))
    focused = [i['key'] for i in focus.get('item', [])]
    expected_focus = [i['key'] for i in before_focus.get('item', [])]
    if (after.get('key') != window['key'] or not live or identity(live) != identity(form)
            or focus.get('current_status') == 'unavailable' or focused != expected_focus):
        raise Failure('context_unstable', 'The active form or focus changed during the read.')
    objects = adapter.action('find_objects', root_key=form['key'])['objects']
    def shape(values):
        return {o['key']: tuple(o.get(k) for k in ('handle', 'class', 'type', 'name', 'title'))
                for o in values if o['key'] != form['key']}
    if shape(objects) != shape(elements.values()):
        raise Failure('context_unstable', 'The form elements changed during the read.')
    if len(json.dumps(result, ensure_ascii=False)) > 1000000:
        raise Failure('scenario_limit', 'The window description exceeds 1000000 characters.')
    return identity(form), result


def run(adapter, changes=False):
    previous = adapter.window_baseline
    current = read(adapter)
    if current is None:
        if changes: raise Failure('active_form_unavailable', 'The active window has no managed form.')
        return None
    owner, tree = current
    selected = identity(adapter.current.data) if adapter.current is not None else None
    if changes and previous is not None and previous[0] == owner and selected == owner:
        from compatible_scenarios.shared.diff import compare
        result = compare(previous[1], tree, lambda: adapter.host.tick(**adapter.host.location))
    else: result = value_in(tree[0] if len(tree) == 1 else tree)
    adapter.window_baseline = owner, copy.deepcopy(tree)
    return result
