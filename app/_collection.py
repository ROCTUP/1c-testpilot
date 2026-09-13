# -*- coding: utf-8 -*-
"""Декодер коллекций/объектов тест-протокола 1С.
Каждый объект в потоке кодируется как <handle GUID 16б><ключ-строка>.
Ключ адресует объект логически, handle — живой дескриптор для команд НА объекте."""
import re, struct, uuid

# Ключ проверяется ЦЕЛИКОМ (до конца строки), а не по префиксу: иначе ложный тег строки,
# случайно попавший перед настоящим, даёт «ключ» вида fSecondaryFrame[...]<мусор>, сканер
# перескакивает через него по мнимой длине и теряет следующий реальный объект.
# Форма: Класс[GUID-окна] + сегменты .Класс[имя] либо .Класс (напр. .CommandPanel).
_KEY_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]*\[[0-9a-fA-F-]{36}\]'
                     r'(\.[A-Za-z][A-Za-z0-9]*(\[[^\[\]]*\])?)*$')
_TITLE_MARK = bytes.fromhex('82f7')   # заголовок: 82 f7 <len8> <utf16>


def is_object_key(s):
    """Строка — адрес объекта тест-клиента (первый сегмент Класс[GUID-окна]).
    Единственное определение адреса в проекте: им пользуется и сканер кадров, и слой
    представления ответов. Значение ячейки таблицы вроде 'Order[2026].Line[A]' адресом не
    является — GUID окна обязателен."""
    return isinstance(s, str) and bool(_KEY_RE.match(s))


def _scan_strings(raw):
    """Позиции строк-ключей: список (pos_tag, key, handle_le_или_None).
    Понимает и ДЛИННЫЕ формы строк (len16: _b ASCII / _8 UTF-16) — длинные ключи реально
    приходят от 1С у глубоко вложенных элементов (контекстные меню таблиц)."""
    out = []; i = 0; n = len(raw); caption_at = None
    while i < n - 1:
        t = raw[i]; ln = raw[i+1]; s = None; adv = 1
        ln16 = struct.unpack_from('<H', raw, i+1)[0] if i+3 <= n else 0
        if t in (0x9a, 0xba, 0xda, 0xfa) and i+2+ln <= n:
            try: s = raw[i+2:i+2+ln].decode('latin1'); adv = 2+ln
            except Exception: s = None
        elif t in (0x97, 0xb7, 0xd7, 0xf7) and i+2+ln*2 <= n:
            try: s = raw[i+2:i+2+ln*2].decode('utf-16le'); adv = 2+ln*2
            except Exception: s = None
        elif t in (0x9b, 0xbb, 0xdb, 0xfb) and i+3+ln16 <= n:
            try: s = raw[i+3:i+3+ln16].decode('latin1'); adv = 3+ln16
            except Exception: s = None
        elif t in (0x98, 0xb8, 0xd8, 0xf8) and i+3+ln16*2 <= n:
            try: s = raw[i+3:i+3+ln16*2].decode('utf-16le'); adv = 3+ln16*2
            except Exception: s = None
        if s is not None and _KEY_RE.match(s):
            # A caption may itself look exactly like an object address.
            if i == caption_at:
                i += adv
                continue
            handle = raw[i-16:i] if i >= 16 else None       # 16 байт перед тегом
            out.append((i, s, handle)); i += adv
            caption_at = i + (1 if i < n and raw[i] in (0x81, 0x82) else 0)
        else:
            i += 1
    return out


def object_handles(raw, parent=None):
    """Отобразить ключ объекта -> его handle (GUID-строка) по всему кадру.
    parent — ключ объекта-получателя: в ответе он приходит служебным эхом и в состав
    подчинённых не входит."""
    m = {}
    for _, key, h in _scan_strings(raw):
        if key == parent:
            continue
        if h is not None and key not in m:
            try: m[key] = str(uuid.UUID(bytes_le=h))
            except Exception: pass
    return m


