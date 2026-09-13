# -*- coding: utf-8 -*-
"""tc1c — клиент тест-клиента 1С:Предприятие напрямую, без тест-менеджера.

Пайплайн: intro -> при необходимости NTLM -> сессия -> бинарные команды.
Кадры собираются программно (build_frame): голова + ключ + хвост, подстановка GUID сессии.
"""
import socket, re, base64, uuid, struct, time, os
if os.name == 'nt':
    import sspi, sspicon, win32security
else:
    sspi = sspicon = win32security = None
from _collection import decode_collection, object_handles

TR = bytes.fromhex('6653b2a6')
TOK_GUID = b'671507fd-50a9-4b63-b70e-58b3d364f48f'
SESS_BLOCK = b'3ace6d91-51bb-4344-9388-c8105ad4ad11'
NETWORK_GREETING = bytes.fromhex('53f5c61a7b')

# ---------- кодек значений ----------
def enc_int(n):
    if 0 <= n <= 0xff:   return b'\x8b' + struct.pack('<B', n)
    if 0 <= n <= 0xffff: return b'\x8d' + struct.pack('<H', n)
    return b'\x8f' + struct.pack('<i', n)

import datetime as _dt
def enc_date(y, m, d, hh=0, mm=0, ss=0):
    """Дата -> тег f1 + int64 LE (секунды от 0001-01-01)*10000."""
    secs = int((_dt.datetime(y,m,d,hh,mm,ss) - _dt.datetime(1,1,1)).total_seconds())
    return b'\xf1' + (secs*10000).to_bytes(8,'little')
def dec_date(b8):
    """8 байт LE (после тега f1) -> datetime."""
    v = int.from_bytes(b8,'little')//10000
    return _dt.datetime(1,1,1)+_dt.timedelta(seconds=v)
