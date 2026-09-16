# -*- coding: utf-8 -*-
"""MCP-сервер управления тест-клиентом 1С напрямую (без тест-менеджера).
Инструменты поверх tc1c.TestClient. На Windows для NTLM используется SSPI.
"""
import re, os, sys, glob, socket, time, base64, subprocess, inspect, typing, functools, threading, json
from contextvars import ContextVar
from mcp.server.fastmcp import FastMCP
import tc1c
import guids as G
import _response
import _refs
import _collection
import _connections
import _screenshots
import _field_batches as _batches
import _snapshots
from mcp.types import CallToolResult, ImageContent, TextContent
import anyio
# Минимальная версия платформы 1С: по инструменту и по GUID метода (для воспроизведения
# сценариев, где метод известен только по GUID).
try:
    from tool_versions import TOOL_MIN_VERSION, GUID_MIN_VERSION
except Exception:
    TOOL_MIN_VERSION = {}; GUID_MIN_VERSION = {}

# ---- middle-байты (маркеры результата / особые) ----
RS   = tc1c.RES_SCALAR                 # e1      — скаляр/булево
RC   = tc1c.RES_COLLECTION             # e04b55  — коллекция
CHILD_MIDDLE = b'\xe1' + b'\x81' * 7   # GetChildObjects без фильтра
FIND_MIDDLE = b'\xe1\x82' + b'\x81' * 6  # FindObjects: рекурсивный поиск без фильтра
HTML_READ    = b'\xe1\xcb\x55'         # GetDocumentHTML
CHOICE_READ  = b'\xe2\xcb\x55'         # GetChoiceListPresentation (kind=read818)
PERF_MIDDLE  = b'\xe1\x82\xcb\x55'     # GetAccumulatedPerformanceIndicators (read818)
PERF_CLEAR   = b'\xe2\x82\xcb\x55'     # он же с ОчиститьНакопленные=Истина
CANCEL_EDIT_MID = b'\xe2'              # ОтменаРедактирования=Истина в кадре действия (e1=Ложь)
ERR_MIDDLE   = b'\xe0\x41'             # GetCurrentErrorInfo (pad=4)
REC_START    = b'\xe1\x81'             # StartUILogRecording (read818)
REC_FINISH   = b'\xe5\x81'             # FinishUILogRecording (read818)
# захваченная дата «следующего месяца» (ПерейтиНаМесяцВперед = GotoDate с датой)
CAL_MONTH_DATE = bytes.fromhex('f100b073f89b450200')

mcp = FastMCP(
    '1c-testpilot',
    host=os.environ.get('TC1C_HTTP_HOST', '127.0.0.1'),
    port=int(os.environ.get('TC1C_HTTP_PORT', '6004')),
    streamable_http_path=os.environ.get('TC1C_HTTP_PATH', '/mcp'),
)
_state = _connections.State()
_pool = _connections.Pool(_state)
_snapshot_store = _snapshots.Store()
SCREENSHOTS = os.environ.get('TC1C_SCREENSHOTS', 'true').strip().lower() not in ('false', '0', 'no', 'off')

_ACTIONS = {}          # группа -> {действие: функция-обработчик}

_SPREADSHEET_ACTIONS = frozenset({
    'set_current_area', 'get_current_area_address', 'get_current_area_text',
    'get_area_text', 'get_current_area_field', 'begin_edit_current_area',
    'end_edit_current_area', 'included_in_merged_area', 'text_within_area_bounds',
    'get_doc_area_horizontal_size', 'get_doc_area_vertical_size',
})
_spreadsheet_checked = ContextVar('spreadsheet_checked', default=None)

def _action(group):
    """Пометить функцию действием группы. В MCP она сама НЕ регистрируется: группа публикуется
    одним инструментом (см. _register_groups). В модуле остаётся функция с той же сигнатурой и
    обычными dict, поэтому прямые Python-вызовы и внутренний контур записи работают как раньше.

    Проверка цели оборачивает мутирующее действие, чтобы работать и при вызове через MCP,
    и при прямом Python-вызове, включая запись сценария."""
    def deco(fn):
        name = fn.__name__[3:]
        target = _verified(fn, name) if name in _VERIFY_ACTIONS else fn
        if name in ('expand', 'collapse', 'can_be_expanded', 'is_expanded',
                    'go_one_level_up', 'go_one_level_down'):
            target = _with_tree_criterion(target)
        if name in _SPREADSHEET_ACTIONS:
            target = _with_spreadsheet_target(target)
        target = _addressed(target, name)
        target = _with_operation_errors(target)
        _ACTIONS.setdefault(group, {})[name] = target
        return target
    return deco