# Тип элемента = f(КЛАСС, байт-код). Код лежит перед завершителем после имени.
# Значения — идентификаторы перечней платформы (FormFieldType, FormGroupType, FormButtonType,
# FormDecorationType, FormItemAdditionType), а не перевод: перевод уже приводил к тому, что
# элемент получал имя ЧУЖОГО типа.
# ВАЖНО: один и тот же код в разных классах означает РАЗНОЕ, поэтому карта class-aware.
# Класса здесь нет ⇒ типа у него нет: у платформы для него перечня не существует, а придумывать
# слово нельзя — непустой type обязан быть значением перечня.
_TYPE_BY_CLASS = {
    'EditField': {                       # FormFieldType
        0xe2: 'LabelField', 0xe3: 'InputField', 0xe4: 'CheckBoxField',
        0xe5: 'PictureField', 0xe6: 'RadioButtonField',
        0xe7: 'SpreadsheetDocumentField', 0xe8: 'TextDocumentField',
        0xe9: 'CalendarField', 0xea: 'ProgressBarField',
        # 0xeb намеренно отсутствует: этот код делят восемь разных полей, и общего значения у
        # него нет. Он разбирается ТОЛЬКО по подтипу (см. _TYPE_SUBTYPE); неизвестный подтип
        # оставляет тип пустым, потому что «не знаю» вернее правдоподобной подмены
    },
    'Additional': {                      # FormItemAdditionType — ровно три значения
        0xe1: 'SearchStringRepresentation',
        0xe2: 'ViewStatusRepresentation',
        0xe3: 'SearchControl',
    },
    'Group': {                           # FormGroupType — ровно восемь значений
        0xe1: 'CommandBar', 0xe2: 'Popup', 0xe3: 'ColumnGroup',
        0xe4: 'Pages', 0xe5: 'Page', 0xe6: 'UsualGroup',
        0xe7: 'ButtonGroup', 0xe9: 'ContextMenu',
    },
    'Button': {                          # FormButtonType — ровно четыре значения
        0xe1: 'CommandBarButton', 0xe2: 'UsualButton',
        0xe3: 'Hyperlink', 0xe4: 'CommandBarHyperlink',
    },
    'Decoration': {0xe1: 'Label', 0xe2: 'Picture'},   # FormDecorationType
}
# 0xeb у EditField неоднозначен — различаем по СЛЕДУЮЩЕМУ байту (подтип), иначе полоса
# регулирования выглядела бы как HTML-поле и попадала в фильтр по типу документа.
_TYPE_SUBTYPE = {
    # 0xeb — код «сложного» поля: сам по себе он не значит ничего, конкретику несёт подтип
    ('EditField', 0xeb): {0x0a: 'TrackBarField',
                          0x0b: 'ChartField',
                          0x0c: 'GanttChartField',
                          0x0d: 'DendrogramField',
                          0x0e: 'GraphicalSchemaField',
                          0x0f: 'HTMLDocumentField',
                          0x10: 'GeographicalSchemaField',
                          0x11: 'FormattedDocumentField',
                          0x12: 'PeriodField',
                          0x13: 'PlannerField',
                          0x14: 'PDFDocumentField'},
}
_VIEW_ANCHOR = bytes.fromhex('20eb2395')       # завершитель: <код> 20 eb 23 95 (после имени)
_VIEW_ALT_ANCHOR = bytes.fromhex('20a1')       # вторая раскладка того же места
_HANDLE_LEN = 16                               # запись: <handle 16 байт><строка-ключа>

# Классы, которые встречаются в ответах. Перечень отдельный от карты видов: у окна и стартовой
# страницы видов нет вовсе, но классы существуют, и объявлять их неизвестными — неправда.
# Часть классов — подобъекты, в ключе они идут суффиксом без скобок: у элемента это своё
# контекстное меню или редактор (EditField[Поле].ContextMenu, EditField[Д].MoxelEditField),
# у окна — командный интерфейс (SecondaryFrame[..].CI), а внутри него панели разделов и
# открытых окон с кнопками командного интерфейса
_CLASSES = frozenset((
    'Additional', 'Button', 'CI', 'CIButton', 'CommandPanel', 'ContextMenu', 'Decoration',
    'EditField', 'Group', 'HomePage', 'MainFrame', 'ManagedForm', 'MoxelEditField',
    'Navigation', 'SecondaryFrame', 'SubSystems', 'SWINOpened', 'Table', 'UnmanagedForm',
))