def enc_str(s):
    b = s.encode('utf-16le')       # длина — в кодовых единицах UTF-16, а не в символах
    return b'\xf7' + bytes([len(b) // 2]) + b
def enc_ascii(s): b=s.encode('latin1'); return b'\xda'+bytes([len(b)])+b

def value_size(b, i=0):
    """Сколько байт занимает закодированное значение в кадре, начиная с позиции i.
    Строка: тег + длина + данные, где длина в БАЙТАХ у ASCII (_a len8 / _b len16) и в
    КОДОВЫХ ЕДИНИЦАХ у UTF-16 (_7 len8 / _8 len16, по 2 байта на единицу). Число: 8b/8d/8f,
    дата: f1 + 8. 0 — тег не распознан. Без этого сдвиг курсора по кадру «на глаз» теряет
    следующие элементы коллекции."""
    if i >= len(b):
        return 0
    t = b[i]
    if t == 0x8b: return 2
    if t == 0x8d: return 3
    if t == 0x8f: return 5
    if t == 0xf1: return 9
    low = t & 0x0f
    if t >= 0x90 and low == 0x0a: return 2 + b[i+1]
    if t >= 0x90 and low == 0x07: return 2 + 2 * b[i+1]
    if t >= 0x90 and low == 0x0b: return 3 + int.from_bytes(b[i+1:i+3], 'little')
    if t >= 0x90 and low == 0x08: return 3 + 2 * int.from_bytes(b[i+1:i+3], 'little')
    return 0

def _plausible_str(s):
    """Правдоподобная строка 1С: почти нет символов CJK-диапазона (>=0x2000). Много таких =
    ложный захват тега строки в бинарных данных (даёт сдвиг потока и «иероглифы»)."""
    if not s:
        return True
    return sum(1 for ch in s if ord(ch) >= 0x2000) <= len(s) * 0.2

def decode_stream(b, start=0):
    i=start; out=[]
    while i < len(b):
        t=b[i]
        if t>=0x90 and (t&0x0f)==0x0a and i+1<len(b):      # ASCII1: 9a/ba/da/fa (len8)
            n=b[i+1]; out.append(('str', b[i+2:i+2+n].decode('latin1','replace'))); i+=2+n
        elif t>=0x90 and (t&0x0f)==0x0b and i+2<len(b):     # ASCII2: 9b/bb/db/fb (len16 LE)
            n=struct.unpack_from('<H',b,i+1)[0]; out.append(('str', b[i+3:i+3+n].decode('latin1','replace'))); i+=3+n
        elif t>=0x90 and (t&0x0f)==0x07 and i+1<len(b):     # UTF-16: 97/b7/d7/f7 (len8)
            n=b[i+1]; s=b[i+2:i+2+n*2].decode('utf-16le','replace')
            if _plausible_str(s): out.append(('ustr', s)); i+=2+n*2
            else: out.append(('raw','%02x'%t)); i+=1     # ложный захват тега в бинарных данных
        elif t>=0x90 and (t&0x0f)==0x08 and i+2<len(b):     # UTF-16 длинная: _8 (len16 LE, предв.)
            n=struct.unpack_from('<H',b,i+1)[0]; s=b[i+3:i+3+n*2].decode('utf-16le','replace')
            if _plausible_str(s): out.append(('ustr', s)); i+=3+n*2
            else: out.append(('raw','%02x'%t)); i+=1
        elif t==0x8b and i+1<len(b): out.append(('int', b[i+1])); i+=2
        elif t==0x8d and i+2<len(b): out.append(('int', struct.unpack_from('<H',b,i+1)[0])); i+=3
        elif t==0x8f and i+4<len(b): out.append(('int', struct.unpack_from('<i',b,i+1)[0])); i+=5
        elif t==0xf1 and i+8<len(b):                        # дата: f1 + int64 LE (сек*10000)
            v=int.from_bytes(b[i+1:i+9],'little')//10000
            try:
                import datetime as _d; out.append(('date', str(_d.datetime(1,1,1)+_d.timedelta(seconds=v))))
            except Exception: out.append(('date', v))
            i+=9
        else: out.append(('raw','%02x'%t)); i+=1
    return out

# ---------- программный сборщик кадров ----------
OPCODE_GUID = '102301e1-f311-4cbb-acb2-9dbfa0aeb4bd'
FIX_HEAD = b'\x81\x84\x83\x81\xcb\x53\x81'   # FIX-блок, далее байт a3(действие)/a1(коммит) + cb 23 95

def _enc_key(key):
    """Ключ-получатель, семейство 9_: ASCII (9a<256 / 9b>=256) если строка latin1
    (ключи окон MainFrame[guid]/SecondaryFrame[guid]), иначе UTF-16 (97<256 / 98>=256)
    для ключей с кириллицей (EditField[ПолеТекст] и т.п.)."""
    return _enc_like(0x90, key)

def _percall_guid(per_call):
    """GUID получателя кадра (handle объекта). Мусор ловим здесь: дальше стоит uuid.UUID, и он
    падал ValueError'ом, из которого не видно, какой параметр неверен."""
    if per_call is None:
        return uuid.UUID(str(uuid.uuid4())).bytes_le
    try:
        return uuid.UUID(str(per_call)).bytes_le
    except (ValueError, AttributeError, TypeError):
        raise ValueError('handle: expected the object handle (a GUID) returned by find_objects, '
                         'got %r' % (per_call,))

def build_command(session, counter, method_guid, key, per_call=None, commit=False, result='scalar'):
    """Собрать бинарный командный кадр БЕЗ аргументов (read/no-arg методы).
    Для методов с аргументами обрамление значения метод-специфично (mk_* хелперы).
      session   - GUID сессии (из connect)
      counter   - счётчик команды (int16 LE), инкрементируется
      method_guid - GUID метода из реестра
      commit    - False: кадр действия (хвост 88..a3-контекст); True: коммит (2-й кадр state-команд)
    """
    import struct as _s
    out = bytearray(b'\x41\x95')
    out += uuid.UUID(session).bytes_le
    out += b'\x8d' + _s.pack('<H', counter & 0xffff)
    out += b'\x81\x85\x95'
    out += uuid.UUID(OPCODE_GUID).bytes_le
    out += FIX_HEAD + (b'\xa1' if commit else b'\xa3') + b'\xcb\x23\x95'
    out += uuid.UUID(method_guid).bytes_le
    out += b'\xd5'
    out += _percall_guid(per_call)
    out += _enc_key(key)
    # хвост зависит от типа результата:
    #   'scalar'     — скаляр/булево: 88 81 81 e1 + 3×0x20 (CurrentVisible/Enable/ReadOnly, ToolTip)
    #   'collection' — коллекция: 88 81 81 e0 4b 55 + 4×0x20 (GetContextMenu, GetSelectedRows)
    lead = b'\x81' if commit else b'\x88'
    if result == 'collection':
        out += lead + b'\x81\x81\xe0\x4b\x55\x20\x20\x20\x20'
    else:
        out += lead + b'\x81\x81\xe1\x20\x20\x20'
    out += TR
    return bytes(out)

# ---------- обобщённый программный сборщик кадров ----------
# lead (3 байта) и байт действия (a3/a1) — ДВЕ независимые оси:
#   read     lead 88 81 81, act a3  — геттер без побочного эффекта
#   action   lead 88 82 81, act a3  — мутатор/навигация/ввод (с аргументом или без)
#   read818  lead 81 81 81, act a3  — спец-геттер (GetChoiceListPresentation и родств.)
#   commit   lead 81 81 81, act a1  — 2-й кадр state-команды (коммит)
LEAD = {'read': b'\x88\x81\x81', 'action': b'\x88\x82\x81',
        'read818': b'\x81\x81\x81', 'commit': b'\x81\x81\x81',
        'wait': b'\x88\x81'}   # WaitForDropListGeneration: 2-байтовый lead + int-таймаут в middle

def mk_wait(timeout):
    """Аргумент WaitForDropListGeneration: int8/int16 таймаут + маркер результата e1.
    Идёт после 2-байтового lead 'wait' (88 81): 8b<t8>e1 (<256) / 8d<t16>e1 (иначе)."""
    t = int(timeout) & 0xffff
    return (b'\x8b' + bytes([t]) if t < 256 else b'\x8d' + struct.pack('<H', t)) + b'\xe1'

def _pad_len(middle):
    """Число хвостовых 0x20: 4 при коллекции/списке (e0 4b ..) или суффиксе cb 55, иначе 3."""
    if b'\x4b\x53' in middle or b'\x4b\x55' in middle or b'\x4b\x4e' in middle \
       or middle.endswith(b'\xcb\x55'):
        return 4
    return 3

def build_frame(session, counter, method_guid, key, kind='read', middle=b'', per_call=None, pad=None):
    """Собрать любой командный кадр программно (голова+ключ+хвост).
      kind   - 'read' (геттер 88 81 81 a3) | 'action' (мутатор 88 82 81 a3)
               | 'read818' (спец-геттер 81 81 81 a3) | 'commit' (коммит 81 81 81 a1)
      middle - байты между lead и хвостовыми 0x20: маркер результата (e1/e0 4b 55/e2/cb 55…)
               и/или закодированный аргумент (см. mk_* ниже). Для no-arg read: b'\\xe1' или
               b'\\xe0\\x4b\\x55'. Пусто (b'') = действие без результата/аргумента.
      pad    - переопределить число хвостовых 0x20 (по умолчанию по _pad_len).
    """
    import struct as _s
    commit = (kind == 'commit')
    out = bytearray(b'\x41\x95')
    out += uuid.UUID(session).bytes_le
    # счётчик: натуральная ширина — 8b+int8 (<256) / 8d+int16 (иначе)
    c = counter & 0xffff
    out += (b'\x8b' + bytes([c])) if c < 256 else (b'\x8d' + _s.pack('<H', c))
    out += b'\x81\x85\x95'
    out += uuid.UUID(OPCODE_GUID).bytes_le
    out += FIX_HEAD + (b'\xa1' if commit else b'\xa3') + b'\xcb\x23\x95'
    out += uuid.UUID(method_guid).bytes_le
    out += b'\xd5'
    out += _percall_guid(per_call)
    out += _enc_key(key) if key is not None else b'\x81'   # key=None -> nil-получатель (сессионные команды)
    out += LEAD[kind]
    out += middle
    out += b'\x20' * (pad if pad is not None else _pad_len(middle))
    out += TR
    return bytes(out)

# --- маркеры результата и обёртки аргументов (middle-байты) ---
RES_SCALAR     = b'\xe1'              # скаляр/булево
RES_COLLECTION = b'\xe0\x4b\x55'      # коллекция (ContextMenu, SelectedRows, дети…)
RES_E2         = b'\xe2'              # спец-скаляр (ЗаголовокОтображается, GotoNextItem)
RES_HTML       = b'\xcb\x55'          # суффикс чтения документа (HTML/область)

def mk_input_text(value):
    """Аргумент ВвестиТекст: e0 41 81 81 + значение семейства b_ (ba ASCII / b7 UTF-16)."""
    return b'\xe0\x41\x81\x81' + _enc_like(0xb0, value)

def mk_choice(value):
    """Аргумент выбора из списка/меню/выпадающего (тип аргумента «Строка, Число»):
    строка = ПРЕДСТАВЛЕНИЕ -> e0 4b 53 + значение семейства 9_ (9a ASCII / 97 UTF-16);
    число  = ИНДЕКС (с 0)  -> mk_choice_index (маркер e0 4b 4e + байт 0x81+индекс;
    маркер 4e = число, 53 = строка)."""
    if not isinstance(value, str):
        return mk_choice_index(value)
    return b'\xe0\x4b\x53' + _enc_like(0x90, value)

def mk_goto_value(value):
    """Аргумент ПерейтиКЗначению (полоса регулирования): e0 4b 4e + число (проценты 0..100).
    Маркер 4e (не 53 как у mk_choice)."""
    return b'\xe0\x4b\x4e' + (enc_int(int(value)) if not isinstance(value, str) else _enc_like(0x90, value))

def mk_choice_index(index):
    """Аргумент ВыполнитьВыборИзСписка/ИзМеню (модальный диалог на ТестируемойФорме):
    e0 4b 4e + один байт (0x81 + индекс). Индекс 0 -> 0x81, 1 -> 0x82, ...
    Начиная с 127 малый байт переполняется — тогда число явным int (8b/8d/8f), как в
    ПерейтиКЗначению (тот же маркер e0 4b 4e)."""
    i = int(index)
    return b'\xe0\x4b\x4e' + (bytes([0x81 + i]) if 0 <= i <= 126 else enc_int(i))

# Типы значений в аргументе «Вложения» ВвестиHTMLДокумента (GUID типов платформы).
_STRUCT_TYPE  = uuid.UUID('4238019d-7e49-4fc9-91db-b6b951d5cf8e').bytes_le
_PICTURE_TYPE = uuid.UUID('87126200-3e98-44e0-b931-ccb1d7edc497').bytes_le
HTML_ATT_PAD = 9        # хвостовых 0x20 у кадра ВвестиHTMLДокумента со вложениями

def _enc_bytes(hi, b):
    """Двоичное значение в контейнере строки семейства hi: _a + len8 / _b + len16 LE."""
    if len(b) < 256:
        return bytes([hi | 0x0a, len(b)]) + b
    return bytes([hi | 0x0b]) + len(b).to_bytes(2, 'little') + b

def mk_attachments(attachments):
    """Аргумент «Вложения» (Структура {имя: Картинка}):
    23 95 + type-GUID структуры + <0xc1+число> + пары. Пара: <c0|20 20 20 e0> 4b 53 +
    имя(9_) + eb 23 95 + type-GUID картинки + c2 c1 + байты картинки (9a/9b по длине)."""
    out = b'\x23\x95' + _STRUCT_TYPE + bytes([0xc1 + len(attachments)])
    for i, (name, data) in enumerate(attachments.items()):
        out += (b'\x20\x20\x20\xe0' if i else b'\xc0') + b'\x4b\x53'
        out += _enc_like(0x90, name)
        out += b'\xeb\x23\x95' + _PICTURE_TYPE + b'\xc2\xc1' + _enc_bytes(0x90, bytes(data))
    return out

def mk_html(value, attachments=None):
    """Аргумент ВвестиHTML(Документа): значение семейства f_ (fa ASCII / f7 UTF-16) + cb 55.
    attachments ({имя: bytes картинки}) ставится ВМЕСТО хвостового 55 (как ОписаниеСтроки
    в tree-методах); при этом кадру нужен pad=HTML_ATT_PAD."""
    body = _enc_like(0xf0, value)
    if not attachments:
        return body + b'\xcb\x55'
    return body + b'\xcb' + mk_attachments(attachments)

def dec_attachments(middle):
    """Обратный разбор вложений из middle -> {имя: bytes}; {} если их там нет."""
    p = middle.find(b'\xcb\x23\x95' + _STRUCT_TYPE)
    if p < 0:
        return {}
    out = {}
    i = p + 3 + len(_STRUCT_TYPE) + 1                    # + байт числа элементов
    mark = b'\x4b\x53'
    while True:
        q = middle.find(mark, i)
        if q < 0:
            break
        j = q + 2
        tag = middle[j]; ln = middle[j + 1]
        name = middle[j + 2:j + 2 + ln].decode('latin1', 'replace') if (tag & 0x0f) == 0x0a \
            else middle[j + 2:j + 2 + ln * 2].decode('utf-16le', 'replace')
        j += value_size(middle, j)
        v = middle.find(b'\xc2\xc1', j)
        if v < 0:
            break
        t = middle[v + 2]
        if (t & 0x0f) == 0x0a:
            n = middle[v + 3]; data = middle[v + 4:v + 4 + n]; i = v + 4 + n
        else:
            n = int.from_bytes(middle[v + 3:v + 5], 'little'); data = middle[v + 5:v + 5 + n]; i = v + 5 + n
        out[name] = data
    return out

def mk_date(year, month, day):
    """Аргумент ПерейтиКДате: f1 + int64 (см. enc_date)."""
    return enc_date(year, month, day)

def mk_order(column):
    """Аргумент УстановитьПорядок: значение семейства f_ (f7 UTF-16 имя колонки)."""
    return _enc_like(0xf0, column)

def mk_area(addr, marker=b'\x81'):
    """Аргумент табличного документа: fa ASCII адрес (R1C1) + маркер 81/82."""
    return _enc_like(0xf0, addr) + marker

def mk_command(name):
    """Аргумент ExecuteCommand: имя команды семейства f_ (f7 UTF-16 / fa ASCII напрямую)."""
    return _enc_like(0xf0, name)

def mk_cell(column):
    """Аргумент GetCellText: e0 4b 55 eb + значение.

    Платформа принимает ИМЯ ЭЛЕМЕНТА либо ИНДЕКС, и это разные типы в кадре:
      строка -> тег 53 + семейство 9_ (97 UTF-16 / 9a ASCII)
      число  -> тег 4e + 0x81+N для малых значений, иначе enc_int
    Тот же маркер числа используют mk_choice_index и mk_goto_value."""
    if isinstance(column, bool):
        raise TypeError('column: expected an element name or an index, got a boolean')
    if isinstance(column, int):
        return b'\xe0\x4b\x55\xeb\x4e' + (bytes([0x81 + column]) if 0 <= column <= 126
                                          else enc_int(column))
    return b'\xe0\x4b\x55\xeb\x53' + _enc_like(0x90, column)

def mk_set_file_dialog_result(result, filename=None, filter_index=0):
    """Аргумент SetFileDialogResult (kind=commit, nil-ключ, pad=4):
      Истина -> e2 <0x81+ИндексФильтра> <0x81+ЧислоИмён> <имя1 семейства d_> <имя2..N семейства 9_>
      Ложь   -> e1 81 82 c1 (имя/индекс игнорируются).
    filename: строка (одно имя) либо список/кортеж имён — имитация множественного выбора;
    список из одного имени даёт тот же кадр, что одиночная строка."""
    if not result:
        return b'\xe1\x81\x82\xc1'
    names = [filename or ''] if isinstance(filename, str) or filename is None else list(filename)
    if not names:
        names = ['']
    out = b'\xe2' + bytes([0x81 + (filter_index or 0)]) + bytes([0x81 + len(names)])
    out += _enc_like(0xd0, names[0])                       # первое имя — семейство d_
    for n in names[1:]:
        out += _enc_like(0x90, n)                          # последующие — семейство 9_
    return out

# GUID типа-Соответствия для аргумента ПерейтиКСтроке (константа платформы)
_GOTOROW_MAPTYPE = uuid.UUID('3d48feae-a9c6-4c5a-a099-9eb6477630c6').bytes_le

def mk_row_map(pairs):
    """ОписаниеСтроки (Соответствие {колонка: значение}) из ЛЮБОГО числа пар:
    23 95 + type-GUID + <0xc1+число пар> + пары. Пара: <c0 | 20 e0> 4b 53 + колонка(9_)
    + eb 53 + значение (9_ строка либо 8b/8d/8f число). Ставится ВМЕСТО хвостового 55
    в middle метода (e1 cb 55 -> e1 cb <карта>; e0 4b 55 -> e0 4b <карта>)."""
    out = b'\x23\x95' + _GOTOROW_MAPTYPE + bytes([0xc1 + len(pairs)])
    for i, (col, val) in enumerate(pairs):
        v = enc_int(val) if isinstance(val, int) else _enc_like(0x90, val)
        out += (b'\x20\xe0' if i else b'\xc0') + b'\x4b\x53'
        out += _enc_like(0x90, col) + b'\xeb\x53' + v
    return out


def mk_row_desc(column, value):
    """ОписаниеСтроки из одной пары (tree-методы: Развернуть/Свернуть/ВозможноРазвернуть/
    Развернут/ПерейтиНаУровень…)."""
    return mk_row_map([(column, value)])


def mk_tree_middle(base, row_column=None, row_value=None, pairs=None):
    """Middle tree-метода с необязательным ОписаниеСтроки: pairs (список пар) или одна пара
    row_column/row_value заменяют хвостовой 55 в base; без них base возвращается как есть."""
    rows = list(pairs) if pairs else ([(row_column, row_value)] if row_column is not None else [])
    if not rows:
        return base
    assert base.endswith(b'\x55'), 'tree-middle не оканчивается на 55'
    return base[:-1] + mk_row_map(rows)


def tree_row_pad(row_column=None, pairs=None):
    """Соответствие в описании строки добавляет три закрывающих маркера к обычным четырём."""
    return 7 if row_column is not None or pairs else None


GOTOROW_PAD = 7          # хвостовых 0x20 у ПерейтиКСтроке С ОписаниемСтроки (без него — обычный)

def mk_gotorow(column=None, value=None, toggle_selection=False, direction='down', fields=None):
    """Аргумент ПерейтиКСтроке (GotoRow):
    e1 <ПереключитьВыделение> 81 <Направление> cb + <ОписаниеСтроки | 55>.
    ПереключитьВыделение: 81=Ложь(умолч.)/82=Истина. Направление: 82=Вниз(умолч.)/81=Вверх.
    fields — список пар (колонка, значение) для поиска строки; можно одну пару через
    column/value. БЕЗ описания строки (55) метод меняет выделение ТЕКУЩЕЙ строки.
    С описанием кадру нужен pad=GOTOROW_PAD, без него — обычный."""
    tog = 0x82 if toggle_selection else 0x81
    dirb = 0x81 if str(direction).lower() == 'up' else 0x82
    head = bytes([0xe1, tog, 0x81, dirb]) + b'\xcb'
    rows = list(fields) if fields else ([(column, value)] if column is not None else [])
    return head + (mk_row_map(rows) if rows else b'\x55')

_RESULT_EPILOGUE = b'\x20\xa1\xa3'   # хвост ответа: <значение> 20 a1 a3 <трейлер>


def _reply_status_offset(raw, method_guid=None):
    """Locate the reply body by consuming the envelope, including both binary GUIDs."""
    if not raw.startswith(b'\x42') or not raw.endswith(TR) or len(raw) < 50:
        return None
    size = 1 if 0x81 <= raw[1] <= 0x8a else value_size(raw, 1)
    if not size or raw[1] not in range(0x81, 0x90):
        return None
    p = 1 + size
    if raw[p:p+7] != b'\x84\x83\x81\x83\xcb\x23\x95':
        return None
    p += 7
    if method_guid and raw[p:p+16] != uuid.UUID(method_guid).bytes_le:
        return None
    p += 16
    if raw[p:p+1] != b'\xd5':
        return None
    p += 17
    if raw[p:p+1] not in (b'\x81', b'\x97', b'\x98', b'\x9a', b'\x9b'):
        return None
    if p + 3 > len(raw) - len(TR):
        return None
    size = 1 if raw[p] == 0x81 else value_size(raw, p)
    if not size or p + size >= len(raw) - 4:
        return None
    return p + size


def decode_operation_status(raw, method_guid=None):
    """Status immediately after the receiver in a complete method-reply envelope.

    A 0x42 frame acknowledges the RPC; its status can still reject the operation.
    Do not search payload text for status bytes: field values may contain any bytes.
    """
    p = _reply_status_offset(raw, method_guid)
    if p is None:
        return None
    tag = raw[p]
    if 0x81 <= tag <= 0x8a:
        return tag - 0x81
    widths = {0x8b: 1, 0x8d: 2, 0x8f: 4}
    width = widths.get(tag)
    if width and p + 1 + width <= len(raw) - 4:
        return int.from_bytes(raw[p+1:p+1+width], 'little')
    return None


class OperationError(RuntimeError):
    """A complete RPC reply rejected the requested operation."""
    def __init__(self, status, key):
        self.status, self.key = status, key
        known = {
            7: ('target_unavailable', 'The target element is unavailable. Find it again.'),
            8: ('target_hidden', 'The target element is not visible.'),
            9: ('target_not_interactive', 'The action is unavailable in the current form state. An enabled element can still refuse it; check the command conditions, active window and dialogs.'),
            10: ('unsupported_element_type', 'The test client does not support this action for this element type.'),
            11: ('invalid_element_state', 'The element is not in a state that permits this action.'),
            12: ('value_not_found', 'The requested value is not present in this element.'),
            15: ('client_busy', 'The test client cannot process commands in its current state. Check for an open dialog on the client computer.'),
            17: ('unsupported_receiver', 'The test client does not support this action on this object.'),
        }
        self.code, message = known.get(status, ('client_operation_rejected',
            'The test client rejected this operation (status %d).' % status))
        super().__init__(message)

    def result(self):
        result = {'ok': False, 'target': self.key, 'code': self.code,
                  'error': str(self), 'status_code': self.status}
        if self.status == 15:
            result['recovery'] = 'Complete or cancel any open dialog on the client computer, then retry. For a file dialog, prepare set_file_dialog_result before opening it.'
        return result

# Признак существования объекта-получателя в ответе на кадр ЧТЕНИЯ: байт за 4 позиции до эпилога.
# Это признак НАЛИЧИЯ объекта, а не значение «Ложь»: у скрытого элемента он тоже 0x81.
# Это смещение относится только к CurrentVisible; общий статус операции читается
# отдельно из заголовка ответа функцией decode_operation_status.
TARGET_PRESENT_BYTE = 0x81
TARGET_ABSENT_BYTE  = 0x88

def decode_target_state(raw):
    """Существует ли объект-получатель: 'present' | 'absent' | 'unknown'.

    Применимо только к CurrentVisible: у других read-методов признак лежит иначе либо
    отсутствует. Для неизвестного формата возвращается 'unknown' — состояние, которое нельзя
    подменять ни наличием объекта, ни его отсутствием."""
    ep = raw.rfind(_RESULT_EPILOGUE)
    if ep < 4:
        return 'unknown'
    b = raw[ep - 4]
    if b == TARGET_PRESENT_BYTE: return 'present'
    if b == TARGET_ABSENT_BYTE:  return 'absent'
    return 'unknown'

def decode_goto_row(raw):
    """Результат ПерейтиКСтроке: True (строка найдена) / False (не найдена) / None (не распознан).

    Отдельная функция, а не ветка decode_bool: тот обслуживает state-геттеры и ожидания, и
    правка его шаблона изменила бы их поведение. Якорь — 'cb 55', результат за 4 байта до него;
    признак приходит в ОБОИХ направлениях поиска, тогда как шаблон decode_bool '81 81 82'
    совпадает только при поиске вниз."""
    cb = raw.rfind(b'\xcb\x55')
    if cb < 4:
        return None
    m = raw[cb - 4]
    if m == 0xe2: return True
    if m == 0xe1: return False
    return None

# Имена полей, которыми клиент размечает пункты списка выбора. Ответ приходит четвёрками
# «имя поля, значение, имя поля, значение», поэтому плоский список строк смешивает разметку с
# данными: первый его элемент — имя поля, а не вариант выбора.
CHOICE_FIELDS = ('ПредставлениеДанных', 'ОтображаемыйТекст')

def decode_choice_items(strings):
    """Пункты списка выбора из строк ответа: ([{presentation, text}], status).

    status: 'ok' — пункты распознаны; 'unknown' — разметки нет. Отдельного «распознанная пустота»
    нет намеренно: достоверного признака пустого списка в кадрах не найдено, а обещать различие,
    которого нет, нельзя. Пустой ответ закрытого выпадающего списка — штатный случай 'unknown'."""
    if isinstance(strings, bytes):
        # Both fields are strings; even a numeric presentation uses the compact
        # single-character encoding. A flattened string scan drops that value.
        raw = strings
        if not raw.startswith(b'\x42') or not raw.endswith(TR):
            return [], 'unknown'
        first = b'\xc0\x4b\x53' + _enc_key(CHOICE_FIELDS[0]) + b'\xeb\x53'
        second = b'\x20\xe0\x4b\x53' + _enc_key(CHOICE_FIELDS[1]) + b'\xeb\x53'
        items, start = [], 0
        def value(pos):
            if pos >= len(raw):
                return None
            tag = raw[pos]
            if tag == 0x81:
                return '', pos + 1
            if tag == 0x8b:
                return (chr(raw[pos + 1]), pos + 2) if pos + 1 < len(raw) else None
            if tag not in (0x97, 0x98, 0x9a, 0x9b):
                return None
            header = 3 if tag in (0x98, 0x9b) else 2
            if pos + header > len(raw):
                return None
            size = value_size(raw, pos)
            if pos + size > len(raw):
                return None
            try:
                return raw[pos + header:pos + size].decode('utf-16le' if tag in (0x97, 0x98) else 'latin1'), pos + size
            except UnicodeDecodeError:
                return None
        while (pos := raw.find(first, start)) >= 0:
            presentation = value(pos + len(first))
            if presentation is None:
                return [], 'unknown'
            text_pos = presentation[1]
            if raw[text_pos:text_pos + len(second)] != second:
                return [], 'unknown'
            text = value(text_pos + len(second))
            if text is None:
                return [], 'unknown'
            items.append(dict(presentation=presentation[0], text=text[0]))
            start = text[1]
        return (items, 'ok') if items else ([], 'unknown')
    items, cur = [], {}
    i = 0
    while i + 1 < len(strings):
        name, val = strings[i], strings[i + 1]
        if name == CHOICE_FIELDS[0]:
            if cur:
                items.append(cur)
            cur = {'presentation': val, 'text': val}
        elif name == CHOICE_FIELDS[1]:
            if cur:
                cur['text'] = val
        else:
            i += 1
            continue
        i += 2
    if cur:
        items.append(cur)
    return (items, 'ok') if items else ([], 'unknown')

def decode_expanded(raw):
    """Результат Развернут/ВозможноРазвернуть: True / False / None (не распознан).

    Третья раскладка булева ответа: `<результат> cb 55 20 20 ¡£`, результат вплотную перед
    `cb 55`. Ни маркер перед эпилогом (decode_bool), ни `cb-4` (decode_goto_row) сюда не
    подходят — у каждого метода своя длина средней части, поэтому распознаватель отдельный."""
    cb = raw.rfind(b'\xcb\x55')
    if cb < 1:
        # При адресации строки её Соответствие возвращается вместо пустого 55.
        cb = raw.rfind(b'\xcb\x23\x95' + _GOTOROW_MAPTYPE)
    if cb < 1:
        return None
    m = raw[cb - 1]
    if m == 0xe2: return True
    if m == 0xe1: return False
    return None

def decode_bool(raw):
    """Булев результат из ОТВЕТА. State-геттеры (CurrentVisible/Enable/ReadOnly/Check/
    ModeIsEdit…) кодируют его МАРКЕРОМ перед эпилогом ' ¡£': e2=Да(True), e1=Нет(False)
   . Иначе — строка Да/Нет (decode_result). None если нет."""
    r = decode_result(raw)
    if r in ('Да', 'Нет'):
        return r == 'Да'
    ep = raw.rfind(_RESULT_EPILOGUE)
    if ep > 0:
        m = raw[ep-1]
        if m == 0xe2: return True
        if m == 0xe1: return False
    j = raw.rfind(b'\x81\x81\x82')
    if j >= 4 and raw[j-4:j-1] == b'\x81\x81\x81':
        m = raw[j-1]
        if m == 0xe2: return True
        if m == 0xe1: return False
    return None

def decode_result(raw):
    """Скалярный результат из ОТВЕТА (op 0x42): значение, чей конец совпадает с эпилогом
    ' ¡£' (20 a1 a3). Кодирование варьируется (e0 41 81 81 b7… или f7… напрямую), но
    значение всегда прилегает к эпилогу. Возвращает str/int (Да/Нет — как строку) или None.
    Устойчивее эвристики extract_strings для чтения возврата read-методов."""
    ep = raw.rfind(_RESULT_EPILOGUE)
    end = ep if ep >= 0 else (len(raw) - 4 if raw.endswith(TR) else len(raw))
    for pos in range(end - 2, max(end - 2200, 20), -1):
        t = raw[pos]; hi = t & 0xf0; lo = t & 0x0f
        try:
            if hi in (0x90, 0xb0, 0xd0, 0xf0):
                if lo == 0x0a and pos + 2 + raw[pos+1] == end:            return raw[pos+2:end].decode('latin1')
                if lo == 0x07 and pos + 2 + raw[pos+1]*2 == end:          return raw[pos+2:end].decode('utf-16le')
                if lo == 0x0b and pos + 3 + struct.unpack_from('<H', raw, pos+1)[0] == end:   return raw[pos+3:end].decode('latin1')
                if lo == 0x08 and pos + 3 + struct.unpack_from('<H', raw, pos+1)[0]*2 == end: return raw[pos+3:end].decode('utf-16le')
            if t == 0x8b and pos + 2 == end: return raw[pos+1]
            if t == 0x8d and pos + 3 == end: return struct.unpack_from('<H', raw, pos+1)[0]
            if t == 0x8f and pos + 5 == end: return struct.unpack_from('<i', raw, pos+1)[0]
        except Exception:
            pass
    return None

# ---------- программное рукопожатие соединения ----------
# SCOM-кадр рукопожатия (intro/NTLM) — текстовый; отличается только base64-токеном в
# блоке 671507fd. Фикс-GUID структуры:
_SCOM_A = 'ae135932-4f94-44df-92c1-c91f15a92848'
_SCOM_B = 'd450256e-76cf-4404-b8ae-056edd642053'
# ФИКСИРОВАННЫЕ идентификаторы клиента TestController (
# НЕ случайные; тест-клиент их проверяет, случайные -> «Сеанс завершён администратором»).
_SCOM_CONN = 'e23134a2-14ff-4160-ba5f-ccef04e3786f'
_SCOM_SUB  = '7f58f27d-5ad8-43a1-aa1e-c982f41bed5c'

def build_scom(conn_guid, num, sub_guid, pc, token_b64, version):
    """Текстовый SCOM-кадр рукопожатия. token_b64 — base64 (bytes/str) токена в блоке
    671507fd: для intro — тикет V8IntroTicketReq, для NTLM — SSPI-токен."""
    if isinstance(token_b64, bytes):
        token_b64 = token_b64.decode('ascii')
    txt = ('{0,%s,%d,4,%s,0,1,11,0,\r\n'
           '{"S","TestClient:TestClient"},0,\r\n'
           '{"#",%s,\r\n{1,%s}\r\n},0,\r\n'
           '{"S","TestController"},0,\r\n'
           '{"S","%s"},2,\r\n'
           '{"#",%s,\r\n{%s}\r\n},1,0,\r\n'
           '{"S","%s"},0,\r\n{"S","1ru"},0,\r\n{"S","1ru_RU"},0,\r\n'
           '{"#",%s,\r\n{1,00000000-0000-0000-0000-000000000000}\r\n},1}'
           ) % (conn_guid, num, sub_guid, _SCOM_A, _SCOM_B, pc, TOK_GUID.decode(), token_b64, version, _SCOM_A)
    return b'\xef\xbb\xbf' + txt.encode('utf-8') + TR

def intro_ticket(user, pc):
    """Тикет V8IntroTicketReq: 'V8IntroTicketReq' + len16LE + '<user>@<pc>' + 0x00."""
    s = ('%s@%s' % (user, pc)).encode('utf-8')
    return b'V8IntroTicketReq' + struct.pack('<H', len(s)) + s + b'\x00'

_ANONYMOUS_NTLM_FLAGS = 0x00080205  # Unicode, request target, NTLM, extended session security

def _anonymous_ntlm_negotiate():
    """MS-NLMP anonymous connection; no account, password, signing or sealing requested."""
    return b'NTLMSSP\x00' + struct.pack('<II', 1, _ANONYMOUS_NTLM_FLAGS) + b'\x00' * 16

def _anonymous_ntlm_authenticate(challenge):
    """MS-NLMP anonymous response: one zero LM byte, empty NT/account/session-key fields.

    Only used without SSPI. It succeeds only when the test client allows anonymous
    authentication; the session response remains authoritative. No credentials are guessed.
    """
    if not 48 <= len(challenge) <= 65536 or challenge[:12] != b'NTLMSSP\x00\x02\x00\x00\x00':
        raise ConnectionError('The test client returned an invalid NTLM challenge.')
    for pos in (12, 40):
        length, maximum, offset = struct.unpack_from('<HHI', challenge, pos)
        if maximum < length or (length and (offset < 48 or offset + length > len(challenge))):
            raise ConnectionError('The test client returned an invalid NTLM challenge.')
    flags = struct.unpack_from('<I', challenge, 20)[0]
    if flags & 0x201 != 0x201:
        raise ConnectionError('The test client does not support the requested NTLM exchange.')
    flags = (flags & _ANONYMOUS_NTLM_FLAGS) | 0x800  # NTLMSSP_ANONYMOUS
    return (b'NTLMSSP\x00' + struct.pack('<I', 3) + struct.pack('<HHI', 1, 1, 64)
            + b'\x00' * 40 + struct.pack('<I', flags) + b'\x00')

def _network_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=1024)

def build_network_preamble(key=None):
    """Native mode-n negotiation. Only a fresh public RSA key is sent."""
    if key is None:
        key = _network_key()
    public = key.public_key().public_numbers()
    modulus = public.n.to_bytes((public.n.bit_length() + 7) // 8, 'big')
    exponent = public.e.to_bytes((public.e.bit_length() + 7) // 8, 'big')
    body = (b'\xef\xbb\xbf"n",\r\n{#base64:' + base64.b64encode(modulus) +
            b'},\r\n{#base64:' + base64.b64encode(exponent) + b'}')
    return struct.pack('<H', len(body)) + body

def _decode_network_intro(key, preamble, frame):
    """Unwrap the native RSA/3DES intro reply; key material stays in this call."""
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    try:
        encoded = re.fullmatch(rb'\xef\xbb\xbf\{#base64:([A-Za-z0-9+/=\r\n]+)\}', preamble)
        if not encoded:
            raise ValueError('invalid RSA envelope')
        encrypted = base64.b64decode(re.sub(rb'\s', b'', encoded[1]), validate=True)
        secret = key.decrypt(encrypted, padding.PKCS1v15())
        if len(secret) < 2 or int.from_bytes(secret[:2], 'little') != len(secret) - 2:
            raise ValueError('invalid negotiation length')
        part = rb'\{#base64:([A-Za-z0-9+/=\r\n]+)\}'
        params = re.fullmatch(rb'\xef\xbb\xbf"n",\s*' + part + rb',\s*' + part + rb',\s*' + part, secret[2:])
        if not params:
            raise ValueError('unsupported network negotiation')
        secret_key, receive_iv, send_iv = [base64.b64decode(re.sub(rb'\s', b'', v), validate=True) for v in params.groups()]
        if [len(secret_key), len(receive_iv), len(send_iv)] != [24, 8, 8]:
            raise ValueError('invalid network key lengths')
        if not frame.endswith(TR) or len(frame) <= len(TR):
            raise ValueError('incomplete encrypted intro')
        cipher = Cipher(TripleDES(secret_key), modes.CBC(receive_iv)).decryptor()
        plain = cipher.update(frame[:-len(TR)]) + cipher.finalize()
        # Native framing uses a final padding length, with arbitrary preceding pad bytes.
        # It is not uniform PKCS padding: preceding padding bytes can be random.
        if not plain or not 1 <= plain[-1] <= 8:
            raise ValueError('invalid encrypted intro padding')
        plain = plain[:-plain[-1]]
        if not plain.startswith(b'\xef\xbb\xbf{1,'):
            raise ValueError('invalid decrypted intro')
        return plain + TR
    except Exception:
        # Do not expose negotiated key material or provider diagnostics in MCP errors.
        raise ConnectionError('Could not decode the test-client network handshake.') from None

def _intro_attach_frames(session, counter):
    """Text bootstrap used by native clients when intro directly supplies a session."""
    head = '{0,%s,%d,4,%s,0,3,2,0,\r\n{"S",""},2,\r\n'
    bodies = [
        '{"#",bee47c3e-36bd-4926-8daf-71e22243feb0,\r\n'
        '{f853502e-f7cd-41f8-835f-10d65e948d14,"",0,0,0},\r\n'
        '{69801339-b3c5-40a4-8f7a-ae4dd660ef92,0,0}\r\n}\r\n}',
        '{"#",0d854d55-8a06-49ee-9e29-2f8d0e7a9f0e,\r\n'
        '{0f86fc63-d235-4360-97aa-88b5b64631c2,"",7,0,0},\r\n'
        '{\r\n{"U"}\r\n}\r\n}\r\n}',
    ]
    return [b'\xef\xbb\xbf' + ((head % (session, counter+i, OPCODE_GUID)) + body).encode('utf8') + TR
            for i, body in enumerate(bodies)]

def build_session_frame(sess, num):
    """Кадр установки сессии: {0,<sess>,<num>,1,<opcode-GUID>}."""
    return b'\xef\xbb\xbf' + ('{0,%s,%d,1,%s}' % (sess, num, OPCODE_GUID)).encode('utf-8') + TR

# attach — фикс-кадр рукопожатия с нетипичным заголовком (85 95 вместо 81 85 95);
# хранится байтовой константой в коде, подставляется текущая сессия.
_ATTACH = bytes.fromhex(
    '41950d6cf8136348ce4fa17c5442fc5818078d18588595e101231011f3bb4cacb29dbfa0ae'
    'b4bd81848381cb5381a3cb23953e7ce4bebd3626498daf71e22243feb0d58cef77c65626424'
    '3b752a03d53bcbde681818181f5a287a5072cb2ec44b10e36c7bd2452b581812020206653b2a6')
_ATTACH_SESS = '13f86c0d-4863-4fce-a17c-5442fc581807'

def build_attach(sess):
    """Кадр attach рукопожатия с подставленной живой сессией."""
    return _ATTACH.replace(uuid.UUID(_ATTACH_SESS).bytes_le, uuid.UUID(sess).bytes_le)

# Ключ проверяется целиком (см. _collection._KEY_RE): префиксная проверка принимала
# ложный тег строки и уводила сканер мимо следующего реального объекта.
_KEY_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]*\[[0-9a-fA-F-]{36}\]'
                     r'(\.[A-Za-z][A-Za-z0-9]*(\[[^\[\]]*\])?)*$')


def validate_address(key, handle=None):
    """Отказать до отправки: неверный адрес платформа часто принимает как пустое действие."""
    if key is not None:
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key) or any(ord(ch) < 32 for ch in key):
            raise ValueError('invalid key: use the complete element address returned by find_objects or get_child_objects')
        # Имена обычных элементов произвольны; идентификаторы окон и форм — UUID.
        ids = [key.split('[', 1)[1].split(']', 1)[0]]
        ids += re.findall(r'\.ManagedForm\[([^\]]*)\]', key)
        try:
            for value in ids:
                uuid.UUID(value)
        except ValueError:
            raise ValueError('invalid key: the window or form identifier is not valid') from None
    if handle is not None:
        try:
            if not isinstance(handle, str): raise ValueError()
            uuid.UUID(handle)
        except ValueError:
            raise ValueError('invalid handle: use the handle returned with this element') from None