def _with_operation_errors(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except tc1c.OperationError as exc:
            return exc.result()
        except _PlatformVersionError as exc:
            return exc.result
    return wrapped


def _with_spreadsheet_target(fn):
    """Reject known incompatible editors before observation, input or recording."""
    sig = inspect.signature(fn)
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        key = bound.arguments.get('key')
        c, error = _need_ver(TOOL_MIN_VERSION.get(fn.__name__, '8.3.1'))
        if error:
            return error
        area = bound.arguments.get('area')
        if (fn.__name__ in ('tc_get_area_text', 'tc_get_current_area_text')
                and area and ':' in area and not _guid_available(c, G.INCLUDED_IN_MERGED_AREA)):
            return _range_refused(key, area)
        # MoxelEditField is the spreadsheet's editing subobject, not a tree child.
        kind = 'SpreadsheetDocumentField' if _key_class(key) == 'MoxelEditField' else _kind_of(c, key)
        if kind and kind != 'SpreadsheetDocumentField':
            result = {'ok': False, 'target': key, 'code': 'unsupported_element_type',
                      'kind': kind, 'error': 'This action requires a spreadsheet-document field.'}
            if kind == 'TextDocumentField':
                result['suggested_action'] = 'get_data_presentation'
            elif kind in ('FormattedDocumentField', 'HTMLDocumentField'):
                result['suggested_action'] = 'get_html'
            return result
        # Unknown types are not evidence of incompatibility (older clients/subobjects).
        token = _spreadsheet_checked.set((c, key))
        try:
            return fn(*bound.args, **bound.kwargs)
        finally:
            _spreadsheet_checked.reset(token)
    return wrapped

class _TreeCriterionError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _tree_criteria(c, key, pairs):
    """Translate unambiguous column names/titles to the titles used by 1C row criteria."""
    if not pairs:
        return []
    if _key_class(key) != 'Table':
        raise _TreeCriterionError('invalid_table', 'Row criteria require a table.')
    for column, value in pairs:
        if not isinstance(column, str) or not column or value is None:
            raise _TreeCriterionError('invalid_row_criterion', 'Supply both row_column and row_value.')
    # The table address is known: its GetChildObjects is available from 8.3.1.
    # Resolving a name to a title is required for the native row criterion.
    columns = _table_columns(c, key)
    if not columns:
        raise _TreeCriterionError('columns_unavailable', 'The table columns could not be read.')
    result = []
    for column, value in pairs:
        matches = [o for o in columns if column in (o.get('name'), o.get('title'))]
        if not matches:
            raise _TreeCriterionError('column_not_found', 'Use a column name or title returned by find_objects for this table.')
        if len(matches) != 1 or not matches[0].get('title') or sum(
                o.get('title') == matches[0]['title'] for o in columns) != 1:
            raise _TreeCriterionError('ambiguous_column', 'The row criterion does not identify a unique column.')
        title = matches[0]['title']
        if any(t == title for t, _ in result):
            raise _TreeCriterionError('ambiguous_column', 'The row criterion repeats the same column.')
        result.append((title, value))
    return result


def _with_tree_criterion(fn):
    sig = inspect.signature(fn)
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        b = sig.bind(*args, **kwargs)
        column, value = b.arguments.get('row_column'), b.arguments.get('row_value')
        if column is not None or value is not None:
            key = b.arguments.get('key')
            try:
                b.arguments['row_column'] = _tree_criteria(_need(), key, [(column, value)])[0][0]
            except _TreeCriterionError as exc:
                return {'ok': False, 'target': key, 'code': exc.code, 'error': str(exc)}
        # Normalize before observation, so diagnostics measure the same row as the action.
        return fn(*b.args, **b.kwargs)
    return wrapped


class _PlatformVersionError(RuntimeError):
    def __init__(self, result):
        super().__init__(result['error'])
        self.result = result


def _version_refusal(action, minv, cur):
    result = {'ok': False, 'code': 'unsupported_platform_version',
              'error': 'this method needs 1C platform %s+, connected to %s' % (minv, cur),
              'available_since': minv, 'connected_version': cur}
    alternatives = {
        'tc_get_current_row': ('get_cell_text', 'Read the current row with get_cell_text and a column name; use read_rows for the table.'),
        'tc_select_row': ('goto_row', 'Use goto_row with toggle_selection=true to toggle the current row.'),
        'tc_deselect_row': ('goto_row', 'Use goto_row with toggle_selection=true to toggle the current row.'),
        'tc_deselect_all_rows': ('goto_row', 'Use goto_row without a search condition to leave only the current row selected.'),
    }
    if action in alternatives:
        result['suggested_action'], result['message'] = alternatives[action]
    return result


def _need():
    """Подключённый клиент. Заодно сверяет версию ПОДКЛЮЧЕНИЯ с минимальной версией
    вызывающего инструмента (TOOL_MIN_VERSION): старому клиенту нельзя слать неизвестный
    GUID — он не отвечает, и вызов висит до таймаута сокета."""
    if not _state['client']:
        raise RuntimeError('not connected — call tc_connect first')
    c = _state['client']
    action = sys._getframe(1).f_code.co_name
    minv = TOOL_MIN_VERSION.get(action)
    cur = _conn_ver(c)
    if minv and _vt(cur) and _vt(minv) and _vt(cur) < _vt(minv):
        raise _PlatformVersionError(_version_refusal(action, minv, cur))
    return c

def _vt(s):
    try: return tuple(int(x) for x in str(s).split('.')[:3])
    except Exception: return ()

# Методы, после которых активное окно может смениться (открылась форма/диалог, закрылось
# окно): кэш window_key после них недействителен — иначе следующая команда уйдёт старому окну.
_WINDOW_CHANGING = {G.CLICK, G.CLICK_FIELD, G.CLICK_DECORATION, G.CLICK_CI, G.START_CHOOSING, G.OPEN_FIELD, G.CREATE, G.CHOOSE_ROW,
                    G.CLICK_HTML_DOC_HYPERLINK, G.CLICK_FORMATTED_DOC_HYPERLINK,
                    G.CLICK_FORMATTED_STRING_HYPERLINK, G.DELETE_ROW, G.COPY_ROW,
                    G.SWITCH_ROW_DELETE_MARK, G.EXECUTE_COMMAND, G.CLOSE,
                    G.GOTO_NEXT_WINDOW, G.GOTO_PREVIOUS_WINDOW, G.GOTO_START_PAGE,
                    G.START_CHOOSING_FROM_CHOICE_LIST, G.EXECUTE_CHOICE_FROM_LIST,
                    G.EXECUTE_CHOICE_FROM_MENU, G.EXECUTE_CHOICE_FROM_CHOICE_LIST}

def _key_class(key):
    """Класс объекта из последнего сегмента ключа: '...EditField[Поле]' -> 'EditField'."""
    return _collection.object_class(key)

def _conn_ver(c):
    """Версия платформы этого соединения (аргумент tc_connect, иначе версия по умолчанию)."""
    return getattr(c, 'platform_version', None) or getattr(tc1c.TestClient, 'DEFAULT_VER', '8.3.1')

def _need_ver(minv):
    """Как _need, но с проверкой версии платформы. Возвращает (client, error):
    если метод требует версию новее подключённой — client=None, error=понятный dict
    (иначе вызов недоступного метода приводит к зависанию клиента, напр. 8.5.1 на 8.3.27)."""
    c = _need()
    cur = _conn_ver(c)
    if _vt(cur) and _vt(minv) and _vt(cur) < _vt(minv):
        return None, _version_refusal(sys._getframe(1).f_code.co_name, minv, cur)
    return c, None

# ================= предполётная проверка объекта-цели =======================
# CurrentVisible проверяет наличие и видимость цели до изменения состояния.
# Отказы самой операции дополнительно обрабатываются в TestClient.send_cmd.
VERIFY_TARGET = os.environ.get('TC1C_VERIFY_TARGET', 'true').strip().lower() \
                not in ('false', '0', 'no', 'off')

# Классы объектов, к которым применим CurrentVisible — по одному на каждый тип из справки
# (docs/TOOLS.md, «Применим к типам»); соответствие закреплено тестом, чтобы имя класса не
# разошлось с типом. Для остальных классов кадр НЕ отправляется: неприменимость метода — это
# отсутствие проверки ('unknown'), а не отсутствие объекта.
VERIFY_CLASS_BY_TYPE = {
    'ТестируемоеПолеФормы':                'EditField',
    'ТестируемаяКнопкаФормы':              'Button',
    'ТестируемаяТаблицаФормы':             'Table',
    'ТестируемаяГруппаФормы':              'Group',
    'ТестируемаяДекорацияФормы':           'Decoration',
    'ТестируемоеДополнениеЭлементаФормы':  'Additional',
}
_VERIFY_CLASSES = frozenset(VERIFY_CLASS_BY_TYPE.values())

def _verify_target(c, key, handle):
    """Проверка цели перед действием: (state, visible).

    state — 'present' | 'absent' | 'unknown' | 'off'. 'unknown' означает, что проверка НЕ
    выполнена (выше версии соединения, неприменимый тип, нераспознанный формат ответа). Это не
    «объекта нет»: блокировать действие по 'unknown' нельзя, иначе на платформе 8.3.1–8.3.2 запрет
    получат действия, которые там доступны (CurrentVisible — 8.3.3, click/input_text — 8.3.1).

    visible — True/False/None. Это НЕЗАВИСИМЫЙ факт из ТОГО ЖЕ кадра, а не следствие state:
    существующий объект бывает невидим, и действие по нему принимается, но эффекта не имеет.
    Дополнительного чтения ради видимости не выполняется."""
    if not VERIFY_TARGET:
        return 'off', None
    if not c or not key or _key_class(key) not in _VERIFY_CLASSES:
        return 'unknown', None
    minv = TOOL_MIN_VERSION.get('tc_is_visible')       # источник версии один — tool_versions
    cur = _conn_ver(c)
    if minv and _vt(cur) and _vt(minv) and _vt(cur) < _vt(minv):
        return 'unknown', None
    tr = getattr(c, '_track', None)
    try:
        c._track = None      # диагностический кадр — не действие пользователя, в запись не идёт
        r = c.send_cmd(G.CURRENT_VISIBLE, key, kind='read', middle=RS, handle=handle)
    except tc1c.OperationError as exc:
        return ('absent', None) if exc.status == 7 else ('unknown', None)
    except Exception:
        return 'unknown', None
    finally:
        c._track = tr
    if not r.get('ok'):
        return 'unknown', None       # кадр отказа о состоянии цели не сообщает ничего
    return tc1c.decode_target_state(r['raw']), _bool_from_resp(r['raw'])

# ---- разбор ответа: из кадра ОТКАЗА значений не бывает ---------------------
# В кадре отказа лежит диагностика платформы, из которой декодер достаёт правдоподобную строку.
# Охраняется СТАТУС, а не способ разбора: декодеры у читателей разные (документ — extract_strings,
# строки таблицы — decode_rows). Статус — у КОНКРЕТНОГО кадра, а не итоговый ok операции.

def _rec_reset():
    """Сбросить всё состояние записи сценария: признаков несколько, и рассыпанный сброс уже
    приводил к тому, что «запись идёт» переживала потерю накопленного."""
    for k in ('rec_paused_track', 'rec_obs', 'rec_obs_paused', 'rec_active',
              'rec_native_parts', 'rec_native_stopped', 'rec_native_errors', 'rec_native_covered'):
        _state.pop(k, None)


def _body(r):
    """Тело успешного кадра, иначе пусто — там, где разбирает свой код, а не декодер ниже."""
    return r['raw'] if r.get('ok') else b''

def _vals(r, min_len=1):
    """Короткие строки ответа; у отказа — пусто."""
    return tc1c.clean_strings(r['raw'], min_len) if r.get('ok') else []

# Сколько байт 0x20 стоит в ответе перед завершением кадра. Это свойство КОМАНДЫ, а не значения:
# у одной команды число одинаково и для пустого значения, и для одного символа, и для длинной
# строки. Компактную форму читаем только у команд отсюда: для остальных число неизвестно, а
# гадать нельзя — байт значения может совпасть с байтом тега.
_TAIL_PAD = {G.GET_DISPLAYED_TEXT: 1, G.GET_EDIT_TEXT: 1, G.GET_TOOLTIP: 1,
             G.GET_AREA_TEXT: 1, G.GET_CURRENT_AREA_TEXT: 1,
             G.GET_PROPERTY: 2, G.GET_CELL_TEXT: 2}

def _tail_char(raw, cmd):
    """Односимвольное значение из компактной формы хвоста ответа на команду `cmd`.
    Непечатный байт значением не считаем."""
    b = tc1c.decode_tail_byte(raw, _TAIL_PAD.get(cmd))
    return chr(b) if b is not None and 0x20 <= b <= 0x7e else None

def _scalar_text(r, cmd):
    """Значение-строка из ответа на команду `cmd`. Если обычный разбор ничего не дал, пробуем
    компактную форму «один байт» из хвоста: односимвольное значение приходит именно ею, и без
    этого «в поле один символ» неотличимо от «поле пусто»."""
    if r.get('ok') and cmd in (G.GET_DISPLAYED_TEXT, G.GET_EDIT_TEXT, G.GET_PROPERTY):
        text = tc1c.decode_field_text(r['raw'], property_value=cmd == G.GET_PROPERTY)
        if text is not None:
            return text
    vals = _vals(r)
    if vals:
        return vals[-1]
    return _tail_char(r['raw'], cmd) if r.get('ok') else None

def _field_scalar_text(c, key, r, cmd):
    """Пустая строка поля: подтверждённый вид и полный известный ответ.

    Такой же скалярный хвост бывает у неподдерживаемых элементов. Одного маркера
    недостаточно: у колонки дополнительно проверяем открытый редактор этой ячейки.
    У отсутствующей цели перед значением другой маркер; отсутствие/обрыв кадра
    тоже не являются пустой строкой.
    """
    value = _scalar_text(r, cmd)
    if value is not None or not r.get('ok'):
        return value
    tails = {
        G.GET_DISPLAYED_TEXT: b'\x81\x81\x81\xe1\x20\xa1\xa3',
        G.GET_EDIT_TEXT: b'\x81\x81\x81\xe1\x20\xa1\xa3',
        G.GET_PROPERTY: b'\x81\x81\x81\xe0\x4b\x53\x81\x20\x20\xa1\xa3',
    }
    tail = tails.get(cmd)
    if tail and r['raw'].startswith(b'\x42') and r['raw'].endswith(tail + tc1c.TR):
        cls = _key_class(key)
        if cls == 'EditField' and _kind_of(c, key) == 'InputField':
            if '.Table[' not in key:
                return ''
            p = tc1c._reply_status_offset(r['raw'], cmd)
            if (p is not None and r['raw'][p:] == tail + tc1c.TR
                    and _active_column_editor(c, key)):
                return ''
        if (cls == 'Additional' and cmd == G.GET_EDIT_TEXT
                and _kind_of(c, key) == 'SearchStringRepresentation'):
            return ''
    return None


def _active_column_editor(c, key):
    """An empty scalar is meaningful only for this table's current input editor."""
    table = _table_owner(key)
    if not table or not all(_guid_available(c, g) for g in
                            (G.CURRENT_MODE_IS_EDIT, G.GET_CURRENT_ITEM)):
        return False
    try:
        handle = _own_handle(c, table)
        if not handle or _cell_flag(c, G.CURRENT_MODE_IS_EDIT, table, handle) is not True:
            return False
        current = tc_get_current_item(table, handle)
        return (current.get('ok') and len(current.get('item', [])) == 1
                and current['item'][0].get('key') == key)
    except Exception:
        return False  # Unreadable state is not evidence of an empty cell.


def _remember_handles(objects):
    c = _state.get('client')
    if c is not None:
        registry = _refs.for_client(c)
        for obj in objects:
            if obj.get('key') and obj.get('handle'):
                registry[obj['key']] = obj['handle']
    return objects


def _coll(r, parent=None, remember=True):
    """Коллекция объектов ответа; у отказа — пусто."""
    objects = tc1c.decode_collection(r['raw'], parent) if r.get('ok') else []
    return _remember_handles(objects) if remember else objects

def _rows(r):
    """Строки таблицы; у отказа — пусто."""
    return tc1c.decode_rows(r['raw']) if r.get('ok') else []

def _scalar(r, fn, *a):
    """Скаляр (булево, признак развёрнутости, результат поиска строки); у отказа — None,
    то есть «значения нет», а не «значение ложно»."""
    return fn(r['raw'], *a) if r.get('ok') else None

# ---- адресация колонки: индекс и заголовок различаются по НАПИСАНИЮ ---------
# Имя элемента формы в 1С не может начинаться с цифры, поэтому '0' — это всегда индекс. Иначе
# цифровая строка уходила бы как имя колонки и молча давала null.

def _is_index(column):
    return (isinstance(column, int) and not isinstance(column, bool)) or \
           (isinstance(column, str) and column.strip().isdigit())

def _as_index(column):
    return int(column) if isinstance(column, str) and column.strip().isdigit() else column

_COLUMN_BY_TITLE = ('column: here the column is addressed by its TITLE as shown in the table; '
                    'an index is accepted only by get_cell_text')

def _blocked_by_version(action, c):
    """Действие недоступно на этом подключении — проверять его цель незачем: обработчик всё равно
    откажет по версии, а лишний кадр уже уйдёт."""
    minv = TOOL_MIN_VERSION.get('tc_' + action)
    cur = _conn_ver(c)
    return bool(minv and _vt(cur) and _vt(minv) and _vt(cur) < _vt(minv))

def _absent_error(key, check='absent'):
    return {'ok': False, 'target': key, 'target_check': check,
            'error': 'no object at this address: %s. Addresses come from walking the tree '
                     '(tc_app(action="get_child_objects") / tc_find(action="find_objects")), '
                     'they are not composed by hand.' % key}

# Действия, перед которыми проверяется существование цели. Перечень ЯВНЫЙ: вид кадра семантику не
# задаёт — goto_row и построчная навигация меняют текущую строку и выделение, но отправляются с
# kind='read'. Сессионные команды без объекта-получателя (connect, оконные переходы,
# set_file_dialog_result) в перечень не входят.
_VERIFY_ACTIONS = frozenset({
    # ввод и клик
    'input_text', 'input_html', 'click', 'activate', 'set_check', 'clear', 'cancel_edit', 'create',
    'open_field', 'start_choosing', 'start_choosing_from_choice_list', 'select_option',
    'goto_value', 'increase_value', 'decrease_value',
    'open_drop_list', 'close_drop_list', 'choose_from_drop_list', 'execute_choice_from_choice_list',
    'click_view_status_item', 'delete_view_status_item',
    # календарь
    'goto_date', 'calendar_next_month', 'calendar_previous_month',
    'calendar_next_year', 'calendar_previous_year',
    # таблица: строки, выделение, порядок, иерархия
    'table_add_row', 'delete_row', 'copy_row', 'change_row', 'end_edit_row', 'choose_row',
    'switch_row_delete_mark', 'set_order', 'expand', 'collapse',
    'go_one_level_down', 'go_one_level_up',
    'select_row', 'deselect_row', 'select_all_rows', 'deselect_all_rows',
    'goto_row', 'goto_first_row', 'goto_last_row', 'goto_next_row', 'goto_previous_row',
    'goto_next_item', 'goto_previous_item',
    # документы
    'set_current_area', 'begin_edit_current_area', 'end_edit_current_area',
    'write_content_to_file', 'click_html_hyperlink', 'click_formatted_doc_hyperlink',
    'click_formatted_string_hyperlink',
    # форма
    'execute_choice_from_list', 'execute_choice_from_menu',
    'goto_next_element', 'goto_previous_element',
})

# Те же операции на уровне ПРОТОКОЛА — для воспроизведения сценария, где действие известно по
# GUID, а не по имени инструмента. Перечни обязаны совпадать по составу операций.
_VERIFY_GUIDS = frozenset({
    G.INPUT_TEXT, G.INPUT_HTML, G.CLICK, G.CLICK_FIELD, G.CLICK_DECORATION, G.CLICK_CI, G.SET_CHECK, G.CLEAR, G.CANCEL_EDIT, G.CREATE,
    G.OPEN_FIELD, G.START_CHOOSING, G.START_CHOOSING_FROM_CHOICE_LIST, G.SELECT_OPTION,
    G.GOTO_VALUE, G.INCREASE_VALUE, G.DECREASE_VALUE, G.ACTIVATE,
    G.OPEN_DROP_LIST, G.CLOSE_DROP_LIST, G.CHOOSE_FROM_DROP_LIST, G.EXECUTE_CHOICE_FROM_CHOICE_LIST,
    G.CLICK_VIEW_STATUS_ITEM, G.DELETE_VIEW_STATUS_ITEM,
    G.GOTO_DATE, G.GOTO_NEXT_MONTH, G.GOTO_PREVIOUS_MONTH, G.GOTO_NEXT_YEAR, G.GOTO_PREVIOUS_YEAR,
    G.ADD_ROW, G.DELETE_ROW, G.COPY_ROW, G.CHANGE_ROW, G.END_EDIT_ROW, G.CHOOSE_ROW,
    G.SWITCH_ROW_DELETE_MARK, G.SET_ORDER,
    # у разворачивания/сворачивания ДВА метода: группа формы и узел таблицы. Воспроизведение
    # шага <expand/> внутри FormTable шлёт табличный, поэтому нужны оба.
    G.EXPAND_GROUP, G.COLLAPSE_GROUP, G.EXPAND_TABLE, G.COLLAPSE_TABLE,
    G.GO_ONE_LEVEL_DOWN, G.GO_ONE_LEVEL_UP,
    G.SELECT_ROW, G.DESELECT_ROW, G.SELECT_ALL_ROWS, G.DESELECT_ALL_ROWS,
    G.GOTO_ROW, G.GOTO_FIRST_ROW, G.GOTO_LAST_ROW, G.GOTO_NEXT_ROW, G.GOTO_PREVIOUS_ROW,
    G.GOTO_NEXT_ITEM, G.GOTO_PREVIOUS_ITEM, G.FORM_GOTO_NEXT_ITEM, G.FORM_GOTO_PREVIOUS_ITEM,
    G.SET_CURRENT_AREA, G.BEGIN_EDIT_CURRENT_AREA, G.END_EDIT_CURRENT_AREA,
    G.WRITE_CONTENT_TO_FILE, G.CLICK_HTML_DOC_HYPERLINK, G.CLICK_FORMATTED_DOC_HYPERLINK,
    G.CLICK_FORMATTED_STRING_HYPERLINK,
    G.EXECUTE_CHOICE_FROM_LIST, G.EXECUTE_CHOICE_FROM_MENU,
})

# Чтения, у которых признак существования лежит на известном месте. Для остальных read-методов
# состояние остаётся 'unknown': значение при этом не меняется — статус адресата и достоверность
# прочитанного это разные вопросы.
_TARGET_STATE_READS = frozenset({G.CURRENT_VISIBLE})

# ---- наблюдение до/после действия ------------------------------------------
# Кадр действия эффекта не сообщает: у построчной навигации ответы «строка сменилась» и «не
# менялась» побайтово одинаковы. Единственный способ что-то утверждать — прочитать состояние
# обратно. Наблюдение НАЗЫВАЕТСЯ в ответе (`observed`), потому что оно не равно «состоянию»:
# текст колонки не идентифицирует строку, в списке бывают строки с одинаковым текстом.
READBACK = os.environ.get('TC1C_READBACK', 'true').strip().lower() \
           not in ('false', '0', 'no', 'off')

def _obs_text(c, key, handle):
    """Отображаемый текст элемента: один кадр вместо двух у _read_value.

    Пустое поле — это НАБЛЮДЕНИЕ '' , а не отсутствие наблюдения: иначе ввод в пустое поле,
    самое частое действие, всегда давал бы changed=null. None остаётся только там, где прочитать
    не удалось: метод недоступен по версии либо клиент ответил отказом."""
    if _vt(_conn_ver(c)) < (8, 3, 12):        # GetDisplayedText — с 8.3.12
        return None
    r = c.send_cmd(G.GET_DISPLAYED_TEXT, key, kind='read', middle=RS, handle=handle)
    if not r['ok']:
        return None
    # разбор тот же, что у публичных чтений: односимвольное значение приходит компактной формой,
    # и без неё ввод 'X' выглядел бы как «поле осталось пустым»
    v = _scalar_text(r, G.GET_DISPLAYED_TEXT)
    if v is None and (_key_class(key) == 'MoxelEditField' or
                      _kind_of(c, key) in ('SpreadsheetDocumentField', 'TextDocumentField')):
        # Пока идёт редактирование ячейки, GetAreaText ещё возвращает старое значение,
        # а GetDisplayedText документа ничего не говорит о буфере ячейки.
        return None
    return v if v is not None else ''

# Виды полей, которые документом не являются: их содержимое через tc_doc не читается, и пустой
# ответ у них означает не «документ пуст», а «это не документ».
_NOT_DOC_KINDS = ('GraphicalSchemaField', 'PlannerField', 'PictureField', 'ChartField',
                  'DendrogramField', 'GanttChartField', 'GeographicalSchemaField')

def _kind_of(c, key):
    """Вид элемента по ТОЧНОМУ адресу: один кадр GetChildObjects по непосредственному родителю.
    Обход всего дерева для одного `type` не нужен — он шлёт кадр на каждый узел.
    None, когда родителя в ключе нет, кадр отвергнут или цели среди детей не оказалось: у
    подобъектов (.CI, .ContextMenu, .CommandPanel, .MoxelEditField) детей родителя не бывает,
    и это штатный исход, а не признак чего-либо."""
    if not key or '.' not in key:
        return None
    if not _guid_available(c, G.GET_CHILD_OBJECTS):
        return None
    parent = _collection_parent(key)
    if not parent:
        return None
    # Addressed form/field metadata exists from 8.3.1. Window children require
    # 8.3.2; the public tool's 8.3.3 minimum includes finding the active window.
    version = _vt(_conn_ver(c))
    if _key_class(parent) in ('MainFrame', 'SecondaryFrame') and version and version < (8, 3, 2):
        return None
    try:
        r = c.send_cmd(G.GET_CHILD_OBJECTS, parent, kind='read', middle=CHILD_MIDDLE)
    except Exception:
        return None
    for it in _coll(r, parent):
        if it.get('key') == key:
            return it.get('type')
    return None

# Чтения группы tc_doc, у которых пустой ответ может объясняться видом элемента, и критерий
# пустоты КАЖДОГО. Перечень здесь один: иначе добавленное позже чтение молча останется без
# объяснения, как это уже случилось в прошлом цикле (защита стояла у трёх чтений из десяти).
# Критерии выписаны по фактическому возврату обработчика, а не по смыслу его имени:
#   get_html                     html         пусто: None
#   get_doc_area_vertical_size   size         пусто: 0 и None
#   get_doc_area_horizontal_size size         пусто: 0 и None
#   get_area_text                text         пусто: None (строка '' — это результат)
#   get_current_area_text        text         пусто: None
#   get_current_area_address     address      пусто: None
#   get_current_area_field       field        пусто: [] (возвращает КОЛЛЕКЦИЮ, не адрес)
#   get_formatted_string_hyperlinks hyperlinks пусто: []
#   included_in_merged_area      merged_area  пусто: None (возвращает АДРЕС строкой, не булево)
#   text_within_area_bounds      fits         пусто: None (False — это результат)
_DOC_READS = ('get_html', 'get_doc_area_vertical_size', 'get_doc_area_horizontal_size',
              'get_area_text', 'get_current_area_text', 'get_current_area_address',
              'get_current_area_field', 'get_formatted_string_hyperlinks',
              'included_in_merged_area', 'text_within_area_bounds')

def _doc_read(c, key, handle, result, empty):
    """Объяснить пустой ответ видом элемента, если его ещё не проверила обёртка действия."""
    if not empty or _spreadsheet_checked.get() == (c, key):
        return result
    return _not_a_document(c, key, handle) or result

def _not_a_document(c, key, handle):
    """Отказ, если пустой ответ объясняется видом элемента. None — оставить ответ как есть:
    неразрешённый вид доказательством неприменимости метода не является."""
    kind = _kind_of(c, key)
    if kind in _NOT_DOC_KINDS:
        return {'ok': False, 'target': key, 'kind': kind, 'error':
                'this element is a %s, not a document field — its content is not readable '
                'through tc_doc' % kind}
    return None

def _unavailable_read(result, field, *, suggested_action=None, message=None):
    """Explain missing values without treating them as empty content."""
    if result.get(field) is not None:
        return result
    result['value_status'] = 'unavailable'
    result['message'] = message or 'No value was returned; this does not establish that the field is empty.'
    if suggested_action:
        result['suggested_action'] = suggested_action
    return result


def _html_target(c, key, *, write=False):
    kind = _kind_of(c, key)
    supported = ('FormattedDocumentField',) if write else ('FormattedDocumentField', 'HTMLDocumentField')
    if kind and kind not in supported:
        return {'ok': False, 'target': key, 'code': 'unsupported_element_type',
                'kind': kind, 'error': ('HTML input requires a formatted-document field.' if write else
                                        'HTML reading requires a formatted-document or HTML-document field.')}
    return None


_TRACKBAR_KIND = 'TrackBarField'      # единственный вид, где применим переход к значению

def _not_applicable(c, key, verdict):
    """Ветка «команда принята, а значение не сдвинулось»: ОДИН адресованный кадр за видом элемента,
    чтобы отличить неприменимый метод от отсутствия эффекта. Неизвестный вид доказательством не
    считается — как и в `_not_a_document`."""
    kind = _kind_of(c, key)
    return verdict(kind) if kind else None

def _committed(c, args, after):
    """Compare the accepted data presentation, never displayed input alone.
    A different representation is inconclusive unless the requested text is still pending.
    Diagnostic reads must not enter a recording.
    """
    want = args.get('text')
    if after is None or not isinstance(want, str):
        return None
    # то же обхождение, что у _observe: диагностическое чтение не смеет ни уронить уже принятое
    # действие, ни попасть в запись сценария — оно не действие пользователя
    tr = getattr(c, '_track', None)
    try:
        c._track = None
        r = c.send_cmd(G.GET_PROPERTY, args.get('key'), kind='read', middle=RC,
                       handle=args.get('handle'))
        accepted = _field_scalar_text(c, args.get('key'), r, G.GET_PROPERTY)
        if accepted == want:
            return True
        if after == want:
            return False if accepted is not None else None
        r = c.send_cmd(G.GET_EDIT_TEXT, args.get('key'), kind='read', middle=RS,
                       handle=args.get('handle'))
    except Exception:
        return None                  # прочитать не вышло — оснований судить нет
    finally:
        c._track = tr
    vals = _vals(r)
    return False if (vals and vals[-1] == want) else None

def _target_disabled(c, args):
    """Недоступен ли элемент. Спрашиваем ТОЛЬКО когда ввод не записался: у недоступного элемента
    команда принимается и не делает ничего, и без этого ответ не отличить от «не прочиталось».
    True лишь при явном «нет»; отказ чтения оставляет вопрос открытым."""
    tr = getattr(c, '_track', None)
    try:
        c._track = None
        r = c.send_cmd(G.CURRENT_ENABLE, args.get('key'), kind='read', middle=RS,
                       handle=args.get('handle'))
    except Exception:
        return False
    finally:
        c._track = tr
    return _scalar(r, _bool_from_resp) is False

def _obs_expanded(c, key, handle, row_column=None, row_value=None, pairs=None):
    """Развёрнутость узла ТАБЛИЦЫ. Для группы формы наблюдателя нет: Развернут применим только
    к таблице, и подставлять его группе значило бы измерять не то. Строку выбирает ВЕСЬ критерий
    действия: по одной паре из нескольких нашёлся бы другой узел с тем же значением первой
    колонки, и наблюдение относилось бы не к тому, что меняли."""
    if _key_class(key) != 'Table':
        return None
    mid = tc1c.mk_tree_middle(b'\xe1\xcb\x55', row_column, row_value, pairs=pairs)
    r = c.send_cmd(G.EXPANDED, key, kind='read', middle=mid, handle=handle,
                   pad=tc1c.tree_row_pad(row_column, pairs))
    return _scalar(r, tc1c.decode_expanded)

def _obs_value(c, key, handle):
    """Значение поля: отображаемый текст, иначе представление данных. Два кадра, зато работает
    и на ссылочных полях, у которых отображаемого текста нет."""
    txt, pres = _read_value(c, key, handle)
    return txt or pres

def _obs_window(c, key, handle):
    """Ключ активного окна. Наблюдение для выбора строки: удавшийся выбор закрывает форму
    выбора, то есть активным становится другое окно. Кэш сбрасывается, иначе «до» и «после»
    вернут одно и то же значение из памяти."""
    _state['window_key'] = None
    return _window(c).get('key')

def _resolve_column(c, key, handle, column):
    """ЗАГОЛОВОК колонки -> ИМЯ её элемента. None, если разрешить нельзя.

    Платформа адресует колонку по-разному в соседних методах: поиск строки принимает ЗАГОЛОВОК,
    чтение ячейки — ИМЯ ЭЛЕМЕНТА. Там, где нам достался заголовок, а прочитать надо ячейку,
    перевод делается здесь — единственным местом на оба пути наблюдения.

    Неоднозначность разрешается ОТКАЗОМ: один заголовок могут носить несколько колонок, и выбор
    первой попавшейся вернул бы значение чужой колонки под видом запрошенной."""
    if not column or not isinstance(column, str):
        return None
    # Optional traversal must not poison a supported action on older clients.
    version = _vt(_conn_ver(c))
    if version and version < _vt(TOOL_MIN_VERSION['tc_get_child_objects']):
        return None
    try:
        cols = [i for i in _walk_tree(c, key)
                if i.get('class') == 'EditField' and i.get('title') == column]
    except Exception:
        return None
    if len(cols) != 1:
        return None                      # не нашлось или несколько — наблюдения не будет
    return cols[0].get('name')


def _obs_cell(c, key, handle, column):
    """Текст ячейки текущей строки в названной колонке. Пустая ячейка — наблюдение '', а не
    отсутствие наблюдения."""
    if column is None or column == '':
        return None
    r = c.send_cmd(G.GET_CELL_TEXT, key, kind='read', middle=tc1c.mk_cell(column), handle=handle)
    if not r['ok']:
        return None
    decoded, value = tc1c.decode_cell_text(r['raw'])
    if decoded:
        return value if value is not None else ''
    vals = _vals(r)
    if vals and vals[-1] == column:
        vals = vals[:-1]
    if vals:
        return vals[-1]
    v = _scalar_text_only_tail(r, G.GET_CELL_TEXT)
    return v if v is not None else ''


def _observe_area(c, key, handle, prepared=None):
    """Зафиксировать адрес до завершения редактирования и прочитать ту же область после."""
    if not _guid_available(c, G.GET_AREA_TEXT):
        return None, None
    if prepared is None:
        r = c.send_cmd(G.GET_CURRENT_AREA_ADDRESS, key, kind='read', middle=RS, handle=handle)
        values = _vals(r)
        area = values[-1] if values else None
    else:
        area = prepared.get('area')
    if not area:
        return None, None
    r = c.send_cmd(G.GET_AREA_TEXT, key, kind='read', middle=tc1c.mk_area(area), handle=handle)
    if not r['ok']:
        return {'kind': 'area_text', 'area': area}, None
    text = _area_value(r['raw'], area, G.GET_AREA_TEXT)
    return {'kind': 'area_text', 'area': area}, text if text is not None else ''

# Действие -> чем наблюдаем. Операции, которых здесь нет, наблюдения не имеют и отдают
# changed=null: у `set_order` проверка потребовала бы перехода на первую строку, то есть
# диагностика меняла бы ровно то, что проверяет.
_OBSERVERS = {
    'input_text': ('displayed_text', _obs_text),
    'clear':      ('displayed_text', _obs_text),
    'expand':     ('expanded', _obs_expanded),
    'collapse':   ('expanded', _obs_expanded),
    'end_edit_current_area': ('area_text', None),
}
for _a in ('goto_first_row', 'goto_last_row', 'goto_next_row', 'goto_previous_row', 'goto_row',
           'go_one_level_up', 'go_one_level_down'):
    _OBSERVERS[_a] = ('cell_text', _obs_cell)
# действия, которые раньше читали значение сами: тот же контракт, одна реализация
for _a in ('set_check', 'goto_date', 'increase_value', 'decrease_value', 'select_option',
           'goto_value', 'choose_from_drop_list', 'execute_choice_from_choice_list'):
    _OBSERVERS[_a] = ('field_value', _obs_value)
# у выбора строки наблюдается не значение, а ОКНО: удавшийся выбор закрывает форму выбора
_OBSERVERS['choose_row'] = ('active_window', _obs_window)

# Операции, чей результат оформляется как наблюдение, даже когда наблюдателя нет. `set_order`
# наблюдать нечем: достоверная проверка сортировки потребовала бы перехода на первую строку, то
# есть диагностика меняла бы ровно то, что проверяет.
_READBACK_ACTIONS = set(_OBSERVERS) | {'set_order', 'switch_row_delete_mark'}

# Те же наблюдатели на уровне ПРОТОКОЛА — для воспроизведения сценария, где операция известна по
# GUID. Построчной навигации здесь нет: наблюдение требует названной колонки, а в шаге сценария
# её нет, и угадывать колонку значило бы наблюдать не то.
_OBS_GUIDS = {
    G.INPUT_TEXT: ('displayed_text', _obs_text),
    G.CLEAR:      ('displayed_text', _obs_text),
    G.END_EDIT_CURRENT_AREA: ('area_text', None),
    G.EXPAND_TABLE:   ('expanded', _obs_expanded),
    G.COLLAPSE_TABLE: ('expanded', _obs_expanded),
    G.SET_CHECK:  ('field_value', _obs_value),
    G.GOTO_DATE:  ('field_value', _obs_value),
    G.INCREASE_VALUE: ('field_value', _obs_value),
    G.DECREASE_VALUE: ('field_value', _obs_value),
    G.SELECT_OPTION:  ('field_value', _obs_value),
    G.GOTO_VALUE:     ('field_value', _obs_value),
    G.CHOOSE_FROM_DROP_LIST: ('field_value', _obs_value),
    G.EXECUTE_CHOICE_FROM_CHOICE_LIST: ('field_value', _obs_value),
    G.GOTO_ROW:   ('cell_text', _obs_cell),   # колонку даёт <Field title=...> самого шага
    G.GO_ONE_LEVEL_UP: ('cell_text', _obs_cell),
    G.GO_ONE_LEVEL_DOWN: ('cell_text', _obs_cell),
    # выбор строки: наблюдается ОКНО, и для этого от шага ничего не требуется — в отличие от
    # построчной навигации, где нужна названная колонка. Поэтому шаг сценария получает то же
    # наблюдение, что и прямой вызов
    G.CHOOSE_ROW: ('active_window', _obs_window),
}

# Операции, чей шаг обязан сказать про наблюдение, даже если наблюдения не вышло. Построчная
# навигация сюда входит: колонки в шаге нет, наблюдения не будет, но ответ должен это сказать —
# ровно как прямой вызов без параметра column.
_READBACK_GUIDS = set(_OBS_GUIDS) | {
    G.SET_ORDER, G.GOTO_FIRST_ROW, G.GOTO_LAST_ROW, G.GOTO_NEXT_ROW, G.GOTO_PREVIOUS_ROW,
    # пометка удаления читаемого признака не имеет: наблюдения не будет, но сказать об этом шаг
    # обязан ровно так же, как прямой вызов
    G.SWITCH_ROW_DELETE_MARK,
    # раскрытие ГРУППЫ формы: наблюдателя нет (Развернут — метод таблицы), но сказать об этом
    # шаг обязан, как это делает прямой вызов
    G.EXPAND_GROUP, G.COLLAPSE_GROUP}

def _observe_guid(c, guid, key, handle, args=None, prepared=None):
    """Наблюдение для шага воспроизведения. Параметры логической операции (строка дерева,
    колонка) приходят из самого шага: наблюдать надо ТОТ узел, который меняем. Как и у
    обработчиков, диагностический кадр не идёт в запись сценария."""
    ent = _OBS_GUIDS.get(guid)
    if not READBACK or not ent or not c or not key:
        return None, None
    kind, fn = ent
    args = args or {}
    tr = getattr(c, '_track', None)
    try:
        c._track = None
        if kind == 'area_text':
            return _observe_area(c, key, handle, prepared)
        if kind == 'expanded':
            val = fn(c, key, handle, args.get('row_column'), args.get('row_value'),
                     args.get('row_pairs'))
        elif kind == 'cell_text':
            if prepared is not None:
                col = prepared.get('column')      # адрес разрешён один раз на операцию
            else:
                col = args.get('column')
                if not col:
                    return None, None
                # в шаге сценария колонка записана как <Field title=...>, то есть ЗАГОЛОВОК,
                # а читать ячейку платформа умеет по имени элемента
                col = _resolve_column(c, key, handle, col)
                if not col:
                    return None, None             # не разрешилось или неоднозначно
            return {'kind': kind, 'column': col}, fn(c, key, handle, col)
        else:
            val = fn(c, key, handle)
        return ({'kind': kind} if val is not None else None), val
    except Exception:
        return None, None
    finally:
        c._track = tr

def _observe(c, action, args, prepared=None):
    """Снять наблюдение. Возвращает (описание, значение) или (None, None), если наблюдения нет.
    Диагностический кадр не идёт в запись сценария — он не действие пользователя."""
    ent = _OBSERVERS.get(action)
    key = args.get('key')
    if not READBACK or not ent or not c or not key:
        return None, None
    kind, fn = ent
    tr = getattr(c, '_track', None)
    try:
        c._track = None
        if kind == 'area_text':
            return _observe_area(c, key, args.get('handle'), prepared)
        if kind == 'cell_text':
            if prepared is not None:
                col = prepared.get('column')      # адрес разрешён один раз на операцию
            else:
                col = args.get('column')
                if col is None or col == '':
                    col = next(iter(args.get('fields') or {}), None)
                if col is None or col == '':
                    return None, None        # без колонки наблюдать нечем
                if action == 'goto_row':
                    # у поиска строки колонка — КРИТЕРИЙ, то есть заголовок; у пошаговой
                    # навигации она лишь НАЗЫВАЕТ наблюдение и уже является именем элемента
                    col = _resolve_column(c, key, args.get('handle'), col)
                    if not col:
                        return None, None    # не разрешилось или неоднозначно
            return {'kind': kind, 'column': col}, fn(c, key, args.get('handle'), col)
        if kind == 'expanded':
            val = fn(c, key, args.get('handle'), args.get('row_column'),
                     args.get('row_value'), args.get('row_pairs'))
        else:
            val = fn(c, key, args.get('handle'))
        # None означает «наблюдатель к этой цели неприменим» (напр. Развернут на группе формы)
        return ({'kind': kind} if val is not None else None), val
    except Exception:
        return None, None
    finally:
        c._track = tr

# Применимость метода к ВИДУ элемента. Спрашивается только когда значение не сдвинулось:
# False — метод точно не для этого вида, None — вид не запрещает (или неизвестен).
_APPLICABILITY = {
    'increase_value': lambda kind: False if kind == _TRACKBAR_KIND else None,
    'decrease_value': lambda kind: False if kind == _TRACKBAR_KIND else None,
    'goto_value':     lambda kind: False if kind != _TRACKBAR_KIND else None,
}

def _addressed(fn, action):
    """Проверка формы адресов и пары из ранее выданной коллекции, без сетевых команд."""
    sig = inspect.signature(fn)
    if not any(p in sig.parameters for p in ('key', 'root_key', 'handle')):
        return fn
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        c = _state.get('client')
        if c is None or _blocked_by_version(action, c):
            return fn(*a, **kw)
        args = sig.bind(*a, **kw).arguments
        try:
            tc1c.validate_address(args.get('key'), args.get('handle'))
            tc1c.validate_address(args.get('root_key'))
        except ValueError as exc:
            return {'ok': False, 'target_check': 'invalid', 'error': str(exc)}
        expected = getattr(c, '_object_handles', {}).get(args.get('key'))
        if expected and args.get('handle') and expected.lower() != args['handle'].lower():
            return {'ok': False, 'target_check': 'invalid', 'error':
                    'key and handle do not match; use both values from the same element returned by the tools'}
        return fn(*a, **kw)
    return wrapper


def _verified(fn, action):
    """Обработчик с предполётной проверкой цели — ОДИН раз на логическую операцию.

    Обёртка не встаёт между обработчиком и `_need`: `_need` вызывается ИЗ тела обработчика, и
    `sys._getframe(1)` по-прежнему указывает на него, поэтому версионная защита не снимается.
    Сигнатура и докстринг сохраняются через functools.wraps, поэтому описание действия и схема
    группы строятся как раньше."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        c = _state['client']
        if c is None or _blocked_by_version(action, c):
            return fn(*a, **kw)          # обработчик сам откажет: без соединения или по версии
        b = sig.bind(*a, **kw); b.apply_defaults()
        key = b.arguments.get('key')
        state, visible = _verify_target(c, key, b.arguments.get('handle'))
        if state == 'absent':
            return _absent_error(key)
        if action in ('goto_first_row', 'goto_last_row', 'goto_next_row', 'goto_previous_row',
                      'go_one_level_up', 'go_one_level_down') and b.arguments.get('column'):
            error = _column_error(c, key, b.arguments['column'])
            if error:
                return error
        obs, before = _observe(c, action, b.arguments)
        res = fn(*a, **kw)
        if not isinstance(res, dict):
            return res
        if action == 'activate' and res.get('ok'):
            page = _page_activation_result(c, key, b.arguments.get('handle'))
            if page:
                res.update(page)
                state, visible = page['target_check'], page['visible']
        res['target_check'] = state
        if state == 'present' and visible is False:
            # цель СУЩЕСТВУЕТ, но невидима: команду клиент примет, эффекта не будет. Условие
            # включает state: у отсутствующей цели булев результат кадра видимостью не является,
            # иначе ответ утверждал бы «объекта нет» и «объект невидим» одновременно.
            # Поле есть только при true — его отсутствие утверждением о видимости НЕ является
            res['target_hidden'] = True
        if obs is not None:
            _, after = _observe(c, action, b.arguments, prepared=obs)
            res['observed'] = obs
            # true доказывает эффект; false означает лишь «наблюдение не изменилось» и НЕ
            # доказывает отсутствия эффекта — так и сказано в описаниях действий
            # сравнивать можно только ДВА прочитанных значения: если любое наблюдение не
            # получено, ответ null. Иначе ошибка чтения превратилась бы в доказательство эффекта
            res['changed'] = (before != after) if (before is not None and after is not None) else None
            res['value_before'], res['value_after'] = before, after
            if res.get('ok') and action in ('goto_next_row', 'goto_previous_row') and res['changed'] is False:
                res.update(suggested_action='read_rows',
                           message='Equal cell text does not establish a row boundary. Use read_rows to check the table contents and count.')
            if action == 'input_text' and 'committed' not in res:
                res['committed'] = _committed(c, b.arguments, after) if res.get('ok') else None
                res['input_status'] = ('accepted' if res['committed'] is True else
                                       'pending' if res['committed'] is False else 'unverified')
                if res['committed'] is False:
                    res['suggested_action'] = 'goto_next_element'
                    res['message'] = ('Input is pending. Call tc_form(action="goto_next_element") on the owning form '
                                      'to finish input, or activate another focusable element; '
                                      'a reference field may require choosing a matching item.')
                # текст не записан — единственная ветка, где стоит спросить о доступности:
                # у недоступного элемента команда принимается молча
                if state == 'present' and res['committed'] is not True and _target_disabled(c, b.arguments):
                    res['target_disabled'] = True
        elif action in _READBACK_ACTIONS:
            # наблюдателя нет вовсе (set_order) либо он неприменим к этой цели (Развернут на
            # группе формы): в обоих случаях ответ обязан сказать «наблюдения не было»
            res['changed'] = None
            if action == 'input_text':
                res.setdefault('committed', None)
            if not READBACK:
                res['readback'] = 'off'
        if action == 'input_text' and READBACK and res.get('ok') and obs is None:
            if _kind_of(c, key) == 'TextDocumentField':
                res.update(value_before=None, value_after=None, value_status='unavailable',
                           suggested_action='get_edit_text',
                           message='Displayed text cannot verify this document. Read its editing text with get_edit_text; activate another element to finish input.')
                tr = getattr(c, '_track', None)
                try:
                    c._track = None
                    r = c.send_cmd(G.GET_EDIT_TEXT, key, kind='read', middle=RS,
                                   handle=b.arguments.get('handle'))
                    text = _scalar_text(r, G.GET_EDIT_TEXT)
                    res['buffer_matches'] = text == b.arguments.get('text') if text is not None else None
                except Exception:
                    res['buffer_matches'] = None
                finally:
                    c._track = tr
        # в журнал записи попадает КАЖДОЕ действие из охвата, а не только наблюдаемое: иначе
        # клики исчезали бы из статистики, доля наблюдавшихся оказывалась завышенной, а номера
        # вызовов — неверными
        if action in _APPLICABILITY and res.get('changed') is False:
            # значение не сдвинулось: ОДИН кадр за видом элемента, чтобы отличить «метод не для
            # этого вида поля» от «эффекта не было». Только эта ветка — на успешном пути ноль.
            verdict = _not_applicable(c, key, _APPLICABILITY[action])
            if verdict is not None:
                res['applicable'] = verdict
        _rec_note(action, key, res.get('observed'), res.get('changed'))
        return res
    return wrapper


def _read_target_check(guid, raw):
    """Статус адресата по ответу самого чтения (дополнительный кадр не отправляется)."""
    if not VERIFY_TARGET:
        return 'off'
    return tc1c.decode_target_state(raw) if guid in _TARGET_STATE_READS else 'unknown'

def _window(c):
    """Read the active object and its exact caption; cache its window key."""
    r = c.send_cmd(G.GET_ACTIVE_WINDOW, None, kind='read', middle=RC)
    # у кадра отказа фрагмент выглядел бы настоящим заголовком, хотя это остаток диагностики
    keys = tc1c.extract_object_keys(r['raw']) if r.get('ok') else []
    title = next((it.get('title') for it in tc1c.decode_collection(r['raw'])
                  if keys and it.get('key') == keys[0]), None) if r.get('ok') else None
    cls = keys[0].split('[')[0] if keys else None
    _state['window_key'] = keys[0] if keys else None
    if not keys:
        native = _native_active_window(c) if r.get('ok') else None
        out = {'ok': r['ok'], 'key': None, 'class': None, 'title': None, 'addressable': False}
        if native:
            out.update(title=native['title'], native=True, recovery='close_window')
        elif r.get('ok'):
            out.update(_unavailable_window())
        return out
    return {'ok': r['ok'], 'key': keys[0], 'class': cls, 'title': title, 'addressable': True}


def _native_active_window(c):
    if os.name != 'nt':
        return None
    # PID принадлежит запущенному нами клиенту именно этого локального подключения.
    if (getattr(c, 'port', None) != _state.get('launched_port') or
            getattr(c, 'host', None) not in ('127.0.0.1', 'localhost', '::1')):
        return None
    pid = _state.get('launched_pid')
    if not pid:
        return None
    isolated = _state.get('_isolated_process')
    if isolated is not None:
        try:
            actual_pid, created = _screenshots.resolve_process(c)
            if actual_pid != pid:
                return None
            return isolated.call('active_window', dict(pid=pid, created=created)).get('window')
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
    from _native_window import active_secondary_window
    return active_secondary_window(pid)


def _unavailable_window():
    return {'code': 'active_window_unavailable',
            'message': '1C did not provide an active window that these tools can identify or control. '
                       'Its title and type are unknown; local window recovery is unavailable.',
            'recovery': 'Inspect the 1C client on its computer. If a preview or dialog is open, '
                        'close it there, then call get_active_window again.'}

def _form_title(c, window_key):
    """Опознание окна по его дочерней ManagedForm -> (заголовок, имя формы в метаданных).

    Оба сведения приходят одной коллекцией и разбираются декодером, а не выбираются эвристикой
    из строк ответа, поэтому берутся за одно чтение. Заголовок — то, что видит пользователь;
    имя формы — путь в метаданных конфигурации. Любое из двух None, если формы нет или значение
    не пришло."""
    if not window_key:
        return None, None
    try:
        r = c.send_cmd(G.GET_CHILD_OBJECTS, window_key, kind='read', middle=CHILD_MIDDLE)
        for it in _coll(r, window_key):
            if '.ManagedForm[' in (it.get('key') or ''):
                return it.get('title'), it.get('form_name')
    except Exception:
        pass
    return None, None

# ================================ соединение =================================
@_action('tc_session')
def tc_connect(port: int, host: str = '127.0.0.1', version: str = None) -> str:
    """Connect to a running 1C test client (started with /TESTCLIENT -TPort <port>).
    `version` is the platform version
    (e.g. '8.5.1.1343') and MUST match the running platform, else the handshake fails; if
    omitted a built-in default is used. Call this before any other tc_* tool.
    A different host/port creates another connection. The same host/port reconnects that client;
    its connection_id is retained, but find elements again before using them."""
    host, port = _connections.endpoint(host, port)
    c = tc1c.TestClient(host, port)
    c._io_deadline = _state.get('_launch_deadline')
    if version:
        c.platform_version = version
    try:
        sess = c.connect()
        c.attach()                          # attach (программный кадр рукопожатия)
        if getattr(c, 's', None) is not None:
            c.s.settimeout(c._recv_timeout())
    except Exception:
        c.close()
        raise
    finally:
        c._io_deadline = None
    old = _state.get('client')
    if old is not None:
        old.close()
    _state['client'] = c
    isolated = _state.get('_isolated_process')
    if isolated is not None and port == _state.get('launched_port') and host in ('127.0.0.1', 'localhost', '::1'):
        c._isolated_process = isolated
    _state['window_key'] = None
    _rec_reset()          # запись принадлежит конкретному клиенту: новая сессия её не наследует
    # версию соединения сообщаем сразу: иначе её неоткуда узнать, кроме текста отказа
    # «метод требует платформу 1С 8.5.1+, подключено …»
    return f'подключено, сессия {sess}, платформа {_conn_ver(c)}'

def _exe_version(path):
    """Platform version from a standard Windows or Linux installation path."""
    m = re.search(r'[\\/]1cv8[\\/](?:(?:x86_64|i386|amd64|arm64)[\\/])?'
                  r'(\d+(?:\.\d+)+)[\\/](?:bin[\\/])?1cv8c?(?:\.exe)?$', path or '')
    return m.group(1) if m else ''

def _find_1cv8(want_version=None):
    """Platform executable: env TC1C_PLATFORM_EXE -> совпадение с want_version (точное/по префиксу) ->
    самая новая по ЧИСЛОВОМУ сравнению версии (не строковому). Только стандартные пути установки."""
    env = os.environ.get('TC1C_PLATFORM_EXE')
    if env and os.path.isfile(env):
        return env
    patterns = ([r'C:\Program Files\1cv8\*\bin\1cv8.exe',
                 r'C:\Program Files (x86)\1cv8\*\bin\1cv8.exe'] if os.name == 'nt' else
                ['/opt/1cv8/*/*/1cv8', '/opt/1cv8/*/*/1cv8c',
                 '/opt/1cv8/*/1cv8', '/opt/1cv8/*/1cv8c'])
    cands = [c for pattern in patterns for c in glob.glob(pattern) if os.path.isfile(c)]
    if not cands:
        return None
    ver = lambda c: tuple(int(x) for x in _exe_version(c).split('.') if x.isdigit())
    if want_version:
        exact = [c for c in cands if _exe_version(c) == want_version]
        if exact:
            return exact[0]
        pref = [c for c in cands if _exe_version(c).startswith(want_version + '.')]
        return max(pref, key=ver) if pref else None   # запрошенной версии нет — не подменять другой
    return max(cands, key=ver)

@_action('tc_session')
def tc_launch_client(base: str, port: int = None, server: bool = False, user: str = None,
                     password: str = None, version: str = None, exe: str = None,
                     extra_args: list = None, wait: int = 30, connect: bool = True,
                     desktop: typing.Literal['default', 'isolated'] = 'default') -> dict:
    """Launch a 1C test client and wait until it accepts connections on `port`, then optionally
    connect to it. `base` is a file infobase path (default), or 'server\\infobase' when `server=True`.
    `user`/`password` — infobase credentials (optional; the password is passed on the command line
    and is visible in the OS process list). `exe` — full path to 1cv8.exe or 1cv8c.exe on Windows,
    or 1cv8/1cv8c on Linux (else env
    TC1C_PLATFORM_EXE or standard install path). Only the 1C platform executable is launched.
    Omit port to allocate a free local port. Each launch creates a separate connection_id.
    On Linux, default requires DISPLAY/XAUTHORITY for a graphical session; isolated requires Xvfb.
    desktop: default = normal launch; isolated = a separate desktop on Windows 10+ or Linux,
    keeping client windows away from the user's desktop. Isolated clients stop with the server.
    With connect=false ok means only that the port answers — the client can be up and showing an
    error, so check it before relying on it."""
    if desktop not in ('default', 'isolated'):
        return {'ok': False, 'code': 'invalid_desktop', 'error': 'desktop must be default or isolated.'}
    if desktop == 'isolated' and os.name != 'nt' and sys.platform != 'linux':
        return {'ok': False, 'code': 'isolated_desktop_unsupported',
                'error': 'Isolated desktops are supported on Windows 10 or newer and Linux. Use desktop=default.'}
    if _state.get('_isolated_process') is not None:
        return {'ok': False, 'code': 'client_already_launched', 'error': 'Stop the isolated client before reusing this connection.'}
    exe = exe or _find_1cv8(version)
    if not exe or not os.path.isfile(exe):
        return {'ok': False, 'error': '1C platform executable not found — pass exe or set TC1C_PLATFORM_EXE'}
    allowed = ('1cv8.exe', '1cv8c.exe') if os.name == 'nt' else ('1cv8', '1cv8c')
    if os.path.basename(exe).lower() not in allowed:
        return {'ok': False, 'error': 'only %s may be launched' % ', '.join(allowed)}
    # файловую базу проверяем ДО запуска: клиент открывает порт раньше, чем смотрит на базу, и
    # по одному порту ответ выглядел бы успехом. Заодно не остаётся процесса с модальной ошибкой
    if not server and not os.path.isdir(base):
        return {'ok': False, 'error': 'base: no such infobase directory — %s' % base}
    if desktop == 'default' and os.name != 'nt' and not os.environ.get('DISPLAY'):
        return {'ok': False, 'code': 'graphical_session_unavailable',
                'error': 'Launch requires a graphical session. Start the MCP server in the desktop session '
                         'or supply its DISPLAY and XAUTHORITY environment variables.'}
    actual_version = _exe_version(exe) or _exe_version(os.path.realpath(exe))
    if not version or (actual_version and actual_version.startswith(version + '.')):
        version = actual_version or None
    if port is None:
        port = _connections.free_port()
    args = [exe, 'ENTERPRISE', ('/S' if server else '/F') + base,
            '/TESTCLIENT', '-TPort' + str(port), '/DisableStartupMessages']
    if user:     args.append('/N' + user)
    if password: args.append('/P' + password)
    if extra_args:
        args += [str(a) for a in extra_args]
    flags = 0
    for fl in ('DETACHED_PROCESS', 'CREATE_NEW_PROCESS_GROUP'):
        flags |= getattr(subprocess, fl, 0)
    try:
        if desktop == 'isolated':
            if os.name == 'nt':
                from _desktop_windows import IsolatedProcess
            else:
                from _desktop_linux import IsolatedProcess
            proc = IsolatedProcess(args)
            _state['_isolated_process'] = proc
        else:
            proc = subprocess.Popen(args, creationflags=flags, close_fds=True,
                                    start_new_session=os.name != 'nt', stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return {'ok': False, **({'code': 'isolated_desktop_unavailable'} if desktop == 'isolated' else {}),
                'error': 'Could not launch the test client: %s' % e}
    _state['desktop'] = desktop
    _state['launched_pid'] = proc.pid
    _state['launched_process'] = proc
    _state['launched_port'] = port      # чтобы tc_stop_client закрыл соединение именно с ним
    deadline = time.monotonic() + max(1, wait)
    up = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            if desktop == 'isolated':
                return _finish_client_launch({'ok': False, 'pid': proc.pid, 'desktop': desktop,
                    'error': 'The isolated test client exited (code %s).' % proc.returncode})
            _state['launched_pid'] = None
            _state.pop('launched_process', None)
            _state.pop('launched_port', None)
            return {'ok': False, 'pid': proc.pid,
                    'error': 'the test client exited (code %s) — check the base, credentials and flags'
                % proc.returncode}
        s = socket.socket(); s.settimeout(max(0.001, min(1, deadline - time.monotonic())))
        try:
            if s.connect_ex(('127.0.0.1', port)) == 0:
                up = True
        except Exception:
            pass
        finally:
            s.close()
        if up:
            break
        time.sleep(max(0, min(0.5, deadline - time.monotonic())))
    if not up:
        return _finish_client_launch({'ok': False, 'pid': proc.pid, 'desktop': desktop,
                'error': 'the client did not start listening on port %d within %d s' % (port, wait)})
    out = {'ok': True, 'pid': proc.pid, 'port': port, 'exe': exe, 'version': version, 'desktop': desktop}
    if connect:
        # порт открывается раньше, чем платформа проверила базу, поэтому ok=true по одному порту
        # обещал бы работающий клиент и для несуществующей базы. Раз подключиться просили — оно и
        # есть признак пригодности
        while True:
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Test-client startup deadline expired.')
                _state['_launch_deadline'] = deadline
                out['connected'] = tc_connect(port, version=version)
                break
            except Exception as e:
                # 1C opens its socket before it accepts attach while loading the infobase.
                if proc.poll() is not None or time.monotonic() >= deadline:
                    out['ok'] = False
                    out['connect_error'] = str(e)
                    break
                time.sleep(max(0, min(0.5, deadline - time.monotonic())))
            finally:
                _state.pop('_launch_deadline', None)
    return _finish_client_launch(out)


def _finish_client_launch(result):
    # клиент, поднятый здесь и не взлетевший, гасим при ЛЮБОМ рабочем столе: иначе процесс
    # остаётся жив и держит порт, а запись о нём остаётся в реестре, и повторный запуск на
    # тот же endpoint отвечает connection_in_use
    if not result['ok'] and _state.get('launched_pid'):
        cleanup = tc_stop_client()
        result['client_stopped'] = cleanup['ok']
        if not cleanup['ok']:
            result['cleanup_error'] = cleanup['error']
    return result

@_action('tc_session')
def tc_stop_client() -> dict:
    """Stop the test client started by launch_client in this connection, and disconnect from it.
    Unsaved changes may be lost. If stopping fails, retain the process so the call can be retried."""
    pid = _state.get('launched_pid')
    if not pid:
        return {'ok': False, 'error': 'no client was launched through tc_launch_client'}
    proc = _state.get('launched_process')
    isolated = _state.get('_isolated_process')
    if isolated is not None:
        if isolated is not proc or proc.pid != pid:
            return {'ok': False, 'pid': pid, 'error': 'The isolated process is no longer owned by this connection.'}
        try:
            isolated.close()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {'ok': False, 'pid': pid, 'error': 'Could not stop the isolated client: %s' % exc}
        _state.pop('_isolated_process', None)
    elif os.name != 'nt':
        if proc is None or proc.pid != pid:
            return {'ok': False, 'pid': pid, 'error': 'The launched process is no longer owned by this connection.'}
        try:
            if proc.poll() is None:
                import signal
                # Each client starts in its own session; never signal the MCP server's group.
                os.killpg(pid, signal.SIGKILL)
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {'ok': False, 'pid': pid, 'error': 'Could not stop the test client: %s' % e}
    elif proc is None or proc.poll() is None:
        p = subprocess.run(['taskkill', '/PID', str(pid), '/F', '/T'], capture_output=True)
        if p.returncode != 0:
            err = (p.stderr or p.stdout or b'').decode('cp866', 'replace').strip()
            return {'ok': False, 'pid': pid, 'returncode': p.returncode,
                    'error': 'taskkill did not end the process: %s' % (err or 'code %d' % p.returncode)}
        if proc is not None:
            proc.wait(timeout=10)
    _state['launched_pid'] = None
    _state.pop('launched_process', None)
    port = _state.pop('launched_port', None)
    # соединение закрываем, только если оно ведёт к ОСТАНОВЛЕННОМУ клиенту: при
    # tc_launch_client(connect=False) активным может быть соединение с ДРУГИМ клиентом,
    # в том числе на удалённом хосте с тем же номером порта — сверяем и хост, и порт
    cl = _state.get('client')
    local = str(getattr(cl, 'host', '') or '').lower() in ('127.0.0.1', 'localhost', '::1', '')
    if cl is not None and port is not None and getattr(cl, 'port', None) == port and local:
        tc_disconnect()
    return {'ok': True, 'stopped_pid': pid}

@_action('tc_session')
def tc_disconnect() -> str:
    """Close the connection to the test client."""
    c = _state['client']
    if c: c.close(); _state['client'] = None; _state['window_key'] = None
    _rec_reset()          # с отключением накопленное недостижимо: «запись идёт» стало бы ложью
    return 'disconnected'


@_action('tc_session')
def tc_list_connections() -> dict:
    """List registered clients with connection_id, host, port, base, user and recording status.
    base/user are known for clients launched here; listing does not probe client health."""
    return {'ok': True, 'connections': _pool.list()}

# ============================== окно / дерево ================================
@_action('tc_app')
def tc_get_screenshot(scale: int = 100, grid: bool = False, region: list[int] = None):
    """Capture the connected 1C window with popups as an image, without changing focus.
    Requires a local client on Windows or Linux with X11/XWayland and desktop access. scale: 25..100 percent.
    Optional region=[x,y,width,height] uses original screenshot pixels; grid labels those coordinates.
    capture_complete=false means some popups are missing. Screenshots are not recorded in scenarios."""
    if not SCREENSHOTS:
        return {'ok': False, 'code': 'screenshots_disabled', 'error': 'Screenshots are disabled on this server.'}
    try:
        return _screenshots.capture(_need(), scale=scale, grid=grid, region=region)
    except _screenshots.CaptureError as exc:
        return {'ok': False, 'code': exc.code, 'error': str(exc)}


@_action('tc_app')
def tc_get_active_window() -> dict:
    """Return the application's active window: {key, class, title, platform_version} and sometimes
    form_name. title is the caption of the window's managed form — null only when the window has
    no form or the caption could not be read. form_name is the form's name in the configuration
    metadata ('Справочник.Контрагенты.Форма.ФормаСписка'), unrelated to name, which for a form
    is a GUID. form_name is read only when the window itself gave no caption and may be absent
    even then: its absence says nothing about the form. To read it explicitly call
    tc_get_child_objects on the window key. platform_version is the version of this connection;
    actions unavailable on it are refused with available_since. addressable=false means the
    window has no element address; this alone does not identify its type. When local recovery
    is unavailable, code=active_window_unavailable explains how to continue. A local print preview may report native=true and
    recovery="close_window": close it to return to the form before addressing form elements."""
    c = _need()
    w = _window(c)
    # заголовок берём у дочерней формы: в ответе самого окна его либо нет, либо он неотличим от
    # имени класса. Иначе за ним пришлось бы ходить отдельным обходом на каждое опознание окна.
    # Имя формы приходит тем же чтением, поэтому достаётся даром — но только в этой ветке
    if not w.get('title'):
        w['title'], fname = _form_title(c, w.get('key'))
        if fname is not None:
            w['form_name'] = fname
    w['platform_version'] = _conn_ver(c)
    return w

@_action('tc_window')
def tc_activate_window() -> dict:
    """Activate the current active window."""
    c = _need()
    w = _window(c)
    if not w['key']:
        return {'ok': False, 'error': 'no active window'}
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.ACTIVATE, w['key'], kind=kind, middle=b'')['ok'] and ok
    return {'ok': ok, 'window': w['key']}

@_action('tc_app')
def tc_get_child_objects(key: str = None) -> dict:
    """List one level of child UI objects under the addressed parent. If its address is omitted,
    use the last observed active window, querying the client if no window has been observed.
    For a whole subtree in one call use tc_find(action="find_objects").
    Returns an object with parent and children: [...]. Child metadata includes class and title;
    name and type may also be present. type names a recognized platform element kind
    (e.g. CheckBoxField or Pages), and may be null when no kind is known for that object.
    A ManagedForm row can also carry form_name, its name in configuration metadata
    (e.g. 'Справочник.Контрагенты.Форма.ФормаСписка'), useful when the title is empty.
    Always obtain object addresses from tool results."""
    c = _need()
    target = key or _state.get('window_key')
    if not target:
        target = _window(c)['key']
    if not target:
        return {'ok': False, 'error': 'no object key'}
    r = _read_children(c, target)
    return {'ok': r['ok'], 'parent': target, 'children': _coll(r, target)}

# ============================== ввод / клик ==================================
def _input_text_command(text, c=None, key=None):
    # Обычные поля очищаются Clear, текстовый документ — пустым InputText.
    # Выбор команды общий для инструментов и воспроизведения uilog.
    if text == '' and not (c is not None and _kind_of(c, key) == 'TextDocumentField'):
        return G.CLEAR, b''
    return G.INPUT_TEXT, tc1c.mk_input_text(text)


def _input_current(c, form):
    r = c.send_cmd(G.FORM_GET_CURRENT_ITEM, form['key'], kind='read', middle=RC,
                   handle=form['handle'])
    items = _coll(r, form['key']) if r.get('ok') else []
    if len(items) == 1:
        return items[0].get('key')
    # Empty address means explicitly no current item; None means unknown.
    return '' if r.get('ok') and tc1c.empty_current_form_item(r['raw']) else None


def _finish_input_text(c, key, text, handle):
    """Finish only the addressed field; diagnostic reads are not recorded as actions."""
    result = {'committed': None, 'input_status': 'unverified', 'edit_finished': None}
    track = getattr(c, '_track', None)
    try:
        c._track = None
        required = (G.FORM_GOTO_NEXT_ITEM, G.FORM_GET_CURRENT_ITEM, G.GET_PROPERTY,
                    G.CURRENT_VISIBLE, G.CURRENT_ENABLE, G.CURRENT_READONLY)
        if not all(_guid_available(c, g) for g in required):
            result['message'] = 'This platform cannot verify automatic input completion.'
            return result
        _cell_ready(c, key, handle)
        parent = _collection_parent(key)
        while parent and _key_class(parent) != 'ManagedForm':
            parent = _collection_parent(parent)
        form = _ref_live_object(c, parent) if parent else None
        window = _collection_parent(parent) if parent else None
        if not form or not form.get('handle') or not window or _window(c).get('key') != window:
            result['message'] = 'The owning form is not active or is unavailable. Inspect the active window.'
            return result
        if _input_current(c, form) != key:
            result['message'] = 'The input field did not remain current. Inspect the form before continuing.'
            return result
        c._track = track
        ok = True
        for kind in ('action', 'commit'):
            ok = c.send_cmd(G.FORM_GOTO_NEXT_ITEM, form['key'], kind=kind,
                            middle=b'', handle=form['handle'])['ok'] and ok
        c._track = None
        if not ok:
            result.update(ok=False, error='The form could not finish input.')
            return result
        if _window(c).get('key') != window:
            result['message'] = 'The active window changed while finishing input. Inspect it before continuing.'
            return result
        current = _input_current(c, form)
        result['edit_finished'] = current != key if current is not None else None
        r = c.send_cmd(G.GET_PROPERTY, key, kind='read', middle=RC, handle=handle)
        actual = _field_scalar_text(c, key, r, G.GET_PROPERTY) if r.get('ok') else None
        result['data_presentation'] = actual
        if result['edit_finished'] is True and actual == text:
            result.update(committed=True, input_status='accepted')
        elif result['edit_finished'] is True and _numeric_equivalent(text, actual):
            result.update(verification='numeric_equivalent', message=
                          'Editing finished with an equivalent numeric representation; exact text is not verified.')
        elif result['edit_finished'] is False:
            result.update(committed=False, input_status='pending', message=
                          'Input remains active after form navigation. Inspect validation messages or choose a matching reference value.')
        else:
            result['message'] = 'Input completion or the accepted value could not be verified. Inspect the field and any validation messages.'
        return result
    except _CellEditFailure as exc:
        result.update(ok=False, error=str(exc))
        return result
    finally:
        c._track = track


@_action('tc_field')
def tc_input_text(key: str, text: str, handle: str, finish: bool = True) -> dict:
    """Enter text. Ordinary form input fields finish automatically: the owning form moves
    focus once to its next element, then the accepted value is checked. finish=false leaves
    the editing buffer active, for example before choosing a reference suggestion or cancelling.
    Empty text clears the value directly. Reference input can still need a matching value.
    committed=true confirms the accepted text; false means pending; null means unverified.
    edit_finished reports whether focus left the ordinary field. Numeric formatting can produce
    verification=numeric_equivalent with committed=null. changed compares displayed text.
    These flags do not mean the record was saved. Table cells use tc_table(action="set_cell_text").
    Text documents finish by activating another element with tc_field(action="activate");
    spreadsheet cells use tc_doc(action="end_edit_current_area")."""
    c = _need()
    guid, middle = _input_text_command(text, c, key)
    ok = True
    try:
        for kind in ('action', 'commit'):
            r = c.send_cmd(guid, key, kind=kind, middle=middle, handle=handle)
            ok = ok and r['ok']
    except tc1c.OperationError as exc:
        if exc.status != 10 or _kind_of(c, key) != 'InputField':
            raise
        track = getattr(c, '_track', None)
        try:
            c._track = None
            readonly = (_cell_flag(c, G.CURRENT_READONLY, key, handle)
                        if _guid_available(c, G.CURRENT_READONLY) else None)
        except tc1c.OperationError:
            readonly = None
        finally:
            c._track = track
        if readonly is True:
            return dict(exc.result(), code='readonly', input_status='rejected',
                        committed=None, readonly=True,
                        error='The field is read-only.',
                        message='The field is read-only; text input is unavailable.')
        if _table_owner(key):
            return dict(exc.result(), input_status='rejected', committed=None,
                        suggested_action='set_cell_text',
                        message='This is a table column. Use tc_table(action="set_cell_text") '
                                'on its table with the column element name and the requested text.')
        return dict(exc.result(), input_status='rejected', committed=None,
                    suggested_action='start_choosing',
                    message='Text input was refused. If this field selects an object, use start_choosing to select a value.')
    result = {'ok': ok, 'target': key, 'text': text}
    if ok and finish and text and _key_class(key) == 'EditField' and '.Table[' not in key:
        if _kind_of(c, key) == 'InputField':
            result.update(_finish_input_text(c, key, text, handle))
    if ok and _key_class(key) == 'EditField' and '.Table[' not in key:
        # Only one field can own the input focus; keep this per connection, bounded.
        c._pending_text_input = (key if text and result.get('edit_finished') is not True
                                and result.get('committed') is not True else None)
    return result

@_action('tc_doc')
def tc_input_html(key: str, html: str, handle: str, attachments: dict = None) -> dict:
    """Set HTML/text into a formatted-document field. attachments maps an image name used in
    the HTML (e.g. <img src="p1.png"> -> "p1") to that image as a base64 string; names must be
    identifiers (no dots)."""
    c = _need()
    error = _html_target(c, key, write=True)
    if error:
        return error
    att = {k: base64.b64decode(v) for k, v in (attachments or {}).items()}
    mid = tc1c.mk_html(html, att or None)
    pad = tc1c.HTML_ATT_PAD if att else None
    ok = True
    for kind in ('action', 'commit'):
        r = c.send_cmd(G.INPUT_HTML, key, kind=kind, middle=mid, handle=handle, pad=pad)
        ok = ok and r['ok']
    return {'ok': ok, 'target': key, 'len': len(html), 'attachments': sorted(att)}

def _page_activation_result(c, key, handle):
    """An inactive page may become visible; judge its state only after activation."""
    if (not VERIFY_TARGET or _key_class(key) != 'Group'
            or not _guid_available(c, G.GET_CHILD_OBJECTS) or not _guid_available(c, G.CURRENT_VISIBLE)):
        return {}
    track = getattr(c, '_track', None)
    try:
        c._track = None
        if _kind_of(c, key) != 'Page':
            return {}
        state, visible = _verify_target(c, key, handle)
    finally:
        c._track = track
    result = {'target_check': state, 'visible': visible}
    if state == 'present' and visible is False:
        result.update(ok=False, code='target_hidden', command_accepted=True,
                      error='The page remains hidden after activation.',
                      message='Change the form state to make this page available, or choose another visible page.')
    elif state == 'absent':
        result.update(_absent_error(key))
    return result


@_action('tc_field')
def tc_activate(key: str, handle: str) -> dict:
    """Give a form element the focus — this is how you switch to a page or make a table column
    the current cell; a click does neither. It also commits text left uncommitted by tc_field(action="input_text"),
    but only when you activate a DIFFERENT focusable element: the field you typed into already has the focus.
    To let the form choose the next focus target, use tc_form(action="goto_next_element") on the form.
    Pages report visibility after activation; a page that remains hidden returns target_hidden.
    Reports no `changed` — verify with tc_field(action="get_text"), tc_field(action="get_current_page"),
    tc_form(action="get_current_element") or tc_table(action="get_current_item")."""
    c = _need()
    if _key_class(key) == 'ManagedForm':
        active = _window(c)
        if active.get('native'):
            return {'ok': False, 'target': key, 'error': 'close the active preview with close_window before activating a form'}
        if not active.get('key'):
            info = _unavailable_window()
            return {'ok': False, 'target': key, 'error': info.pop('message'), **info}
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.ACTIVATE, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'activate'}

def _click_guid(key):
    return {'Button': G.CLICK, 'EditField': G.CLICK_FIELD,
            'Decoration': G.CLICK_DECORATION, 'CIButton': G.CLICK_CI}.get(_key_class(key))


@_action('tc_field')
def tc_click(key: str, handle: str) -> dict:
    """Click a button, field, decoration or command-interface button. The element must support
    clicking. To focus an input, table cell or page, use activate."""
    c = _need()
    guid = _click_guid(key)
    if guid is None:
        return {'ok': False, 'target': key, 'code': 'unsupported_element_type',
                'error': 'Click requires a button, field, decoration or command-interface button.',
                'suggested_action': 'activate'}
    ok = True
    for kind in ('action', 'commit'):
        r = c.send_cmd(guid, key, kind=kind, middle=b'', handle=handle)
        ok = ok and r['ok']
    _state['window_key'] = None          # клик мог открыть новое окно — кэш недействителен
    return {'ok': ok, 'target': key}

@_action('tc_field')
def tc_set_check(key: str, handle: str) -> dict:
    """Toggle a checkbox field. Inside a table this acts on the column that is the CURRENT cell,
    so make the target column current first — tc_activate on the column element does that; a click
    on the cell does not."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        r = c.send_cmd(G.SET_CHECK, key, kind=kind, middle=b'', handle=handle)
        ok = ok and r['ok']
    return {'ok': ok, 'target': key}

# ============================== чтения ======================================
@_action('tc_field')
def tc_get_text(key: str, handle: str) -> dict:
    """Read displayed text (checkbox text follows the client language). An empty input field
    returns "". For an edit buffer use get_edit_text.
    If text is unavailable, the answer explains the limitation and suggests another reading
    action where applicable. null does not confirm an empty field."""
    c = _need()
    try:
        r = c.send_cmd(G.GET_DISPLAYED_TEXT, key, kind='read', middle=RS, handle=handle)
    except tc1c.OperationError as exc:
        if exc.status not in (10, 17) and not (
                exc.status == 11 and '.Table[' in key and _key_class(key) == 'EditField'):
            raise
        return _display_text_alternative(c, key, dict(exc.result(), text=None))
    result = {'ok': r['ok'], 'text': _field_scalar_text(c, key, r, G.GET_DISPLAYED_TEXT)}
    if result['text'] is not None or not r['ok']:
        return _unavailable_read(result, 'text')
    return _display_text_alternative(c, key, result)


def _display_text_alternative(c, key, result):
    if '.Table[' in key and _key_class(key) == 'EditField':
        return _unavailable_read(result, 'text', suggested_action='get_cell_text',
                                 message='Read this column through its table, using the column element name.')
    kind = _kind_of(c, key)
    if kind == 'SpreadsheetDocumentField':
        return _unavailable_read(result, 'text', suggested_action='read_document',
                                 message='Read spreadsheet cells with read_document.')
    if kind in ('FormattedDocumentField', 'HTMLDocumentField'):
        return _unavailable_read(result, 'text', suggested_action='get_html',
                                 message='Read document content with get_html.')
    if kind in ('CalendarField', 'TrackBarField', 'ProgressBarField', 'InputField'):
        return _unavailable_read(result, 'text', suggested_action='get_data_presentation')
    return _unavailable_read(result, 'text')

@_action('tc_field')
def tc_get_tooltip(key: str, handle: str) -> dict:
    """Read an element's tooltip text (empty → None)."""
    c = _need()
    r = c.send_cmd(G.GET_TOOLTIP, key, kind='read', middle=RS, handle=handle)
    return {'ok': r['ok'], 'target': key, 'tooltip': _scalar_text(r, G.GET_TOOLTIP)}

@_action('tc_doc')
def tc_get_html(key: str, handle: str) -> dict:
    """Read the HTML of a formatted/HTML-document field. After the form has put up a menu or a
    modal choice list, the platform stops returning this field's content until it is written
    again — an empty answer right after such a window does not mean the field is empty."""
    c = _need()
    error = _html_target(c, key)
    if error:
        return error
    r = c.send_cmd(G.GET_HTML, key, kind='read', middle=HTML_READ, handle=handle)
    html = tc1c.decode_html(r['raw']) if r.get('ok') else None
    return _unavailable_read({'ok': r['ok'], 'html': html}, 'html')

@_action('tc_field')
def tc_get_context_menu(key: str, handle: str) -> dict:
    """Get an element's context menu. The platform returns the menu as a form GROUP, not as a list
    of commands: 'menu' holds that group, and its items are read with a separate
    tc_get_child_objects on the group's key."""
    c = _need()
    r = c.send_cmd(G.GET_CONTEXT_MENU, key, kind='read', middle=RC, handle=handle)
    return {'ok': r['ok'], 'target': key, 'menu': _coll(r, key)}

def _own_handle(c, key):
    """Собственный handle объекта: берём из перечня детей его РОДИТЕЛЯ (один адресованный кадр).
    None, когда родителя в ключе нет или цели среди детей не оказалось."""
    parent = _collection_parent(key) if key else None
    if not parent:
        return None
    try:
        r = c.send_cmd(G.GET_CHILD_OBJECTS, parent, kind='read', middle=CHILD_MIDDLE)
    except Exception:
        return None
    for o in (_coll(r, parent) or []):
        if o.get('key') == key:
            return o.get('handle')
    return None

@_action('tc_app')
def tc_get_parent(key: str, handle: str) -> dict:
    """Get an element's parent in parent: [...]. The server resolves the parent's own address
    and returns it with the available object metadata."""
    c = _need()
    recv = _collection_parent(key) or key
    r = c.send_cmd(G.GET_PARENT, recv, kind='read', middle=tc1c.mk_area(key), handle=handle)
    parent = _coll(r, key, remember=False)
    for o in parent:
        # ответ повторяет handle ЗАПРОСА; подставляем собственный, иначе адресация родителя
        # выглядит как несуществующий объект
        if o.get('handle') == handle:
            o['handle'] = _own_handle(c, o.get('key'))
    return {'ok': r['ok'], 'target': key, 'parent': _remember_handles(parent)}

@_action('tc_table')
def tc_get_selected_rows(key: str, handle: str) -> dict:
    """Read selected rows as {column title: displayed value} maps. Row order is not guaranteed.
    The platform chooses the columns. Missing columns are unread, not empty; empty text can also
    represent an omitted value. Values may contain search highlighting.
    Use read_rows to read all selectable rows of the current table."""
    c = _need()
    r = c.send_cmd(G.GET_SELECTED_ROWS, key, kind='read', middle=RC, handle=handle)
    # ответ = массив Соответствий: маркер c04b начинает СТРОКУ, внутри пары <колонка> eb53 <значение>
    rows = _rows(r)
    return {'ok': r['ok'], 'target': key, 'rows': rows}

@_action('tc_field')
def tc_get_choice_list(key: str, handle: str) -> dict:
    """Read radio-button options or an input's open drop-down list. For an input, open its list
    immediately before reading: the answer describes whichever drop-down is currently open.
    items contains {presentation, text}; presentations contains the displayed texts to select.
    A closed input list may return items=[] with status=unknown."""
    c = _need()
    r = c.send_cmd(G.GET_CHOICE_LIST, key, kind='read818', middle=CHOICE_READ, handle=handle)
    items, status = tc1c.decode_choice_items(r['raw'])
    return {'ok': r['ok'], 'target': key, 'items': items, 'status': status,
            'presentations': [i['text'] for i in items]}

@_action('tc_field')
def tc_is_visible(key: str, handle: str) -> dict:
    """Whether an element is currently visible. Returns an error instead of visible=false when
    there is no object at key: for this read the protocol does report a missing target, so a
    mistyped address cannot pass for a hidden element. On the pages of a page group this does NOT
    tell you which page is on screen — several pages report visible=true at once; use
    tc_get_current_page for that."""
    c = _need()
    r = c.send_cmd(G.CURRENT_VISIBLE, key, kind='read', middle=RS, handle=handle)
    tcheck = _read_target_check(G.CURRENT_VISIBLE, _body(r))
    if tcheck == 'absent':
        return _absent_error(key)
    return {'ok': r['ok'], 'visible': _scalar(r, _bool_from_resp), 'target_check': tcheck}

@_action('tc_field')
def tc_is_enabled(key: str, handle: str) -> dict:
    """Read the element's availability. A command can still refuse execution in the current form state."""
    c = _need()
    r = c.send_cmd(G.CURRENT_ENABLE, key, kind='read', middle=RS, handle=handle)
    return {'ok': r['ok'], 'enabled': _scalar(r, _bool_from_resp)}

@_action('tc_field')
def tc_is_readonly(key: str, handle: str) -> dict:
    """Whether an element is currently read-only — that is the element's own read-only property.
    Two other things look the same and are NOT this: an element switched off entirely (read that
    with tc_field(action="is_enabled")), and a spreadsheet-document field shown in view mode,
    which neither read reflects — there, tc_doc(action="begin_edit_current_area") runs the cell's
    details instead of editing."""
    c = _need()
    r = c.send_cmd(G.CURRENT_READONLY, key, kind='read', middle=RS, handle=handle)
    return {'ok': r['ok'], 'readonly': _scalar(r, _bool_from_resp)}

def _bool_from_resp(raw):
    # state-геттеры кодируют булево маркером e2(Да)/e1(Нет) перед эпилогом; иначе строкой
    r = tc1c.decode_bool(raw)
    if r is not None: return r
    for v in tc1c.extract_strings(raw, 2):      # фолбэк: строка Да/Нет
        if v in ('Да', 'Нет'): return v == 'Да'
    return None

def _read_value(c, key, handle):
    """Наблюдаемое значение поля для read-back: (отображаемый текст, представление данных).
    Позволяет action-инструментам вернуть честный флаг changed вместо голого ok=True.
    Read-back НЕОБЯЗАТЕЛЕН: методы, которых нет в версии соединения, пропускаются — иначе
    старый клиент не ответит на неизвестный GUID и вызов провисит до таймаута."""
    txt = pres = None
    if _vt(_conn_ver(c)) >= (8, 3, 12):        # GetDisplayedText — с 8.3.12
        try:
            r = c.send_cmd(G.GET_DISPLAYED_TEXT, key, kind='read', middle=RS, handle=handle)
            txt = _scalar_text(r, G.GET_DISPLAYED_TEXT)   # тот же разбор, что у публичных чтений
        except Exception:
            pass
    try:
        r = c.send_cmd(G.GET_PROPERTY, key, kind='read', middle=RC, handle=handle)
        pres = _scalar_text(r, G.GET_PROPERTY)   # у полосы регулирования значение только здесь
    except Exception:
        pass
    return (txt, pres)

@_action('tc_app')
def tc_get_current_error() -> dict:
    """Get info about the session's last CLIENT error (none → null). error is the main description;
    details preserves additional text, including nested causes, module locations and stacks.
    This is not where an
    application refusal shows up: messages raised by the configuration ("field not filled in",
    "posting is not possible") arrive in tc_get_user_message_texts. Read that one with care — it
    returns the ACCUMULATED messages of the session, so an old complaint is still there after a
    later action succeeded; clear it with tc_close_user_messages_panel before the action you want
    to judge."""
    c = _need()
    r = c.send_cmd(G.GET_CURRENT_ERROR, None, kind='read', middle=ERR_MIDDLE, pad=4)
    vals = _vals(r)
    return {'ok': r['ok'], 'error': vals[0] if vals else None,
            'details': list(dict.fromkeys(vals[1:]))}

@_action('tc_app')
def tc_get_performance(clear: bool = False) -> dict:
    """Get accumulated session performance counters (calls, duration, sent, received).
    Set clear to also reset them, so the next read measures only what happened after this call."""
    c = _need()
    r = c.send_cmd(G.GET_PERFORMANCE, None, kind='read818',
                   middle=PERF_CLEAR if clear else PERF_MIDDLE)
    return {'ok': r['ok'], 'cleared': bool(clear), 'indicators': tc1c.parse_perf(_body(r))}

# ===================== выпадающий список / число / дата =====================
@_action('tc_field')
def tc_open_drop_list(key: str, handle: str) -> dict:
    """Open the drop-down list of a reference/enum field (call before choose_from_drop_list)."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.OPEN_DROP_LIST, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key}

@_action('tc_field')
def tc_choose_from_drop_list(key: str, value: str | int, handle: str) -> dict:
    """Pick a value from a field's open drop-down list by its display text (e.g. a colour name)
    or by its 0-based index in the list. The value is written at once — no focus change is needed.
    changed may come back null here even when the value did change: with the list open the
    value cannot be read. Read the field with tc_get_text to confirm."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        r = c.send_cmd(G.CHOOSE_FROM_DROP_LIST, key, kind=kind, middle=tc1c.mk_choice(value), handle=handle)
        ok = ok and r['ok']
    _resolved_text_input(c, key, ok)
    return {'ok': ok, 'target': key, 'value': value}

@_action('tc_field')
def tc_increase_value(key: str, handle: str) -> dict:
    """Increment a numeric (spinner) field. A track bar does NOT take this — move it with
    tc_field(action="goto_value") in percent. When the value does not move, `applicable: false`
    in the answer means the method does not fit this kind of field."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.INCREASE_VALUE, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key}

@_action('tc_field')
def tc_decrease_value(key: str, handle: str) -> dict:
    """Decrement a numeric (spinner) field. A track bar does NOT take this — move it with
    tc_field(action="goto_value") in percent. When the value does not move, `applicable: false`
    in the answer means the method does not fit this kind of field."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.DECREASE_VALUE, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key}

@_action('tc_calendar')
def tc_goto_date(key: str, handle: str, year: int, month: int, day: int) -> dict:
    """Go to a date (year, month, day) in a calendar field. Returns changed/value_before/value_after."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GOTO_DATE, key, kind=kind, middle=tc1c.mk_date(year, month, day), handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'date': '%04d-%02d-%02d' % (year, month, day)}

@_action('tc_calendar')
def tc_calendar_next_month(key: str, handle: str) -> dict:
    """Move a calendar field to the next month. The command is accepted, but nothing readable about
    the field changes, so this cannot be verified. To move the date use tc_goto_date."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GOTO_NEXT_MONTH, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'next_month'}

@_action('tc_calendar')
def tc_calendar_next_year(key: str, handle: str) -> dict:
    """Move a calendar field to the next year. The command is accepted, but nothing readable about
    the field changes, so this cannot be verified. To move the date use tc_goto_date."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GOTO_NEXT_YEAR, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'next_year'}

# ============================ таблица: строки ===============================
@_action('tc_table')
def tc_goto_first_row(key: str, handle: str, toggle_selection: bool = False,
                       column: str | int = None) -> dict:
    """Move to the first row of a table. Set toggle_selection to also toggle that row's selection. Pass
    column (the column element NAME) to have that cell read before and after the move: it comes
    back as value_before/value_after with changed=true when they differ. changed=false only
    means the two texts are the same — different rows can share a value; without column nothing
    is read and changed is null."""
    c = _need()
    r = c.send_cmd(G.GOTO_FIRST_ROW, key, kind='read', middle=(b'\xe2' if toggle_selection else RS), handle=handle)
    return {'ok': r['ok'], 'target': key, 'action': 'first_row'}

@_action('tc_table')
def tc_goto_next_row(key: str, handle: str, toggle_selection: bool = False,
                      column: str | int = None) -> dict:
    """Move to the next row of a table. Set toggle_selection to also toggle that row's selection. Pass
    column (the column element NAME) to have that cell read before and after the move: it comes
    back as value_before/value_after with changed=true when they differ. changed=false only
    means the two texts are the same — different rows can share a value; without column nothing
    is read and changed is null."""
    c = _need()
    r = c.send_cmd(G.GOTO_NEXT_ROW, key, kind='read', middle=(b'\xe2' if toggle_selection else RS), handle=handle)
    return {'ok': r['ok'], 'target': key, 'action': 'next_row'}

@_action('tc_table')
def tc_goto_previous_row(key: str, handle: str, toggle_selection: bool = False,
                          column: str | int = None) -> dict:
    """Move to the previous row of a table. Set toggle_selection to also toggle that row's selection.
    Pass column (the column element NAME) to have that cell read before and after the move: it
    comes back as value_before/value_after with changed=true when they differ. changed=false
    only means the two texts are the same — different rows can share a value; without column
    nothing is read and changed is null."""
    c = _need()
    r = c.send_cmd(G.GOTO_PREVIOUS_ROW, key, kind='read', middle=(b'\xe2' if toggle_selection else RS), handle=handle)
    return {'ok': r['ok'], 'target': key, 'action': 'prev_row'}

@_action('tc_table')
def tc_goto_last_row(key: str, handle: str, toggle_selection: bool = False,
                      column: str | int = None) -> dict:
    """Move to the last row of a table. Set toggle_selection to also toggle that row's selection. Pass
    column (the column element NAME) to have that cell read before and after the move: it comes
    back as value_before/value_after with changed=true when they differ. changed=false only
    means the two texts are the same — different rows can share a value; without column nothing
    is read and changed is null."""
    c = _need()
    r = c.send_cmd(G.GOTO_LAST_ROW, key, kind='read', middle=(b'\xe2' if toggle_selection else RS), handle=handle)
    return {'ok': r['ok'], 'target': key, 'action': 'last_row'}

@_action('tc_table')
def tc_select_all_rows(key: str, handle: str) -> dict:
    """Select all rows of a table."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):        # два кадра: без commit выделение не применяется
        ok = c.send_cmd(G.SELECT_ALL_ROWS, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'select_all'}

@_action('tc_table')
def tc_get_current_row(key: str, handle: str) -> dict:
    """Get the current table row as [{column: value}]. Returns [] if there is no current row or
    its values could not be read. Needs platform 8.5.1 or newer — on every earlier one, use
    get_selected_rows after moving to the desired row, or get_cell_text to read one column."""
    c, err = _need_ver('8.5.1')
    if err: return err
    r = c.send_cmd(G.GET_CURRENT_ROW, key, kind='read', middle=RC, handle=handle)
    return {'ok': r['ok'], 'target': key, 'row': tc1c.decode_rows(r['raw']) if r['ok'] else []}

@_action('tc_table')
def tc_select_row(key: str, handle: str) -> dict:
    """Add the current table row to the selection. Needs platform 8.5.1 or newer — on every
    earlier one the platform has no such method. Build a selection there by moving through rows
    with toggle_selection set — each row the cursor passes is toggled."""
    c, err = _need_ver('8.5.1')
    if err: return err
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.SELECT_ROW, key, kind=kind, middle=RC, handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'select_row'}