def _record_bounds(raw, spans, idx):
    """Разметка СОБСТВЕННОЙ записи объекта -> (конец ключа, начало поиска кода, конец записи,
    заголовок).

    Единственное определение в проекте: по нему работает и разбор коллекции, и служебное
    чтение кодов. Две копии этой разметки разъезжаются молча — тогда приёмка проверяет не ту
    выборку кода, которая работает на самом деле."""
    pos, key = spans[idx][0], spans[idx][1]
    # Конец ключа зависит от ТЕГА: байт на символ у ASCII (_a/_b) против двух у UTF-16 (_7/_8),
    # плюс длина занимает 1 или 2 байта. Иначе заголовок ищется мимо и теряется.
    low = raw[pos] & 0x0f
    header = 2 if low in (0x0a, 0x07) else 3
    length = int.from_bytes(raw[pos+1:pos+header], 'little')
    ke = pos + header + length * (1 if low in (0x0a, 0x0b) else 2)
    # Запись устроена как <handle 16 байт><строка-ключа>, а _scan_strings отдаёт позицию тега
    # ключа, а не начало записи. Без вычета длины handle разбор заглядывает в двоичный GUID
    # соседа: там может встретиться завершитель, и объект получит чужой вид — тихо и правдоподобно
    if idx + 1 < len(spans):
        nxt_pos, nxt_h = spans[idx + 1][0], spans[idx + 1][2]
        rec_end = nxt_pos - _HANDLE_LEN if nxt_h is not None else nxt_pos
    else:
        rec_end = len(raw)
    rec_end = max(rec_end, ke)
    # После ключа идут заголовок и имя; код лежит ЗА ними, поэтому поиск начинается с конца
    # имени, а не с конца ключа — иначе завершитель может найтись внутри байтов заголовка
    title, q = _read_record_string(raw, ke, rec_end)
    _name, q2 = _read_record_string(raw, q, rec_end)
    return ke, q2, rec_end, title


def _code_at(raw, q2, rec_end):
    """Код вида и подтип из собственной записи объекта -> (код, подтип) либо (None, None).

    Код — байт диапазона e0..ef перед ЗАВЕРШИТЕЛЕМ, идущим после имени; между кодом и
    завершителем может стоять байт ПОДТИПА — он и различает виды с общим кодом. Завершителей
    два, и раскладка выбирается платформой: у одних элементов идёт длинный "20 eb 23 95", у
    других короткий "20 a1". Берётся ближайший к имени — иначе код был бы прочитан из участка
    следующего объекта."""
    ends = [p for p in (raw.find(_VIEW_ANCHOR, q2, rec_end),
                        raw.find(_VIEW_ALT_ANCHOR, q2, rec_end)) if p > 0]
    end = min(ends) if ends else -1
    if end <= 0:
        return None, None
    cpos = next((b for b in range(end-1, max(q2-1, end-6), -1)
                 if 0xe0 <= raw[b] <= 0xef), None)
    if cpos is None:
        return None, None
    return raw[cpos], (raw[cpos+1] if cpos + 1 < end else None)


def type_codes(raw, parent=None):
    """Служебное: ключ объекта -> (код, подтип) как они лежат в кадре.

    Наружу код не отдаётся — он сырой байт протокола. Но приёмке он нужен, чтобы проверять
    цепочку «кадр -> код -> значение перечня», не полагаясь на саму таблицу значений. Разметка
    берётся общая с разбором коллекции, иначе проверялась бы другая выборка."""
    out = {}
    spans = _scan_strings(raw)
    for idx, (pos, key, h) in enumerate(spans):
        if key == parent:
            continue
        _ke, q2, rec_end, _title = _record_bounds(raw, spans, idx)
        code, sub = _code_at(raw, q2, rec_end)
        if code is not None:
            out[key] = (code, sub)
    return out


def known_classes():
    """Классы объектов, которые встречаются в ответах. Служит только для диагностики — отличить
    опечатку в имени класса от объекта, который ещё не появился. Наличие вида у класса значения
    не имеет: класс из ключа сохраняется и тогда, когда вида у него нет."""
    return _CLASSES


def known_types():
    """Значения `type`, которые декодер вообще способен отдать. Служит той же диагностике, что и
    known_classes: отличить опечатку в виде от объекта, который ещё не появился. Владелец знания о
    видах — эта таблица, поэтому перечень отдаётся отсюда, а не собирается на стороне сервера."""
    return sorted({v for m in _TYPE_BY_CLASS.values() for v in m.values()} |
                  {v for m in _TYPE_SUBTYPE.values() for v in m.values()})


def object_name(key):
    """Имя объекта из адреса: содержимое скобок последнего сегмента Класс[Имя].
    None, если последний сегмент без скобок (напр. '...CommandPanel') — имени у такого элемента
    нет, и подставлять сюда имя родителя нельзя.

    Единственное определение деривации имени в проекте: им пользуется и декодер, и слой
    представления ответов, который по нему решает, дублирует ли колонка `name` данные ключа."""
    seg = str(key).split('.')[-1]
    if '[' in seg and seg.endswith(']'):
        return seg.split('[', 1)[1][:-1]
    return None


def _key_name_class(key):
    """Из последнего сегмента ключа Класс[Имя] -> (имя, класс)."""
    seg = key.split('.')[-1]
    if '[' in seg and seg.endswith(']'):
        return object_name(key), seg.split('[', 1)[0]
    return None, seg