def parse_perf(raw):
    """{имя: значение} для GetAccumulatedPerformanceIndicators.
    Запись счётчика: 53 97 <len8> <utf16-имя> eb 4e <значение>.
    Значение: 8b=int8 / 8d=int16 / 8f=int32, иначе ASCII-десятичное (напр. '0.008')."""
    res = {}; i = 0; mk = bytes([0x53, 0x97])
    while True:
        p = raw.find(mk, i)
        if p < 0:
            break
        ln = raw[p+2]
        name = raw[p+3:p+3+ln*2].decode('utf-16le', 'replace')
        v = raw[p+3+ln*2:]
        i = p+3+ln*2
        if v[:2] == bytes([0xeb, 0x4e]):
            b = v[2:]; t = b[0]
            if t == 0x8b:   val = b[1]
            elif t == 0x8d: val = struct.unpack_from('<H', b, 1)[0]
            elif t == 0x8f: val = struct.unpack_from('<i', b, 1)[0]
            else:
                run = ''; started = False
                for ch in b:
                    c = chr(ch)
                    if c in '0123456789.': run += c; started = True
                    elif started: break
                # контракт метода: все показатели — ЧИСЛО (Длительность приходит ASCII-строкой)
                try:    val = float(run) if '.' in run else int(run)
                except ValueError: val = None
            if all(0x20 <= ord(c) < 0x500 for c in name):
                res[name] = val
    return res