@_action('tc_table')
def tc_deselect_row(key: str, handle: str) -> dict:
    """Remove the current table row from the selection. Needs platform 8.5.1 or newer — on
    every earlier one the platform has no such method. The closest thing there is to pass over
    the row with toggle_selection set: that toggles it, so a selected row becomes unselected."""
    c, err = _need_ver('8.5.1')
    if err: return err
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.DESELECT_ROW, key, kind=kind, middle=RC, handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'deselect_row'}

@_action('tc_table')
def tc_deselect_all_rows(key: str, handle: str) -> dict:
    """Clear the table's row selection. Needs platform 8.5.1 or newer — on every earlier one
    the platform has no such method. Plain row navigation drops the selection down to the current
    row, which is the only way to undo a multi-row selection there."""
    c, err = _need_ver('8.5.1')
    if err: return err
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.DESELECT_ALL_ROWS, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'deselect_all_rows'}


@_action('tc_table')
def tc_read_rows(key: str, handle: str, max_rows: int = 500) -> dict:
    """Read all selectable rows of the current table, respecting its filters and collapsed groups.
    Returns row_count and up to max_rows rows (0 for count only); row order is not guaranteed.
    Temporarily selects rows, then clears selection. Keeps a usable current row; on older
    platforms an unavailable current row requires the first row, reported as cursor_repositioned=true.
    Previous selection is cleared. Refuses unfinished row edits. Column keys are titles."""
    c, error = _need_ver('8.3.6')
    if error:
        return error
    if _key_class(key) != 'Table':
        return {'ok': False, 'code': 'invalid_table', 'error': 'Read rows requires a table.'}
    if type(max_rows) is not int or not 0 <= max_rows <= 10000:
        return {'ok': False, 'code': 'invalid_row_limit', 'error': 'max_rows must be an integer from 0 to 10000.'}
    result = {'ok': False, 'target': key, 'rows': [], 'row_count': None,
              'selection_cleared': None, 'scope': 'selectable_rows'}
    attempted = False
    window = None
    record_mark = None
    try:
        window = _cell_window(c)
        state, visible = _verify_target(c, key, handle)
        if state == 'absent':
            return {**result, **_absent_error(key), 'code': 'target_unavailable', 'selection_changed': False}
        if state == 'present' and visible is False:
            return dict(result, code='target_hidden', target_hidden=True,
                        error='The table is hidden. Open its page or change the form state before reading it.',
                        selection_changed=False)
        mode = _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle)
        if mode is not False:
            return dict(result, code='row_edit_pending' if mode is True else 'edit_state_unavailable',
                        error='Finish or explicitly cancel row editing before reading the table.',
                        edit_mode=mode, selection_changed=False)
        record_mark = _native_composite_begin(c)
        if not _guid_available(c, G.DESELECT_ALL_ROWS):
            _table_activate(c, key, handle, window)
        attempted = True
        _cell_step(tc_select_all_rows(key, handle), 'The table rows could not be selected.')
        rows = _cell_step(tc_get_selected_rows(key, handle), 'The selected rows could not be read.')['rows']
        result.update(ok=True, rows=rows[:max_rows], row_count=len(rows),
                      returned_rows=min(len(rows), max_rows), truncated=len(rows) > max_rows)
    except Exception as exc:
        details = exc.result() if isinstance(exc, tc1c.OperationError) else getattr(exc, 'details', {})
        result.update(details, ok=False, error=str(exc))
    finally:
        if attempted:
            try:
                _cell_window(c, window)
                if _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle) is not False:
                    raise RuntimeError('Row editing started or could not be checked; it was left untouched.')
                if _guid_available(c, G.DESELECT_ALL_ROWS):
                    _cell_step(tc_deselect_all_rows(key, handle), 'Selection could not be cleared.')
                elif result.get('row_count') != 0:
                    # No search condition: the cursor stays on the same row, even for duplicate rows.
                    reduced = tc_goto_row(key, handle=handle)
                    if (not reduced.get('ok') and reduced.get('status_code') == 13
                            and (result.get('row_count') or 0) > 0):
                        # Some read-only summaries have no usable current row. Never hide
                        # the fallback's cursor movement, or run it for unrelated refusals.
                        _cell_window(c, window)
                        if _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle) is not False:
                            raise RuntimeError('Row editing started; selection cleanup was stopped.')
                        result['cursor_repositioned'] = None
                        _cell_step(tc_goto_first_row(key, handle), 'The first row could not be made current.')
                        result['cursor_repositioned'] = True
                        _cell_window(c, window)
                        if _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle) is not False:
                            raise RuntimeError('Row editing started after navigation; it was left untouched.')
                    else:
                        _cell_step(reduced, 'Selection could not be reduced to the current row.')
                    remaining = _cell_step(tc_get_selected_rows(key, handle), 'Selection could not be checked.')['rows']
                    if remaining:
                        if len(remaining) != 1:
                            raise RuntimeError('The table did not leave only the current row selected.')
                        _cell_step(tc_goto_row(key, handle=handle, toggle_selection=True), 'Selection could not be cleared.')
                remaining = _cell_step(tc_get_selected_rows(key, handle), 'Selection could not be checked.')['rows']
                if remaining:
                    raise RuntimeError('The table still has selected rows.')
                result['selection_cleared'] = True
            except Exception as exc:
                if result.get('error'):
                    result['read_error'] = result['error']
                result.update(ok=False, code='selection_cleanup_failed', selection_cleared=False,
                              error='Table selection could not be cleared: ' + str(exc),
                              suggested_action='get_selected_rows')
        if record_mark is not None:
            try:
                _native_composite_end(c, record_mark)
            except Exception as exc:
                if result.get('error'):
                    result['operation_error'] = result['error']
                result.update(ok=False, code='recording_incomplete',
                              error='Scenario recording could not continue after this action: ' + str(exc))
    return result

@_action('tc_table')
def tc_copy_row(key: str, handle: str, confirm: bool = None) -> dict:
    """Copy the current table row. On catalog/document
    lists a confirmation dialog may appear — set confirm=True/False to auto-answer it
    (default None: no dialog handling)."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.COPY_ROW, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    ans = _answer_confirm_dialog(c, confirm, max_wait=2.0)[0] if confirm is not None else None
    _state['window_key'] = None          # мог открыться модальный вопрос — активное окно другое
    return {'ok': ok, 'target': key, 'action': 'copy_row', 'dialog_answered': ans}

@_action('tc_table')
def tc_delete_row(key: str, handle: str, confirm: bool = None) -> dict:
    """Delete the current table row. On 8.3.6+, prepares an empty selection and refuses
    unfinished row edits; existing selection is preserved during preparation.
    Set confirm=True/False to answer a deletion dialog
    (None leaves it open). ok confirms the command; deleted=null means its effect is
    unverified. Lists may mark records for deletion instead of removing rows."""
    c = _need()
    mark = None
    result = {'ok': False, 'target': key, 'action': 'delete_row', 'deleted': None}
    try:
        mark = _native_composite_begin(c, 'delete_row')
        count = _table_prepare_delete(c, key, handle)
        for kind in ('action', 'commit'):
            _cell_step(c.send_cmd(G.DELETE_ROW, key, kind=kind, middle=b'', handle=handle),
                       'The delete command was refused.')
        ans = _answer_confirm_dialog(c, confirm, max_wait=2.0)[0] if confirm is not None else None
        result.update(ok=True, selected_rows=count, dialog_answered=ans,
                      deletion_status='requested', message='Delete command accepted; its effect has not been verified.')
        if ans and confirm is False:
            result.update(deleted=False, deletion_status='cancelled', message='Deletion was cancelled.')
    except Exception as exc:
        details = exc.result() if isinstance(exc, tc1c.OperationError) else getattr(exc, 'details', {})
        result.update(details, ok=False, error=str(exc))
    finally:
        _state['window_key'] = None
        if mark is not None:
            try:
                _native_composite_end(c, mark, 'delete_row')
            except Exception as exc:
                result.update(ok=False, code='recording_incomplete', error=str(exc))
    return result


def _table_activate(c, key, handle, window):
    """Prepare a table without committing another element's pending input."""
    form = _form_object(c)
    if not form or not key.startswith(form['key'] + '.'):
        raise _CellEditFailure('The table is not on the active form.', {'code': 'target_not_interactive'})
    current = _input_current(c, form)
    other_table = re.match(r'^(.*\.Table\[[^\]]+\])(?:\.|$)', current or '')
    if other_table:
        owner = other_table.group(1)
        if owner != key:
            obj = _ref_live_object(c, owner)
            if not obj or _cell_flag(c, G.CURRENT_MODE_IS_EDIT, owner, obj['handle']) is not False:
                raise _CellEditFailure('Finish or cancel editing in the other table first.',
                                       {'code': 'other_input_pending'})
    elif current and _key_class(current) == 'EditField':
        obj = _ref_live_object(c, current)
        if not obj:
            raise _CellEditFailure('The current field is unavailable.', {'code': 'edit_state_unavailable'})
        kind = _kind_of(c, current)
        if kind == 'InputField' and getattr(c, '_pending_text_input', None) == current:
            try:
                r = c.send_cmd(G.GET_EDIT_TEXT, current, kind='read', middle=RS, handle=obj['handle'])
                text = _field_scalar_text(c, current, r, G.GET_EDIT_TEXT) if r['ok'] else None
            except tc1c.OperationError as exc:
                if exc.status not in (10, 11, 17):
                    raise
                text = None  # This field has no readable input buffer in this state.
            if text is None:
                raise _CellEditFailure('Pending field input could not be checked; finish or cancel it first.',
                                       {'code': 'edit_state_unavailable'})
            if text is not None:
                r = c.send_cmd(G.GET_PROPERTY, current, kind='read', middle=RC, handle=obj['handle'])
                accepted = _field_scalar_text(c, current, r, G.GET_PROPERTY) if r['ok'] else None
                if text != accepted:
                    raise _CellEditFailure('Finish or cancel input in the current field first.',
                                           {'code': 'other_input_pending'})
                c._pending_text_input = None
        elif kind == 'SpreadsheetDocumentField':
            if _cell_flag(c, G.CURRENT_MODE_IS_EDIT_FIELD, current, obj['handle']) is not False:
                raise _CellEditFailure('Finish or cancel document editing first.', {'code': 'other_input_pending'})
        elif kind in ('TextDocumentField', 'FormattedDocumentField', 'HTMLDocumentField'):
            raise _CellEditFailure('Activate the table explicitly after finishing document input.',
                                   {'code': 'edit_state_unavailable'})
    _cell_step(tc_activate(key, handle), 'The table could not be activated.')
    _cell_window(c, window)
    if _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle) is not False:
        raise _CellEditFailure('Finish or cancel row editing first.', {'code': 'row_edit_pending'})


def _table_prepare_delete(c, key, handle):
    if _key_class(key) != 'Table':
        raise _CellEditFailure('Deletion requires a table.', {'code': 'invalid_table'})
    if not _guid_available(c, G.GET_SELECTED_ROWS):
        # Preserve deletion on 8.3.1--8.3.5 without sending unavailable diagnostics.
        return None
    window = _cell_window(c)
    if _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle) is not False:
        raise _CellEditFailure('Finish or cancel row editing before deletion.', {'code': 'row_edit_pending'})
    before = _cell_step(tc_get_selected_rows(key, handle), 'Selection could not be read.')['rows']
    _table_activate(c, key, handle, window)
    selected = _cell_step(tc_get_selected_rows(key, handle), 'Selection could not be checked.')['rows']
    if before:
        # Compare as multisets: platform selection order is unspecified, duplicates matter.
        def canonical(rows):
            return sorted(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)
        if canonical(before) != canonical(selected):
            raise _CellEditFailure('Selection changed while activating the table. Select the intended rows again.',
                                   {'code': 'selection_changed'})
    else:
        if not selected:
            if _guid_available(c, G.SELECT_ROW):
                current = _cell_step(tc_get_current_row(key, handle), 'The current row could not be read.')['row']
                if not current:
                    raise _CellEditFailure('No current row is available for deletion.', {'code': 'no_current_row'})
                _cell_step(tc_select_row(key, handle), 'The current row could not be selected.')
            else:
                # No criteria: retain the current row, including when several rows have identical values.
                _cell_step(tc_goto_row(key, handle=handle), 'The current row could not be selected.')
            selected = _cell_step(tc_get_selected_rows(key, handle), 'Selection could not be checked.')['rows']
        if len(selected) != 1:
            raise _CellEditFailure('No single current row is available for deletion.', {'code': 'no_current_row'})
    return len(selected)

@_action('tc_table')
def tc_table_add_row(key: str, handle: str) -> dict:
    """Add a row to a form table. Fill its cells with set_cell_text using column element names."""
    c = _need()
    ok = True
    for guid in (G.ACTIVATE, G.ADD_ROW):
        for kind in ('action', 'commit'):
            r = c.send_cmd(guid, key, kind=kind, middle=b'', handle=handle)
            ok = ok and r['ok']
    return {'ok': ok, 'table': key}

# ======================= таблица/дерево: элементы ===========================
@_action('tc_table')
def tc_goto_next_item(key: str, handle: str) -> dict:
    """Move to the next item within a table. key/handle: the table."""
    if _key_class(key) != 'Table':
        return {'ok': False, 'target': key, 'code': 'unsupported_element_type',
                'error': 'This action requires a table. For form navigation use '
                         'tc_form(action="goto_next_element") on the managed form.'}
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GOTO_NEXT_ITEM, key, kind=kind, middle=tc1c.RES_E2, handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'next_item'}

@_action('tc_table')
def tc_goto_previous_item(key: str, handle: str) -> dict:
    """Move to the previous item within a table. key/handle: the table."""
    if _key_class(key) != 'Table':
        return {'ok': False, 'target': key, 'code': 'unsupported_element_type',
                'error': 'This action requires a table. For form navigation use '
                         'tc_form(action="goto_previous_element") on the managed form.'}
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GOTO_PREVIOUS_ITEM, key, kind=kind, middle=RS, handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'prev_item'}

@_action('tc_table')
def tc_go_one_level_up(key: str, handle: str, row_column: str = None, row_value=None,
                       column: str | int = None) -> dict:
    """Go one level up in a table tree. Pass row_column+row_value to target a specific row by a
    column value; row_column accepts a column name or title. Omit both to use the current row. Optional column names a column to read
    before and after the move; changed compares its cell text."""
    c = _need()
    mid = tc1c.mk_tree_middle(RC, row_column, row_value)
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GO_ONE_LEVEL_UP, key, kind=kind, middle=mid, handle=handle,
                        pad=tc1c.tree_row_pad(row_column))['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'level_up'}

@_action('tc_table')
def tc_go_one_level_down(key: str, handle: str, row_column: str = None, row_value=None,
                         column: str | int = None) -> dict:
    """Go one level down in a table tree. Pass row_column+row_value to target a specific row by a
    column value; row_column accepts a column name or title. Omit both to use the current row. Optional column names a column to read
    before and after the move; changed compares its cell text."""
    c = _need()
    mid = tc1c.mk_tree_middle(RC, row_column, row_value)
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GO_ONE_LEVEL_DOWN, key, kind=kind, middle=mid, handle=handle,
                        pad=tc1c.tree_row_pad(row_column))['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'level_down'}

@_action('tc_table')
def tc_change_row(key: str, handle: str) -> dict:
    """Start editing the current table row/column. Activate the intended column first.
    Needs an existing row not already in edit mode. edit_mode confirms whether editing started;
    null means it could not be checked. For text input use set_cell_text for automatic preparation."""
    c = _need()
    check = _guid_available(c, G.CURRENT_MODE_IS_EDIT)
    window = _window(c).get('key') if check else None
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.CHANGE_ROW, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    result = {'ok': ok, 'target': key, 'action': 'change_row', 'edit_mode': None}
    if not ok:
        return result
    if not check:
        result['message'] = 'Editing requested; this platform cannot check the row edit mode.'
        return result
    try:
        if not window:
            raise _CellEditFailure('The active window could not be determined.')
        _cell_window(c, window)
        result['edit_mode'] = _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle)
    except Exception:
        pass  # An accepted edit may open another window; do not act on the covered table.
    if result['edit_mode'] is not True:
        result.update(ok=False, command_accepted=True,
                      code='row_edit_refused' if result['edit_mode'] is False else 'edit_state_unavailable',
                      error='Row editing did not start.' if result['edit_mode'] is False
                            else 'The row edit mode could not be verified. Inspect the active window.',
                      suggested_action='get_current_item' if result['edit_mode'] is False else 'get_active_window')
        if result['edit_mode'] is False:
            result['message'] = 'Activate the intended column before change_row, or use set_cell_text for text input.'
    return result