def _read_string(raw, i, end):
    """One complete string at its declared position, bounded by its object record."""
    end = min(end, len(raw))
    if i >= end or raw[i] not in (0xf7, 0xf8, 0xfa, 0xfb):
        return None, i, False
    ascii_ = raw[i] in (0xfa, 0xfb)
    header = 3 if raw[i] in (0xf8, 0xfb) else 2
    if i + header > end: return None, i, False
    n = int.from_bytes(raw[i+1:i+header], 'little')
    nxt = i + header + n * (1 if ascii_ else 2)
    if nxt > end: return None, i, False
    try: text = raw[i+header:nxt].decode('latin1' if ascii_ else 'utf-16le')
    except UnicodeDecodeError: return None, i, False
    return text, nxt, ascii_


def _read_record_string(raw, i, end):
    # Unlike metadata scanning, do not skip e1: it means this caption is absent.
    j = i
    if j < end and raw[j] in (0x81, 0x82): j += 1
    text, nxt, _ = _read_string(raw, j, end)
    return (text, nxt) if text is not None else (None, i)

def _read_str_after(raw, i, end):
    """Строка по смещению i: [81|82|e1|e2], ASCII или UTF-16, длина len8 или len16.
    -> (строка, конец, ascii_ли). За границу участка end не выходит: тег ASCII-строки совпадает
    по кодировке с тегом ключа, и без границы чтение утащило бы ключ СЛЕДУЮЩЕГО объекта."""
    j = i
    while j < end and raw[j] in (0x81, 0x82, 0xe1, 0xe2):
        j += 1
    text, nxt, ascii_ = _read_string(raw, j, end)
    return (text, nxt, ascii_) if text is not None else (None, i, False)

def _metadata_name(raw, i, end, cls):
    """Имя объекта в метаданных конфигурации: строка сразу за ASCII-именем его класса.

    Раскладка участка плавает: при наличии заголовка идёт <заголовок> <класс> <имя>, при
    отсутствии — признак 0xe1 вместо заголовка. Поэтому опорой служит имя класса, а не смещение.
    Само имя приходит и в UTF-16, и в ASCII — кодировка выбирается по содержимому.
    None, если такой строки в участке нет (например, у служебного эха запрошенного объекта)."""
    for _ in range(4):
        s, nxt, is_ascii = _read_str_after(raw, i, end)
        if s is None:
            return None
        if is_ascii and s == cls:
            return _read_str_after(raw, nxt, end)[0]
        i = nxt
    return None

def decode_collection(raw, parent=None):
    """Ответ GetChildObjects -> [{key, handle, title, name, class, type, form_name}].
    title/name приходят в ответе; type — значение перечня платформы, выбранное по байт-коду
    после них (сам код наружу не отдаётся); class — из ключа; form_name — только у управляемой
    формы и только когда имя реально прочитано.
    parent — ключ объекта-получателя: он приходит служебным эхом конверта и подчинённым
    самому себе не является, поэтому в коллекцию не попадает."""
    items = []
    spans = _scan_strings(raw)
    for idx, (pos, key, h) in enumerate(spans):
        if key == parent:
            continue
        item = {'key': key}
        if h is not None:
            try: item['handle'] = str(uuid.UUID(bytes_le=h))
            except Exception: pass
        ke, q2, rec_end, title = _record_bounds(raw, spans, idx)
        # заголовок кладём ВСЕГДА, в том числе None: поиск объектов отдаёт отсутствующий
        # заголовок как null, и два пути не должны отвечать на один вопрос по-разному
        item['title'] = title
        nm, cls = _key_name_class(key)
        if nm is not None: item['name'] = nm
        item['class'] = cls
        # Имя формы в метаданных — единственное сведение участка, которое не выражается ни
        # ключом, ни заголовком: у формы name равен GUID из скобок ключа, а title бывает пуст.
        # Ключ не ставим, когда имени нет: отсутствие поля и пустое значение — разные утверждения
        if cls == 'ManagedForm':
            fname = _metadata_name(raw, ke, rec_end, cls)
            if fname is not None: item['form_name'] = fname
        # Сам код наружу НЕ отдаётся: это сырой байт протокола, которого нет ни в одной
        # документации и который потребителю не истолковать. Он служит только для выбора
        # значения по таблице
        code, subtype = _code_at(raw, q2, rec_end)
        if code is not None:
            byclass = _TYPE_BY_CLASS.get(cls)
            tp = _TYPE_SUBTYPE.get((cls, code), {}).get(subtype)
            if tp is None:
                # без класса код не значит ничего определённого: один и тот же код у разных
                # классов означает разное, поэтому для класса вне карты типа нет
                tp = byclass.get(code) if byclass is not None else None
            item['type'] = tp
        items.append(item)
    return items