def extract_object_keys(raw):
    """Найти ключи объектов Класс[guid][.суффикс] — тег-based скан строк, длина из тега:
    короткие (ASCII 9a/ba/da/fa, UTF-16 97/b7/d7/f7) и ДЛИННЫЕ формы len16
    (ASCII 9b/bb/db/fb, UTF-16 98/b8/d8/f8) — длинные ключи приходят у вложенных элементов."""
    keys=[]; i=0; n=len(raw)
    while i < n-1:
        t=raw[i]; ln=raw[i+1]; s=None; adv=1
        ln16=struct.unpack_from('<H', raw, i+1)[0] if i+3<=n else 0
        if t in (0x9a,0xba,0xda,0xfa) and i+2+ln<=n:
            try: s=raw[i+2:i+2+ln].decode('latin1'); adv=2+ln
            except Exception: s=None
        elif t in (0x97,0xb7,0xd7,0xf7) and i+2+ln*2<=n:
            try: s=raw[i+2:i+2+ln*2].decode('utf-16le'); adv=2+ln*2
            except Exception: s=None
        elif t in (0x9b,0xbb,0xdb,0xfb) and i+3+ln16<=n:
            try: s=raw[i+3:i+3+ln16].decode('latin1'); adv=3+ln16
            except Exception: s=None
        elif t in (0x98,0xb8,0xd8,0xf8) and i+3+ln16*2<=n:
            try: s=raw[i+3:i+3+ln16*2].decode('utf-16le'); adv=3+ln16*2
            except Exception: s=None
        # доверяем длине тега только если это валидный ключ; иначе тег ложный -> +1
        if s is not None and _KEY_RE.match(s):
            if s not in keys: keys.append(s)
            i += adv
        else:
            i += 1
    return keys