@_action('tc_table')
def tc_switch_row_delete_mark(key: str, handle: str, confirm: bool = True) -> dict:
    """Toggle the deletion mark of the current row. Raises a modal 'mark for deletion?' dialog that is auto-answered: confirm=True → Yes
    (default), False → No. dialog_answered only reports that a modal question was answered — it is
    NOT evidence that the mark changed, and changed is always null here because the platform
    exposes no readable deletion-mark flag. To check the result, click the row's mark command and
    read the question text: 'mark for deletion?' means it is not marked, 'remove the mark?' means
    it is."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.SWITCH_ROW_DELETE_MARK, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    answered = _answer_confirm_dialog(c, confirm, max_wait=5.0)[0]  # ответить на «Пометить на удаление?»
    _state['window_key'] = None          # диалог мог остаться открытым — окно не то, что было
    return {'ok': ok, 'target': key, 'confirmed': confirm, 'dialog_answered': answered}

@_action('tc_table')
def tc_end_edit_row(key: str, handle: str, cancel: bool = False) -> dict:
    """Finish editing the current table row. Set cancel to discard the edits instead of
    committing them."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        mid = b'\xe2' if cancel else RS
        ok = c.send_cmd(G.END_EDIT_ROW, key, kind=kind, middle=mid, handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'end_edit_row'}

@_action('tc_table')
def tc_expand(key: str, handle: str, row_column: str = None, row_value=None, subordinates: bool = False) -> dict:
    """Expand a form group (Group[...]) or a table node. For a table, pass row_column+row_value to
    target a row by a column name or title; omit them for the current row. Set subordinates to also
    expand the child rows. Nothing to expand is not an error: ok only reports that the client
    accepted the command. changed is false only when value_before and value_after were both read
    and came back equal, which happens for a TABLE node, and null when they could not be read —
    a FORM GROUP (the platform's Expanded applies to tables only), a failed read, or
    readback='off'. can_be_expanded checks a table row, not a form group, and its true is not a
    promise: judge by value_before/value_after."""
    c = _need()
    is_group = _key_class(key) == 'Group'
    if is_group:
        guid, middle = G.EXPAND_GROUP, b''
    else:
        base = b'\xe2\xcb\x55' if subordinates else b'\xe1\xcb\x55'
        guid, middle = G.EXPAND_TABLE, tc1c.mk_tree_middle(base, row_column, row_value)
    ok = True
    # Для узла таблицы действие применяется только после завершающего кадра.
    for kind in ('action', 'commit'):
        ok = c.send_cmd(guid, key, kind=kind, middle=middle, handle=handle,
                        pad=None if is_group else tc1c.tree_row_pad(row_column))['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'expand'}

@_action('tc_table')
def tc_collapse(key: str, handle: str, row_column: str = None, row_value=None) -> dict:
    """Collapse a form group (Group[...]) or a table node. For a table, pass row_column+row_value to
    target a row by a column name or title; omit them for the current row. Nothing to collapse is not an
    error: ok only reports that the client accepted the command. changed is false only when
    value_before and value_after were both read and came back equal, which happens for a TABLE
    node, and null when they could not be read — a FORM GROUP (the platform's Expanded applies
    to tables only), a failed read, or readback='off'. can_be_expanded checks a table row, not a
    form group, and its true is not a promise: judge by value_before/value_after."""
    c = _need()
    is_group = _key_class(key) == 'Group'
    if is_group:
        guid, mid = G.COLLAPSE_GROUP, b''
    else:
        guid = G.COLLAPSE_TABLE
        mid = tc1c.mk_tree_middle(RC, row_column, row_value)
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(guid, key, kind=kind, middle=mid, handle=handle,
                        pad=None if is_group else tc1c.tree_row_pad(row_column))['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'collapse'}

@_action('tc_field')
def tc_get_current_page(key: str, handle: str) -> dict:
    """Get the current page of a page group. To switch pages use tc_activate: a click on a page
    does not switch to it. tc_current_check is useless here — pages always report checked=false."""
    c = _need()
    r = c.send_cmd(G.GET_CURRENT_PAGE, key, kind='read', middle=RC, handle=handle)
    return {'ok': r['ok'], 'target': key, 'page': _coll(r, key)}

@_action('tc_field')
def tc_get_command_bar(key: str, handle: str) -> dict:
    """Get an element's own command panel object, if it has one. This is NOT the list of buttons:
    the panel is a container, and its buttons are read with a separate tc_get_child_objects on the
    returned key. An empty result means the element has no command panel of its own — a list
    table is the usual case, its buttons live in a form group next to it."""
    c = _need()
    r = c.send_cmd(G.GET_COMMAND_BAR, key, kind='read', middle=RC, handle=handle)
    return {'ok': r['ok'], 'target': key, 'commandbar': _coll(r, key)}

# ===================== окно: навигация / команды / сообщения ================
def _winkey(c):
    k = _window(c)['key']
    if not k:
        raise RuntimeError('no active window')
    return k

@_action('tc_window')
def tc_goto_next_window() -> dict:
    """Go to the next open window from the active main application window. The client returns an error if navigation is unavailable."""
    c = _need()
    wk = _winkey(c); ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GOTO_NEXT_WINDOW, wk, kind=kind, middle=b'')['ok'] and ok
    _state['window_key'] = None          # активным стало ДРУГОЕ окно — кэш ключа недействителен
    return {'ok': ok, 'action': 'next_window'}

@_action('tc_window')
def tc_goto_previous_window() -> dict:
    """Go to the previous open window from the active main application window. The client returns an error if navigation is unavailable."""
    c = _need()
    wk = _winkey(c); ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GOTO_PREVIOUS_WINDOW, wk, kind=kind, middle=b'')['ok'] and ok
    _state['window_key'] = None
    return {'ok': ok, 'action': 'prev_window'}

@_action('tc_window')
def tc_goto_start_page() -> dict:
    """Go to the start page from the active main application window. The client returns an error if navigation is unavailable."""
    c = _need()
    wk = _winkey(c); ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GOTO_START_PAGE, wk, kind=kind, middle=b'')['ok'] and ok
    _state['window_key'] = None
    return {'ok': ok, 'action': 'start_page'}

@_action('tc_window')
def tc_close_window() -> dict:
    """Close the current active window. ok only says the close was accepted: the configuration may
    answer with a modal question ('Send the invitations?', 'Save the changes?') and leave the
    window open. Check with get_active_window afterwards. Also closes an active local print
    preview identified by get_active_window, returning to the underlying form. If the active
    window cannot be identified and local recovery is unavailable, returns
    code=active_window_unavailable without closing another window."""
    return _close_active_window(_need())


def _close_active_window(c, send=None, native_only=False):
    send = send or c.send_cmd
    active = _window(c)
    wk = active['key']; ok = True
    if native_only and not active.get('native'):
        return {'ok': False, 'error': 'the expected preview is not active; no window was closed'}
    if not wk:
        native = _native_active_window(c) if active.get('native') else None
        if native:
            isolated = _state.get('_isolated_process')
            if isolated is not None:
                try:
                    pid, created = _screenshots.resolve_process(c)
                    closed = isolated.call('close_window', dict(pid=pid, created=created, window=native)).get('closed', False)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    closed = False
            else:
                from _native_window import close_secondary_window
                closed = close_secondary_window(native)
            if closed and getattr(c, '_track', None) is not None:
                c._track.append((G.CLOSE, None, b'\xe2', 'action'))
            return {'ok': closed, 'closed': native['title'] if closed else None, 'native': True,
                    **({} if closed else {'error': 'the preview did not close; check the active window'})}
        info = _unavailable_window()
        return {'ok': False, 'closed': None, 'error': info.pop('message'), **info}
    for kind in ('action', 'commit'):
        ok = send(G.CLOSE, wk, kind=kind, middle=b'')['ok'] and ok
    _state['window_key'] = None
    return {'ok': ok, 'closed': wk}

@_action('tc_window')
def tc_execute_command(command: str) -> dict:
    """Open a navigation link, e.g. 'e1cib/list/Справочник.Контрагенты', 'e1cib/app/Обработка.Имя'
    or a URL returned by the command interface. For a button name or title, find it and use click.
    Returns the resulting window; an opened error window is a failure."""
    c = _need()
    if not isinstance(command, str) or not re.fullmatch(
            r'(?:e1cib/\S.*|(?:e1c|https?)://\S.*)', command.strip(), re.IGNORECASE) or any(
                ord(ch) < 32 for ch in command):
        return {'ok': False, 'code': 'invalid_navigation_link',
                'error': 'Pass a navigation link, not a button name or title.',
                'suggested_action': 'find_objects',
                'message': 'Find the button by name or title, then call click on its ref.'}
    command = command.strip()
    wk = _winkey(c); ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.EXECUTE_COMMAND, wk, kind=kind, middle=tc1c.mk_command(command))['ok'] and ok
    _state['window_key'] = None          # команда могла открыть/переключить окно
    result = {'ok': ok, 'command': command}
    if ok:
        try:
            window = _window(c)
            result['window'] = window
            if window.get('key'):
                title, form = _form_title(c, window['key'])
                if title is not None:
                    window['title'] = title
                if form:
                    window['form_name'] = form
                if form == 'ErrorWindow':
                    result.update(ok=False, code='navigation_failed',
                                  error='1C opened an error window instead of completing navigation.',
                                  suggested_action='find_objects',
                                  message='Read the fields of the returned error window for details.')
            else:
                result['verification'] = 'window_unavailable'
        except Exception:
            result['verification'] = 'window_unavailable'
    return result

@_action('tc_window')
def tc_get_command_interface() -> dict:
    """Get the window's command interface → collection of buttons/groups."""
    c = _need()
    wk = _winkey(c)
    r = c.send_cmd(G.GET_COMMAND_INTERFACE, wk, kind='read', middle=RC)
    return {'ok': r['ok'], 'commands': _coll(r, wk)}

@_action('tc_window')
def tc_get_user_message_texts() -> dict:
    """Get the user-message texts shown in the window → list of strings. These ACCUMULATE over the
    session: a complaint from an earlier attempt is still listed after a later attempt succeeded.
    To judge one action, call tc_close_user_messages_panel first, then the action, then this."""
    c = _need()
    try:
        r = c.send_cmd(G.GET_USER_MESSAGE_TEXTS, _winkey(c), kind='read', middle=RC)
    except tc1c.OperationError as exc:
        if exc.status != 17:
            raise
        return dict(exc.result(), code='user_messages_unavailable', messages=None,
                    error='The user-message panel is unavailable in this window.',
                    message='The message panel is closed or unavailable in this window. '
                            'This does not establish that the last action had no validation errors.')
    vals = _vals(r)
    return {'ok': r['ok'], 'messages': vals}

@_action('tc_window')
def tc_answer_dialog(confirm: bool = True, timeout: int = 5) -> dict:
    """Answer a modal Yes/No question raised by the configuration. The question is an ordinary window
    and its buttons are picked by NAME: Button0 answers yes, Button1 answers no. A dialog may
    offer a THIRD choice (Button2 is often 'Отмена') which this action never presses — read
    `question` and inspect the window with tc_find_objects when the answer you need is a
    different button. Waits up to timeout seconds for such a dialog to appear. Returns
    answered='Да'/'Нет' and `question`, or both null when no dialog showed up; `question` alone
    is null when the dialog carried no readable message."""
    c = _need()
    ans, question = _answer_confirm_dialog(c, confirm, max_wait=max(timeout, 0))
    return {'ok': True, 'answered': ans, 'question': question}

@_action('tc_window')
def tc_close_user_messages_panel() -> dict:
    """Close the window's user-messages panel. This is also how you tell which messages belong
    to which action: clear the panel, perform the action, then read the messages."""
    c = _need()
    wk = _winkey(c); ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.CLOSE_USER_MESSAGES_PANEL, wk, kind=kind, middle=b'')['ok'] and ok
    return {'ok': ok}

@_action('tc_form')
def tc_goto_next_element(key: str, handle: str) -> dict:
    """Move focus to the next element in the managed form's tab order. key/handle: the form
    (ManagedForm). This can finish the current field's pending input; read the field's data
    presentation to verify acceptance. Reference fields may still require choosing a value."""
    if _key_class(key) != 'ManagedForm':
        return {'ok': False, 'target': key, 'code': 'unsupported_element_type',
                'error': 'This action requires a managed form. Address the form containing the element.'}
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.FORM_GOTO_NEXT_ITEM, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'next_element'}

@_action('tc_form')
def tc_goto_previous_element(key: str, handle: str) -> dict:
    """Move focus to the previous element in the managed form's tab order. key/handle: the form
    (ManagedForm). This can finish the current field's pending input; read the field's data
    presentation to verify acceptance."""
    if _key_class(key) != 'ManagedForm':
        return {'ok': False, 'target': key, 'code': 'unsupported_element_type',
                'error': 'This action requires a managed form. Address the form containing the element.'}
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.FORM_GOTO_PREVIOUS_ITEM, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'previous_element'}

@_action('tc_form')
def tc_get_current_element(key: str, handle: str) -> dict:
    """Get the managed form's focused element as item: [{key, handle}]. key/handle: the form
    (ManagedForm). The platform can return no current element after navigating out of its fields."""
    if _key_class(key) != 'ManagedForm':
        return {'ok': False, 'target': key, 'code': 'unsupported_element_type',
                'error': 'This action requires a managed form. Address the form containing the element.'}
    c = _need()
    r = c.send_cmd(G.FORM_GET_CURRENT_ITEM, key, kind='read', middle=RC, handle=handle)
    items = _coll(r, key)
    result = {'ok': r['ok'], 'item': items}
    if not items:
        result['current_status'] = 'none' if r.get('ok') and tc1c.empty_current_form_item(r['raw']) else 'unavailable'
    return result

@_action('tc_form')
def tc_find_default_button(key: str, handle: str) -> dict:
    """Find the form's default button. key is the form's key."""
    c = _need()
    r = c.send_cmd(G.FIND_DEFAULT_BUTTON, key, kind='read', middle=RC, handle=handle)
    return {'ok': r['ok'], 'button': _coll(r, key)}

@_action('tc_form')
def tc_execute_choice_from_list(key: str, index: int | str, handle: str) -> dict:
    """Pick an item from a modal choice list by 0-based index or display text. Not a field's
    drop-down (use tc_choose_from_drop_list). key/handle: the form (ManagedForm), not a field."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.EXECUTE_CHOICE_FROM_LIST, key, kind=kind, middle=tc1c.mk_choice(index), handle=handle)['ok'] and ok
    _state['window_key'] = None          # модальный выбор закрылся — активное окно другое
    return {'ok': ok, 'target': key, 'index': index}

@_action('tc_form')
def tc_execute_choice_from_menu(key: str, index: int | str, handle: str) -> dict:
    """Choose an item from an open menu by 0-based index or display text. For a submenu,
    call again to choose its item. Address the form or the spreadsheet field that opened
    the menu. A spreadsheet field requires platform 8.3.25 or newer."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.EXECUTE_CHOICE_FROM_MENU, key, kind=kind, middle=tc1c.mk_choice(index), handle=handle)['ok'] and ok
    _state['window_key'] = None          # модальный выбор закрылся — активное окно другое
    return {'ok': ok, 'target': key, 'index': index}

# ===================== гиперссылки / порядок / варианты =====================
@_action('tc_doc')
def tc_click_html_hyperlink(key: str, index: int | str, handle: str) -> dict:
    """Click a hyperlink in an HTML-document field. The platform clicks the FIRST link whatever
    you pass: measured with three links and an index of 0, 1 and 2, and the same encoding the
    platform's own test manager sends. An index beyond the number of links is refused, so the
    argument is read — it just does not choose. Addressing by text does nothing at all here."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.CLICK_HTML_DOC_HYPERLINK, key, kind=kind, middle=tc1c.mk_choice(index), handle=handle)['ok'] and ok
    _state['window_key'] = None          # переход по ссылке мог сменить активное окно
    return {'ok': ok, 'target': key, 'index': index}

@_action('tc_doc')
def tc_click_formatted_doc_hyperlink(key: str, index: int | str, handle: str) -> dict:
    """Click a hyperlink in a formatted-document field by 0-based index (or by its text). On a
    document without links the platform answers the same and puts up its own error window, and
    tc_doc(action="get_formatted_string_hyperlinks") cannot be used to check first: for a
    formatted DOCUMENT it answers with an empty list even when the document does have a link (it
    lists links only for a formatted-string label). Judge by what the click was supposed to do."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.CLICK_FORMATTED_DOC_HYPERLINK, key, kind=kind, middle=tc1c.mk_choice(index), handle=handle)['ok'] and ok
    _state['window_key'] = None          # переход по ссылке мог сменить активное окно
    return {'ok': ok, 'target': key, 'index': index}

@_action('tc_doc')
def tc_click_formatted_string_hyperlink(key: str, index: int | str, handle: str) -> dict:
    """Click a hyperlink in a formatted string by 0-based index (or by its text). key/handle may
    be a label field or a form decoration bearing the formatted string."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.CLICK_FORMATTED_STRING_HYPERLINK, key, kind=kind, middle=tc1c.mk_choice(index), handle=handle)['ok'] and ok
    _state['window_key'] = None          # переход по ссылке мог сменить активное окно
    return {'ok': ok, 'target': key, 'index': index}

@_action('tc_doc')
def tc_get_formatted_string_hyperlinks(key: str, handle: str) -> dict:
    """Get a formatted string's hyperlink presentations."""
    c = _need()
    r = c.send_cmd(G.GET_FORMATTED_STRING_HYPERLINKS, key, kind='read818', middle=CHOICE_READ, handle=handle)
    vals = _vals(r)
    return _doc_read(c, key, handle, {'ok': r['ok'], 'target': key, 'hyperlinks': vals},
                     not vals)

@_action('tc_table')
def tc_set_order(key: str, column: str | int, handle: str) -> dict:
    """Sort a table by a column, addressed by its TITLE. There is no direction parameter and no way
    to read the current direction: calling it again on the same column reverses the order. To learn
    which way it went, go to the first row and read a cell."""
    c = _need()
    # тип параметра общий на всю группу: число сюда доходит, но сортировка адресует колонку
    # заголовком — отказываем внятно, а не питоновской ошибкой из сборщика кадра
    if _is_index(column):
        return {'ok': False, 'target': key, 'error': _COLUMN_BY_TITLE}
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.SET_ORDER, key, kind=kind, middle=tc1c.mk_order(column), handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'column': column}

@_action('tc_field')
def tc_goto_value(key: str, percent: int, handle: str) -> dict:
    """Move a TRACK BAR to a value in PERCENT (0..100). This is a track bar method: on any other
    kind of field the command is accepted and nothing moves, and the answer then carries
    `applicable: false`. A spinner is stepped with increase_value/decrease_value instead.
    Returns changed/value_before/value_after."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GOTO_VALUE, key, kind=kind, middle=tc1c.mk_goto_value(percent), handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'percent': percent}

@_action('tc_field')
def tc_select_option(key: str, value: str | int, handle: str) -> dict:
    """Pick a radio-button option by its display text or by its 0-based index."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        r = c.send_cmd(G.SELECT_OPTION, key, kind=kind, middle=tc1c.mk_choice(value), handle=handle)
        ok = ok and r['ok']
    return {'ok': ok, 'target': key, 'value': value}

# ===================== табличный документ: области ==========================
@_action('tc_doc')
def tc_set_current_area(key: str, address: str, handle: str) -> dict:
    """Set the current area of a spreadsheet-document field (e.g. 'R1C1')."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.SET_CURRENT_AREA, key, kind=kind, middle=tc1c.mk_command(address), handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'area': address}

@_action('tc_doc')
def tc_get_current_area_address(key: str, handle: str) -> dict:
    """Get the address of the current spreadsheet-document area."""
    c = _need()
    r = c.send_cmd(G.GET_CURRENT_AREA_ADDRESS, key, kind='read', middle=RS, handle=handle)
    vals = _vals(r)
    address = vals[-1] if vals else None
    return _doc_read(c, key, handle, {'ok': r['ok'], 'address': address}, address is None)

@_action('tc_doc')
def tc_get_current_area_text(key: str, handle: str, area: str = None) -> dict:
    """Get the text of ONE spreadsheet-document area; omit area to read the current one. This is the
    older form of tc_doc(action="get_area_text"), which the platform deprecated in 8.3.6 in
    favour of that one; prefer get_area_text."""
    return _area_text_read(_need(), G.GET_CURRENT_AREA_TEXT, key, handle, area)

@_action('tc_doc')
def tc_get_area_text(key: str, handle: str, area: str = None) -> dict:
    """Get the text of ONE spreadsheet-document area; omit area to read the current one.
    tc_doc(action="get_current_area_text") is the older form of this same call and answers
    identically; prefer this one."""
    return _area_text_read(_need(), G.GET_AREA_TEXT, key, handle, area)

@_action('tc_doc')
def tc_get_current_area_field(key: str, handle: str) -> dict:
    """Get the field of the current spreadsheet-document area."""
    c = _need()
    r = c.send_cmd(G.GET_CURRENT_AREA_FIELD, key, kind='read', middle=RC, handle=handle)
    field = _coll(r, key)
    return _doc_read(c, key, handle, {'ok': r['ok'], 'field': field}, not field)

@_action('tc_doc')
def tc_begin_edit_current_area(key: str, handle: str) -> dict:
    """Start editing the current spreadsheet area; follow with input_text and end_edit_current_area.
    In view mode this can instead open a drill-down menu, object or field chooser.
    A menu may leave the active window unchanged; use execute_choice_from_menu to continue."""
    c = _need()
    before = _area_action_window(c)
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.BEGIN_EDIT_CURRENT_AREA, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    result = {'ok': ok, 'target': key}
    if ok:
        after = _area_action_window(c)
        changed = before != after if before and after else None
        result['window_changed'] = changed
        mode = None
        if changed is False and _guid_available(c, G.CURRENT_MODE_IS_EDIT_FIELD):
            try:
                mode = _cell_flag(c, G.CURRENT_MODE_IS_EDIT_FIELD, key, handle)
            except Exception:
                pass
        result['edit_mode'] = mode
        pending = getattr(c, '_pending_area_edits', set())
        if mode is not False and changed is not True:
            pending.add(key)
        else:
            pending.discard(key)
        c._pending_area_edits = pending
        if changed is True:
            result.update(suggested_action='get_active_window',
                          message='The action changed the active window. Inspect it to continue.')
        elif mode is False:
            result.update(suggested_action='execute_choice_from_menu',
                          message='Cell editing did not start. If this area opens a drill-down menu, use execute_choice_from_menu on this field.')
    return result


def _area_action_window(c):
    if not _guid_available(c, G.GET_ACTIVE_WINDOW):
        return None
    try:
        return _window(c).get('key')
    except Exception:
        return None

@_action('tc_doc')
def tc_end_edit_current_area(key: str, handle: str, cancel: bool = False) -> dict:
    """Finish editing the current spreadsheet-document area. Set cancel to discard the edit
    instead of committing it. Returns the cell address and its text before/after finishing;
    changed compares those values, including when an edit is cancelled."""
    c = _need()
    ok = True
    # ОтменаРедактирования: маркер e2(Истина)/e1(Ложь) в ОБОИХ кадрах пары
    mid = CANCEL_EDIT_MID if cancel else RS
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.END_EDIT_CURRENT_AREA, key, kind=kind, middle=mid, handle=handle)['ok'] and ok
    if ok:
        getattr(c, '_pending_area_edits', set()).discard(key)
    return {'ok': ok, 'target': key, 'cancelled': bool(cancel)}

@_action('tc_doc')
def tc_included_in_merged_area(key: str, address: str, handle: str) -> dict:
    """Return the address of the merged area containing the cell (e.g. 'R1C1'), or None if the
    cell is not part of a merged area. A null answer is ambiguous in one more way: it also comes
    back when the document has no such cell or no area by that name — the platform does not
    distinguish the two, and neither can this action. Only the FORM of the address is checked
    here (cell, range, area name, intersection); whether it exists is up to the document."""
    c = _need()
    if not _area_form_ok(address):
        return {'ok': False, 'target': key, 'address': address,
                'error': 'this is not an area address: pass a cell (R5C1), a range (R1C1:R1C3), '
                         'an area NAME, or an intersection of two names (Header | Total)'}
    merged, ok = _merged_area(c, key, handle, address)
    return _doc_read(c, key, handle, {'ok': ok, 'merged_area': merged}, merged is None)

@_action('tc_doc')
def tc_text_within_area_bounds(key: str, handle: str, area: str = None) -> dict:
    """Whether the text in a spreadsheet-document area fits within its bounds (True) or is
    clipped to '#####' (False). Pass area (e.g. 'R1C1'); omit to check the current cell."""
    c = _need()
    mid = tc1c.mk_area(area) if area else b'\xe1\x81'
    r = c.send_cmd(G.TEXT_WITHIN_AREA_BOUNDS, key, kind='read', middle=mid, handle=handle)
    # булево — байт в позиции raw[-8] (хвост: <маркер> 20 a1 a3 <TR>). В контексте
    # этого метода Да/Нет кодируется 82/81 (аналог скалярных e2/e1).
    raw = _body(r)
    b = raw[-8] if len(raw) >= 8 else None
    fits = True if b in (0x82, 0xe2) else (False if b in (0x81, 0xe1) else None)
    return _doc_read(c, key, handle, {'ok': r['ok'], 'fits': fits}, fits is None)

_RANGE_REFUSAL = ('ranges are not supported: the method returns the text of a single area. '
                  'Read the cells one by one (R1C1, R1C2, …).')

def _guid_available(c, guid):
    """Знает ли подключённый клиент этот метод. Неизвестный GUID старый клиент не отвечает,
    и вызов висит до таймаута сокета, поэтому внутренние команды сверяются с той же таблицей
    версий, что и инструменты."""
    minv = GUID_MIN_VERSION.get(guid)
    cur = _conn_ver(c)
    return not (minv and _vt(cur) and _vt(minv) and _vt(cur) < _vt(minv))

_AREA_FORMS = (
    # координата и диапазон: R<число>C<число>[:R<число>C<число>]
    r'^R\d+C\d+(:R\d+C\d+)?$',
    # имя области: как идентификатор 1С (буквы/цифры/подчёркивание, не с цифры)
    r'^[^\W\d]\w*$',
)

def _area_form_ok(area):
    """Является ли строка ХОТЬ ОДНОЙ из форм адреса: координата, диапазон, имя области или
    пересечение имён («А | Б»). Существование области здесь не проверяется и проверено быть не
    может: имя может быть любым идентификатором, а наличие ячейки зависит от документа."""
    import re as _re
    if not isinstance(area, str) or not area.strip():
        return False
    parts = [p.strip() for p in area.split('|')]
    if any(not p for p in parts):
        return False
    return all(any(_re.match(f, p, _re.UNICODE) for f in _AREA_FORMS) for p in parts)

def _range_refused(key, area):
    return {'ok': False, 'target': key, 'area': area, 'error': _RANGE_REFUSAL}

def _merged_area(c, key, handle, address):
    """Адрес объединённой области, содержащей ячейку, либо None. Ответ несёт ЭХО переданного
    адреса — снимаем его, иначе одиночная ячейка выглядела бы объединённой областью."""
    r = c.send_cmd(G.INCLUDED_IN_MERGED_AREA, key, kind='read',
                   middle=tc1c.mk_area(address), handle=handle)
    text = tc1c.decode_area_text(_body(r), address)
    if text is not None:
        return text, r['ok']
    vals = _vals(r)
    if vals and vals[0] == address:
        vals = vals[1:]  # Remove only the argument echo, even when the result is identical.
    return (vals[-1] if vals else None), r['ok']

def _area_text_read(c, guid, key, handle, area):
    """Текст ОДНОЙ области. Общий для обоих чтений: это один метод платформы, и расхождение
    между ними было бы дефектом само по себе. Клиент передаётся уже полученным — версию
    инструмента проверяет `_need` в самом инструменте, а вызов её отсюда молча отключил бы её.

    Диапазон адресуется только когда он и есть ОДНА область (объединённая). Порядок здесь
    существенный: уточняющая команда `ВходитВОбъединённуюОбласть` отправляется лишь после того,
    как обычное чтение вернуло содержательный ответ, то есть получатель уже доказал, что ведёт
    себя как табличный документ. На пустом ответе она не отправляется вовсе."""
    rng = bool(area) and ':' in area
    if rng and not _guid_available(c, G.INCLUDED_IN_MERGED_AREA):
        return _range_refused(key, area)          # старый клиент: прежний отказ, без команд
    mid = tc1c.mk_area(area) if area else b'\xe1\x81'
    r = c.send_cmd(guid, key, kind='read', middle=mid, handle=handle)
    text = _area_value(_body(r), area, guid)
    if not rng:
        result = _doc_read(c, key, handle, {'ok': r['ok'], 'text': text}, text is None)
        if 'text' in result:
            return _unavailable_read(result, 'text', suggested_action='read_document',
                                     message='No cell text is available; this does not establish that the area is empty. '
                                             'Use read_document to locate readable cells.')
        return result
    if text is None:
        return _doc_read(c, key, handle, _range_refused(key, area), True)
    merged, _ok = _merged_area(c, key, handle, area.split(':')[0])
    same = merged and merged.replace(' ', '').upper() == area.replace(' ', '').upper()
    return {'ok': r['ok'], 'text': text} if same else _range_refused(key, area)

def _scalar_text_only_tail(r, cmd):
    """Только компактная форма хвоста (обычный разбор уже сделан вызывающим)."""
    return _tail_char(r['raw'], cmd) if r.get('ok') else None

def _area_value(raw, area, cmd):
    """Текст области: в ответе рядом с ним лежит ЭХО переданного адреса — снимаем только его
    (хвостовое), иначе теряется значение, совпадающее с адресом области. Если после снятия эха
    строк не осталось, значение может лежать компактной формой «один байт» (так приходят
    односимвольные значения ячеек)."""
    text = tc1c.decode_area_text(raw, area)
    if text is not None:
        return text
    vals = tc1c.clean_strings(raw, 1)
    if area and vals and vals[-1] == area:
        vals = vals[:-1]
    if vals:
        return vals[-1]
    return _tail_char(raw, cmd)

def _area_size(raw, values):
    """Размер: компактное значение e1..ea либо eb/ed/ef + 1/2/4 байта числа."""
    suffix = b'\x20\xa1\xa3' + tc1c.TR
    pos = tc1c._reply_status_offset(raw)
    if pos is not None:
        if not raw.startswith(b'\x81\x81\x81', pos) or not raw.endswith(suffix):
            return None
        pos += 3
        end = len(raw) - len(suffix)
        for tag, width in ((0xeb, 1), (0xed, 2), (0xef, 4)):
            if pos + 1 + width == end and raw[pos] == tag:
                return int.from_bytes(raw[pos + 1:end], 'little', signed=width == 4)
        if pos + 1 == end and 0xe1 <= raw[pos] <= 0xea:
            return raw[pos] - 0xe1
        return None
    ints = [v for k, v in values if k == 'int']
    return ints[-1] if ints else None

@_action('tc_doc')
def tc_get_doc_area_horizontal_size(key: str, handle: str) -> dict:
    """Get the horizontal size (max column number holding data) of a spreadsheet-document."""
    c = _need()
    r = c.send_cmd(G.GET_DOC_AREA_HORIZONTAL_SIZE, key, kind='read', middle=RS, handle=handle)
    size = _area_size(r['raw'], r['values']) if r.get('ok') else None
    return _doc_read(c, key, handle, {'ok': r['ok'], 'size': size}, not size)

# ===================== состояние / просмотр-статус ==========================
@_action('tc_field')
def tc_get_state_presentation(key: str, handle: str) -> dict:
    """Read a form field's state presentation. An unavailable value is explained in the answer;
    null does not confirm that the field has no state presentation."""
    c = _need()
    r = c.send_cmd(G.GET_STATE_PRESENTATION, key, kind='read', middle=RS, handle=handle)
    vals = _vals(r)
    return _unavailable_read({'ok': r['ok'], 'presentation': vals[-1] if vals else None}, 'presentation',
                             message='The state presentation could not be read; no conclusion about the field state is available.')

@_action('tc_field')
def tc_current_check(key: str, handle: str) -> dict:
    """Whether a form BUTTON is pressed, or shows a check mark next to it. Only buttons answer
    meaningfully: anything else — a checkbox field, a page, a table — always reports false, which
    means 'not applicable', not 'switched off'. Read a checkbox with tc_get_text ('Да'/'Нет') and
    the active page with tc_get_current_page."""
    c = _need()
    r = c.send_cmd(G.CURRENT_CHECK, key, kind='read', middle=RS, handle=handle)
    return {'ok': r['ok'], 'checked': _scalar(r, _bool_from_resp)}

@_action('tc_field')
def tc_current_mode_is_edit(key: str, handle: str) -> dict:
    """Whether a table row or spreadsheet-document field is currently in edit mode."""
    c = _need()
    cls = _key_class(key)
    if cls == 'Table':
        guid = G.CURRENT_MODE_IS_EDIT
    elif cls == 'EditField' and _kind_of(c, key) == 'SpreadsheetDocumentField':
        guid = G.CURRENT_MODE_IS_EDIT_FIELD
    else:
        return {'ok': False, 'target': key, 'code': 'unsupported_element_type',
                'error': 'Edit mode can be read for a table row or spreadsheet-document field.'}
    r = c.send_cmd(guid, key, kind='read', middle=RS, handle=handle)
    return {'ok': r['ok'], 'edit_mode': _scalar(r, _bool_from_resp)}

@_action('tc_form')
def tc_current_modified(key: str, handle: str) -> dict:
    """Whether a form has been modified."""
    c = _need()
    r = c.send_cmd(G.CURRENT_MODIFIED, key, kind='read', middle=RS, handle=handle)
    return {'ok': r['ok'], 'modified': _scalar(r, _bool_from_resp)}

@_action('tc_field')
def tc_get_view_status_item_texts(key: str, handle: str) -> dict:
    """Get the view-status item texts of a form-element addition → list of strings. Do not use it
    to tell whether a filter or search is active: with a list narrowed down to one row by search
    the platform still answers with an empty collection. Check the effect by reading the rows."""
    c = _need()
    r = c.send_cmd(G.GET_VIEW_STATUS_ITEM_TEXTS, key, kind='read', middle=RC, handle=handle)
    vals = _vals(r)
    return {'ok': r['ok'], 'items': vals}

@_action('tc_field')
def tc_click_view_status_item(key: str, index: int | str, handle: str) -> dict:
    """Click a view-status item by 0-based index (or by its text). Nothing here proves the item
    was there: the platform answers the same when the form has no view-status line at all, and
    reading the texts first does not settle it either — that read comes back empty even while a
    search or filter is active. Judge by the list itself: read the rows before and after."""
    c = _need()
    r = c.send_cmd(G.CLICK_VIEW_STATUS_ITEM, key, kind='action', middle=tc1c.mk_choice(index), handle=handle)
    return {'ok': r['ok'], 'target': key, 'index': index}

@_action('tc_field')
def tc_delete_view_status_item(key: str, index: int | str, handle: str) -> dict:
    """Delete a view-status item by 0-based index (or by its text) — this is how a filter or a
    search chip is dropped. Nothing here proves the item was there: the platform answers the same
    when the form has no view-status line, and reading the texts first does not settle it either.
    Judge by the list itself: read the rows before and after."""
    c = _need()
    r = c.send_cmd(G.DELETE_VIEW_STATUS_ITEM, key, kind='action', middle=tc1c.mk_choice(index), handle=handle)
    return {'ok': r['ok'], 'target': key, 'index': index}

# ============ ссылочное поле: выбор / открытие / очистка / создание =========
@_action('tc_field')
def tc_start_choosing(key: str, handle: str) -> dict:
    """Open a reference field's choice form. Handles focus and table-cell editing.
    For CalendarField, selects the current date like a double-click; use goto_date first.
    Returns opened and the active window. A calendar selection need not open another window."""
    c = _need()
    if not _guid_available(c, G.CURRENT_VISIBLE):
        # The native choice action predates the focus/edit-state inspection methods.
        ok = True
        for kind in ('action', 'commit'):
            ok = c.send_cmd(G.START_CHOOSING, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
        _state['window_key'] = None
        return {'ok': ok, 'target': key, 'opened': None,
                'message': 'Choice requested; this platform cannot verify the opened window.'}
    stage = 'check_target'
    try:
        before = _cell_window(c)
        _cell_ready(c, key, handle)
        calendar = _kind_of(c, key) == 'CalendarField'
        table = _table_owner(key)
        table_handle = _own_handle(c, table) if table else None
        if table and not table_handle:
            raise _CellEditFailure('the owning table could not be found')
        if table:
            _cell_ready(c, table, table_handle)
        stage = 'focus'
        _cell_step(tc_activate(key, handle), 'the field could not be activated')
        _cell_window(c, before)
        if table:
            current = tc_get_current_item(table, table_handle)
            if not current.get('ok') or not any(o.get('key') == key for o in current.get('item', [])):
                raise _CellEditFailure('the requested column did not become current')
            stage = 'begin_edit'
            mode = _cell_flag(c, G.CURRENT_MODE_IS_EDIT, table, table_handle)
            if mode is False:
                _cell_step(tc_change_row(table, table_handle), 'row editing could not be started')
                mode = _cell_flag(c, G.CURRENT_MODE_IS_EDIT, table, table_handle)
            _cell_window(c, before)
            if mode is not True:
                raise _CellEditFailure('the table did not enter edit mode')
        stage = 'open_choice'
        for kind in ('action', 'commit'):
            _cell_step(c.send_cmd(G.START_CHOOSING, key, kind=kind, middle=b'', handle=handle),
                       'the choice request was refused')
        _state['window_key'] = None
        window = _window(c)
        opened = bool(window.get('key') and window['key'] != before)
        if calendar:
            return {'ok': True, 'target': key, 'opened': opened, 'window': window}
        result = {'ok': opened, 'target': key, 'opened': opened, 'window': window}
        if not opened:
            result.update(code='choice_not_opened', error='No separate choice window opened. Inspect the field or its choice list.')
        return result
    except _CellEditFailure as exc:
        # Never cancel here: another cell or a newly added row may contain pending input.
        return {'ok': False, 'target': key, 'opened': False, 'stage': stage, 'error': str(exc)}

@_action('tc_field')
def tc_start_choosing_from_choice_list(key: str, handle: str) -> dict:
    """Start choosing from a field's choice list."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.START_CHOOSING_FROM_CHOICE_LIST, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    _state['window_key'] = None          # открылся список выбора — активное окно другое
    return {'ok': ok, 'target': key}

@_action('tc_field')
def tc_execute_choice_from_choice_list(key: str, value: str | int, handle: str) -> dict:
    """Pick from a field's choice list by its display text or by its 0-based index. Reports
    changed/value_before/value_after — the field's value read before and after, same as
    tc_field(action="choose_from_drop_list")."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.EXECUTE_CHOICE_FROM_CHOICE_LIST, key, kind=kind, middle=tc1c.mk_choice(value), handle=handle)['ok'] and ok
    _state['window_key'] = None          # список выбора закрылся — активное окно другое
    _resolved_text_input(c, key, ok)
    return {'ok': ok, 'target': key, 'value': value}

@_action('tc_field')
def tc_open_field(key: str, handle: str) -> dict:
    """Open a reference field's value (F4 / follow the link)."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.OPEN_FIELD, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    _state['window_key'] = None          # открывается новая форма — кэш окна недействителен
    return {'ok': ok, 'target': key}

@_action('tc_field')
def tc_clear(key: str, handle: str) -> dict:
    """Clear an input field's value."""
    c = _need()
    guid, middle = _input_text_command('', c, key)
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(guid, key, kind=kind, middle=middle, handle=handle)['ok'] and ok
    _resolved_text_input(c, key, ok)
    return {'ok': ok, 'target': key}

@_action('tc_field')
def tc_create(key: str, handle: str) -> dict:
    """Create a new object from a reference field: opens the new object's form, as the field's
    '+' does. The field must have the FOCUS first — call tc_activate on it, otherwise the command
    is accepted and nothing opens. Verify by reading the active window."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):    # без коммита платформа команду не выполняет
        ok = c.send_cmd(G.CREATE, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    _state['window_key'] = None          # открывается форма нового объекта
    return {'ok': ok, 'target': key}

@_action('tc_field')
def tc_cancel_edit(key: str, handle: str) -> dict:
    """Cancel editing an input field."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.CANCEL_EDIT, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    _resolved_text_input(c, key, ok)
    return {'ok': ok, 'target': key}


def _resolved_text_input(c, key, ok):
    # A successful native choice, clear or cancellation replaces the text attempt.
    # Do not release another field, or dismiss an intervening validation dialog.
    if not ok or getattr(c, '_pending_text_input', None) != key or not _guid_available(c, G.GET_ACTIVE_WINDOW):
        return
    track = getattr(c, '_track', None)
    try:
        c._track = None
        form = _batches.owner(sys.modules[__name__], key)
        if form and _window(c).get('key') == _collection_parent(form):
            c._pending_text_input = None
    finally:
        c._track = track

@_action('tc_field')
def tc_drop_list_is_open(key: str, handle: str) -> dict:
    """Whether a field's drop-down list is open."""
    c = _need()
    r = c.send_cmd(G.DROP_LIST_IS_OPEN, key, kind='read', middle=RS, handle=handle)
    return {'ok': r['ok'], 'open': _scalar(r, _bool_from_resp)}

@_action('tc_field')
def tc_close_drop_list(key: str, handle: str) -> dict:
    """Close a field's drop-down list. Verify with tc_field(action="drop_list_is_open")."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):    # без коммита список остаётся открытым
        ok = c.send_cmd(G.CLOSE_DROP_LIST, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key}

@_action('tc_field')
def tc_get_edit_text(key: str, handle: str) -> dict:
    """Read an input field's edit buffer — what is being typed, which is not necessarily what the
    form holds. get_data_presentation reads the accepted value; get_text reads displayed text.
    An empty input buffer returns ""; null means unavailable."""
    c = _need()
    r = c.send_cmd(G.GET_EDIT_TEXT, key, kind='read', middle=RS, handle=handle)
    return {'ok': r['ok'], 'text': _field_scalar_text(c, key, r, G.GET_EDIT_TEXT)}

# ============ календарь: назад ==============================================
@_action('tc_calendar')
def tc_calendar_previous_month(key: str, handle: str) -> dict:
    """Move a calendar field to the previous month. The command is accepted, but nothing readable about
    the field changes, so this cannot be verified. To move the date use tc_goto_date."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GOTO_PREVIOUS_MONTH, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'prev_month'}

@_action('tc_calendar')
def tc_calendar_previous_year(key: str, handle: str) -> dict:
    """Move a calendar field to the previous year. The command is accepted, but nothing readable about
    the field changes, so this cannot be verified. To move the date use tc_goto_date."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.GOTO_PREVIOUS_YEAR, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    return {'ok': ok, 'target': key, 'action': 'prev_year'}

# ============ таблица/группа: состояние и элементы ==========================
@_action('tc_table')
def tc_choose_row(key: str, handle: str) -> dict:
    """Choose (select / double-click) the current table row. In a choice form this picks the row and
    closes the form; in a list form the same action OPENS the row's item — on a hierarchical
    list, the folder own card rather than stepping into the folder. `changed` reports whether
    the ACTIVE WINDOW changed: true means a window opened or closed, which is what a completed
    choice looks like; false means the window stayed — the choice did not go through, or the row
    was picked without closing anything, so read the field you were filling to tell those apart."""
    c = _need()
    ok = True
    for kind in ('action', 'commit'):
        ok = c.send_cmd(G.CHOOSE_ROW, key, kind=kind, middle=b'', handle=handle)['ok'] and ok
    _state['window_key'] = None          # выбор строки списка открывает форму элемента
    return {'ok': ok, 'target': key, 'action': 'choose_row'}

@_action('tc_table')
def tc_can_be_expanded(key: str, handle: str, row_column: str = None, row_value=None) -> dict:
    """Whether a table row/group can be expanded. Pass row_column+row_value to target a
    specific row by a column name or title; omit them to use the current row.
    can_expand=true does not promise that tc_expand will work: rows reporting true have been
    observed to stay collapsed. The reliable evidence is tc_expand's own
    value_before/value_after pair."""
    c = _need()
    mid = tc1c.mk_tree_middle(b'\xe1\xcb\x55', row_column, row_value)
    r = c.send_cmd(G.CAN_BE_EXPANDED, key, kind='read', middle=mid, handle=handle,
                   pad=tc1c.tree_row_pad(row_column))
    return {'ok': r['ok'], 'can_expand': _scalar(r, tc1c.decode_expanded)}

@_action('tc_table')
def tc_is_expanded(key: str, handle: str, row_column: str = None, row_value=None) -> dict:
    """Whether a table row is expanded. Without row_column this reads the CURRENT row.
    Pass row_column (column name or title) and row_value to read another row without moving the cursor."""
    c = _need()
    mid = tc1c.mk_tree_middle(b'\xe1\xcb\x55', row_column, row_value)
    r = c.send_cmd(G.EXPANDED, key, kind='read', middle=mid, handle=handle,
                   pad=tc1c.tree_row_pad(row_column))
    if not r['ok'] and row_column is not None:
        # молчаливый expanded=null не отличить от «прочитать не удалось»: называем причину
        return {'ok': False, 'target': key, 'error':
                'row_column: the client refused the row criterion.'}
    return {'ok': r['ok'], 'expanded': _scalar(r, tc1c.decode_expanded)}

@_action('tc_table')
def tc_get_current_item(key: str, handle: str) -> dict:
    """Get the current item of a table → {key, handle}. key/handle: the table."""
    if _key_class(key) != 'Table':
        return {'ok': False, 'target': key, 'code': 'unsupported_element_type',
                'error': 'This action requires a table. To read a form\'s focused element use '
                         'tc_form(action="get_current_element") on the managed form.'}
    c = _need()
    r = c.send_cmd(G.GET_CURRENT_ITEM, key, kind='read', middle=RC, handle=handle)
    return {'ok': r['ok'], 'item': _coll(r, key)}

@_action('tc_table')
def tc_get_cell_text(key: str, column: str | int, handle: str) -> dict:
    """Read a cell in the current row. Text may include search-highlight markup.
    column is the column element NAME, or its 0-based index as a number or
    a string of digits ('0' is index 0, not a name — an element name cannot start with a digit).
    Unknown or ambiguous column names are refused; a matching title suggests the element name.
    Only displayed columns can be read. null does not establish that the cell is empty.
    A column inside a COLUMN GROUP that the table shows as one column may answer with
    the GROUP's text — a neighbouring column's value — and that answer cannot be told from a
    correct one; members of other kinds in the same group answer null instead."""
    c = _need()
    column = _as_index(column)          # '0' — это индекс, а не имя: имя не начинается с цифры
    resolved = []
    error = _column_error(c, key, column, resolved=resolved)
    if error:
        return error
    r = c.send_cmd(G.GET_CELL_TEXT, key, kind='read', middle=tc1c.mk_cell(column), handle=handle)
    decoded, text = tc1c.decode_cell_text(r['raw']) if r.get('ok') else (False, None)
    if not decoded:
        vals = _vals(r)
        if vals and vals[-1] == column:
            vals = vals[:-1]
        text = vals[-1] if vals else _scalar_text_only_tail(r, G.GET_CELL_TEXT)
    result = _unavailable_read({'ok': r['ok'], 'column': column, 'text': text}, 'text',
                               message='No displayed cell text is available. The column may be hidden or the cell empty; '
                                       'null alone does not distinguish these cases.')
    if text is None and r['ok']:
        if len(resolved) == 1:
            result['suggested_call'] = {'tool': 'tc_field', 'arguments': {
                'action': 'is_visible', 'key': resolved[0]['key'], 'handle': resolved[0]['handle']}}
        else:
            result['suggested_call'] = {'tool': 'tc_find', 'arguments': {
                'action': 'find_objects', 'root_key': key}}
    return result


def _column_error(c, key, column, *, resolved=None):
    """Validate a column before reading or moving the cursor."""
    if _key_class(key) != 'Table':
        return {'ok': False, 'column': column, 'code': 'invalid_table', 'error': 'Address the table containing the cell.'}
    if isinstance(column, str):
        # Preserve native name-based reads before full discovery is exposed.
        version = _vt(_conn_ver(c))
        if version and version < _vt(TOOL_MIN_VERSION['tc_get_child_objects']):
            return None
        cols = _table_columns(c, key)
        matches = [o for o in cols if o.get('name') == column]
        if resolved is not None:
            resolved.extend(matches)
        if len(matches) != 1:
            result = {'ok': False, 'column': column,
                      'code': 'ambiguous_column' if matches else 'column_not_found',
                      'error': 'Use a column element name returned by find_objects for this table.'}
            suggestions = [o['name'] for o in cols if o.get('title') == column and o.get('name')]
            if suggestions:
                result['suggested_columns'] = suggestions
            return result
    elif not isinstance(column, int) or isinstance(column, bool) or column < 0:
        return {'ok': False, 'column': column, 'code': 'invalid_column', 'error': 'Use a column element name or a non-negative integer index.'}
    return None


def _table_owner(key):
    parent = _collection_parent(key)
    while parent:
        if _key_class(parent) == 'Table':
            return parent
        parent = _collection_parent(parent)
    return None


def _table_columns(c, key):
    return [o for o in _walk_tree(c, key) if o.get('class') == 'EditField'
            and _table_owner(o.get('key', '')) == key]


def _row_criteria_error(c, key, pairs):
    """Validate titles without changing criteria or moving the row cursor."""
    if not pairs:
        return None
    if _key_class(key) != 'Table':
        return {'ok': False, 'code': 'invalid_table', 'error': 'Row criteria require a table.'}
    for column, value in pairs:
        if not isinstance(column, str) or not column or _is_index(column):
            return {'ok': False, 'code': 'invalid_column', 'error': _COLUMN_BY_TITLE}
    # Preserve native title-based search before full discovery is exposed.
    version = _vt(_conn_ver(c))
    if version and version < _vt(TOOL_MIN_VERSION['tc_get_child_objects']):
        return None
    track = getattr(c, '_track', None)
    try:
        c._track = None
        columns = _table_columns(c, key)
    except Exception:
        return {'ok': False, 'code': 'columns_unavailable',
                'error': 'The table columns could not be checked. Find the table again before searching.'}
    finally:
        c._track = track
    if not columns:
        return {'ok': False, 'code': 'columns_unavailable', 'error': 'No column titles are available for this table.'}
    for column, value in pairs:
        matches = [o for o in columns if o.get('title') == column]
        if len(matches) != 1:
            named = [o for o in columns if o.get('name') == column]
            # 1C can omit an inherited title, e.g. the row-number title N.
            # Missing metadata is not proof that a displayed title is invalid.
            if not matches and not any(o.get('title') for o in named) and any(
                    not o.get('title') for o in columns):
                continue
            result = {'ok': False, 'column': column,
                      'code': 'ambiguous_column' if matches else 'column_not_found',
                      'error': 'Row search requires a unique column title returned by find_objects for this table.'}
            suggestions = list(dict.fromkeys(o['title'] for o in columns
                               if o.get('name') == column and o.get('title')))
            if suggestions:
                result['suggested_columns'] = suggestions
            return result
    return None


def _row_search_refusal(pairs):
    return {'ok': False, 'code': 'row_criteria_rejected', 'criteria': dict(pairs),
            'error': 'The table rejected the search criteria. Use displayed column titles and values.',
            'message': 'Column titles can also be read from the keys returned by read_rows.'}

@_action('tc_field')
def tc_current_opened(key: str, handle: str) -> dict:
    """Whether a form group is currently open."""
    c = _need()
    r = c.send_cmd(G.CURRENT_OPENED, key, kind='read', middle=RS, handle=handle)
    return {'ok': r['ok'], 'opened': _scalar(r, _bool_from_resp)}

@_action('tc_field')
def tc_title_is_shown(key: str, handle: str) -> dict:
    """Whether an element's title is shown."""
    c = _need()
    r = c.send_cmd(G.TITLE_IS_SHOWN, key, kind='read818', middle=tc1c.RES_E2, handle=handle)
    return {'ok': r['ok'], 'title_shown': _scalar(r, _bool_from_resp)}

@_action('tc_table')
def tc_goto_row(key: str, column: str | int = None, value=None, handle: str = None,
                direction: str = 'down', toggle_selection: bool = False, fields: dict = None) -> dict:
    """Go to the table row where column equals value (int or string; the wildcards * and ? work).
    column is the column TITLE; an index is not accepted here. Pass fields ({column: value}) to
    match several columns at once; do not combine fields with column/value. Seeks directly,
    so there is no need to walk rows. Matching is
    CASE-SENSITIVE and compares the value as SHOWN ("Встреча агента (Совещание)"), so a wildcard
    is often what you want. The search starts at the CURRENT row, runs in direction (down by
    default, or up) to the end of the list and does NOT wrap; the current row is itself a
    candidate, so searching for the value the cursor already sits on reports found=true without
    moving — step off the row first to find the NEXT match. A search that finds nothing still
    MOVES the cursor, to the last row going down, the first going up. Set toggle_selection to
    toggle the row it lands on; with no column and no fields it just toggles the current row.
    found is null when there was nothing to search for or the search result could not be determined — null
    never means "not found". In the answer `criteria` repeats the TITLE you searched by and
    `observed.column` gives that same column NAME."""
    c = _need()
    # тип общий на всю группу, поэтому число доходит и сюда; платформа ищет по ЗАГОЛОВКУ и
    # индекса в критерии не принимает — отказываем внятно, а не питоновской ошибкой
    if _is_index(column):
        return {'ok': False, 'target': key, 'error': _COLUMN_BY_TITLE}
    if fields is not None and (column is not None or value is not None):
        return {'ok': False, 'target': key, 'code': 'conflicting_row_criteria',
                'error': 'Use either fields or column/value. Put all search conditions in fields.'}
    pairs = list(fields.items()) if fields else ([(column, value)] if column is not None else [])
    error = _row_criteria_error(c, key, pairs)
    if error:
        return dict(error, target=key)
    mid = tc1c.mk_gotorow(fields=pairs, toggle_selection=toggle_selection, direction=direction)
    try:
        r = c.send_cmd(G.GOTO_ROW, key, kind='read', middle=mid,
                       pad=(tc1c.GOTOROW_PAD if pairs else None), handle=handle)
    except tc1c.OperationError as exc:
        if exc.status == 13 and pairs:
            return dict(_row_search_refusal(pairs), target=key)
        raise
    # без условий поиска метод переключает выделение текущей строки и результата поиска не имеет
    return {'ok': r['ok'], 'target': key, 'criteria': dict(pairs),
            'found': _scalar(r, tc1c.decode_goto_row) if pairs else None}

_WRITE_KINDS = frozenset(('HTMLDocumentField', 'FormattedDocumentField',
                          'SpreadsheetDocumentField', 'TextDocumentField'))
# Проверены по содержимому файлов на 8.3.27 и 8.5.1, не по расширению имени.
_SHEET_FORMATS = {'mxl': 0, 'html': 1, 'pdf': 2, 'xls': 6, 'xlsx': 7, 'ods': 8, 'docx': 9}


@_action('tc_doc')
def tc_write_content_to_file(key: str, handle: str, filename: str = None,
                            file_format: str = None, filter_index: int = None,
                            save_as: bool = None) -> dict:
    """Save an HTML, formatted, spreadsheet or text document field. PDF fields do not support
    this action. With filename, Save As is used even for a previously saved document; this call
    replaces pending file-dialog answers and clears its answer afterwards on 8.3.25+.
    Older platforms cannot clear unused answers; prepare only the next dialog.
    For spreadsheets choose file_format: mxl, html, pdf, xls, xlsx, ods or docx. Example:
    filename="C:/exports/report.xlsx", file_format="xlsx". The extension alone does not select
    a format. Alternatively filter_index selects a dialog's file type (0-based); do not combine
    it with file_format. Without either option the dialog's first file type is selected.
    Without filename, saves under the current name unless save_as=true. If a dialog can appear,
    prepare it before EACH call using tc_app(action="set_file_dialog_result").
    ok confirms that the save request was accepted, not that a file has finished writing."""
    c = _need()
    kind = _kind_of(c, key)
    if kind not in _WRITE_KINDS:
        return {'ok': False, 'target': key, 'applicable': False if kind else None,
                'kind': kind, 'error': 'saving is not supported for this element' if kind else
                'the document field could not be identified; find the element again'}
    if filename == '':
        return {'ok': False, 'error': 'filename must not be empty'}
    if filename and save_as is False:
        return {'ok': False, 'error': 'filename requires Save As; omit save_as or set it to true'}
    if (file_format is not None or filter_index is not None) and not filename:
        return {'ok': False, 'error': 'filename is required when choosing a file format'}
    if file_format is not None:
        if filter_index is not None:
            return {'ok': False, 'error': 'choose file_format or filter_index, not both'}
        if kind != 'SpreadsheetDocumentField' or file_format.lower() not in _SHEET_FORMATS:
            return {'ok': False, 'error': 'file_format is supported for spreadsheets: ' + ', '.join(_SHEET_FORMATS)}
        file_format = file_format.lower()
        filter_index = _SHEET_FORMATS[file_format]
    if filter_index is not None and (type(filter_index) is not int or not 0 <= filter_index <= 126):
        return {'ok': False, 'error': 'filter_index must be an integer from 0 to 126'}
    index = filter_index if filter_index is not None else 0
    if filename and kind == 'SpreadsheetDocumentField':
        ext = os.path.splitext(filename)[1].lstrip('.').lower()
        expected = _SHEET_FORMATS.get(ext)
        if expected is not None and expected != index:
            return {'ok': False, 'error': 'the filename extension does not match the selected format; '
                    'use file_format="%s" or change the filename' % ext}
    use_save_as = bool(filename) if save_as is None else save_as
    can_clear = _guid_available(c, G.CLEAR_FILE_DIALOG_RESULT)
    out = {'ok': False, 'target': key, 'filename': filename, 'save_as': use_save_as}
    if filename:
        out['filter_index'] = index
        if file_format: out['file_format'] = file_format
        if can_clear:
            cleared = c.send_cmd(G.CLEAR_FILE_DIALOG_RESULT, None, kind='commit', middle=b'')
            if not cleared['ok']:
                return dict(out, error='previous file-dialog answers could not be cleared; save was not sent')
        else:
            out.update(dialog_answer_cleared=False,
                       message='This platform cannot clear pending file-dialog answers; prepare only the next dialog.')
    try:
        if filename:
            pre = c.send_cmd(G.SET_FILE_DIALOG_RESULT, None, kind='commit',
                             middle=tc1c.mk_set_file_dialog_result(True, filename, index), pad=4)
            if not pre['ok']:
                out['error'] = 'the file dialog answer was not accepted; save was not sent'
                return out
        ok = True
        for call_kind in ('action', 'commit'):
            ok = c.send_cmd(G.WRITE_CONTENT_TO_FILE, key, kind=call_kind,
                            middle=b'\xe2' if use_save_as else RS, handle=handle)['ok'] and ok
        out['ok'] = ok
        return out
    finally:
        if filename and can_clear:
            cleaned = c.send_cmd(G.CLEAR_FILE_DIALOG_RESULT, None, kind='commit', middle=b'')
            out['dialog_answer_cleared'] = cleaned['ok']
            if not cleaned['ok']:
                out.update(ok=False, error='the pending file-dialog answer could not be cleared')

@_action('tc_field')
def tc_wait_for_drop_list_generation(key: str, handle: str, timeout: int = 60) -> dict:
    """Wait up to timeout seconds for a drop-down list to be generated.
    Returns generated=True if a list appeared within the timeout, else False. The answer is not
    tied to the element you addressed — it can come back true before this field's list is open at
    all, the same way get_choice_list describes. Open the list on the field you care about first
    (tc_field(action="open_drop_list")) and read it right after."""
    c = _need()
    # ответ придёт только по окончании ожидания на клиенте -> ждём его дольше самого ожидания
    r = c.send_cmd(G.WAIT_FOR_DROP_LIST_GENERATION, key, kind='wait', middle=tc1c.mk_wait(timeout),
                   handle=handle, timeout=max(int(timeout), 0) + tc1c.TestClient.RECV_TIMEOUT)
    return {'ok': r['ok'], 'target': key, 'timeout': timeout, 'generated': _scalar(r, tc1c.decode_bool)}

@_action('tc_doc')
def tc_get_doc_area_vertical_size(key: str, handle: str) -> dict:
    """Get the vertical size (max row number holding data) of a spreadsheet-document."""
    c = _need()
    r = c.send_cmd(G.GET_DOC_AREA_VERTICAL_SIZE, key, kind='read', middle=RS, handle=handle)
    size = _area_size(r['raw'], r['values']) if r.get('ok') else None
    return _doc_read(c, key, handle, {'ok': r['ok'], 'size': size}, not size)

@_action('tc_field')
def tc_get_data_presentation(key: str, handle: str) -> dict:
    """Get an element's data presentation. Form fields only — not form decorations, and on a table
    the answer is always null. An empty input field returns "". presentation is null when no
    value was available; that is not proof
    that the field has no presentation."""
    c = _need()
    # ПолучитьПредставлениеДанных — метод ПОЛЯ формы. На декорации тест-клиент 1С не
    # возвращает ошибку, а аварийно завершается, унося сессию пользователя, — не шлём кадр.
    if _key_class(key) == 'Decoration':
        return {'ok': False, 'target': key,
                'error': 'a form-field method; not available for a form decoration'}
    r = c.send_cmd(G.GET_PROPERTY, key, kind='read', middle=RC, handle=handle)
    return _unavailable_read({'ok': r['ok'], 'presentation': _field_scalar_text(c, key, r, G.GET_PROPERTY)}, 'presentation')

@_action('tc_field')
def tc_get_linked_window(key: str, handle: str) -> dict:
    """Get the linked window of a command-interface button. An empty answer means the button has
    no linked window — but only when `target_check` says the button itself is there; a wrong key
    answers empty too, and the check is what tells the two apart."""
    c = _need()
    try:
        r = c.send_cmd(G.GET_LINKED_WINDOW, key, kind='read', middle=RC, handle=handle)
    except tc1c.OperationError as exc:
        # The native method also reports ObjectNotFound when the button exists
        # but its linked window does not. Do not blame a valid button reference.
        if exc.status == 7 and _key_class(key) == 'CIButton' and _ref_live_object(c, key) is not None:
            return {'ok': True, 'target': key, 'target_check': 'present', 'window': []}
        raise
    win = _coll(r, key)
    if win:
        return {'ok': r['ok'], 'target': key, 'window': win}
    # пусто — и только теперь спрашиваем, есть ли сама кнопка: у несуществующего ключа ответ
    # выглядит так же, а на содержательном ответе лишнего кадра нет (политика ленивой диагностики)
    state, _vis = _verify_target(c, key, handle)
    if state == 'absent':
        return _absent_error(key)
    return {'ok': r['ok'], 'target': key, 'target_check': state, 'window': win}

# =========================== прочие / сессия ================================
@_action('tc_app')
def tc_set_max_action_time(seconds: int) -> dict:
    """Set the max action-execution time in seconds: how long a result-returning action may take
    before the call gives up (0 = wait indefinitely). Stored on the client (no network call) and
    applied to every subsequent command."""
    c = _need()
    c._max_action_time = seconds
    return {'ok': True, 'max_action_time': seconds,
            'effective_timeout': c._recv_timeout(), 'note': 'local, no network frame'}

@_action('tc_app')
def tc_set_file_dialog_result(result: bool = True, filename: str | list = None,
                              filter_index: int = 0) -> dict:
    """Predefine the next file dialog's result: result=True + filename to simulate picking a
    file, result=False to cancel. Pass a list of names to simulate a multi-file selection.
    filter_index selects which dialog filter is active (0-based).
    On 8.3.25+, replaces pending answers; clear_file_dialog_result cancels an unused answer.
    Older platforms cannot clear unused answers; prepare only the next dialog.
    Each answer is consumed once. Call BEFORE opening the dialog;
    this cannot answer a file dialog that is already open."""
    c = _need()
    middle = tc1c.mk_set_file_dialog_result(result, filename, filter_index)
    can_clear = _guid_available(c, G.CLEAR_FILE_DIALOG_RESULT)
    try:
        # Setting the next answer can succeed even while an already-open native dialog
        # blocks normal commands. Check responsiveness before changing the pending answer.
        ready = c.send_cmd(G.GET_ACTIVE_WINDOW, None, kind='read', middle=RC)
        if not ready.get('ok'):
            return {'ok': False, 'prepared': False, 'code': 'client_state_unavailable',
                    'error': 'The client state could not be checked; no file selection was prepared.'}
        cleared = (c.send_cmd(G.CLEAR_FILE_DIALOG_RESULT, None, kind='commit', middle=b'')
                   if can_clear else {'ok': True})
    except tc1c.OperationError as exc:
        return dict(exc.result(), prepared=False)
    if not cleared.get('ok'):
        return {'ok': False, 'prepared': False, 'code': 'file_dialog_reset_failed',
                'error': 'The previous file selection could not be cleared; no new selection was prepared.'}
    try:
        r = c.send_cmd(G.SET_FILE_DIALOG_RESULT, None, kind='commit',
                       middle=middle, pad=4)
    except tc1c.OperationError as exc:
        return dict(exc.result(), prepared=False)
    return {'ok': r['ok'], 'result': result, 'filename': filename if result else None,
            'filter_index': filter_index, 'replaces_pending': can_clear,
            'prepared': bool(r['ok']), 'applies_to': 'next_dialog'}

@_action('tc_app')
def tc_clear_file_dialog_result() -> dict:
    """Clear a previously set file-dialog result."""
    c = _need()
    r = c.send_cmd(G.CLEAR_FILE_DIALOG_RESULT, None, kind='commit', middle=b'')
    return {'ok': r['ok']}

@_action('tc_app')
def tc_get_max_action_time() -> dict:
    """Get the max action-execution time in seconds (set via tc_set_max_action_time; None if unset)."""
    c = _need()
    return {'ok': True, 'max_action_time': c._max_action_time}

# ================= СИНТЕЗ uilog из вызовов инструментов (режим 'synth') =================
# Собираем сценарий сами из отправленных команд — не полагаясь на клиентский рекордер.
# Это покрывает и то, что платформа НЕ пишет в нативный журнал (setOrder и пр.).
def _synth_dec_str(b):
    """Декод строки, закодированной _enc_like (ta ASCII / t7,t8 UTF-16)."""
    if not b:
        return ''
    t = b[0] & 0x0f
    try:
        if t == 0x0a: n = b[1]; return b[2:2+n].decode('latin1', 'replace')
        if t == 0x0b: n = int.from_bytes(b[1:3], 'little'); return b[3:3+n].decode('latin1', 'replace')
        if t == 0x07: n = b[1]; return b[2:2+n*2].decode('utf-16le', 'replace')
        if t == 0x08: n = int.from_bytes(b[1:3], 'little'); return b[3:3+n*2].decode('utf-16le', 'replace')
    except Exception:
        return ''
    return ''

def _synth_dec_int(b):
    if not b: return None
    if b[0] == 0x8b: return b[1]
    if b[0] == 0x8d: return int.from_bytes(b[1:3], 'little')
    if b[0] == 0x8f: return int.from_bytes(b[1:5], 'little', signed=True)
    return None

def _synth_step(guid, key, middle):
    """(guid, key, middle) -> (tag, attrs, fields) для uilog, либо None (не действие сценария)."""
    m = middle or b''
    def after(pfx): return m[len(pfx):] if m.startswith(pfx) else m
    simple = {G.SET_CHECK: 'setCheck', G.CLICK: 'click', G.CLICK_FIELD: 'click',
              G.CLICK_DECORATION: 'click', G.CLICK_CI: 'click', G.CLEAR: 'clear',
              G.INCREASE_VALUE: 'increaseValue', G.DECREASE_VALUE: 'decreaseValue',
              G.OPEN_DROP_LIST: 'openDropList', G.START_CHOOSING: 'startChoosing',
              G.CHOOSE_ROW: 'choose', G.ADD_ROW: 'addRow', G.DELETE_ROW: 'deleteRow',
              G.COPY_ROW: 'copyRow', G.ACTIVATE: 'activate',
              G.BEGIN_EDIT_CURRENT_AREA: 'beginEditingCurrentArea',
              G.SELECT_ALL_ROWS: 'selectAllRows', G.DESELECT_ALL_ROWS: 'deselectAllRows',
              G.GOTO_NEXT_MONTH: 'gotoNextMonth', G.GOTO_PREVIOUS_MONTH: 'gotoPreviousMonth',
              G.GOTO_NEXT_YEAR: 'gotoNextYear', G.GOTO_PREVIOUS_YEAR: 'gotoPreviousYear',
              G.GOTO_NEXT_ITEM: 'gotoNextItem', G.GOTO_PREVIOUS_ITEM: 'gotoPreviousItem',
              G.FORM_GOTO_NEXT_ITEM: 'gotoNextItem', G.FORM_GOTO_PREVIOUS_ITEM: 'gotoPreviousItem',
              G.CHANGE_ROW: 'changeRow', G.SWITCH_ROW_DELETE_MARK: 'switchRowDeleteMark',
              G.SELECT_ROW: 'selectRow', G.DESELECT_ROW: 'deselectRow',
              G.CREATE: 'create', G.CANCEL_EDIT: 'cancelEdit', G.OPEN_FIELD: 'openField',
              G.CLOSE_DROP_LIST: 'closeDropList', G.WRITE_CONTENT_TO_FILE: 'writeContentToFile',
              G.START_CHOOSING_FROM_CHOICE_LIST: 'startChoosingFromChoiceList'}
    if guid == G.GOTO_NEXT_ITEM:  # == GOTO_PREVIOUS_ITEM (общий GUID): различаем по middle (RES_E2 vs RS)
        return ('gotoNextItem' if m[:1] == b'\xe2' else 'gotoPreviousItem', {}, None)
    rownav = {G.GOTO_FIRST_ROW: 'gotoFirstRow', G.GOTO_NEXT_ROW: 'gotoNextRow',
              G.GOTO_PREVIOUS_ROW: 'gotoPreviousRow', G.GOTO_LAST_ROW: 'gotoLastRow'}
    if guid in rownav:            # middle e2 = переход С ПЕРЕКЛЮЧЕНИЕМ выделения строки (e1 = без)
        return (rownav[guid], ({'toggleSelection': 'true'} if m[:1] == b'\xe2' else {}), None)
    if guid == G.WRITE_CONTENT_TO_FILE:
        return ('writeContentToFile', {'saveAs': 'true'} if m[:1] == b'\xe2' else {}, None)
    if guid in simple: return (simple[guid], {}, None)
    def choice_attr():
        """Аргумент «Строка, Число» -> атрибут тега: e0 4b 4e + (0x81+idx) -> index;
        e0 4b 53 + строка -> presentation (устаревшая форма e0 4b 53 + int -> тоже index)."""
        if m[:3] == b'\xe0\x4b\x4e' and len(m) >= 4:
            # индексы 0..126 — один байт 0x81+idx (len(m)==4); с 127 — явный int 8b/8d/8f
            iv = _synth_dec_int(m[3:]) if len(m) > 4 else None
            return {'index': str(iv if iv is not None else m[3] - 0x81)}
        rest = after(b'\xe0\x4b\x53')
        if rest[:1] in (b'\x8b', b'\x8d', b'\x8f'):
            return {'index': str(_synth_dec_int(rest))}
        return {'presentation': _synth_dec_str(rest)}
    if guid == G.SELECT_OPTION: return ('selectOption', choice_attr(), None)
    if guid == G.EXECUTE_CHOICE_FROM_CHOICE_LIST:
        # тег общий с ВыполнитьВыборИзВыпадающегоСписка (так пишет НАТИВНЫЙ рекордер);
        # method= отмечает, что записан именно ВыполнитьВыборИзСпискаВыбора (другой GUID)
        a = choice_attr(); a['method'] = 'ExecuteChoiceFromChoiceList'
        return ('executeChoiceFromChoiceList', a, None)
    if guid == G.EXECUTE_CHOICE_FROM_LIST: return ('executeChoiceFromList', choice_attr(), None)
    if guid == G.EXECUTE_CHOICE_FROM_MENU: return ('executeChoiceFromMenu', choice_attr(), None)
    if guid == G.CLICK_HTML_DOC_HYPERLINK: return ('clickHTMLHyperlink', choice_attr(), None)
    if guid == G.CLICK_FORMATTED_DOC_HYPERLINK: return ('clickFormattedDocHyperlink', choice_attr(), None)
    if guid == G.CLICK_FORMATTED_STRING_HYPERLINK: return ('clickFormattedStringHyperlink', choice_attr(), None)
    if guid == G.CLICK_VIEW_STATUS_ITEM: return ('clickViewStatusItem', choice_attr(), None)
    if guid == G.DELETE_VIEW_STATUS_ITEM: return ('deleteViewStatusItem', choice_attr(), None)
    if guid in (G.EXPAND_TABLE, G.EXPAND_GROUP):
        # база e2cb55 = развернуть ВМЕСТЕ С подчинёнными строками (e1cb55 — только узел)
        sub = {'subordinates': 'true'} if (guid == G.EXPAND_TABLE and m[:1] == b'\xe2') else {}
        return ('expand', sub, _synth_rowdesc(m))
    if guid in (G.COLLAPSE_TABLE, G.COLLAPSE_GROUP): return ('collapse', {}, _synth_rowdesc(m))
    if guid == G.EXECUTE_COMMAND: return ('executeCommand', {'command': _synth_dec_str(m)}, None)
    if guid == G.INPUT_TEXT: return ('inputText', {'text': _synth_dec_str(after(b'\xe0\x41\x81\x81'))}, None)
    if guid == G.INPUT_HTML:
        a = {'HTML': _synth_dec_str(m)}
        for nm, data in tc1c.dec_attachments(m).items():
            a['attachment.' + nm] = base64.b64encode(data).decode('ascii')
        return ('inputHTML', a, None)
    if guid == G.SET_ORDER: return ('setOrder', {'columnTitle': _synth_dec_str(m)}, None)
    if guid == G.SET_CURRENT_AREA: return ('setCurrentArea', {'area': _synth_dec_str(m)}, None)
    if guid == G.END_EDIT_CURRENT_AREA:
        return ('finishEditingCurrentArea', {'cancel': 'true' if m[:1] == CANCEL_EDIT_MID else 'false'}, None)
    if guid == G.END_EDIT_ROW: return ('endEditRow', {'cancel': 'true' if m[:1] == b'\xe2' else 'false'}, None)
    if guid == G.CHOOSE_FROM_DROP_LIST:   # нативный рекордер пишет его тем же тегом (без method=)
        return ('executeChoiceFromChoiceList', choice_attr(), None)
    if guid == G.GOTO_VALUE:
        return ('gotoValue', {'presentation': str(_synth_dec_int(after(b'\xe0\x4b\x4e')))}, None)
    if guid == G.GOTO_DATE:
        if m[:1] == b'\xf1':
            try: return ('gotoDate', {'date': tc1c.dec_date(m[1:9]).strftime('%Y-%m-%dT%H:%M:%S')}, None)
            except Exception: pass
        return ('gotoDate', {}, None)
    if guid == G.GOTO_ROW:
        attrs = {}
        if len(m) >= 4:
            attrs['direction'] = 'up' if m[3] == 0x81 else 'down'
            if m[1] == 0x82: attrs['switchSelection'] = 'true'
        return ('gotoRow', attrs, _synth_rowdesc(m))
    if guid == G.GO_ONE_LEVEL_DOWN: return ('goOneLevelDown', {}, _synth_rowdesc(m))
    if guid == G.GO_ONE_LEVEL_UP:   return ('goOneLevelUp', {}, _synth_rowdesc(m))
    return None