def replace_ascii_str(frame, old, new):
    """Заменить строку old на new в кадре. old ищется как ASCII (теги 9a/ba/da/fa).
    new: если укладывается в Latin-1 — та же ASCII-кодировка; иначе UTF-16
    (низкий ниббл тега меняется a->7, длина = число символов). old/new — python-строки."""
    ob = old.encode('latin1')
    for tag in (0x9a, 0xba, 0xda, 0xfa):
        pat = bytes([tag, len(ob)]) + ob
        i = frame.find(pat)
        if i < 0:
            continue
        try:
            nb = new.encode('latin1')
            repl = bytes([tag, len(nb)]) + nb                      # ASCII (тот же тег)
        except UnicodeEncodeError:
            nb = new.encode('utf-16le')
            repl = bytes([(tag & 0xf0) | 0x07, len(nb) // 2]) + nb  # UTF-16 (тег *7), длина в к.е.
        return frame[:i] + repl + frame[i+len(pat):]
    raise ValueError('string not found in the frame')



def extract_uilog(raw):
    """Извлечь uilog XML из ответа (строка длинная, 2-байтовая длина -> берём по границам)."""
    for enc in ('utf-16le','utf-8'):
        u=raw.decode(enc,'ignore')
        i=u.find('<?xml'); j=u.rfind('</uilog>')
        if i>=0 and j>i: return u[i:j+len('</uilog>')]
        i=u.find('<uilog'); j=u.rfind('</uilog>')
        if i>=0 and j>i: return u[i:j+len('</uilog>')]
    return None

def extract_strings(raw, min_len=1):
    """Все строки-значения из кадра. Сканируем КАЖДУЮ позицию (+1), чтобы ложный
    тег перед значением не «съедал» реальную строку. Возможны наложения/дубли."""
    out=[]; i=0; n=len(raw)
    while i<n-1:
        tg=raw[i]; ln=raw[i+1]; s=None
        if tg in (0x9a,0xba,0xda,0xfa) and i+2+ln<=n:
            try: s=raw[i+2:i+2+ln].decode('latin1')
            except Exception: s=None
        elif tg in (0x97,0xb7,0xd7,0xf7) and i+2+ln*2<=n:
            try: s=raw[i+2:i+2+ln*2].decode('utf-16le')
            except Exception: s=None
        elif tg in (0x9b,0xbb,0xdb,0xfb) and i+3<=n:   # длинная ASCII (len16 LE)
            L=struct.unpack_from('<H',raw,i+1)[0]
            if i+3+L<=n:
                try: s=raw[i+3:i+3+L].decode('latin1')
                except Exception: s=None
        elif tg in (0x98,0xb8,0xd8,0xf8) and i+3<=n:   # длинная UTF-16 (len16 LE)
            L=struct.unpack_from('<H',raw,i+1)[0]
            if i+3+L*2<=n:
                try: s=raw[i+3:i+3+L*2].decode('utf-16le')
                except Exception: s=None
        # \t \n \r — законные символы данных: HTML-документ, многострочный текст поля,
        # записанный uilog. Их запрет выбрасывал ВЕСЬ документ, а не лишний символ.
        if s and len(s)>=min_len and all(ch in '\t\n\r' or ord(ch)>=0x20 for ch in s):
            out.append(s)
        i+=1
    return out

_PAIR_MARK = b'\xeb\x53'      # разделитель пары Соответствия: <ключ 9_> eb 53 <значение>
_ROW_MARK  = b'\xc0\x4b'      # начало Соответствия (одной строки таблицы)

def _str_ending_at(raw, q, window=600):
    """Строка или компактный символ, заканчивающиеся РОВНО в q (ключ пары перед eb53).
    Берём самую длинную правдоподобную — короткие совпадения обычно ложные (байт-мусор)."""
    best = None
    for p in range(max(0, q - window), q - 1):
        tg = raw[p]; ln = raw[p + 1]
        s = None
        if tg in (0x9a, 0xba, 0xda, 0xfa) and p + 2 + ln == q:
            s = raw[p + 2:q].decode('latin1', 'replace')
        elif tg in (0x97, 0xb7, 0xd7, 0xf7) and p + 2 + ln * 2 == q:
            s = raw[p + 2:q].decode('utf-16le', 'replace')
        elif tg == 0x8b and p + 2 == q:
            s = chr(ln)
        if s and _plausible_str(s) and (best is None or len(s) > len(best)):
            best = s
    return best

def _value_at(raw, i, *, text=False):
    """Значение пары Соответствия, начиная с позиции i: строка/число/дата или ''
    (пусто/Неопределено кодируется одиночным 81 — его НЕЛЬЗЯ терять, иначе колонки
    и значения разъезжаются). Режем РОВНО по объявленной длине значения: длина ячейки
    ничем не ограничена, а обрезка буфера дала бы префикс вместо полного текста."""
    size, value = _text_at(raw, i, compact=text)
    if size:
        return value
    n = value_size(raw, i)
    toks = decode_stream(raw[i:i + n] if n else raw[i:i + 16])
    if not toks:
        return ''
    kind, val = toks[0]
    if kind in ('str', 'ustr'):
        return val
    if kind in ('int', 'date'):
        return str(val)
    return ''                      # raw-маркер (81 = пусто и т.п.)


def _text_at(raw, i, *, compact=True):
    """Decode a length-delimited text value at a known boundary, without content heuristics."""
    if i >= len(raw):
        return 0, None
    tag = raw[i]
    if compact and tag == 0x8b and i + 2 <= len(raw):
        return 2, chr(raw[i + 1])
    if tag not in (0x9a, 0xba, 0xda, 0xfa, 0x97, 0xb7, 0xd7, 0xf7,
                   0x9b, 0xbb, 0xdb, 0xfb, 0x98, 0xb8, 0xd8, 0xf8):
        return 0, None
    prefix = 3 if tag & 15 in (11, 8) else 2
    if i + prefix > len(raw):
        return 0, None
    size = value_size(raw, i)
    if i + size > len(raw):
        return 0, None
    try:
        value = raw[i + prefix:i + size].decode('utf-16le' if tag & 15 in (7, 8) else 'latin1')
    except UnicodeDecodeError:
        return 0, None
    return size, value


def decode_field_text(raw, *, property_value=False):
    """Text at the known scalar reply boundary, without filtering its contents.

    Empty/unavailable scalar markers remain for the caller's element-type checks.
    Consume the envelope first so UUID bytes and the echoed key cannot become data.
    """
    p = _reply_status_offset(raw)
    prefix = b'\x81\x81\x81' + (b'\xe0\x4b\x53' if property_value else b'')
    suffix = (b'\x20\x20' if property_value else b'\x20') + b'\xa1\xa3' + TR
    if p is None or not raw.startswith(prefix, p) or not raw.endswith(suffix):
        return None
    p += len(prefix)
    size, text = _text_at(raw, p)
    return text if size and p + size == len(raw) - len(suffix) else None


def decode_cell_text(raw):
    """GetCellText returns text before the echoed column; compact bytes are characters."""
    marker = b'\x81\x81\x81\xe0\x4b\x53'
    start = 0
    while True:
        p = raw.find(marker, start)
        if p < 0:
            return False, None
        start = p + 1
        p += len(marker)
        try:
            size = 1 if raw[p] == 0x81 else value_size(raw, p)
            q = p + size
            if not size or raw[q:q + 2] != b'\xeb\x53':
                continue
            column_size = value_size(raw, q + 2)
            if not column_size:
                continue
            end = q + 2 + column_size
            if raw[end:end + 4] != b'\x20\x20\xa1\xa3' or end + 8 != len(raw):
                continue
            if raw[p] == 0x81:
                return True, None
            if raw[p] == 0x8b and size == 2:
                return True, chr(raw[p + 1])
            size, value = _text_at(raw, p)
            if size and p + size == q:
                return True, value
        except (IndexError, ValueError, struct.error):
            continue


def decode_area_text(raw, area):
    """Read area text after its echoed address or current-area marker, up to the frame end.
    This position contains data, so Unicode and key-like text must not be filtered.
    Unknown layouts return None for the caller's compatibility decoder.
    """
    if not raw.startswith(b'\x42') or not raw.endswith(b'\x20\xa1\xa3' + TR):
        return None
    marker = b'\x81\x81\x81' + (_enc_like(0xf0, area) if area else b'\xe1')
    start, end = 0, len(raw) - 7
    while True:
        p = raw.find(marker, start)
        if p < 0:
            return None
        start = p + 1
        p += len(marker)
        if p >= end:
            continue
        tag = raw[p]
        if tag == 0x8b and p + 2 == end:
            return chr(raw[p + 1])
        if tag not in (0x9a, 0xba, 0xda, 0xfa, 0x97, 0xb7, 0xd7, 0xf7,
                       0x9b, 0xbb, 0xdb, 0xfb, 0x98, 0xb8, 0xd8, 0xf8):
            continue
        prefix = 3 if tag & 0x0f in (0x0b, 0x08) else 2
        if p + prefix > end or p + value_size(raw, p) != end:
            continue
        encoding = 'utf-16le' if tag & 0x0f in (0x07, 0x08) else 'latin1'
        try:
            return raw[p + prefix:end].decode(encoding)
        except UnicodeDecodeError:
            continue


def decode_rows(raw):
    """Ответ ПолучитьВыделенныеСтроки -> [{колонка: значение}] (по Соответствию на строку).
    Разбор ПО СТРУКТУРЕ: строку открывает c0 4b, внутри пары <колонка> eb 53 <значение>.
    Так пустые и нестроковые значения сохраняют своё место (попарное «склеивание» подряд
    идущих строк давало заголовок соседней колонки вместо пустого значения)."""
    # A receiver UUID can contain row markers and apparently valid text lengths.
    # Consume the envelope first; never scan its GUIDs or echoed object address.
    i = _reply_status_offset(raw) if raw.startswith(b'\x42') else 0
    if i is None:
        raise ValueError('Cannot read table rows: unrecognized reply envelope')
    rows = []; row = None
    while i < len(raw):
        # Collection/type identifiers in the body are binary GUID values too.
        if raw.startswith(b'\x23\x95', i):
            i += 18
            continue
        if raw.startswith(_ROW_MARK, i):
            if row:
                rows.append(row)
            row = {}; i += len(_ROW_MARK)
            continue
        size, col = _text_at(raw, i)
        if size:
            q = i + size
            if row is not None and raw.startswith(_PAIR_MARK, q):
                p = q + len(_PAIR_MARK)
                if p >= len(raw):
                    break
                try:
                    n = value_size(raw, p) or 1
                except IndexError:
                    break
                if p + n > len(raw):
                    break
                row[col] = _value_at(raw, p, text=True)
                i = p + n
            else:
                i = q
        else:
            i += 1
    if row:
        rows.append(row)
    return rows

def is_byteswapped_ascii(s):
    """True, если строка — это ASCII/латиница, декодированная со СДВИГОМ на 1 байт
    (артефакт посимвольного скана extract_strings): почти каждый символ имеет вид
    0xXX00, где XX — печатный ASCII-байт (напр. '['=0x5B -> 0x5B00='嬀'). Реальная
    кириллица (0x0410..) сюда не попадает: у неё младший байт ненулевой."""
    if not s:
        return False
    hits = sum(1 for ch in s if (ord(ch) & 0x00FF) == 0 and 0x20 <= (ord(ch) >> 8) <= 0x7E)
    return hits >= max(3, len(s) * 0.5)

def is_cjk_garble(s):
    """True, если строка в основном символы CJK-диапазона (>=0x2000) — артефакт неверного
    декода (ASCII/UTF-16 прочитаны со сдвигом). В деловых данных 1С таких символов нет."""
    return bool(s) and sum(1 for ch in s if ord(ch) >= 0x2000) > len(s) * 0.2

_KEY_LIKE = re.compile(r'[A-Za-z][A-Za-z0-9]*\[')   # Class[ — так начинается сегмент ключа кадра

def is_key_like(s):
    """Похоже на ключ кадра (SecondaryFrame[..].EditField[Имя]) или на обрывок скана.
    Само по себе наличие '[' признаком не является: скобки — обычный символ данных
    («Цена [руб.]»), и отбрасывать такие значения нельзя."""
    if '[' not in s:
        return False
    return bool(_KEY_LIKE.search(s)) or ']' not in s.split('[', 1)[1]

_TAIL_END      = b'\xa1\xa3'              # конец кадра перед 4-байтовым завершением
_ONE_BYTE_TAGS = (0x8b, 0xab, 0xcb, 0xeb)   # семейство «значение в один байт»

def decode_tail_byte(raw, pad):
    """Компактная форма значения: <тег семейства> <байт> <pad байт 0x20> a1 a3 <4 байта>.
    Возвращает САМ БАЙТ либо None, если формы нет. Что он означает — число (размер области)
    или символ (текст) — решает читатель, а не декодер: одна и та же форма встречается в обоих
    смыслах. Позиция фиксированная; сканировать кадр этой формой нельзя — байт тега часто
    встречается в шуме и даёт сотни ложных значений.
    `pad` обязателен и приходит от вызывающего: сколько байт 0x20 стоит перед завершением —
    свойство ВИДА ОТВЕТА, а не значения, и у одной команды это число не меняется. Определять
    добивку по числу подряд идущих 0x20 нельзя: байт значения сам может равняться 0x20 либо
    совпасть с байтом тега, и тогда добивка выдаётся за значение. pad=0 формой не считается —
    такой ответ не встречается."""
    if not pad or len(raw) < 8 + pad or raw[-6:-4] != _TAIL_END:
        return None
    if any(b != 0x20 for b in raw[-6 - pad:-6]):
        return None
    return raw[-7 - pad] if raw[-8 - pad] in _ONE_BYTE_TAGS else None

def clean_strings(raw, min_len=1):
    """extract_strings без байт-перевёрнутого мусора, без CJK-мусора (неверный декод) и без
    C1-управляющих (0x80..0x9F — их нет в текстах 1С, только у обрывков посимвольного скана).
    Печатные символы Latin-1 (é, неразрывный пробел) — обычные данные, не мусор.
    Годится для КОРОТКИХ значений в шумном кадре; для чтения документа целиком не подходит —
    там содержимое законно содержит и скобки, и иероглифы (см. tc_get_html)."""
    return [x for x in extract_strings(raw, min_len)
            if not is_key_like(x) and not is_byteswapped_ascii(x) and not is_cjk_garble(x)
            and not any(0x80 <= ord(ch) <= 0x9F for ch in x)]

def replace_str(frame, old, new):
    """Заменить строку old->new, ищет и ASCII (9a/ba/da/fa), и UTF-16 (97/b7/d7/f7).
    Кодировка new выбирается по содержимому (Latin-1 -> ASCII, иначе UTF-16),
    старший ниббл тега сохраняется."""
    # ASCII-поиск
    try:
        ob = old.encode('latin1')
        for tag in (0x9a, 0xba, 0xda, 0xfa):
            i = frame.find(bytes([tag, len(ob)]) + ob)
            if i >= 0:
                return frame[:i] + _enc_like(tag, new) + frame[i+2+len(ob):]
    except UnicodeEncodeError:
        pass
    # UTF-16-поиск
    ou = old.encode('utf-16le')
    for tag in (0x97, 0xb7, 0xd7, 0xf7):
        i = frame.find(bytes([tag, len(old)]) + ou)
        if i >= 0:
            return frame[:i] + _enc_like(tag, new) + frame[i+2+len(ou):]
    raise ValueError('string not found in the frame')

def _enc_like(tag, new):
    """Закодировать new с тем же старшим ниблом тега. Тип по содержимому:
    ASCII (низкий ниббл a<256 / b>=256, длина 1/2 байта LE), иначе UTF-16 (7<256 / 8>=256)."""
    hi = tag & 0xf0
    try:
        nb = new.encode('latin1')
        if len(nb) < 256:
            return bytes([hi | 0x0a, len(nb)]) + nb
        return bytes([hi | 0x0b]) + len(nb).to_bytes(2, 'little') + nb   # длинная ASCII
    except UnicodeEncodeError:
        nb = new.encode('utf-16le')
        n = len(nb) // 2            # длина В КОДОВЫХ ЕДИНИЦАХ UTF-16: у символов вне BMP
        if n < 256:                 # их две на символ, и len(строки) дал бы вдвое меньше
            return bytes([hi | 0x07, n]) + nb
        return bytes([hi | 0x08]) + n.to_bytes(2, 'little') + nb        # длинная UTF-16

_FIXBLK = bytes.fromhex('81848381cb5381a3cb2395')
def _percall_pos(frame):
    p = frame.find(_FIXBLK)
    if p < 0: return None
    pos = p + len(_FIXBLK) + 16 + 1   # +метод GUID(16) +d5(1)
    return pos if pos + 16 <= len(frame) else None
def _set_percall(frame, guid_le):
    """Поставить GUID (16б bytes_le) в позицию per-call/хендла объекта."""
    pos = _percall_pos(frame)
    return frame if pos is None else frame[:pos] + guid_le + frame[pos+16:]

# ---------- клиент ----------
class TestClient:
    def __init__(self, host='127.0.0.1', port=1538):
        self.host=host; self.port=port; self.sess=None; self.s=None; self._buf=b''
        self._counter=100      # счётчик команд (int16 LE); значение не валидируется жёстко
        self.platform_version=None   # версия платформы для intro (по умолч. CAP_VER)
        self._max_action_time=None   # локальная настройка Set/GetMaxActionExecutionTime (без кадра)
        self._pending=0              # сколько ответов не дождались (таймаут): поток кадров сдвинут
        self._track=None             # если список — сюда пишутся GUIDʼы отправленных команд (для контроля записи сценария)
        self._direct_session = False
        self._first_binary = False
        self._io_deadline = None

    RECV_TIMEOUT = 30            # базовый таймаут ожидания ответа на команду, с
    CONNECT_TIMEOUT = 10         # ожидание кадров рукопожатия (retries × это время до отказа)
    RESYNC_TIMEOUT = 3           # ожидание запоздавших ответов после таймаута, с

    def _io_timeout(self, timeout):
        deadline = self._io_deadline
        if deadline is None:
            return timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Test-client startup deadline expired.')
        return remaining if timeout is None else min(timeout, remaining)

    def _handshake_send(self, data):
        self.s.settimeout(self._io_timeout(self.CONNECT_TIMEOUT))
        self.s.sendall(data)

    def _recv_timeout(self):
        """Таймаут ожидания ответа. По умолчанию RECV_TIMEOUT; если задан max_action_time
        (SetMaxActionExecutionTime) — ровно он, т.к. ответ приходит по завершении действия,
        и контракт метода — ограничить действие ИМЕННО этим временем. 0 -> без ограничения."""
        mat = self._max_action_time
        if mat is None:
            return self.RECV_TIMEOUT
        return None if int(mat) <= 0 else int(mat)

    def _read_frame(self, timeout):
        """Прочитать ОДИН кадр (до трейлера TR). timeout: секунды или None (без ограничения).
        При таймауте недочитанное остаётся в self._buf. При EOF/обрыве соединение закрывается."""
        self._require_socket()
        try:
            self.s.settimeout(self._io_timeout(timeout))
            while TR not in self._buf:
                self.s.settimeout(self._io_timeout(timeout))
                d=self.s.recv(65536)
                if not d:
                    size = len(self._buf)
                    raise ConnectionError('The test client closed the connection before a complete response '
                                          'was received (%d bytes received). Reconnect with '
                                          'tc_session(action="connect").' % size)
                self._buf+=d
        except socket.timeout:
            raise                   # the remaining bytes may still arrive; preserve the buffer
        except OSError:
            self.close()            # EOF/reset is final; never send another request on this socket
            raise
        j=self._buf.find(TR)
        out=self._buf[:j+len(TR)]; self._buf=self._buf[j+len(TR):]
        return out

    def _require_socket(self):
        if self.s is None:
            raise ConnectionError('The test-client connection is closed. Reconnect with tc_session(action="connect").')

    def _recv(self, timeout=0):
        """timeout: 0 — политика по умолчанию (_recv_timeout), None — без ограничения,
        число — секунды (для методов с собственным ожиданием, напр. WaitFor…)."""
        try:
            return self._read_frame(self._recv_timeout() if timeout == 0 else timeout)
        except socket.timeout:
            # Ответ на эту команду ещё придёт и ляжет в поток. Сверки «ответ↔запрос» в
            # протоколе нет, поэтому следующая команда прочитала бы ЧУЖОЙ кадр — помечаем
            # долг, его выберет _resync перед отправкой следующей команды.
            self._pending += 1
            raise

    def _resync(self):
        """Выбросить запоздавшие ответы после таймаута. Если они ещё не пришли — отказ:
        слать новую команду в сдвинутый поток нельзя, ответы поедут на одну команду назад."""
        while self._pending:
            try:
                self._read_frame(self.RESYNC_TIMEOUT)
            except socket.timeout:
                raise RuntimeError('the connection desynchronised after a timeout: %d answer(s) to '
                                   'earlier commands were never received — call tc_disconnect '
                                   'and tc_connect' % self._pending)
            self._pending -= 1

    def _set_tok(self, tpl, tok):
        return re.subn(TOK_GUID+rb',\s*\{[^}]*\}', TOK_GUID+b',\r\n{'+base64.b64encode(tok)+b'}\r\n', tpl)[0]
    def _get_tok(self, fr):
        m=re.search(TOK_GUID+rb',\s*\{([A-Za-z0-9+/=]*)\}', fr)
        return base64.b64decode(m.group(1)) if (m and m.group(1)) else b''

    # версия платформы по умолчанию для intro-тикета; переопределяется через self.platform_version
    DEFAULT_VER = '8.3.27.1859'

    def _read_exact(self, size):
        """Read a network preamble component, retaining any coalesced SCOM bytes."""
        self._require_socket()
        while len(self._buf) < size:
            self.s.settimeout(self._io_timeout(self.CONNECT_TIMEOUT))
            data = self.s.recv(65536)
            if not data:
                raise ConnectionError('The test client closed the connection during network negotiation.')
            self._buf += data
        result, self._buf = self._buf[:size], self._buf[size:]
        return result

    def _network_intro(self, frame):
        """Reconnect after the peer explicitly requests network negotiation.

        A plain SCOM intro sent to this listener produces GREETING + TR and closes
        that stream. Reconnect and follow the native manager's sequence. Detecting
        the response avoids guessing from host names, IP addresses or short waits.
        """
        self.close()
        self.s = socket.create_connection((self.host, self.port), timeout=self._io_timeout(5))
        self.s.settimeout(self._io_timeout(self.CONNECT_TIMEOUT))
        try:
            if self._read_exact(len(NETWORK_GREETING)) != NETWORK_GREETING:
                raise ConnectionError('Unexpected test-client network greeting.')
            key = _network_key()
            self._handshake_send(build_network_preamble(key) + frame)
            size = int.from_bytes(self._read_exact(2), 'little')
            if not 16 <= size <= 4096:
                raise ConnectionError('Invalid test-client network preamble length.')
            preamble = self._read_exact(size)
            if not re.fullmatch(rb'\xef\xbb\xbf\{#base64:[A-Za-z0-9+/=\r\n]+\}', preamble):
                raise ConnectionError('Invalid test-client network preamble.')
            return _decode_network_intro(key, preamble, self._recv(self.CONNECT_TIMEOUT))
        except Exception:
            self.close()
            raise

    def connect(self, retries=20):
        """Рукопожатие строится программно (build_scom/intro_ticket/build_session_frame):
        intro -> при необходимости NTLM -> установка сессии."""
        import os, getpass
        pc = (os.environ.get('COMPUTERNAME') or socket.gethostname()).strip()
        user = getpass.getuser()
        ver = self.platform_version or self.DEFAULT_VER
        conn_guid = _SCOM_CONN; sub_guid = _SCOM_SUB   # ФИКСИРОВАННЫЕ (не случайные!)
        num = [22548]
        def _scom(tok):
            n = num[0]; num[0] += 1
            return build_scom(conn_guid, n, sub_guid, pc, base64.b64encode(tok), ver)
        for _ in range(retries):
            try:
                self.s=socket.create_connection((self.host,self.port),timeout=self._io_timeout(5))
                self._buf=b''; self._pending=0      # новый сокет — прошлый поток кадров не в счёт
                self._direct_session=False; self._first_binary=False
                self._handshake_send(_scom(intro_ticket(user, pc))); ack=self._recv(self.CONNECT_TIMEOUT)
                if ack == NETWORK_GREETING + TR:
                    num[0] = 22548
                    ack = self._network_intro(_scom(intro_ticket(user, pc)))
                if TR in ack and len(ack)>100: break
                self.close()
            except OSError:
                self.close()
                num[0]=22548
            time.sleep(self._io_timeout(1))
        else:
            raise RuntimeError('intro handshake failed')
        # The peer may supply a session in intro (native Linux client), or require NTLM.
        self._direct_session = bool(re.search(SESS_BLOCK+rb',\s*\{([0-9a-f-]{36})\}', ack))
        auth = ack
        if not self._direct_session:
            if sspi is None:
                try:
                    self._handshake_send(_scom(_anonymous_ntlm_negotiate()))
                    challenge = self._get_tok(self._recv(self.CONNECT_TIMEOUT))
                    self._handshake_send(_scom(_anonymous_ntlm_authenticate(challenge)))
                    auth = self._recv(self.CONNECT_TIMEOUT)
                except Exception:
                    self.close()
                    raise
            else:
                ca=sspi.ClientAuth('NTLM', scflags=sspicon.ISC_REQ_CONFIDENTIALITY|sspicon.ISC_REQ_INTEGRITY|
                               sspicon.ISC_REQ_REPLAY_DETECT|sspicon.ISC_REQ_SEQUENCE_DETECT)
                ib=None
                for _ in range(3):
                    err,out=ca.authorize(ib); self._handshake_send(_scom(out[0].Buffer)); auth=self._recv(self.CONNECT_TIMEOUT)
                    if err==0: break
                    ch=self._get_tok(auth); sb=win32security.PySecBufferDescType()
                    bb=win32security.PySecBufferType(len(ch),sspicon.SECBUFFER_TOKEN); bb.Buffer=ch; sb.append(bb); ib=sb
        session = re.search(SESS_BLOCK+rb',\s*\{([0-9a-f-]{36})\}', auth)
        if not session:
            self.close()
            raise ConnectionError('The test client did not grant a session. Anonymous authentication may be '
                                  'disabled; try a Windows MCP server.' if sspi is None else
                                  'The test client did not grant a session after authentication.')
        self.sess=session.group(1).decode()
        # установка сессии (программный кадр)
        self._handshake_send(build_session_frame(self.sess, num[0])); self._recv(self.CONNECT_TIMEOUT)
        if self._direct_session:self._counter = num[0]
        return self.sess

    def attach(self):
        """Отправить кадр attach рукопожатия (программный, с текущей сессией)."""
        if self._direct_session:
            try:
                for frame in _intro_attach_frames(self.sess, self._counter + 1):
                    self._handshake_send(frame)
                    reply = self._recv()
                    if not reply.startswith(b'\xef\xbb\xbf{1,') or not reply.endswith(b'},0},2' + TR):
                        raise ConnectionError('The test client rejected session initialization.')
                    self._counter += 1
                self._first_binary = True
                return reply
            except Exception:
                self.close()
                raise
        self._handshake_send(build_attach(self.sess)); return self._recv()

    def send_cmd(self, method_guid, key, kind='read', middle=b'', handle=None, per_call=None, pad=None,
                 timeout=0):
        """Построить командный кадр программно (build_frame) и отправить.
        Сессия и счётчик подставляются автоматически; handle — дескриптор объекта-получателя
        (ставится в позицию per-call); pad — переопределение хвостовых 0x20 (GotoRow=7, SFDR=4);
        timeout — ожидание ответа (0 = по умолчанию/max_action_time, см. _recv).
        Возвращает разобранный ответ."""
        validate_address(key, handle)
        self._require_socket()
        if self._pending:
            self._resync()          # выбросить запоздавшие ответы, иначе прочитаем чужой кадр
        self._counter = (self._counter + 1) & 0xffff
        fr = build_frame(self.sess, self._counter, method_guid, key,
                         kind=kind, middle=middle, per_call=per_call or handle, pad=pad)
        if self._first_binary:
            # The first binary request declares the opcode; later requests reuse it.
            pos = 20 if self._counter < 256 else 21
            fr = fr[:pos] + fr[pos+1:]
            self._first_binary = False
        try: self.s.sendall(fr)
        except OSError:
            self.close()
            raise
        r=self._recv(timeout)
        status = decode_operation_status(r, method_guid)
        if status not in (None, 0):
            raise OperationError(status, key)
        if self._track is not None:
            self._track.append((method_guid, key, middle, kind))
        return {'opcode': r[0], 'ok': r[0]==0x42, 'raw': r,
                'values': decode_stream(r[:-4], 21)}

    def close(self):
        try: self.s.close()
        except Exception: pass
        self.s = None
        self.sess = None
        self._buf = b''
        self._pending = 0
        self._direct_session = False
        self._first_binary = False

if __name__=='__main__':
    import sys, io
    sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8',errors='replace')
    port=int(sys.argv[1]) if len(sys.argv)>1 else 1538
    import guids as G
    c=TestClient('127.0.0.1', port); print('сессия:', c.connect())  # рукопожатие программно
    c.attach()                                    # attach (программный кадр)
    r=c.send_cmd(G.GET_ACTIVE_WINDOW, None, kind='read', middle=RES_COLLECTION)  # GetActiveWindow (программно)
    print('GetActiveWindow ok=%s' % r['ok'])
    for k,v in r['values']:
        if k in ('str','ustr'): print('   %s: %s' % (k,v))
    c.close()