def _synth_rowdesc(m):
    """Извлечь ВСЕ пары [(колонка, значение)] из middle с ОписаниеСтроки. Пара:
    <c0 | 20 e0> 4b 53 + колонка + eb 53 + значение. None, если описания строки нет."""
    header = b'\x23\x95' + tc1c._GOTOROW_MAPTYPE
    p = m.find(header)
    if p < 0 or p + len(header) >= len(m):
        return None
    i = p + len(header)
    count = m[i] - 0xc1
    i += 1
    out = []
    for index in range(count):
        marker = b'\xc0\x4b\x53' if index == 0 else b'\x20\xe0\x4b\x53'
        if not m.startswith(marker, i):
            return None
        i += len(marker)
        size, col = tc1c._text_at(m, i, compact=False)
        if not size:
            return None
        i += size
        if not m.startswith(b'\xeb\x53', i):
            return None
        i += 2
        if m[i:i+1] in (b'\x8b', b'\x8d', b'\x8f'):
            size = tc1c.value_size(m, i)
            if i + size > len(m):
                return None
            val = str(_synth_dec_int(m[i:i+size]))
        else:
            size, val = tc1c._text_at(m, i, compact=False)
            if not size:
                return None
        out.append((col, val))
        i += size
    return out or None

# Обратная карта GUID->имя и классификация «мутатор» (для контроля покрытия synth).
_GUID2NAME = {v: k for k, v in vars(G).items() if isinstance(v, str) and len(v) == 36}
_SYNTH_READONLY = {'CAN_BE_EXPANDED', 'EXPANDED', 'DROP_LIST_IS_OPEN', 'FIND_DEFAULT_BUTTON',
                   'TITLE_IS_SHOWN', 'TEXT_WITHIN_AREA_BOUNDS', 'INCLUDED_IN_MERGED_AREA',
                   'WAIT_FOR_DROP_LIST_GENERATION', 'UILOG', 'DISCONNECT', 'GET_PROPERTY',
                   'FORM_GET_CURRENT_ITEM',
                   }
def _synth_is_mutator(guid):
    n = _GUID2NAME.get(guid, '')
    if n.startswith('GET_') or n.startswith('CURRENT_'): return False
    return n not in _SYNTH_READONLY

def _synth_unhandled(tracked):
    """Имена вызванных мутаторов, для которых нет обработчика synth (чтобы не терялись молча)."""
    out = []
    for guid, key, mid, kind in tracked:
        if kind == 'commit' or guid in _SYNTH_WINLEVEL or guid in _SYNTH_COMMIT_ONLY:
            continue      # коммиты, оконные и сессионные команды пишутся отдельными ветками
        if _synth_is_mutator(guid) and _synth_step(guid, key, mid) is None:
            name = _GUID2NAME.get(guid, guid)
            if name not in out:
                out.append(name)
    return out

def _synth_form_id(key):
    m = re.search(r'(?:ManagedForm|UnmanagedForm)\[[^\]]+\]', key or '')
    return m.group(0) if m else (key or '')

def _synth_elem(key):
    ms = re.findall(r'\[([^\[\]]+)\]', key or '')
    return ms[-1] if ms else None

# Оконно-навигационные операции (app-level): пишутся как child <ClientApplicationWindow>, не внутри элемента.
_WINDOW_KEY_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]*\[[0-9a-fA-F-]{36}\]$')   # MainFrame[guid]

_SYNTH_WINLEVEL = {G.CLOSE: 'close', G.GOTO_NEXT_WINDOW: 'gotoNextWindow',
                   G.GOTO_PREVIOUS_WINDOW: 'gotoPreviousWindow', G.GOTO_START_PAGE: 'gotoStartPage',
                   G.CLOSE_USER_MESSAGES_PANEL: 'closeUserMessagesPanel'}

# Сессионные команды: ОДИН кадр kind='commit' (не вторая половина пары action+commit) и
# nil-получатель. Пишутся как child <ClientApplicationWindow> — иначе теряются целиком,
# а сценарий «задать результат файлового диалога -> сохранить в файл» невоспроизводим.
_SYNTH_COMMIT_ONLY = {G.SET_FILE_DIALOG_RESULT: 'setFileDialogResult',
                      G.CLEAR_FILE_DIALOG_RESULT: 'clearFileDialogResult'}

def _synth_fdr(m):
    """setFileDialogResult из middle (см. mk_set_file_dialog_result) -> (атрибуты, имена файлов).
    Истина -> e2 <0x81+ИндексФильтра> <0x81+ЧислоИмён> <имя1 d_> <имя2..N 9_>; Ложь -> e1 81 82 c1."""
    if m[:1] != b'\xe2':
        return {'result': 'false'}, []
    a = {'result': 'true', 'filterIndex': str(m[1] - 0x81) if len(m) > 1 else '0'}
    count = (m[2] - 0x81) if len(m) > 2 else 1
    names = []; i = 3
    while len(names) < count and i < len(m):
        nm = _synth_dec_str(m[i:])
        step = tc1c.value_size(m, i)      # длина имени в кадре зависит от тега (ASCII/UTF-16)
        if not nm or not step:
            break
        names.append(nm)
        i += step
    return a, names

# Действия, после которых платформа задаёт модальный вопрос Да/Нет (на него отвечает
# _answer_confirm_dialog). Ответ пользователя сохраняем атрибутом confirm у САМОГО действия.
_CONFIRM_ACTIONS = {G.DELETE_ROW, G.COPY_ROW, G.SWITCH_ROW_DELETE_MARK}
_DLG_BUTTON_RE = re.compile(r'\.Group\[Buttons\]\.Button\[Button(\d)\]')

def _synth_confirms(tracked):
    """Клик по кнопке модального «Да/Нет» -> ответ на предыдущее действие.
    Возврат ({индекс действия: 'true'/'false'/'none'}, {индексы кликов-ответов}): сам клик
    отдельным шагом не пишется, иначе replay нажимал бы кнопку уже закрытого диалога.
    Ответа не было (confirm=None — быстрый путь без обработки диалога) -> пишем 'none', иначе
    воспроизведение подтвердит операцию, которую запись не подтверждала."""
    conf = {}; drop = set(); pending = None
    for i, (guid, key, mid, kind) in enumerate(tracked):
        if kind == 'commit':
            continue
        m = _DLG_BUTTON_RE.search(key or '') if guid == G.CLICK else None
        if m and pending is not None:
            conf[pending] = 'true' if m.group(1) == '0' else 'false'   # Button0=Да, Button1=Нет
            drop.add(i); pending = None
            continue
        if _synth_step(guid, key, mid) is not None:      # чтения (обход дерева) шаг не сбрасывают
            if guid in _CONFIRM_ACTIONS:
                pending = i; conf[i] = 'none'            # перепишется, если ответ последует
            else:
                pending = None
    return conf, drop

def _synth_uilog(tracked):
    """Построить uilog XML из списка (guid, key, middle) отправленных команд."""
    from xml.sax.saxutils import quoteattr
    parsed = []
    confirms, dlg_clicks = _synth_confirms(tracked)
    for i, (guid, key, mid, kind) in enumerate(tracked):
        if (kind == 'commit' and guid not in _SYNTH_COMMIT_ONLY) or i in dlg_clicks:
            continue            # второй кадр пары action+commit / ответ на модальный вопрос
        if guid in _SYNTH_COMMIT_ONLY:
            attrs, files = _synth_fdr(mid or b'') if guid == G.SET_FILE_DIALOG_RESULT else ({}, [])
            parsed.append({'winnav': _SYNTH_COMMIT_ONLY[guid], 'attrs': attrs,
                           'children': [('File', {'name': f}) for f in files]}); continue
        if guid in _SYNTH_WINLEVEL:
            parsed.append({'winnav': _SYNTH_WINLEVEL[guid],
                           'attrs': {'native': 'true'} if guid == G.CLOSE and mid == b'\xe2' else {}}); continue
        if guid == G.ACTIVATE and _WINDOW_KEY_RE.match(key or ''):
            # активизация ОКНА: элемента с именем-UUID окна на форме нет, шаг оконный
            parsed.append({'winnav': 'activateWindow', 'attrs': {}}); continue
        if guid == G.EXECUTE_COMMAND:
            parsed.append({'wincmd': _synth_dec_str(mid)}); continue
        st = _synth_step(guid, key, mid)
        if not st: continue
        tag, attrs, fields = st
        if i in confirms:
            attrs = dict(attrs); attrs['confirm'] = confirms[i]
        parsed.append({'form': _synth_form_id(key), 'elem': _synth_elem(key),
                       'ci_path': key.split('.CI.', 1)[1] if _key_class(key) == 'CIButton' and '.CI.' in key else None,
                       # таблица — это САМА цель, а не любой предок: строка поиска живёт по адресу
                       # Table[Список].Additional[…] и таблицей не является
                       'table': bool(re.search(r'\.Table\[[^\]]+\]$', key or '')),
                       'tag': tag, 'attrs': attrs, 'fields': fields,
                       # получатель — сама форма: элемента с таким именем (UUID формы) не существует
                       'formlevel': bool(re.search(r'(?:ManagedForm|UnmanagedForm)\[[^\]]+\]$', key or ''))})
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<uilog xmlns:d1p1="http://v8.1c.ru/8.3/uilog">']
    st = {'win': False, 'form': object(), 'elem': None, 'etag': None}
    def close_elem():
        if st['elem'] is not None:
            out.append('\t\t\t</%s>' % st['etag']); st['elem'] = None; st['etag'] = None
    def close_win():
        close_elem()
        if st['win']:
            out.append('\t\t</Form>'); out.append('\t</ClientApplicationWindow>'); st['win'] = False
        st['form'] = object()
    for p in parsed:
        if p.get('winnav') or ('wincmd' in p):
            close_elem()
            if not st['win']:
                out.append('\t<ClientApplicationWindow caption="">'); out.append('\t\t<Form title="">'); st['win'] = True
            out.append('\t\t</Form>')
            if 'wincmd' in p:
                out.append('\t\t<executeCommand command=%s/>' % quoteattr(p['wincmd']))
            else:
                astr = ''.join(' %s=%s' % (k, quoteattr(str(v))) for k, v in (p.get('attrs') or {}).items())
                kids = p.get('children') or []
                if kids:
                    out.append('\t\t<%s%s>' % (p['winnav'], astr))
                    for ktag, kattrs in kids:
                        out.append('\t\t\t<%s%s/>' % (ktag, ''.join(
                            ' %s=%s' % (kk, quoteattr(str(kv))) for kk, kv in kattrs.items())))
                    out.append('\t\t</%s>' % p['winnav'])
                else:
                    out.append('\t\t<%s%s/>' % (p['winnav'], astr))
            out.append('\t</ClientApplicationWindow>')
            st['win'] = False; st['form'] = object(); continue
        if p['form'] != st['form']:
            close_win()
            out.append('\t<ClientApplicationWindow caption="">'); out.append('\t\t<Form title="">')
            st['win'] = True; st['form'] = p['form']
        if p.get('formlevel') or p['tag'] in ('executeChoiceFromList', 'executeChoiceFromMenu'):
            close_elem()   # действие уровня ФОРМЫ — прямой child <Form>, не внутри элемента
            astr = ''.join(' %s=%s' % (k, quoteattr(str(v))) for k, v in (p['attrs'] or {}).items())
            out.append('\t\t\t<%s%s/>' % (p['tag'], astr)); continue
        etag = 'FormTable' if p['table'] else 'FormField'
        if p['elem'] != st['elem'] or etag != st['etag'] or p.get('ci_path') != st.get('ci_path'):
            close_elem()
            locator = ' ciPath=%s' % quoteattr(p['ci_path']) if p.get('ci_path') else ''
            out.append('\t\t\t<%s name=%s%s>' % (etag, quoteattr(p['elem'] or ''), locator))
            st['elem'] = p['elem']; st['etag'] = etag; st['ci_path'] = p.get('ci_path')
        astr = ''.join(' %s=%s' % (k, quoteattr(str(v))) for k, v in (p['attrs'] or {}).items())
        if p['fields']:
            out.append('\t\t\t\t<%s%s>' % (p['tag'], astr))
            for ft, fc in p['fields']:
                out.append('\t\t\t\t\t<Field title=%s cellText=%s/>' % (quoteattr(ft), quoteattr(fc)))
            out.append('\t\t\t\t</%s>' % p['tag'])
        else:
            out.append('\t\t\t\t<%s%s/>' % (p['tag'], astr))
    close_win()
    out.append('</uilog>')
    return '\n'.join(out)

# Операции, меняющие состояние формы, но не попадающие в НАТИВНУЮ запись при прямом вызове.
# tc_record_finish вернёт их в lost_actions (актуально для mode='native'; в 'synth' они попадают в сценарий).
_NONREC_MUTATORS = {
    G.SET_ORDER: 'set_order', G.CHANGE_ROW: 'change_row',
}

# Журнал наблюдений сеанса записи. Хранится ОТДЕЛЬНО от _track: там протокольные кадры, здесь —
# что показало наблюдение. Учитываются только логические операции из _VERIFY_ACTIONS (57 из 128);
# execute_command, оконные/сессионные операции и run_scenario сюда не попадают вовсе.
_REC_SCOPE = ('учитываются только мутирующие действия с проверкой цели; execute_command, '
              'оконные и сессионные операции, а также run_scenario не учитываются')

def _rec_note(action, key, obs, changed):
    """Записать наблюдение вызова, если идёт запись сценария и она не на паузе."""
    log = _state.get('rec_obs')
    if log is None:
        return
    log.append({'call': len(log) + 1, 'action': action, 'target': key,
                'observed': obs, 'changed': changed})


def _native_composite_begin(c, action='read_rows'):
    """Keep native events around a composite whose cleanup 1C does not record.

    Close the preceding native fragment before changing selection. Capture the composite
    from its actual accepted commands, then restart native recording. This avoids matching
    or rewriting similar table actions in an unrelated part of the native log.
    """
    if not (_state.get('rec_active') and _state.get('rec_mode') == 'native'
            and c._track is not None):
        return None
    if _state.get('rec_native_stopped'):
        raise _CellEditFailure('Finish or cancel the interrupted recording before continuing.',
                               {'code': 'recording_incomplete'})
    try:
        r = c.send_cmd(G.UILOG, None, kind='read818', middle=REC_FINISH)
        _cell_step(r, 'The preceding scenario fragment could not be captured.')
        _state.setdefault('rec_native_parts', []).append(tc1c.extract_uilog(_body(r)))
        _state['rec_native_stopped'] = True
        return len(c._track)
    except Exception:
        _state.setdefault('rec_native_errors', []).append(action)
        raise


def _native_composite_end(c, mark, action='read_rows'):
    try:
        _state.setdefault('rec_native_parts', []).append(_synth_uilog(c._track[mark:]))
        r = c.send_cmd(G.UILOG, None, kind='read818', middle=REC_START)
        _cell_step(r, 'Native recording could not be restarted.')
        _state.pop('rec_native_stopped', None)
    except Exception:
        _state.setdefault('rec_native_errors', []).append(action)
        raise


def _join_uilogs(parts):
    import xml.etree.ElementTree as ET
    root = ET.Element('uilog')
    for part in parts:
        if part:
            try:
                fragment = ET.fromstring(part)
            except ET.ParseError as exc:
                raise ValueError('Invalid scenario fragment.') from exc
            if fragment.tag.rsplit('}', 1)[-1] != 'uilog':
                raise ValueError('Unexpected scenario fragment root.')
            root.extend(fragment)
    return ET.tostring(root, encoding='unicode', xml_declaration=True)

@_action('tc_scenario')
def tc_record_start() -> dict:
    """Start recording a scenario (uilog) that tc_run_scenario can replay later. Perform the real,
    effect-producing actions between start and tc_record_finish, then read the scenario from finish."""
    c = _need()
    _state['rec_mode'] = 'native' if os.environ.get('TC1C_RECORD_MODE', '').lower() == 'native' else 'synth'
    _rec_reset()                        # прежняя запись, если была, начисто снимается
    _state['rec_obs'] = []              # журнал наблюдений сеанса: пуст, пока ничего не сделано
    r = c.send_cmd(G.UILOG, None, kind='read818', middle=REC_START)
    c._track = []
    # признак «запись идёт» отдельно от c._track: в режиме native трекер не ведётся, а знать,
    # завершена ли запись, нужно всё равно — иначе повторное завершение изобразит успех
    _state['rec_active'] = True
    return {'ok': r['ok']}

@_action('tc_scenario')
def tc_record_finish(path: str = None) -> dict:
    """Stop recording and return the scenario XML in 'uilog', or write it to `path`, resolved against
    the SERVER working directory and echoed back absolute. If the file cannot be written the
    answer carries ok=false, the path, the reason AND the scenario in 'uilog' — recording is
    already stopped, so a second call will not give it back. lost_actions: actions that could
    not be captured, so the scenario is incomplete for replay. no_effect: calls whose value came
    back unchanged — a hint to check, not a verdict, and it numbers CALLS, not steps of the XML.
    observed/not_observed count the calls whose result could and could not be read back;
    readback/scope say whether reading back was on at all and how wide it reached. An empty
    no_effect with zero observed means nothing was checked."""
    c = _need()
    # завершение необратимо: повторный вызов послал бы его заново и вернул пустой сценарий как
    # успех. Пауза признак не снимает — она приостанавливает пополнение, а не заканчивает запись
    if not _state.get('rec_active'):
        return {'ok': False, 'error': 'not recording: it was already finished or never started. '
                                      'The scenario was returned by the previous finish; '
                                      'call record_start to record again'}
    tracked = c._track if c._track is not None else _state.get('rec_paused_track') or []
    c._track = None
    _state.pop('rec_paused_track', None)          # завершение на паузе — берём накопленное до неё
    # снимаем вместе с накопленным: иначе таймаут при отправке оставит разрешение на повтор,
    # а данных уже нет — повтор вернул бы пустой сценарий как успех
    _state.pop('rec_active', None)
    mode = _state.get('rec_mode', 'native')
    parts = _state.pop('rec_native_parts', [])
    native_errors = _state.pop('rec_native_errors', [])
    covered = _state.pop('rec_native_covered', [])
    stopped = _state.pop('rec_native_stopped', False)
    try:
        r = ({'ok': False, 'raw': b''} if stopped else
             c.send_cmd(G.UILOG, None, kind='read818', middle=REC_FINISH))
    except Exception:
        if not parts:
            raise
        r = {'ok': False, 'raw': b''}
        native_errors.append('read_rows')
    native_xml = tc1c.extract_uilog(_body(r))
    if parts and not r.get('ok'):
        native_errors.append('read_rows')
    xml = _synth_uilog(tracked) if mode == 'synth' else native_xml
    unmerged = None
    if mode == 'native' and parts:
        try:
            xml = _join_uilogs([*parts, native_xml])
        except ValueError:
            native_errors.append('read_rows')
            unmerged = [*parts, native_xml]
    lost = []
    if mode == 'synth':
        lost = _synth_unhandled(tracked)   # мутаторы без synth-обработчика
    else:
        for index, (g, _k, _m, _kind) in enumerate(tracked):
            if any(start <= index < end for start, end in covered):
                continue  # This command was preserved in an explicit composite fragment.
            if g in _NONREC_MUTATORS and _NONREC_MUTATORS[g] not in lost:
                lost.append(_NONREC_MUTATORS[g])
    lost.extend(a for a in dict.fromkeys(native_errors) if a not in lost)
    obs_log = _state.pop('rec_obs', None)
    if obs_log is None:
        obs_log = _state.pop('rec_obs_paused', None) or []   # завершение на паузе
    _state.pop('rec_obs_paused', None)
    seen = [e for e in obs_log if e['changed'] is not None]
    out = {'ok': r['ok'], 'lost_actions': lost,
           'no_effect': [e for e in obs_log if e['changed'] is False],
           'readback': 'on' if READBACK else 'off',
           'observed': len(seen), 'not_observed': len(obs_log) - len(seen),
           'scope': _REC_SCOPE}
    if native_errors:
        out.update(ok=False, code='recording_incomplete',
                   error='Some actions could not be recorded completely; inspect lost_actions before replay.')
    if unmerged is not None:
        out['uilog_fragments'] = unmerged
    if path:
        # Запись сценария к этому моменту завершена НЕОБРАТИМО: трекер снят, журнал наблюдений
        # забран, кадр завершения отправлен. Любое исключение отсюда унесло бы сценарий с собой,
        # а повторный вызов пошлёт завершение заново и вернёт уже не то же самое. Поэтому в
        # защищённый участок входит и РАЗРЕШЕНИЕ пути: строка может быть непригодна сама по себе
        # (например с нулевым символом), и это ValueError, а не OSError.
        full = None
        try:
            # путь разрешается относительно каталога СЕРВЕРА, а не вызывающего, поэтому наружу
            # отдаётся абсолютный: иначе ответ называет место, где у вызывающего файла нет
            full = os.path.abspath(path)
            with open(full, 'w', encoding='utf-8') as f:
                f.write(xml or '')
            out['path'] = full
        except (OSError, ValueError) as e:
            out['ok'] = False
            out['path'] = full if full is not None else path   # абсолютный не получился — исходный
            out['error'] = 'scenario not written to file: %s' % e
            out['uilog'] = xml
    else:
        out['uilog'] = xml
    return out

# UILog Pause/Resume/Cancel: общий GUID 77e9254b, middle e<N>81 (Start=e1..Finish=e5).
# Привязка e2/e3/e4 -> Pause/Resume/Cancel ВЫВЕДЕНА по жизненному циклу (не верифицирована
# операции управляют только ЗАПИСЬЮ сценария, данные не трогают.
@_action('tc_scenario')
def tc_record_pause() -> dict:
    """Pause user-actions recording: actions performed until tc_record_resume stay out of the
    scenario."""
    c = _need()
    if c._track is not None:            # режим synth: приостановить и локальный трекер
        _state['rec_paused_track'] = c._track
        c._track = None
    if _state.get('rec_obs') is not None:                     # на паузе не пополняем, но и не
        _state['rec_obs_paused'] = _state.pop('rec_obs')      # теряем накопленное при повторной
    r = c.send_cmd(G.UILOG, None, kind='read818', middle=b'\xe2\x81')
    return {'ok': r['ok']}

@_action('tc_scenario')
def tc_record_resume() -> dict:
    """Resume user-actions recording."""
    c = _need()
    r = c.send_cmd(G.UILOG, None, kind='read818', middle=b'\xe3\x81')
    if c._track is None and _state.get('rec_paused_track') is not None:
        c._track = _state.pop('rec_paused_track')
    if _state.get('rec_obs') is None and _state.get('rec_obs_paused') is not None:
        _state['rec_obs'] = _state.pop('rec_obs_paused')
    return {'ok': r['ok']}

@_action('tc_scenario')
def tc_record_cancel() -> dict:
    """Cancel user-actions recording (discard the scenario)."""
    c = _need()
    c._track = None
    _rec_reset()                            # запись отменена — завершать больше нечего
    r = c.send_cmd(G.UILOG, None, kind='read818', middle=b'\xe4\x81')
    return {'ok': r['ok']}

# ====================== сценарий: навигация формы ===========================
# ============ чтение детей, обход для внутренних операций и нативный поиск ============
def _read_children(c, key, *, observed=False):
    """Preserve traversal failures except the native ordinary-form leaf case."""
    try:
        return c.send_cmd(G.GET_CHILD_OBJECTS, key, kind='read', middle=CHILD_MIDDLE)
    except tc1c.OperationError as exc:
        if exc.status not in (7, 17) or _key_class(key) not in ('Button', 'EditField'):
            raise
        parent = _collection_parent(key)
        while parent and _key_class(parent) != 'UnmanagedForm':
            parent = _collection_parent(parent)
        # The native manager returns an empty collection for these leaves locally.
        # A caller-supplied address still needs membership verification.
        if exc.status == 7:
            # Ordinary table label columns report ObjectNotFound for child enumeration,
            # while their native GetChildObjects returns []. Recheck membership first.
            if not parent or _key_class(key) != 'EditField':
                raise
            live = _ref_live_object(c, key)
            if not live or live.get('type') != 'LabelField':
                raise
            return {'ok': True, 'raw': b''}
        if parent and (observed or _ref_live_object(c, key) is not None):
            return {'ok': True, 'raw': b''}
        raise


def _walk_tree(c, root_key=None):
    """Все подчинённые объекты (рекурсивно) от root_key (или активного окна)."""
    start = root_key or _window(c)['key']
    if not start:
        return []
    seen = {start}; out = []; stack = [start]
    while stack:
        key = stack.pop()
        r = _read_children(c, key, observed=key != start)
        if not r.get('ok'):
            raise RuntimeError('Could not read the complete element tree; retry the search.')
        for it in _coll(r, key):
            k = it.get('key')
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(it); stack.append(k)
    return out

def _answer_confirm_dialog(c, confirm=True, max_wait=5.0):
    """Ответить на модальный вопрос Да/Нет, если он появился после действия.
    Активное окно диалога (заголовок «1С:Предприятие») содержит Group[Buttons] с
    Button[Button0]=Да, Button[Button1]=Нет (заголовки в коллекции пусты — матч по ИМЕНИ).
    confirm=True → Да (Button0), иначе Нет (Button1).
    Возврат ПАРОЙ ('Да'/'Нет' либо None, текст вопроса либо None): текст вопроса лежит в
    Decoration[Message] того же дерева, и добывать его отдельным обходом значило бы спрашивать
    дважды об одном. Обход материализуется целиком, потому что декорация с вопросом стоит в дереве
    ПОСЛЕ кнопок; окно диалога маленькое и фиксированное."""
    import time as _t
    want = 'Button0' if confirm else 'Button1'
    deadline = _t.time() + max_wait
    while _t.time() < deadline:
        _state['window_key'] = None                   # активное окно могло смениться на диалог
        items = list(_walk_tree(c))
        question = next((it.get('title') for it in items
                         if it.get('name') == 'Message' and it.get('class') == 'Decoration'
                         and it.get('title')), None)
        for it in items:
            k = it.get('key') or ''
            if it.get('name') == want and '.Group[Buttons].Button[' in k:
                # та же проверка цели, что в MCP-пути и в воспроизведении: адрес взят обходом
                # дерева, но диалог мог закрыться между обходом и кликом — тогда ждём дальше
                if _verify_target(c, k, it.get('handle'))[0] == 'absent':
                    break
                for kind in ('action', 'commit'):
                    c.send_cmd(G.CLICK, k, kind=kind, middle=b'', handle=it.get('handle'))
                _state['window_key'] = None
                return ('Да' if confirm else 'Нет'), question
        _t.sleep(0.3)
    _state['window_key'] = None
    return None, None

def _name_matcher(pattern):
    """Шаблон имени со знаками подстановки * ? -> регэксп (без учёта регистра)."""
    if not pattern:
        return None
    rx = '^' + re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.') + '$'
    return re.compile(rx, re.IGNORECASE)

def _search_objects(c, root_key=None):
    """Одна попытка нативного FindObjects; фильтры MCP применяются к этому ответу."""
    start = root_key or _window(c)['key']
    if not start:
        return []
    r = c.send_cmd(G.GET_CHILD_OBJECTS, start, kind='read', middle=FIND_MIDDLE)
    if not r.get('ok'):
        raise RuntimeError('Could not read the search result; retry the search.')
    seen = {start}; out = []
    for item in _coll(r, start):
        key = item.get('key')
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _find(c, name=None, cls=None, type=None, root_key=None, title=None, seen_classes=None,
          seen_types=None):
    """Объекты нативного поиска по критериям. seen_classes/seen_types (если переданы set)
    наполняются из того же ответа: диагностика не выполняет дополнительный поиск."""
    nm = _name_matcher(name); tm = _name_matcher(title)
    res = []
    for it in _search_objects(c, root_key):
        if seen_classes is not None and it.get('class'):
            seen_classes.add(it['class'])
        if seen_types is not None and it.get('type'):
            seen_types.add(it['type'])
        if cls and it.get('class') != cls:
            continue
        if type and it.get('type') != type:
            continue
        if nm and not nm.match(it.get('name') or ''):
            continue
        if tm and not tm.match(it.get('title') or ''):
            continue
        row = {k: it.get(k) for k in ('key', 'handle', 'name', 'class', 'type', 'title')}
        # form_name добавляется ОТДЕЛЬНО и только при наличии ключа: у прочих классов его нет, и
        # общий it.get поставил бы null в каждую строку каждой выдачи. Способ сборки остальных
        # шести полей не трогаем — иначе изменились бы и ответы, где ни одной формы нет
        if 'form_name' in it:
            row['form_name'] = it['form_name']
        res.append(row)
    return res

def _find_wait(c, name=None, cls=None, type=None, root_key=None, title=None, timeout=0,
               seen_classes=None, seen_types=None):
    """_find с повтором до timeout секунд (0 — один проход): объект может появиться не сразу.
    seen_classes (если передан set) накапливает классы, встреченные ЗА ВРЕМЯ ожидания, — чтобы
    диагностика опиралась на его наблюдения, а не на отдельный обход после таймаута."""
    import time as _t
    deadline = _t.time() + max(timeout or 0, 0)
    while True:
        res = _find(c, name, cls, type, root_key, title, seen_classes=seen_classes,
                    seen_types=seen_types)
        if res or _t.time() >= deadline:
            return res
        _t.sleep(0.5)

@_action('tc_find')
def tc_find_objects(name: str = None, cls: str = None, type: str = None, root_key: str = None,
                    title: str = None, timeout: int = 0) -> dict:
    """Find all objects in the UI tree matching the criteria. name and title take the wildcards * and
    ?; cls is the class and type is the platform's element kind (both as reported by
    tc_get_child_objects, e.g. CheckBoxField or Popup); root_key is where to start (default: the
    active window); timeout keeps retrying for that many seconds while nothing matches (0 = a
    single pass). An empty result is not an error, so ok stays true. When nothing matched and a
    cls or type was given, the answer also says whether the server knows that filter and what
    was actually present, so a misspelling is distinguishable from an object that never
    appeared."""
    c = _need()
    seen_c = set() if cls else None
    seen_t = set() if type else None
    objs = _find_wait(c, name, cls, type, root_key, title, timeout,
                      seen_classes=seen_c, seen_types=seen_t)
    out = {'ok': True, 'objects': objs}
    if not objs:
        # пустой результат — не ошибка, но и не ответ: без диагностики опечатка в фильтре
        # неотличима от объекта, который ещё не появился
        diag = _empty_find_diag(cls, type, seen_c, seen_t)
        detail = diag.pop('error_detail', None)
        if detail:
            out['note'] = 'nothing matched: ' + detail
        out.update(diag)
    return out

@_action('tc_find')
def tc_find_object(name: str = None, cls: str = None, type: str = None, root_key: str = None,
                   title: str = None, timeout: int = 0) -> dict:
    """Find the first object matching the criteria (parameters as in tc_find_objects). When
    nothing matched, the answer carries the same filter diagnostics as tc_find_objects."""
    c = _need()
    seen_c = set() if cls else None
    seen_t = set() if type else None
    res = _find_wait(c, name, cls, type, root_key, title, timeout,
                     seen_classes=seen_c, seen_types=seen_t)
    out = {'ok': bool(res), 'object': res[0] if res else None}
    if not res:
        diag = _empty_find_diag(cls, type, seen_c, seen_t)
        detail = diag.pop('error_detail', None)
        if detail:
            out['note'] = 'nothing matched: ' + detail
        out.update(diag)
    return out

def _empty_find_diag(cls, type, seen_classes, seen_types):
    """Диагностика ПУСТОГО поиска: одна на все публичные поиски.

    Без неё опечатка в фильтре неотличима от объекта, который ещё не появился, — и до этой правки
    так и было у find_objects: он отдавал пустой список молча. Сведения берутся из снимка ТОГО ЖЕ
    обхода, который дал пустой результат: второй проход показал бы другое дерево, и два соседних
    поля ответа говорили бы о разном.

    Поля появляются только при пустом результате: у непустого поиска они не нужны и лишь раздували
    бы ответ. Владелец знания о классах и видах — декодер, сервер его не дублирует."""
    out, notes = {}, []
    for kind, plural, want, seen, known_all in (
            ('class', 'classes', cls, seen_classes, _collection.known_classes()),
            ('type', 'types', type, seen_types, _collection.known_types())):
        if not want or seen is None:
            continue
        known = want in known_all
        out['%s_seen' % kind] = want in seen
        out['%s_known' % kind] = known
        out['%s_present' % plural] = sorted(seen)
        if want not in seen:
            notes.append('%s %r was not seen; %s'
                         % (kind, want,
                            'the server knows it, so the object may not have appeared yet' if known
                            else 'and the server does not know it — check the spelling'))
    if notes:
        out['error_detail'] = '; '.join(notes)
    if type and type in _collection.known_classes() and type not in _collection.known_types():
        out['suggested_parameters'] = {'cls': type, 'type': None}
        out['error_detail'] = '%r is an object class. Pass cls=%r and omit type.' % (type, type)
    elif cls and cls in _collection.known_types() and cls not in _collection.known_classes():
        out['suggested_parameters'] = {'cls': None, 'type': cls}
        out['error_detail'] = '%r is an element type. Pass type=%r and omit cls.' % (cls, cls)
    return out


@_action('tc_find')
def tc_wait_for_object_displayed(name: str = None, cls: str = None, type: str = None,
                                 title: str = None, timeout: int = 60) -> dict:
    """Poll the UI tree until an object matching the criteria appears, up to timeout seconds.
    Returns the object, or ok=False on timeout. Criteria as in tc_find_objects. On timeout the
    answer says whether a `cls` was seen at all and lists the classes that were actually present,
    so a misspelled class name is distinguishable from an object that never appeared."""
    c = _need()
    seen_c = set() if cls else None
    seen_t = set() if type else None
    res = _find_wait(c, name, cls, type, None, title, timeout,
                     seen_classes=seen_c, seen_types=seen_t)
    if res:
        return {'ok': True, 'object': res[0]}
    # ожидание НЕ обрывается досрочно: объект может появиться позже, ради этого метод и нужен.
    # Чинится не длительность, а содержательность отказа — по классам и видам, встреченным ЗА
    # ВРЕМЯ ожидания, без дополнительного обхода после таймаута.
    out = {'ok': False, 'error': 'timeout after %d s; the object did not appear' % timeout}
    diag = _empty_find_diag(cls, type, seen_c, seen_t)
    detail = diag.pop('error_detail', None)
    if detail:
        out['error'] += '; ' + detail
    out.update(diag)
    return out

@_action('tc_form')
def tc_wait_for_closing(window_title: str = None, timeout: int = 60) -> dict:
    """Wait until a window closes, up to timeout seconds. Without window_title, watch the window
    active AT THE MOMENT OF THE CALL. After a close, pass window_title to check the closed window.
    Switching windows or opening a preview does not count as closing. If the target cannot be
    identified or its absence confirmed, return ok=False."""
    import time as _t
    c = _need()
    start = _window(c)
    deadline = _t.monotonic() + max(timeout, 0)
    matched_key = None
    if not window_title:
        matched_key = start.get('key')
        if not matched_key:
            return {'ok': False, 'code': 'active_window_unavailable',
                    'error': 'The active window cannot be identified; closing is not confirmed.',
                    'recovery': start.get('recovery') or 'Pass window_title to identify the window.'}
    closed_title = window_title or start.get('title')
    while True:
        # Проверяем существование окна, а не фокус: предпросмотр и другая форма могут
        # перекрывать цель. Отказ перечисления нельзя принимать за пустую коллекцию.
        r = c.send_cmd(G.GET_CHILD_OBJECTS, None, kind='read', middle=CHILD_MIDDLE)
        if not r.get('ok'):
            return {'ok': False, 'code': 'window_list_unavailable',
                    'error': 'The open windows could not be read; closing is not confirmed.'}
        windows = tc1c.decode_collection(r['raw'], None)
        unknown_title = False
        if matched_key:
            if not any(w.get('key') == matched_key for w in windows):
                return {'ok': True, 'closed_title': closed_title}
        else:
            matches = []
            for w in windows:
                title = w.get('title')
                if title is None:
                    title = _form_title(c, w.get('key'))[0]
                if title is None:
                    unknown_title = True
                elif title == window_title:
                    matches.append(w['key'])
            if start.get('key') in matches:
                matched_key = start['key']
            elif len(matches) == 1:
                matched_key = matches[0]
            elif len(matches) > 1:
                return {'ok': False, 'code': 'ambiguous_window',
                        'error': 'Several windows have this title. Activate the target and wait without window_title.'}
            elif not unknown_title:
                if not start.get('key'):
                    return {'ok': False, 'code': 'active_window_unavailable',
                            'error': 'The target was not found, but the active window cannot be identified; closing is not confirmed.'}
                return {'ok': True, 'closed_title': closed_title}
        remaining = deadline - _t.monotonic()
        if remaining <= 0:
            if unknown_title and not matched_key:
                return {'ok': False, 'code': 'window_title_unavailable',
                        'error': 'An open window caption could not be determined reliably; closing is not confirmed.'}
            return {'ok': False, 'error': 'timeout after %d s; the window is still open' % timeout}
        _t.sleep(min(0.5, remaining))

@_action('tc_form')
def tc_create_snapshot(key: str = None) -> dict:
    """Save a baseline of a ManagedForm (default: the active form). Returns snapshot_id and a
    summary. May take several seconds; use when you need to compare form state later.
    Reads element visibility, availability, read-only flags and ordinary field values;
    excludes table-row contents and document contents. Hidden branches skip further state reads;
    disabled elements skip read-only. Reads are sequential, not an atomic view of the form."""
    return _snapshots.run(sys.modules[__name__], 'create_snapshot', key=key)


@_action('tc_form')
def tc_compare_snapshot(snapshot_id: str, key: str = None) -> dict:
    """Compare a saved baseline with the current state of its original form; no new snapshot is
    stored. Optional key must identify that same form instance. Returns changes, added and removed
    elements; observed lists properties read now whose earlier values were not read. Skipped
    readings are not changes. complete/baseline_complete and errors identify gaps in reading.
    For flags in added, '-' means not read and 'unknown' means reading failed.
    Closed forms and reconnected clients require a new baseline."""
    return _snapshots.run(sys.modules[__name__], 'compare_snapshot', key=key, snapshot_id=snapshot_id)


@_action('tc_form')
def tc_list_snapshots(key: str = None) -> dict:
    """List saved snapshots for this connection, optionally restricted to one ManagedForm key.
    Returns IDs, form titles, timestamps, element counts, sizes and global storage limits.
    Listing does not refresh last_used_at or verify forms are still open; a supplied key is validated."""
    return _snapshots.run(sys.modules[__name__], 'list_snapshots', key=key)


@_action('tc_form')
def tc_delete_snapshot(snapshot_id: str) -> dict:
    """Delete a saved snapshot belonging to this connection."""
    return _snapshots.run(sys.modules[__name__], 'delete_snapshot', snapshot_id=snapshot_id)


def _form_elements(c):
    """Навигация окно->форма->ВСЕ элементы (рекурсивно, включая вложенные в группы).
    Возврат {имя_элемента: {key, handle}}."""
    wk = tc1c.extract_object_keys(
        _body(c.send_cmd(G.GET_ACTIVE_WINDOW, None, kind='read', middle=RC)))
    if not wk:
        return {}
    def kids(key):
        return _body(_read_children(c, key))
    forms = [k for k in tc1c.extract_object_keys(kids(wk[0]))
             if _key_class(k) in ('ManagedForm', 'UnmanagedForm')]
    if not forms:
        return {}
    out = {}; seen = set(); stack = [(forms[0], 0)]
    while stack:
        key, d = stack.pop()
        if key in seen or d > 12:
            continue
        seen.add(key)
        for k, h in tc1c.object_handles(kids(key), key).items():
            m = re.search(r'\[([^\[\]]+)\]$', k)
            if m:
                out.setdefault(m.group(1), {'key': k, 'handle': h})
            if k not in seen:
                stack.append((k, d + 1))
    return out

def _command_interface_element(c, path):
    """Resolve a recorded CI path against the current client's windows, without session IDs."""
    if not isinstance(path, str) or _key_class(path) != 'CIButton':
        return None
    windows = _coll(c.send_cmd(G.GET_CHILD_OBJECTS, None, kind='read', middle=CHILD_MIDDLE))
    matches = []
    for window in windows:
        wk = window.get('key', '')
        if _key_class(wk) not in ('MainFrame', 'SecondaryFrame'):
            continue
        roots = _coll(c.send_cmd(G.GET_COMMAND_INTERFACE, wk, kind='read', middle=RC), wk)
        parent = next((o for o in roots if o.get('key') == wk + '.CI'), None)
        target = wk + '.CI.' + path
        chain = []; part = target
        while part and part != wk + '.CI':
            chain.append(part); part = _collection_parent(part)
        if part is None:
            continue
        for expected in reversed(chain):
            if parent is None:
                break
            children = _coll(c.send_cmd(G.GET_CHILD_OBJECTS, parent['key'], kind='read',
                             middle=CHILD_MIDDLE, handle=parent.get('handle')), parent['key'])
            parent = next((o for o in children if o.get('key') == expected), None)
        if parent and parent.get('key') == target and parent.get('handle'):
            matches.append(parent)
    return matches[0] if len(matches) == 1 else None


def _form_object(c):
    """Объект формы (ManagedForm) активного окна: {key, handle} или None."""
    wk = tc1c.extract_object_keys(
        _body(c.send_cmd(G.GET_ACTIVE_WINDOW, None, kind='read', middle=RC)))
    if not wk:
        return None
    raw = _body(c.send_cmd(G.GET_CHILD_OBJECTS, wk[0], kind='read', middle=CHILD_MIDDLE))
    for key, h in tc1c.object_handles(raw, wk[0]).items():
        if _key_class(key) in ('ManagedForm', 'UnmanagedForm'):
            return {'key': key, 'handle': h}
    return None

def _table_cells(c, table_key):
    """Колонки/ячейки таблицы: {имя_колонки: {key, handle}}."""
    return {o['name']: {'key': o['key'], 'handle': o['handle']}
            for o in _table_columns(c, table_key) if o.get('name') and o.get('handle')}

@_action('tc_scenario')
def tc_run_scenario(uilog: str = None, path: str = None) -> dict:
    """Replay a recorded uilog scenario on the current form. Give the XML in `uilog` or a `path` to a
    file with it. Steps that could not be replayed are listed in `unsupported`. `total` counts
    every step in the scenario; `played` counts the ones actually attempted, REGARDLESS of how
    they went, so a failed step is still counted. Neither is a count of effects: `ok` says every
    attempted step was accepted, and the effect of a step is in `steps[].changed`, the same
    contract as when you call the action directly."""
    import xml.etree.ElementTree as ET
    c = _need()
    if not uilog and path:
        # тот же ответ на негодный путь, что у record_finish: отказ с причиной, а не исключение
        try:
            with open(path, encoding='utf-8') as f:
                uilog = f.read()
        except (OSError, ValueError, UnicodeDecodeError) as e:
            return {'ok': False, 'path': os.path.abspath(path),
                    'error': 'the scenario file could not be read: %s' % e}
    if not uilog:
        return {'ok': False, 'error': 'provide uilog or path'}
    try:
        root = ET.fromstring(uilog)
    except ET.ParseError as e:
        return {'ok': False, 'error': 'XML: %s' % e}
    # Любой шаг сначала требует РАЗРЕШЕНИЯ элементов: активное окно + обход детей. Если этих
    # методов в подключённой платформе нет, воспроизводить нечего — отказываем сразу, а не
    # отправляем старому клиенту неизвестный GUID (он не ответит, вызов повиснет).
    for _g in (G.GET_ACTIVE_WINDOW, G.GET_CHILD_OBJECTS):
        _minv = GUID_MIN_VERSION.get(_g)
        if _minv and _vt(_conn_ver(c)) and _vt(_conn_ver(c)) < _vt(_minv):
            return {'ok': False, 'error': 'replaying a scenario needs 1C platform %s+, '
                                          'подключено %s' % (_minv, _conn_ver(c)),
                    'available_since': _minv, 'connected_version': _conn_ver(c)}
    import time as _t
    steps = []
    # Статус предполётной проверки последнего отправленного кадра шага. Без него шаг с
    # подтверждённым адресом, шаг без доступной проверки и шаг с выключенной проверкой выглядят
    # одинаково — а политика «при unknown выполняем» имеет смысл только вместе с видимым статусом.
    _chk = {'v': None, 'hidden': None, 'extra': {}}
    # наблюдение шага: (описание, до, после). Тот же контракт, что у обработчиков, — иначе
    # одинаковое действие через MCP и через воспроизведение отвечало бы по-разному
    _obs = {'v': None, 'busy': False, 'applies': False, 'args': {}}

    def _step(d):
        d.update(_chk['extra'])
        if _chk['v'] is not None:
            d['target_check'] = _chk['v']       # у шага из нескольких кадров — статус последнего
        if _chk['v'] == 'present' and _chk['hidden'] is False:
            d['target_hidden'] = True           # то же правило, что и в прямом пути
        if _obs['v'] is not None:
            desc, before, after = _obs['v']
            d['observed'] = desc
            d['changed'] = (before != after) if (before is not None and after is not None) else None
            d['value_before'], d['value_after'] = before, after
        elif _obs['applies']:
            # операция наблюдаемая, но наблюдения не вышло (нет колонки в шаге, метод неприменим
            # к цели) либо read-back выключен — ответ обязан это сказать, а не молчать
            d['changed'] = None
            if not READBACK:
                d['readback'] = 'off'
        _chk['v'] = None; _chk['hidden'] = None
        _chk['extra'] = {}
        _obs['v'] = None; _obs['applies'] = False; _obs['args'] = {}
        steps.append(d)

    class _VerErr(Exception):
        """Шаг сценария выполнить нельзя: метод новее платформы либо цели нет по адресу."""
        @property
        def details(self):
            cause = self.__cause__
            return ({'code': cause.code, 'status_code': cause.status}
                    if isinstance(cause, tc1c.OperationError) else {})

    class _TgtErr(_VerErr):
        """Объекта по адресу шага не существует (предполётная проверка)."""

    def _guard(guid, key, kind, handle):
        """Проверки, которые обязаны идти ДО наблюдения: версия метода и существование цели.
        Вынесены из _send, чтобы парный путь мог выполнить их перед наблюдением ДО и не читать
        состояние по адресу, который сейчас будет отклонён."""
        minv = GUID_MIN_VERSION.get(guid)
        if minv and _vt(_conn_ver(c)) and _vt(_conn_ver(c)) < _vt(minv):
            raise _VerErr('метод требует платформу 1С %s+, подключено %s' % (minv, _conn_ver(c)))
        if kind != 'commit' and guid in _VERIFY_GUIDS:
            _chk['v'], _chk['hidden'] = _verify_target(c, key, handle)
            if _chk['v'] == 'absent':
                raise _TgtErr('объект по адресу не найден: %s' % key)

    def _send(guid, key, kind='action', middle=b'', handle=None, pad=None, guarded=False):
        """Отправка кадра сценария с проверкой версии: метод новее подключённой платформы
        НЕ шлём — старый клиент не ответит на неизвестный GUID и вызов повиснет до таймаута.
        Здесь же единственная точка проверки цели для воспроизведения: kind='commit' её не
        повторяет, поэтому на пару action+commit приходится одна проверка, как и в MCP-пути."""
        if not guarded:
            _guard(guid, key, kind, handle)
        single = kind != 'commit' and not _obs['busy'] and guid in _READBACK_GUIDS
        if single:
            _obs['applies'] = True
        desc, before = _observe_guid(c, guid, key, handle, _obs['args']) if single else (None, None)
        try:
            r = c.send_cmd(guid, key, kind=kind, middle=middle, handle=handle, pad=pad)
        except tc1c.OperationError as exc:
            raise _VerErr(str(exc)) from exc
        if desc is not None:
            _obs['v'] = (desc, before,
                         _observe_guid(c, guid, key, handle, _obs['args'], prepared=desc)[1])
        if guid in _WINDOW_CHANGING:
            _state['window_key'] = None      # шаг мог открыть/закрыть окно
        return r

    def _ac(guid, key, handle, middle=b'', pad=None):
        _guard(guid, key, 'action', handle)     # цель проверяется ДО наблюдения, как и в _verified
        if guid in _READBACK_GUIDS:
            _obs['applies'] = True
        desc, before = _observe_guid(c, guid, key, handle, _obs['args'])
        _obs['busy'] = True                     # одиночный _send внутри пары не наблюдает повторно
        try:
            ok = True
            for kind in ('action', 'commit'):
                ok = _send(guid, key, kind=kind, middle=middle, handle=handle, pad=pad,
                           guarded=True)['ok'] and ok
        finally:
            _obs['busy'] = False
        if desc is not None:
            _obs['v'] = (desc, before,
                         _observe_guid(c, guid, key, handle, _obs['args'], prepared=desc)[1])
        return ok

    def _fields_of(act):
        """Пары (колонка, значение) из дочерних <Field> тега — ВСЕ, а не только первая."""
        return [(fe.get('title'), fe.get('cellText')) for fe in list(act)
                if fe.tag.split('}')[-1] == 'Field' and fe.get('title')]

    def _choice_mid(act):
        """Аргумент выбора из тега: index (число, 0-based) приоритетнее presentation (строка)."""
        idx = act.get('index')
        if idx in (None, ''):
            return tc1c.mk_choice(act.get('presentation', ''))
        try:
            return tc1c.mk_choice(int(idx))
        except ValueError:              # нечисловой index в чужой записи — как представление
            return tc1c.mk_choice(idx)

    def _confirm_of(act):
        """Ответ на модальный вопрос из тега: "false" -> Нет, "none" -> не отвечать вовсе
        (при записи ответа не было), иначе (в т.ч. без атрибута — нативная запись) Да."""
        v = str(act.get('confirm', 'true')).strip().lower()
        return None if v == 'none' else (v != 'false')

    def _replay(key, handle, act):
        """Действие над ЭЛЕМЕНТОМ. None = тег не поддержан."""
        a = act.tag.split('}')[-1]
        if a == 'activate':
            ok = _ac(G.ACTIVATE, key, handle)
            if ok:
                page = _page_activation_result(c, key, handle)
                if page:
                    _chk.update(v=page['target_check'], hidden=page['visible'], extra=page)
                    ok = page.get('ok', True)
            return ok
        if a == 'inputText':
            guid, middle = _input_text_command(act.get('text', ''), c, key)
            return _ac(guid, key, handle, middle)
        if a == 'inputHTML':
            att = {k.split('.', 1)[1]: base64.b64decode(v)
                   for k, v in act.attrib.items() if k.startswith('attachment.')}
            mid = tc1c.mk_html(act.get('HTML') or act.get('html') or '', att or None)
            return _ac(G.INPUT_HTML, key, handle, mid, tc1c.HTML_ATT_PAD if att else None)
        if a == 'clear':
            guid, middle = _input_text_command('', c, key)
            return _ac(guid, key, handle, middle)
        if a == 'setCheck':      return _ac(G.SET_CHECK, key, handle)
        if a == 'click':
            guid = _click_guid(key)
            return _ac(guid, key, handle) if guid is not None else False
        if a == 'increaseValue': return _ac(G.INCREASE_VALUE, key, handle)
        if a == 'decreaseValue': return _ac(G.DECREASE_VALUE, key, handle)
        if a == 'openDropList':  return _ac(G.OPEN_DROP_LIST, key, handle)
        if a == 'startChoosing': return _ac(G.START_CHOOSING, key, handle)
        if a == 'chooseRow':     return _ac(G.CHOOSE_ROW, key, handle)
        if a == 'gotoValue':     return _ac(G.GOTO_VALUE, key, handle, tc1c.mk_goto_value(int(act.get('presentation', '0'))))
        if a == 'executeChoiceFromChoiceList':
            # нативная запись пишет этим тегом выбор из ВЫПАДАЮЩЕГО списка; method= (synth)
            # отмечает, что записан ВыполнитьВыборИзСпискаВыбора — у него свой GUID
            g = (G.EXECUTE_CHOICE_FROM_CHOICE_LIST if act.get('method') == 'ExecuteChoiceFromChoiceList'
                 else G.CHOOSE_FROM_DROP_LIST)
            return _ac(g, key, handle, _choice_mid(act))
        if a == 'gotoDate':
            d = (act.get('date', '') or '').split('T')[0].split('-')
            if len(d) == 3:
                return _ac(G.GOTO_DATE, key, handle, tc1c.mk_date(int(d[0]), int(d[1]), int(d[2])))
            return False
        if a == 'setCurrentArea':
            return _ac(G.SET_CURRENT_AREA, key, handle, tc1c.mk_command(act.get('area') or act.get('address') or act.get('presentation') or ''))
        if a == 'beginEditingCurrentArea': return _ac(G.BEGIN_EDIT_CURRENT_AREA, key, handle)
        if a == 'finishEditingCurrentArea':
            mid = CANCEL_EDIT_MID if act.get('cancel', 'false') == 'true' else RS
            return _ac(G.END_EDIT_CURRENT_AREA, key, handle, mid)
        if a == 'selectOption': return _ac(G.SELECT_OPTION, key, handle, _choice_mid(act))
        if a == 'clickHTMLHyperlink': return _ac(G.CLICK_HTML_DOC_HYPERLINK, key, handle, _choice_mid(act))
        if a == 'clickFormattedDocHyperlink': return _ac(G.CLICK_FORMATTED_DOC_HYPERLINK, key, handle, _choice_mid(act))
        if a == 'clickFormattedStringHyperlink': return _ac(G.CLICK_FORMATTED_STRING_HYPERLINK, key, handle, _choice_mid(act))
        if a == 'clickViewStatusItem': return _ac(G.CLICK_VIEW_STATUS_ITEM, key, handle, _choice_mid(act))
        if a == 'deleteViewStatusItem': return _ac(G.DELETE_VIEW_STATUS_ITEM, key, handle, _choice_mid(act))
        if a == 'gotoNextMonth':     return _ac(G.GOTO_NEXT_MONTH, key, handle)
        if a == 'gotoPreviousMonth': return _ac(G.GOTO_PREVIOUS_MONTH, key, handle)
        if a == 'gotoNextYear':      return _ac(G.GOTO_NEXT_YEAR, key, handle)
        if a == 'gotoPreviousYear':  return _ac(G.GOTO_PREVIOUS_YEAR, key, handle)
        if a == 'gotoNextItem':      return _ac(G.GOTO_NEXT_ITEM, key, handle, tc1c.RES_E2)
        if a == 'gotoPreviousItem':  return _ac(G.GOTO_PREVIOUS_ITEM, key, handle, RS)
        if a == 'startChoosingFromChoiceList': return _ac(G.START_CHOOSING_FROM_CHOICE_LIST, key, handle)
        if a == 'writeContentToFile':
            return _ac(G.WRITE_CONTENT_TO_FILE, key, handle, b'\xe2' if act.get('saveAs') == 'true' else RS)
        if a == 'openField':  return _ac(G.OPEN_FIELD, key, handle)
        if a == 'cancelEdit': return _ac(G.CANCEL_EDIT, key, handle)
        if a == 'create':        return _ac(G.CREATE, key, handle)
        if a == 'expand':        return _ac(G.EXPAND_GROUP, key, handle)
        if a == 'collapse':      return _ac(G.COLLAPSE_GROUP, key, handle)
        if a == 'closeDropList':
            # Native logs can close a column's list after EndEditRow already closed it.
            # Only a positive read of the same live target permits the redundant close.
            if (_table_owner(key) and _key_class(key) == 'EditField'
                    and _guid_available(c, G.DROP_LIST_IS_OPEN)
                    and _kind_of(c, key) == 'InputField'):
                _guard(G.CLOSE_DROP_LIST, key, 'action', handle)
                if _chk['v'] == 'present':
                    r = _send(G.DROP_LIST_IS_OPEN, key, kind='read', middle=RS, handle=handle)
                    if _scalar(r, _bool_from_resp) is False:
                        return True
            return _ac(G.CLOSE_DROP_LIST, key, handle)
        if a == 'FormField':     # вложенное редактирование области табличного документа: begin + ввод в ту же область
            ok = _ac(G.BEGIN_EDIT_CURRENT_AREA, key, handle)
            for sub in list(act):
                r = _replay(key, handle, sub)
                ok = (r if r is not None else True) and ok
            return ok
        return None

    def _replay_form(formobj, act):
        """Действие на уровне формы. None = не поддержан."""
        a = act.tag.split('}')[-1]
        if not formobj:
            return False
        if a == 'activate':
            return _ac(G.ACTIVATE, formobj['key'], formobj['handle'])
        # аргумент: индекс (из synth) приоритетнее презентации (из нативной записи)
        mid = _choice_mid(act)
        if a == 'executeChoiceFromList':
            return _ac(G.EXECUTE_CHOICE_FROM_LIST, formobj['key'], formobj['handle'], mid)
        if a == 'executeChoiceFromMenu':
            return _ac(G.EXECUTE_CHOICE_FROM_MENU, formobj['key'], formobj['handle'], mid)
        # Form navigation has separate next/previous GUIDs and no direction argument.
        if a == 'gotoNextItem':
            return _ac(G.FORM_GOTO_NEXT_ITEM, formobj['key'], formobj['handle'])
        if a == 'gotoPreviousItem':
            return _ac(G.FORM_GOTO_PREVIOUS_ITEM, formobj['key'], formobj['handle'])
        return None

    FORM_ACTIONS = ('executeChoiceFromList', 'executeChoiceFromMenu',
                    'gotoNextItem', 'gotoPreviousItem', 'activate')

    def _process_form(frm):
        # пере-перечисляем активную форму под ЭТОТ блок (мультиформенность: список->карточка->...)
        elems = _form_elements(c)
        formobj = _form_object(c)

        def _walk(container):                        # рекурсивно, в порядке документа
            for node in list(container):
                tag = node.tag.split('}')[-1]
                if tag in ('FormField', 'FormButton'):
                    name = node.get('name')
                    el = (_command_interface_element(c, node.get('ciPath'))
                          if 'ciPath' in node.attrib else elems.get(name))
                    for act in list(node):
                        a = act.tag.split('}')[-1]
                        if not el:
                            _step({'target': name, 'action': a, 'ok': False, 'error': 'element not found'}); continue
                        try:
                            res = _replay(el['key'], el['handle'], act)
                        except _VerErr as e:
                            _step({'target': name, 'action': a, 'ok': False, 'error': str(e), **e.details}); continue
                        _step({'target': name, 'action': a, 'ok': bool(res), 'skipped': res is None})
                        if a in ('click', 'startChoosing'):
                            _t.sleep(0.8)            # дать открыться новой форме/выбору
                elif tag == 'FormTable':
                    tname = node.get('name'); tel = elems.get(tname)
                    if not tel:
                        _step({'target': tname, 'action': 'FormTable', 'ok': False, 'error': 'table not found'}); continue
                    cells = _table_cells(c, tel['key'])
                    for act in list(node):
                        a = act.tag.split('}')[-1]
                        try:
                            if a == 'addRow':
                                ok = _ac(G.ACTIVATE, tel['key'], tel['handle']) and _ac(G.ADD_ROW, tel['key'], tel['handle'])
                                cells = _table_cells(c, tel['key'])
                                _step({'target': tname, 'action': 'addRow', 'ok': ok})
                            elif a == 'choose':          # открыть текущую строку (напр. элемент списка)
                                ok = _ac(G.CHOOSE_ROW, tel['key'], tel['handle'])
                                _step({'target': tname, 'action': 'choose', 'ok': ok})
                                _t.sleep(0.8)
                            elif a in ('gotoNextRow', 'gotoPreviousRow', 'gotoFirstRow', 'gotoLastRow'):
                                guid = {'gotoFirstRow': G.GOTO_FIRST_ROW, 'gotoNextRow': G.GOTO_NEXT_ROW,
                                        'gotoPreviousRow': G.GOTO_PREVIOUS_ROW, 'gotoLastRow': G.GOTO_LAST_ROW}[a]
                                tgl = (act.get('toggleSelection') or act.get('switchSelection') or 'false') == 'true'
                                r = _send(guid, tel['key'], kind='read', middle=(b'\xe2' if tgl else RS), handle=tel['handle'])
                                _step({'target': tname, 'action': a, 'ok': r['ok']})
                            elif a == 'gotoRow':     # поиск строки ПО ЗНАЧЕНИЯМ колонок либо
                                #                          (без Field) переключение выделения ТЕКУЩЕЙ строки
                                fields = _fields_of(act)
                                error = _row_criteria_error(c, tel['key'], fields)
                                if error:
                                    raise _CellEditFailure(error['error'], error)
                                if not fields and _guid_available(c, G.CURRENT_MODE_IS_EDIT):
                                    if _cell_flag(c, G.CURRENT_MODE_IS_EDIT, tel['key'], tel['handle']) is not False:
                                        raise _CellEditFailure('Finish or cancel row editing first.', {'code': 'row_edit_pending'})
                                    _table_activate(c, tel['key'], tel['handle'], _cell_window(c))
                                    if _guid_available(c, G.GET_CURRENT_ROW) and not _cell_step(
                                            tc_get_current_row(tel['key'], tel['handle']),
                                            'The current row could not be read.')['row']:
                                        raise _CellEditFailure('No current row is available.', {'code': 'no_current_row'})
                                _obs['args'] = {'column': fields[0][0]} if fields else {}
                                tgl = (act.get('switchSelection') or act.get('toggleSelection') or 'false') == 'true'
                                mid = tc1c.mk_gotorow(fields=fields, toggle_selection=tgl,
                                                      direction=act.get('direction', 'down'))
                                r = _send(G.GOTO_ROW, tel['key'], kind='read', middle=mid,
                                               pad=(tc1c.GOTOROW_PAD if fields else None), handle=tel['handle'])
                                _step({'target': tname, 'action': 'gotoRow', 'ok': r['ok'], 'row': dict(fields)})
                            elif a == 'setOrder':
                                col = act.get('columnTitle') or act.get('column') or ''
                                ok = True
                                for kind in ('action', 'commit'):
                                    ok = _send(G.SET_ORDER, tel['key'], kind=kind, middle=tc1c.mk_order(col), handle=tel['handle'])['ok'] and ok
                                _step({'target': tname, 'action': 'setOrder', 'ok': ok})
                            elif a in ('gotoNextItem', 'gotoPreviousItem'):
                                res = _replay(tel['key'], tel['handle'], act)
                                _step({'target': tname, 'action': a, 'ok': bool(res), 'skipped': res is None})
                            elif a in ('goOneLevelDown', 'goOneLevelUp'):
                                guid = G.GO_ONE_LEVEL_DOWN if a == 'goOneLevelDown' else G.GO_ONE_LEVEL_UP
                                fields = _tree_criteria(c, tel['key'], _fields_of(act))
                                mid = tc1c.mk_tree_middle(RC, pairs=fields)
                                ok = _ac(guid, tel['key'], tel['handle'], mid,
                                         pad=tc1c.tree_row_pad(pairs=fields))
                                _step({'target': tname, 'action': a, 'ok': ok})
                            elif a == 'endEditRow':
                                cancel = (act.get('cancel', 'false') == 'true')
                                ok = True
                                for kind in ('action', 'commit'):
                                    mid = b'\xe2' if cancel else RS
                                    ok = _send(G.END_EDIT_ROW, tel['key'], kind=kind, middle=mid, handle=tel['handle'])['ok'] and ok
                                _step({'target': tname, 'action': 'endEditRow', 'ok': ok})
                            elif a == 'deleteRow':
                                selected_count = _table_prepare_delete(c, tel['key'], tel['handle'])
                                ok = _ac(G.DELETE_ROW, tel['key'], tel['handle'])
                                want = _confirm_of(act)          # None -> при записи не отвечали
                                if want is not None:
                                    try: _answer_confirm_dialog(c, want, max_wait=2.0)
                                    except Exception: pass
                                _step({'target': tname, 'action': 'deleteRow', 'ok': ok,
                                       'selected_rows': selected_count, 'deleted': None})
                                _t.sleep(0.4)
                            elif a == 'copyRow':
                                ok = _ac(G.COPY_ROW, tel['key'], tel['handle'])
                                want = _confirm_of(act)          # None -> при записи не отвечали
                                if want is not None:
                                    try: _answer_confirm_dialog(c, want, max_wait=2.0)
                                    except Exception: pass
                                _step({'target': tname, 'action': 'copyRow', 'ok': ok})
                                _t.sleep(0.4)
                            elif a == 'changeRow':
                                _step({'target': tname, 'action': 'changeRow', 'ok': _ac(G.CHANGE_ROW, tel['key'], tel['handle'])})
                            elif a in ('selectAllRows', 'deselectAllRows', 'selectRow', 'deselectRow'):
                                guid = {'selectAllRows': G.SELECT_ALL_ROWS, 'deselectAllRows': G.DESELECT_ALL_ROWS,
                                        'selectRow': G.SELECT_ROW, 'deselectRow': G.DESELECT_ROW}[a]
                                mid = RC if a in ('selectRow', 'deselectRow') else b''
                                _step({'target': tname, 'action': a,
                                              'ok': _ac(guid, tel['key'], tel['handle'], mid)})
                            elif a == 'switchRowDeleteMark':
                                ok = _ac(G.SWITCH_ROW_DELETE_MARK, tel['key'], tel['handle'])
                                want = _confirm_of(act)          # None -> при записи не отвечали
                                if want is not None:
                                    try: _answer_confirm_dialog(c, want, max_wait=3.0)
                                    except Exception: pass
                                _step({'target': tname, 'action': 'switchRowDeleteMark', 'ok': ok})
                                _t.sleep(0.4)
                            elif a in ('expand', 'collapse'):
                                fields = _tree_criteria(c, tel['key'], _fields_of(act))
                                # наблюдение адресуем ТОЙ ЖЕ строке, что и действие
                                _obs['args'] = {'row_pairs': fields} if fields else {}
                                if a == 'expand':
                                    # subordinates="true" -> развернуть вместе с подчинёнными строками
                                    base = b'\xe2\xcb\x55' if act.get('subordinates') == 'true' else b'\xe1\xcb\x55'
                                    mid = tc1c.mk_tree_middle(base, pairs=fields); guid = G.EXPAND_TABLE
                                else:
                                    mid = tc1c.mk_tree_middle(RC, pairs=fields); guid = G.COLLAPSE_TABLE
                                ok = _ac(guid, tel['key'], tel['handle'], mid, tc1c.tree_row_pad(pairs=fields))
                                _step({'target': tname, 'action': a, 'ok': ok})
                            elif a == 'FormField':
                                cn = act.get('name'); cell = cells.get(cn)
                                for cact in list(act):
                                    ca = cact.tag.split('}')[-1]
                                    if not cell:
                                        _step({'target': '%s/%s' % (tname, cn), 'action': ca, 'ok': False, 'error': 'column not found'}); continue
                                    res = _replay(cell['key'], cell['handle'], cact)
                                    _step({'target': '%s/%s' % (tname, cn), 'action': ca, 'ok': bool(res), 'skipped': res is None})
                            else:                          # прочие элементные действия над самой таблицей (view-status, write и т.п.)
                                res = _replay(tel['key'], tel['handle'], act)
                                _step({'target': tname, 'action': a, 'ok': bool(res), 'skipped': res is None})
                        except _CellEditFailure as e:
                            _step({'target': tname, 'action': a, **e.details, 'ok': False, 'error': str(e)})
                        except tc1c.OperationError as e:
                            _step({'target': tname, 'action': a, **e.result()})
                        except _TreeCriterionError as e:
                            _step({'target': tname, 'action': a, 'ok': False, 'code': e.code, 'error': str(e)})
                        except _VerErr as e:   # метод новее подключённой платформы
                            if (a == 'gotoRow' and isinstance(e.__cause__, tc1c.OperationError)
                                    and e.__cause__.status == 13 and _fields_of(act)):
                                _step({'target': tname, 'action': a, **_row_search_refusal(_fields_of(act))})
                            else:
                                _step({'target': tname, 'action': a, 'ok': False, 'error': str(e), **e.details})
                elif tag in FORM_ACTIONS:
                    try:
                        res = _replay_form(formobj, node)
                        _step({'target': '<form>', 'action': tag, 'ok': bool(res), 'skipped': res is None})
                    except _VerErr as e:
                        _step({'target': '<form>', 'action': tag, 'ok': False, 'error': str(e), **e.details})
                    _t.sleep(0.4)
                elif len(list(node)) > 0:            # контейнер (FormGroup, командная панель, страницы) — вглубь
                    _walk(node)
                else:
                    _step({'target': node.get('name') or tag, 'action': tag, 'ok': False, 'skipped': True})

        _walk(frm)

    def _window_action(node, tag):
        """Действие уровня окна/сессии из сценария."""
        if tag == 'Form':
            _process_form(node); _t.sleep(0.4)
        elif tag == 'close':
            result = _close_active_window(c, send=_send, native_only=node.get('native') == 'true')
            _step({'target': '<window>', 'action': 'close', **result})
            _t.sleep(0.8)
        elif tag in ('gotoNextWindow', 'gotoPreviousWindow', 'gotoStartPage'):
            guid = {'gotoNextWindow': G.GOTO_NEXT_WINDOW, 'gotoPreviousWindow': G.GOTO_PREVIOUS_WINDOW,
                    'gotoStartPage': G.GOTO_START_PAGE}[tag]
            ok = _ac(guid, _winkey(c), None)
            _state['window_key'] = None
            _step({'target': '<window>', 'action': tag, 'ok': ok})
            _t.sleep(0.8)
        elif tag == 'activateWindow':
            ok = _ac(G.ACTIVATE, _winkey(c), None)
            _step({'target': '<window>', 'action': tag, 'ok': ok})
        elif tag == 'closeUserMessagesPanel':
            wk = _winkey(c); ok = True
            for kind in ('action', 'commit'):
                ok = _send(G.CLOSE_USER_MESSAGES_PANEL, wk, kind=kind, middle=b'')['ok'] and ok
            _step({'target': '<window>', 'action': tag, 'ok': ok})
        elif tag == 'setFileDialogResult':
            want = node.get('result', 'true') != 'false'
            files = [f.get('name') for f in list(node)
                     if f.tag.split('}')[-1] == 'File' and f.get('name') is not None]
            mid = tc1c.mk_set_file_dialog_result(want, files or node.get('filename'),
                                                 int(node.get('filterIndex', '0') or 0))
            r = _send(G.SET_FILE_DIALOG_RESULT, None, kind='commit', middle=mid, pad=4)
            _step({'target': '<session>', 'action': tag, 'ok': r['ok']})
        elif tag == 'clearFileDialogResult':
            r = _send(G.CLEAR_FILE_DIALOG_RESULT, None, kind='commit', middle=b'')
            _step({'target': '<session>', 'action': tag, 'ok': r['ok']})
        elif tag == 'executeCommand':
            wk = _winkey(c); ok = True
            for kind in ('action', 'commit'):
                ok = _send(G.EXECUTE_COMMAND, wk, kind=kind, middle=tc1c.mk_command(node.get('command', '')))['ok'] and ok
            _state['window_key'] = None
            _step({'target': '<window>', 'action': 'executeCommand', 'ok': ok})
            _t.sleep(1.0)
        elif len(list(node)) == 0:
            _step({'target': '<window>', 'action': tag, 'ok': False, 'skipped': True})

    # обход по ОКНАМ (ClientApplicationWindow) в порядке документа: форма + закрытие окна
    windows = [w for w in root.iter() if w.tag.split('}')[-1] == 'ClientApplicationWindow']
    for w in windows:
        for node in list(w):
            tag = node.tag.split('}')[-1]
            try:
                _window_action(node, tag)
            except _VerErr as e:
                _step({'target': '<window>', 'action': tag, 'ok': False, 'error': str(e), **e.details})

    unsupported = sorted(set(s['action'] for s in steps if s.get('skipped')))
    played = [s for s in steps if not s.get('skipped')]
    return {'ok': all(s['ok'] for s in played) if played else False,
            'played': len(played), 'total': len(steps), 'unsupported': unsupported, 'steps': steps}

class _CellEditFailure(Exception):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


def _cell_step(result, message):
    if not result.get('ok'):
        raise _CellEditFailure(result.get('error') or message, result)
    return result


def _cell_flag(c, guid, key, handle):
    r = c.send_cmd(guid, key, handle=handle, kind='read', middle=RS)
    return _scalar(r, _bool_from_resp)


def _cell_ready(c, key, handle):
    # Composite edits require positive evidence even when optional target checks are off.
    for guid, expected, message in (
            (G.CURRENT_VISIBLE, True, 'the target is not visibly available'),
            (G.CURRENT_ENABLE, True, 'the target is not enabled'),
            (G.CURRENT_READONLY, False, 'the target is read-only or its editability is unknown')):
        if not _guid_available(c, guid):
            raise _CellEditFailure('this platform cannot verify whether the target can be edited')
        if _cell_flag(c, guid, key, handle) is not expected:
            raise _CellEditFailure(message)


def _table_edit_diagnostic(c, key, detail):
    """Explain a refused edit only when a page group's current page proves why."""
    if not _guid_available(c, G.GET_CURRENT_PAGE):
        return detail
    try:
        ancestors = []
        parent = _collection_parent(key)
        while parent and _key_class(parent) != 'ManagedForm':
            ancestors.append(parent)
            parent = _collection_parent(parent)
        for parent in reversed(ancestors):
            if _key_class(parent) != 'Group':
                continue
            page = _ref_live_object(c, parent)
            if not page or page.get('type') != 'Page':
                continue
            group_key = _collection_parent(parent)
            group = _ref_live_object(c, group_key)
            if not group or group.get('type') != 'Pages' or not group.get('handle'):
                continue
            r = c.send_cmd(G.GET_CURRENT_PAGE, group_key, kind='read', middle=RC, handle=group['handle'])
            current = _coll(r, group_key)
            if len(current) == 1 and current[0].get('key') and current[0]['key'] != parent:
                return dict(detail, code='inactive_page', page=page,
                            suggested_action='activate',
                            error='The table is on an inactive page. Activate the returned page, then retry.')
    except Exception as exc:
        # Diagnostics must retain the original refused operation and its status.
        return dict(detail, diagnostic_error=str(exc))
    return detail


def _cell_window(c, expected=None):
    key = _window(c).get('key')
    if not key or (expected is not None and key != expected):
        raise _CellEditFailure('the active window changed or is unavailable; inspect it before continuing')
    return key


def _cell_result(key, before, after, text, stage, error=None):
    verified = stage == 'done' and after is not None and after == text
    result = {'ok': verified and error is None, 'target': key, 'stage': stage,
              'verified': verified, 'value_before': before, 'value_after': after,
              'changed': before != after if before is not None and after is not None else None}
    if stage == 'done' and error is None and not verified and _numeric_equivalent(text, after):
        # The protocol exposes text, not the column's data type. Do not certify codes
        # or arbitrary text as a typed number merely because parsing succeeds.
        result.update(ok=True, verified=None, verification='numeric_equivalent',
                      message='Editing finished with an equivalent numeric representation; exact text is not verified.')
        return result
    if (stage == 'done' and error is None and result['changed'] is True
            and _possibly_rounded(text, after)):
        result.update(verified=None, edit_finished=True, code='value_verification_inconclusive',
                      verification='rounded_display',
                      error='Editing finished, but the displayed precision cannot confirm the requested value.',
                      message='The displayed number is consistent with rounding. Read a more precise representation '
                              'in the form before repeating input; the exact value may already be accepted.')
        return result
    if error or not verified:
        result['error'] = error or 'the cell text after editing does not match the requested text'
    return result


def _numeric_value(value):
    from decimal import Decimal, InvalidOperation
    if not isinstance(value, str):
        return None
    # No leading zero codes, exponents, currency, dates or arbitrary whitespace.
    pattern = r'[+-]?(?:0|[1-9]\d*|[1-9]\d{0,2}(?:[ \u00a0\u202f]\d{3})+)(?:[.,]\d+)?'
    if not re.fullmatch(pattern, value):
        return None
    try:
        return Decimal(re.sub(r'[ \u00a0\u202f]', '', value).replace(',', '.'))
    except InvalidOperation:
        return None


def _numeric_equivalent(wanted, actual):
    left, right = _numeric_value(wanted), _numeric_value(actual)
    return left is not None and right is not None and left == right


def _possibly_rounded(wanted, actual):
    # This is a reason to leave verification inconclusive, never proof of acceptance.
    from decimal import Decimal, localcontext, ROUND_HALF_UP, ROUND_HALF_EVEN
    left, right = _numeric_value(wanted), _numeric_value(actual)
    if left is None or right is None or left == right:
        return False
    exponent = right.as_tuple().exponent
    if exponent <= left.as_tuple().exponent:
        return False
    with localcontext() as ctx:
        ctx.prec = max(len(left.as_tuple().digits), len(right.as_tuple().digits)) + 2
        unit = Decimal(1).scaleb(exponent)
        return any(left.quantize(unit, rounding=mode) == right for mode in (ROUND_HALF_UP, ROUND_HALF_EVEN))


def _verify_empty_cell(c, key, handle, col, window):
    """Read the committed value behind conditional text, then cancel only our readback edit.

    Verification is excluded from both recording modes: replay needs the accepted input,
    not another edit/cancel cycle. Native fragments before and after it remain intact.
    """
    def current():
        _cell_window(c, window)
        r = tc_get_current_item(key, handle)
        if not (r.get('ok') and len(r.get('item', [])) == 1
                and r['item'][0].get('key') == col['key']):
            raise _CellEditFailure('The current editor changed during empty-cell verification.')

    current()
    if _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle) is not False:
        raise _CellEditFailure('The row must be out of edit mode before verifying its empty value.')
    mark = _native_composite_begin(c, 'verify_empty_cell')
    track, observations = c._track, _state.get('rec_obs')
    attempted = False
    try:
        c._track, _state['rec_obs'] = None, None
        attempted = True
        _cell_step(tc_change_row(key, handle), 'The cell could not be reopened for verification.')
        current()
        if _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle) is not True:
            raise _CellEditFailure('The cell editor is unavailable for verification.')
        r = tc_get_data_presentation(col['key'], col['handle'])
        return r.get('ok') and r.get('presentation') == ''
    finally:
        try:
            if attempted:
                try:
                    current()
                    mode = _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle)
                    if mode is True:
                        _cell_step(tc_end_edit_row(key, handle, cancel=True),
                                   'The verification edit could not be cancelled.')
                        current()
                        mode = _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle)
                    if mode is not False:
                        raise _CellEditFailure('The row edit mode is unavailable after verification.')
                except Exception as exc:
                    raise _CellEditFailure(
                        'Empty-cell verification could not restore the editor state; inspect the current window.',
                        {'code': 'verification_cleanup_failed', 'edit_mode': None}) from exc
        finally:
            c._track, _state['rec_obs'] = track, observations
            if mark is not None:
                _native_composite_end(c, mark, 'verify_empty_cell')


@_action('tc_table')
def tc_set_cell_text(key: str, column: str, text: str, handle: str) -> dict:
    """Set text in the current row's column (element name). Handles focus and row editing,
    then reads the result. Empty text clears the cell. Returns verified, changed and
    value_before/value_after (displayed text). verification=empty_value confirms an empty
    value behind display formatting. Numeric formatting can return verified=null with
    verification=numeric_equivalent. Continues an existing row edit, including a newly added row,
    and finishes it without discarding other cells' edits. If validation or reference selection
    keeps the row in edit mode, returns row_edit_pending with the current editor; continue
    there or cancel explicitly with end_edit_row(cancel=true). Rounded output can instead return
    value_verification_inconclusive: editing finished, but the exact value is not verified."""
    c = _need()
    stage, before, after, editing, window = 'check_target', None, None, False, None
    existing_edit = False
    redirected = None
    columns = []
    try:
        if _key_class(key) != 'Table':
            raise _CellEditFailure('key must address a table')
        if not column or not isinstance(text, str):
            raise _CellEditFailure('provide a column element name and text')
        window = _cell_window(c)
        _cell_ready(c, key, handle)
        if not all(_guid_available(c, g) for g in (G.CURRENT_MODE_IS_EDIT,
                                                  G.GET_CURRENT_ITEM, G.GET_CELL_TEXT)):
            raise _CellEditFailure('this platform cannot verify a complete row edit')
        mode = _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle)
        if mode is None:
            raise _CellEditFailure('the current row edit mode could not be determined')
        existing_edit = mode is True
        columns = _table_columns(c, key)
        cols = [o for o in columns if o.get('name') == column]
        if len(cols) != 1:
            raise _CellEditFailure('column is missing or ambiguous; use its element name')
        col = cols[0]
        if col.get('type') != 'InputField':
            raise _CellEditFailure('the column does not support text input')
        _cell_ready(c, col['key'], col['handle'])
        if _guid_available(c, G.GET_CURRENT_ROW):
            rows = c.send_cmd(G.GET_CURRENT_ROW, key, handle=handle, kind='read', middle=RC)
            if not rows.get('ok') or not _rows(rows):
                raise _CellEditFailure('the table has no readable current row')
        before = _obs_cell(c, key, handle, column)
        if before is None:
            raise _CellEditFailure('the current cell text could not be read')
        stage = 'focus'
        _cell_step(tc_activate(col['key'], col['handle']), 'the column could not be activated')
        _cell_window(c, window)
        current = tc_get_current_item(key, handle)
        if not current.get('ok') or not any(o.get('key') == col['key'] for o in current.get('item', [])):
            redirected = current.get('item', [])
            raise _CellEditFailure('the requested column did not become current')
        stage = 'begin_edit'
        # Activating another column can finish the old cell's edit. Inspect the actual
        # mode before starting a new one; ChangeRow can otherwise toggle an active edit.
        mode = _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle)
        if mode is False:
            _cell_step(tc_change_row(key, handle), 'row editing could not be started')
            mode = _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle)
        _cell_window(c, window)
        editing = mode is True
        if not editing:
            raise _CellEditFailure('The form did not allow this cell to enter editing.' if mode is False
                                   else 'The row edit mode could not be determined.',
                                   {'code': 'row_edit_refused' if mode is False else 'edit_state_unavailable',
                                    'edit_mode': mode})
        stage = 'input'
        _cell_step(tc_input_text(col['key'], text, col['handle']), 'text input failed')
        _cell_window(c, window)
        stage = 'finish_edit'
        _cell_step(tc_end_edit_row(key, handle), 'row editing could not be finished')
        _cell_window(c, window)
        if _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle) is not False:
            raise _CellEditFailure('the row is still in edit mode')
        editing = False
        stage = 'verify'
        _cell_ready(c, col['key'], col['handle'])
        after = _obs_cell(c, key, handle, column)
        if text == '' and after not in ('', None):
            stage = 'verify_empty'
            if _verify_empty_cell(c, key, handle, col, window):
                result = _cell_result(key, before, after, text, 'done')
                result.pop('error', None)
                result.update(ok=True, verified=True, verification='empty_value')
                return result
        return _cell_result(key, before, after, text, 'done')
    except _CellEditFailure as exc:
        result = _cell_result(key, before, after, text, stage, str(exc))
        for name in ('code', 'status_code', 'suggested_action', 'message', 'recovery', 'edit_mode'):
            if name in exc.details:
                result[name] = exc.details[name]
    except Exception as exc:
        result = _cell_result(key, before, after, text, stage, str(exc))
        if isinstance(exc, tc1c.OperationError):
            result.update(exc.result())
    if result.get('code') == 'row_edit_refused':
        result.update(requested_column=column, suggested_action='choose_row',
                      message='The current cell can be restricted by form rules even when its column is enabled. '
                              'Choosing the current cell may show an explanation or open its associated form; '
                              'then read get_user_message_texts. No text was entered.')
        try:
            _cell_window(c, window)
            after = _obs_cell(c, key, handle, column)
            result.update(value_after=after, changed=before != after if after is not None else None)
            if _guid_available(c, G.GET_USER_MESSAGE_TEXTS):
                messages = tc_get_user_message_texts()
                result['messages'] = messages.get('messages') if messages.get('ok') else None
                if result['messages']:
                    result['messages_scope'] = 'window'
                    result['message'] += ' Existing window messages may include earlier actions.'
        except Exception:
            pass  # Keep the original refusal when supplementary diagnostics are unavailable.
    if stage == 'input' and result.get('suggested_action') in ('start_choosing', 'set_cell_text'):
        # The composite result addresses the table, while choosing addresses its editor.
        result.update(suggested_action='get_current_item',
                      message='Text input was refused. Read the table\'s current editor; '
                              'if it selects an object, choose a value there.')
    if stage == 'finish_edit' and editing:
        try:
            _cell_window(c, window)
            if _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle) is True:
                current = tc_get_current_item(key, handle)
                result.update(code='row_edit_pending', edit_mode=True, edit_cancelled=False,
                              requested_column=column, current_item=current.get('item', []),
                              suggested_action='get_user_message_texts',
                              message='The row still needs validation or value selection. Input was left in edit mode. '
                                      'Inspect current_item and validation messages, then finish or explicitly cancel the row.')
                matches = [o for o in columns if len(current.get('item', [])) == 1
                           and o.get('key') == current['item'][0].get('key') and o.get('type') == 'InputField']
                if current.get('ok') and len(matches) == 1:
                    actual = matches[0]
                    _cell_ready(c, actual['key'], actual['handle'])
                    result['suggested_call'] = {'tool': 'tc_field', 'arguments': {
                        'action': 'start_choosing', 'key': actual['key'], 'handle': actual['handle']}}
                    result['message'] += ' If the current editor selects an object, use suggested_call to choose it.'
                return result
        except Exception:
            # A changed window or unreadable state must not be presented as a verified editor.
            if result.get('edit_mode') is True:
                return result
    if redirected is not None:
        result.update(code='column_focus_redirected' if redirected else 'current_column_unavailable',
                      requested_column=column,
                      current_item=redirected,
                      suggested_action='set_cell_text' if redirected else 'get_current_item',
                      message=('Inspect current_item and use its column name if it is the intended field. No text was entered.'
                                if redirected else 'The current column could not be read; no text was entered.'))
        # A focus change does not prove that two columns have the same meaning.
        # Offer an explicit retry only for one verified editable column of this table.
        if len(redirected) == 1:
            matches = [o for o in columns if o.get('key') == redirected[0].get('key')]
            if len(matches) == 1 and matches[0].get('type') == 'InputField':
                actual = matches[0]
                try:
                    _cell_window(c, window)
                    _cell_ready(c, actual['key'], actual['handle'])
                    if sum(o.get('name') == actual.get('name') for o in columns) == 1:
                        result['suggested_call'] = {'tool': 'tc_table', 'arguments': {
                            'action': 'set_cell_text', 'key': key, 'handle': handle,
                            'column': actual['name'], 'text': text}}
                        result['message'] = ('The form focused another column. No text was entered. '
                                             'Use suggested_call only if current_item is the intended column.')
                except Exception:
                    pass  # A stale, disabled or unreadable editor must not become a suggested write.
    if editing and not existing_edit:
        try:
            _cell_window(c, window)
            cancel = tc_end_edit_row(key, handle, cancel=True)
            result['edit_cancelled'] = bool(cancel.get('ok') and
                _cell_flag(c, G.CURRENT_MODE_IS_EDIT, key, handle) is False)
        except Exception:
            result['edit_cancelled'] = False
    elif existing_edit:
        # Cancelling here could delete a newly added row or discard another cell's input.
        result['edit_cancelled'] = False
    return result


@_action('tc_doc')
def tc_set_area_text(key: str, address: str, text: str, handle: str) -> dict:
    """Set a spreadsheet cell's text by address, e.g. R2C1. Selects the cell, starts and
    finishes editing, then reads the result. Empty text clears it. Returns verified,
    changed and value_before/value_after. Numeric formatting can return verified=null
    with verification=numeric_equivalent. Rounded output returns value_verification_inconclusive:
    editing finished, but the exact value is not verified. Requires an editable document."""
    c = _need()
    stage, before, after, editing, window = 'check_target', None, None, False, None
    try:
        if not re.fullmatch(r'R[1-9]\d*C[1-9]\d*', address or '') or not isinstance(text, str):
            raise _CellEditFailure('provide one cell address such as R2C1 and text')
        if _key_class(key) != 'EditField' or _kind_of(c, key) != 'SpreadsheetDocumentField':
            raise _CellEditFailure('key must address a spreadsheet-document field')
        if not all(_guid_available(c, g) for g in (G.GET_AREA_TEXT, G.GET_CURRENT_AREA_ADDRESS,
                                                  G.BEGIN_EDIT_CURRENT_AREA, G.END_EDIT_CURRENT_AREA)):
            raise _CellEditFailure('this platform cannot verify a complete spreadsheet edit')
        window = _cell_window(c)
        _cell_ready(c, key, handle)
        stage = 'select_cell'
        if key in getattr(c, '_pending_area_edits', set()):
            raise _CellEditFailure('finish or cancel the existing cell edit first')
        _cell_step(tc_set_current_area(key, address, handle), 'the cell could not be selected')
        _cell_window(c, window)
        actual = _cell_step(tc_get_current_area_address(key, handle), 'the selected address is unavailable')['address']
        if actual != address:
            # A single coordinate inside a merged cell selects its entire merged area.
            merged = tc_included_in_merged_area(key, address, handle) if _guid_available(c, G.INCLUDED_IN_MERGED_AREA) else {}
            if not merged.get('ok') or merged.get('merged_area') != actual:
                raise _CellEditFailure('the requested cell did not become current')
        observed, before = _observe_area(c, key, handle, {'area': actual})
        if before is None:
            raise _CellEditFailure('the cell text could not be read')
        if before == text:
            return _cell_result(key, before, before, text, 'done')
        stage = 'begin_edit'
        _cell_step(tc_begin_edit_current_area(key, handle), 'cell editing could not be started')
        _cell_window(c, window)
        _cell_ready(c, key, handle)
        editing = True
        stage = 'input'
        _cell_step(tc_input_text(key, text, handle), 'text input failed')
        _cell_window(c, window)
        stage = 'finish_edit'
        _cell_step(tc_end_edit_current_area(key, handle), 'cell editing could not be finished')
        _cell_window(c, window)
        editing = False
        stage = 'verify'
        _cell_ready(c, key, handle)
        _, after = _observe_area(c, key, handle, {'area': actual})
        return _cell_result(key, before, after, text, 'done')
    except _CellEditFailure as exc:
        result = _cell_result(key, before, after, text, stage, str(exc))
    except Exception as exc:
        result = _cell_result(key, before, after, text, stage, str(exc))
    if editing:
        try:
            _cell_window(c, window)
            result['edit_cancelled'] = tc_end_edit_current_area(key, handle, cancel=True).get('ok', False)
        except Exception:
            result['edit_cancelled'] = False
    return result


def _document_bounds(c, key, handle):
    sizes = []
    for guid in (G.GET_DOC_AREA_VERTICAL_SIZE, G.GET_DOC_AREA_HORIZONTAL_SIZE):
        r = c.send_cmd(guid, key, kind='read', middle=RS, handle=handle)
        size = _area_size(r['raw'], r['values']) if r.get('ok') else None
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError('the document data bounds could not be read')
        sizes.append(size)
    return tuple(sizes)


def _document_cell(address):
    m = re.fullmatch(r'R([1-9]\d*)C([1-9]\d*)', address or '')
    return tuple(map(int, m.groups())) if m else None


def _document_span(area):
    parts = (area or '').split(':')
    if len(parts) != 2:
        return None
    start, end = (_document_cell(p) for p in parts)
    if not start or not end or start[0] > end[0] or start[1] > end[1]:
        return None
    return start[0], start[1], end[0], end[1]


@_action('tc_doc')
def tc_read_document(key: str, handle: str, start_address: str = None, max_cells: int = 1000) -> dict:
    """Read nonempty spreadsheet cells as rows with cell addresses and merged-cell spans.
    Uses the document's data bounds. If complete=false, pass next_address as start_address
    to continue. max_cells limits positions scanned per call (1–10000)."""
    c = _need()
    result = {'ok': False, 'target': key, 'rows': [], 'complete': False, 'next_address': None,
              'scanned_cells': 0, 'nonempty_cells': 0}
    pos, width, height = None, None, None
    try:
        if isinstance(max_cells, bool) or not isinstance(max_cells, int) or not 1 <= max_cells <= 10000:
            raise ValueError('max_cells must be an integer from 1 to 10000')
        start = _document_cell(start_address) if start_address is not None else (1, 1)
        if start is None:
            raise ValueError('start_address must be a cell address such as R2C1')
        for guid in (G.GET_DOC_AREA_VERTICAL_SIZE, G.GET_DOC_AREA_HORIZONTAL_SIZE, G.GET_AREA_TEXT):
            if not _guid_available(c, guid):
                raise ValueError('reading a whole document requires 1C platform 8.3.13 or newer')
        if _key_class(key) != 'EditField' or _kind_of(c, key) != 'SpreadsheetDocumentField':
            raise ValueError('key must address a spreadsheet-document field')
        # A missing/hidden field must never look like a successfully read empty document.
        if _cell_flag(c, G.CURRENT_VISIBLE, key, handle) is not True:
            raise ValueError('the document field is not visibly available')
        height, width = _document_bounds(c, key, handle)
        result['dimensions'] = {'rows': height, 'columns': width}
        result['merged_cells_identified'] = _guid_available(c, G.INCLUDED_IN_MERGED_AREA)
        if not height or not width:
            if start_address is not None and start != (1, 1):
                raise ValueError('start_address is outside the document data bounds')
            result.update(ok=True, complete=True)
            return result
        if start[0] > height or start[1] > width:
            raise ValueError('start_address is outside the document data bounds')
        pos = (start[0] - 1) * width + start[1] - 1
        first, total = pos, height * width
        end = min(total, pos + max_cells)
        spans, rows = [], {}
        deadline = time.monotonic() + 10
        while pos < end:
            if pos > first and time.monotonic() >= deadline:
                break
            row, col = divmod(pos, width)
            row, col = row + 1, col + 1
            address = 'R%dC%d' % (row, col)
            if any(r1 <= row <= r2 and c1 <= col <= c2 for r1, c1, r2, c2 in spans):
                pos += 1
                result['scanned_cells'] += 1
                continue
            r = c.send_cmd(G.GET_AREA_TEXT, key, kind='read', middle=tc1c.mk_area(address), handle=handle)
            if not r.get('ok'):
                raise ValueError('could not read cell ' + address)
            value = _area_value(r['raw'], address, G.GET_AREA_TEXT)
            if value is not None and value != '':
                cell = {'address': address, 'text': value}
                if result['merged_cells_identified']:
                    merged, ok = _merged_area(c, key, handle, address)
                    if not ok:
                        raise ValueError('could not read the merged area of cell ' + address)
                    span = _document_span(merged)
                    if span and span[0] <= row <= span[2] and span[1] <= col <= span[3]:
                        spans.append(span)
                        row, col = span[:2]
                        cell.update(address='R%dC%d' % (row, col),
                                    row_span=span[2] - row + 1, column_span=span[3] - col + 1)
                # A continuation inside a merged cell does not repeat its earlier anchor.
                anchor = (row - 1) * width + col - 1
                if anchor >= first:
                    if row not in rows:
                        rows[row] = {'row': row, 'cells': []}
                        result['rows'].append(rows[row])
                    rows[row]['cells'].append(cell)
                    result['nonempty_cells'] += 1
            pos += 1
            result['scanned_cells'] += 1
        if _cell_flag(c, G.CURRENT_VISIBLE, key, handle) is not True:
            raise ValueError('the document became unavailable while reading')
        if _document_bounds(c, key, handle) != (height, width):
            result['restart_required'] = True
            raise ValueError('the document data bounds changed; read the document again')
        result.update(ok=True, complete=pos == total)
        if pos < total:
            result['next_address'] = 'R%dC%d' % (pos // width + 1, pos % width + 1)
        return result
    except Exception as exc:
        result['error'] = str(exc)
        if pos is not None and width and height and pos >= width * height:
            result['restart_required'] = True
        if pos is not None and width and not result.get('restart_required'):
            result['next_address'] = 'R%dC%d' % (pos // width + 1, pos % width + 1)
        return result


@_action('tc_field')
def tc_read_fields(targets: list[dict], properties: _batches.PropertyList = None) -> dict:
    """Read 1–100 fields of the active form without moving focus. properties defaults to ['text'];
    presentation reads data, edit_text reads the editing buffer. visible/enabled/readonly are
    optional. Table-column text reads the current row. Results follow input order (index is 0-based);
    unavailable properties remain null with a reason. Reads are sequential, not an atomic snapshot."""
    c, error = _need_ver('8.3.12')
    if error: return error
    return _batches.read_fields(sys.modules[__name__], targets, properties)


@_action('tc_field')
def tc_set_fields(entries: list[dict]) -> dict:
    """Fill 1–100 ordinary input fields of one active form in order; each entry includes text.
    Empty text clears. Stops on refusal, unfinished selection, a changed window or unconfirmed value;
    previous changes remain. Results use 0-based input indexes. Rechecks all values at the end;
    final_verified=null can mean equivalent numeric formatting. Does not save the document."""
    c, error = _need_ver('8.3.12')
    if error: return error
    return _batches.write(sys.modules[__name__], entries=entries)


@_action('tc_table')
def tc_set_row_values(key: str, handle: str, cells: _batches.CellEntries) -> dict:
    """Fill 1–100 input columns in the current row, using cells=[{column: element name, text: ...}].
    Finishes row editing once after entering all cells, then reads the values back. Continues an
    existing row edit. Stops on refusal or an unexpected editor; preserves earlier edits for explicit
    completion or cancellation. completed counts entered cells; edit_finished confirms row completion.
    Results follow input order (0-based index). final_verified=null can
    mean equivalent numeric formatting. Does not add rows, change selection or save the document."""
    c, error = _need_ver('8.3.12')
    if error: return error
    return _batches.write(sys.modules[__name__], key=key, handle=handle, cells=cells)


# ===================== публикация групп инструментов ========================
def _ver_tuple(s):
    try: return tuple(int(x) for x in str(s).split('.'))
    except Exception: return ()

# Целевая версия платформы (env TC_PLATFORM_VERSION, напр. '8.3.24.1548'): действия, чей метод в
# этой версии ещё не существует, не публикуются, и она же используется в рукопожатии.
_TARGET = os.environ.get('TC_PLATFORM_VERSION')
if _TARGET:
    tc1c.TestClient.DEFAULT_VER = _TARGET

_GROUP_DOC = {
    'tc_field':    'Actions on a form field, button, group or element addition. Choose `action`.',
    'tc_table':    'Read and edit table or tree rows, manage selection and expand or collapse nodes. Choose `action`.',
    'tc_doc':      'Actions on document fields and spreadsheet areas. Choose `action`.',
    'tc_calendar': 'Actions on a calendar field.',
    'tc_window':   'Actions on the client application window.',
    'tc_form':     'Actions on the managed form itself, including navigation between form elements '
                   'and reading the focused element. Choose `action`.',
    'tc_find':     'Search the UI tree for objects.',
    'tc_session':  'Connect to, launch or stop the 1C test client.',
    'tc_scenario': 'Record and replay UI scenarios.',
    'tc_app':      'Application-level state: active window, child objects, errors, dialogs, limits.',
}
_ACTION_GROUP = {a: g for g, acts in _ACTIONS.items() for a in acts}

# Операции, применимые к объектам любого типа (по справке клиента тестирования). Опубликованы в
# одной группе, поэтому остальным группам нужен явный указатель, где их искать.
_COMMON_ACTIONS = ('is_visible', 'is_enabled', 'get_context_menu', 'get_parent')


_PARAM_NOTE = ('\nIn the signatures below a trailing * marks a REQUIRED parameter — the '
               'group schema itself accepts every parameter as optional.')

_EFFECT_NOTE = ('\n`ok: true` means the client accepted the command, not that anything '
                'changed — confirm an effect by reading the state back; `target_check: present` '
                'is not proof of one either. Where the address can be checked the response '
                'carries `target_check`: `present`, `unknown` (not checkable here) or `off` '
                '(checking disabled). `target_hidden: true` appears ONLY when the target exists '
                'and was NOT visible; it does NOT prove the absence of an effect — for '
                'tc_field(action="activate") invisibility is the normal precondition — it '
                'describes the ELEMENT itself and not an invisible container around it, and its '
                'absence says nothing. A wrong address is refused with an error only where the '
                'address can be checked: a read without a target marker cannot tell one from an '
                'empty answer, and tc_table(action="get_cell_text") on a table that does '
                'not exist returns text=null exactly as for an empty cell.')



def _common_hint(group, acts, published):
    """Указатель на общие операции, опубликованные в других группах.

    `published` — карта «действие -> группа» по РЕАЛЬНО опубликованным действиям (после отсева по
    целевой версии платформы). Иначе описание рекламировало бы операцию, исключённую из enum, и
    следование указателю упиралось бы в ошибку схемы."""
    if not any('key' in inspect.signature(fn).parameters for fn in acts.values()):
        return ''                                  # группа не работает с объектами по адресу
    other = ['%s(action="%s")' % (published[a], a) for a in _COMMON_ACTIONS
             if published.get(a) and published[a] != group]
    return ('\nCommon operations for objects of this type live elsewhere: %s.' % ', '.join(other)
            if other else '')


def _public_parameters(fn):
    for p in inspect.signature(fn).parameters.values():
        if fn.__name__ in ('tc_read_fields', 'tc_set_fields') and p.name in ('targets', 'entries'):
            p = p.replace(annotation=_batches.public_type(p.name, _response.REF_MODE))
        if _response.REF_MODE == 'id':
            if p.name == 'handle':
                continue
            if p.name in ('key', 'root_key'):
                p = p.replace(name='ref' if p.name == 'key' else 'root_ref')
        yield p


# These operations need explicit address instructions; their common descriptions
# stay independent of the selected public address representation.
_ADDRESS_NOTES = {
    'read_fields': {
        'id': 'targets is an array of {ref} objects returned by discovery.',
        **dict.fromkeys(('prefix', 'off'), 'targets is an array of {key, handle} pairs returned by discovery.'),
    },
    'set_fields': {
        'id': 'entries is an array of {ref, text} objects; use discovered references unchanged.',
        **dict.fromkeys(('prefix', 'off'), 'entries is an array of {key, handle, text} objects; use discovered address pairs.'),
    },
    'get_child_objects': {
        'id': 'Address the parent with ref; children contain ref values. '
              'For a whole subtree, pass root_ref to tc_find(action="find_objects").',
        **dict.fromkeys(('prefix', 'off'),
            'Address the parent with key; children contain key and handle. '
            'Use the pair from the same object for subsequent actions that require both. '
            'For a whole subtree, pass root_key to tc_find(action="find_objects").'),
    },
    'get_parent': {
        'id': 'Address the element with ref; objects in parent contain their own ref values.',
        **dict.fromkeys(('prefix', 'off'),
            'Address the element with key and handle. Objects in parent contain their own key and handle; '
            'if the parent handle cannot be established, it is null.'),
    },
}


def _fmt_action(name, fn):
    """Строка действия: сигнатура, затем описание с отступом, версия платформы инлайном."""
    sp = []
    for p in _public_parameters(fn):
        # обязательный параметр помечается звёздочкой: в объединённой схеме группы все параметры
        # необязательны, и обязательность несёт только эта строка
        sp.append(p.name + '*' if p.default is inspect.Parameter.empty
                  else '%s=%s' % (p.name, 'null' if p.default is None else repr(p.default)))
    doc = inspect.getdoc(fn) or ''
    if _response.REF_MODE == 'id':
        doc = re.sub(r'\bkey/handle\b', 'ref', doc)
        doc = re.sub(r'\broot_key\b', 'root_ref', doc)
        doc = re.sub(r'\bkey\b', 'ref', doc)
        doc = doc.replace('{ref, handle}', '{ref}')
    if name in _ADDRESS_NOTES:
        doc += '\n' + _ADDRESS_NOTES[name][_response.REF_MODE]
    # ссылки на прежние имена инструментов -> пары «группа + действие»
    doc = re.sub(r'\btc_([a-z_]+)\b',
                 lambda m: ('%s(action="%s")' % (_ACTION_GROUP[m.group(1)], m.group(1))
                            if m.group(1) in _ACTION_GROUP else m.group(0)), doc)
    lines = [l.strip() for l in doc.splitlines() if l.strip()]
    minv = TOOL_MIN_VERSION.get('tc_' + name)
    if minv and _ver_tuple(minv) > (8, 3, 1):
        lines[-1] = (lines[-1] if lines else '') + ' (1C %s+)' % minv
    return '\n'.join(['- %s(%s)' % (name, ', '.join(sp))] + ['    ' + l for l in lines])


def _resolve_ref_arguments(fn, kw):
    """Resolve only declared target arguments; never interpret text or row data as references."""
    params = inspect.signature(fn).parameters
    registry = _refs.for_client(_state.get('client'))
    out = dict(kw)
    for public, internal in (('ref', 'key'), ('root_ref', 'root_key')):
        if public not in out:
            continue
        if internal not in params:
            raise _refs.RefError('invalid_ref_argument', public + ' is not accepted by this action')
        key, handle = registry.resolve(out.pop(public))
        # Exact membership check also protects reads against a form closed outside MCP.
        c = _state['client']
        track = getattr(c, '_track', None)
        try:
            c._track = None
            found = _ref_live_object(c, key)
            if found is None or (handle is not None and _collection_parent(key) is not None
                                 and found.get('handle') != handle):
                registry.discard(key)
                raise _refs.RefError('ref_unavailable', 'The element is no longer available. Find the element again.')
            handle = found.get('handle') or handle
            if internal == 'key' and 'handle' in params and handle is None:
                raise _refs.RefError('ref_unavailable', 'This object cannot be used for this action. Find its form or child elements.')
            registry.remember(key, handle)
        finally:
            c._track = track
        out[internal] = key
        if internal == 'key' and 'handle' in params:
            out['handle'] = handle
    return out


def _collection_parent(key):
    # Separators inside element names are not path separators.
    parts = re.findall(r'[A-Za-z][A-Za-z0-9]*(?:\[[^\[\]]*\])?', key)
    return '.'.join(parts[:-1]) if len(parts) > 1 else None


_DERIVED_REF_SOURCES = {
    'CI': G.GET_COMMAND_INTERFACE,
    'CommandPanel': G.GET_COMMAND_BAR,
    'ContextMenu': G.GET_CONTEXT_MENU,
    'MoxelEditField': G.GET_CURRENT_AREA_FIELD,
}


def _ref_live_object(c, key):
    """Re-read the exact object through its native source, including non-tree subobjects."""
    parent = _collection_parent(key)
    if not parent:
        r = c.send_cmd(G.GET_CHILD_OBJECTS, None, kind='read', middle=CHILD_MIDDLE)
        windows = tc1c.decode_collection(r['raw'], None) if r.get('ok') else []
        return next((w for w in windows if w.get('key') == key), None)
    # Named groups/fields use ordinary enumeration even if their names contain dots.
    suffix = key[len(parent) + 1:]
    source = _DERIVED_REF_SOURCES.get(suffix)
    if source:
        owner = _ref_live_object(c, parent)
        if owner is None or (suffix != 'CI' and not owner.get('handle')):
            return None
        if not _guid_available(c, source):
            return None
        r = c.send_cmd(source, parent, kind='read', middle=RC, handle=owner.get('handle'))
    else:
        r = c.send_cmd(G.GET_CHILD_OBJECTS, parent, kind='read', middle=CHILD_MIDDLE)
    candidates = tc1c.decode_collection(r['raw'], parent) if r.get('ok') else []
    return next((o for o in candidates if o.get('key') == key), None)


def _dispatch_connected_action(acts, group, kw, connection_id=None):
    name = kw.pop('action', None)
    fn = acts.get(name)
    if fn is None:
        raise ValueError('%s: unknown action %r. Available: %s' % (group, name, ', '.join(sorted(acts))))
    kw = {k: v for k, v in kw.items() if v is not None}
    try:
        if _response.REF_MODE == 'id':
            if any(k in kw for k in ('key', 'handle', 'root_key')):
                raise _refs.RefError('invalid_ref_argument', 'Use ref or root_ref returned by the tools.')
            if 'ref' in kw or 'root_ref' in kw:
                kw = _resolve_ref_arguments(fn, kw)
        else:
            _response.check_ref_args(kw)
        if name in _batches.FIELD_ACTIONS:
            kw = _batches.resolve_arguments(sys.modules[__name__], name, kw)
        try:
            bound = inspect.signature(fn).bind(**kw)
        except TypeError as e:
            message = str(e)
            if _response.REF_MODE == 'id':
                message = message.replace("'key'", "'ref'").replace("'root_key'", "'root_ref'")
            raise ValueError('%s(action="%s"): %s' % (group, name, message))
        bound.apply_defaults()
        res = fn(*bound.args, **bound.kwargs)
        if isinstance(res, _screenshots.Screenshot):
            metadata = dict(res.metadata)
            if connection_id is not None:
                metadata['connection_id'] = connection_id
            message = _response.respond(metadata)
            if not isinstance(message, str):
                message = json.dumps(message, ensure_ascii=False)
            return CallToolResult(content=[TextContent(type='text', text=message),
                ImageContent(type='image', mimeType='image/png', data=base64.b64encode(res.png).decode('ascii'))],
                structuredContent=metadata)
        if _response.REF_MODE == 'id' and _state.get('client') is not None:
            res = _refs.present(res, name, _refs.for_client(_state['client']))
    except _refs.RefError as exc:
        res = {'ok': False, 'code': exc.code, 'error': str(exc)}
    except _batches.Failure as exc:
        res = {'ok': False, **_batches.error(exc)}
    except tc1c.OperationError as exc:
        res = exc.result()
        if _response.REF_MODE == 'id':
            res.pop('target', None)
    if connection_id is not None:
        if not isinstance(res, dict):
            res = {'ok': True, 'message': res}
        res = {**res, 'connection_id': connection_id}
        if name in ('set_cell_text', 'get_cell_text') and isinstance(res.get('suggested_call'), dict):
            res['suggested_call']['arguments']['connection_id'] = connection_id
    return _response.respond(res, addrs=('tc_' + name) in _response.ADDR_TOOLS)


def _dispatch_action(acts, group, kw):
    kw = dict(kw)
    name = kw.get('action')
    connection_id = kw.pop('connection_id', None)
    connection = None
    try:
        if name not in acts:
            return _dispatch_connected_action(acts, group, kw)
        if name == 'list_connections':
            if connection_id is not None:
                raise _connections.ConnectionError('invalid_connection_id', 'list_connections lists all connections; omit connection_id.')
            return _dispatch_connected_action(acts, group, kw)
        if name in ('connect', 'launch_client'):
            args = {k: v for k, v in kw.items() if k != 'action' and v is not None}
            bound = inspect.signature(acts[name]).bind(**args)
            bound.apply_defaults()
            params = bound.arguments
            launching = name == 'launch_client'
            connection = _pool.reserve('127.0.0.1' if launching else params['host'], params['port'],
                launch=launching, connection_id=connection_id,
                base=params.get('base'), user=params.get('user'))
            kw['port'] = connection.port
        else:
            refs = [kw[k] for k in ('ref', 'root_ref') if kw.get(k) is not None]
            refs.extend(_batches.refs(name, kw))
            connection = _pool.select(connection_id, refs, stop=name == 'stop_client')
        with _pool.use(connection):
            try:
                return _dispatch_connected_action(acts, group, kw, connection.id if connection else None)
            except Exception as exc:
                if name not in ('connect', 'launch_client'): raise
                return _response.respond({'ok': False, 'code': 'connection_failed', 'error': str(exc),
                                          'connection_id': connection.id})
            finally:
                if name in ('connect', 'launch_client', 'disconnect', 'stop_client'):
                    _pool.finish(connection)
    except _connections.ConnectionError as exc:
        result = {'ok': False, 'code': exc.code, 'error': str(exc)}
        if exc.code in ('connection_required', 'connection_not_found', 'not_connected'):
            result['connections'] = _pool.list()
        return _response.respond(result)


def _register_groups():
    tv = _ver_tuple(_TARGET) if _TARGET else None
    # сначала отбираем ВСЕ публикуемые действия: и enum, и указатели на общие операции должны
    # строиться от одного и того же набора, иначе описание сошлётся на исключённое действие
    published = {}
    for group, actions in _ACTIONS.items():
        acts = {a: fn for a, fn in actions.items()
                if not (tv and _ver_tuple(TOOL_MIN_VERSION.get('tc_' + a, '')) > tv)
                and (a != 'get_screenshot' or SCREENSHOTS)}
        if acts:
            published[group] = acts        # группа без доступных действий не публикуется вовсе
    pub_group = {a: g for g, acts in published.items() for a in acts}

    for group, acts in published.items():
        # перечень допустимых действий уходит в СХЕМУ: клиент видит их до вызова, а недоступные
        # в целевой версии платформы в него не попадают
        params = [inspect.Parameter('action', inspect.Parameter.KEYWORD_ONLY,
                                    annotation=typing.Literal[tuple(sorted(acts))]),
                  inspect.Parameter('connection_id', inspect.Parameter.KEYWORD_ONLY,
                                    default=None, annotation=str)]
        seen = set()
        for fn in acts.values():
            for p in _public_parameters(fn):
                if p.name not in seen:
                    seen.add(p.name)
                    params.append(inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY,
                                                    default=None, annotation=p.annotation))
        notes = _PARAM_NOTE + (_EFFECT_NOTE if set(acts) & _VERIFY_ACTIONS else '')
        desc = '%s%s%s\nActions:\n%s' % (_GROUP_DOC.get(group, group),
                                         _common_hint(group, acts, pub_group), notes,
                                         '\n'.join(_fmt_action(a, acts[a]) for a in sorted(acts)))
        desc += '\nconnection_id selects the client. '
        ref_params = [name for name in ('ref', 'root_ref') if name in seen]
        if _response.REF_MODE == 'id' and ref_params:
            desc += ('Passing %s selects the client automatically; otherwise, with several clients, '
                     'connection_id is required. ' % '/'.join(ref_params))
            if set(acts) & _batches.FIELD_ACTIONS.keys():
                desc += 'References in targets/entries also select the client; all must belong to one connection. '
        else:
            desc += 'With several clients, connection_id is required. '
        desc += 'Use tc_session(action="list_connections").'
        if _response.COMPACT and any(('tc_' + a) in _response.ADDR_TOOLS for a in acts):
            desc += _response.HINT
        if _response.REF_MODE == 'id' and (ref_params or
                any(a in _refs.OBJECT_SLOTS or a == 'get_active_window' for a in acts)):
            desc += ('\nPass reference values returned by the tools unchanged in %s. ' % '/'.join(ref_params)
                     if ref_params else '\nReturned references can be passed unchanged to actions that accept them. ')
            desc += 'If a reference expires, find the element again.'

        async def dispatch(_acts=acts, _group=group, **kw):
            return await anyio.to_thread.run_sync(functools.partial(_dispatch_action, _acts, _group, kw))

        dispatch.__signature__ = inspect.Signature(params)
        dispatch.__name__ = group
        dispatch.__doc__ = desc
        mcp.tool(name=group, description=desc)(dispatch)
        if _response.REF_MODE == 'id':
            # Pydantic otherwise silently drops key/handle arguments in ID mode before dispatch,
            # potentially executing an action despite a contradictory key/handle supplied.
            tool = mcp._tool_manager._tools[group]
            model = tool.fn_metadata.arg_model
            model.model_config['extra'] = 'forbid'
            model.model_rebuild(force=True)
            tool.parameters = model.model_json_schema()


_register_groups()

def main():
    # транспорт задаётся окружением: TC1C_TRANSPORT = stdio (по умолч.) | streamable-http | sse
    t = os.environ.get('TC1C_TRANSPORT', 'stdio').strip().lower().replace('_', '-')
    try:
        if t in ('http', 'streamable-http'):
            mcp.run(transport='streamable-http')
        elif t == 'sse':
            mcp.run(transport='sse')
        else:
            mcp.run()
    finally:
        _pool.close()

if __name__ == '__main__':
    main()
