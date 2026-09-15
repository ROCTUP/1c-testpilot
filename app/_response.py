# -*- coding: utf-8 -*-
"""Представление ответов MCP наружу: формат вывода и компактизация длинных адресов.

Границы владения: tc1c — протокол, _collection — декодирование сырых коллекций,
этот модуль — представление наружу, server — инструменты и оркестрация.

Формат и компактизация задаются окружением:
  TC1C_RESPONSE_FORMAT = toon (по умолчанию) | json
  TC1C_COMPACT_REFS    = id (по умолчанию) | prefix | off
"""
import os, re, sys, uuid
import _collection

try:
    from toon_format import encode as _toon_encode
except ImportError:
    _toon_encode = None

_REQ_FMT = os.environ.get('TC1C_RESPONSE_FORMAT', 'toon').strip().lower()
_REQ_REFS = os.environ.get('TC1C_COMPACT_REFS', 'id').strip().lower()
REF_MODE = {'true': 'prefix', '1': 'prefix', 'yes': 'prefix',
            'false': 'off', '0': 'off', 'no': 'off'}.get(_REQ_REFS, _REQ_REFS)
if REF_MODE not in ('off', 'prefix', 'id'):
    raise ValueError('TC1C_COMPACT_REFS must be off, prefix or id')

# Эффективный формат вычисляется ОДИН раз, до регистрации инструментов: и сериализация, и
# подсказки в описаниях берутся из него. Иначе при недоступном кодировщике ответы ушли бы в JSON
# с полными адресами, а описания продолжали бы требовать разворачивать ссылки.
FORMAT = 'toon' if (_REQ_FMT == 'toon' and _toon_encode is not None) else 'json'
if _REQ_FMT == 'toon' and _toon_encode is None:
    sys.stderr.write('tc1c: запрошен TC1C_RESPONSE_FORMAT=toon, но пакет toon-format недоступен — '
                     'ответы отдаются в JSON\n')
COMPACT = REF_MODE == 'prefix' and FORMAT == 'toon'

_REF_RE = re.compile(r'^@[ph]:\d+')

# Инструменты, чьи ответы содержат АДРЕСА объектов UI. Единственный источник истины: этим же
# набором включается компактизация и добавляется правило разворачивания в описание, поэтому
# «ответ содержит ссылки» и «инструмент объявил правило» совпадают по построению.
# Решение принимается по ИСТОЧНИКУ ответа, а не по виду значений: ячейка таблицы может содержать
# настоящий адрес, но tc_get_selected_rows возвращает данные, а не адресуемые объекты, и его
# значения подменять ссылками нельзя.
ADDR_TOOLS = frozenset({
    'tc_create_snapshot', 'tc_compare_snapshot',
    'tc_get_child_objects', 'tc_find_objects', 'tc_find_object', 'tc_get_context_menu',
    'tc_get_parent', 'tc_get_command_bar', 'tc_get_command_interface',
    'tc_get_current_page', 'tc_find_default_button', 'tc_get_current_item', 'tc_get_current_element',
    'tc_set_row_values',
    'tc_get_linked_window', 'tc_get_current_area_field',
    'tc_start_choosing', 'tc_execute_command',
})
# Параметры инструментов, несущие адрес объекта: только их проверяем на неразвёрнутые ссылки,
# чтобы не отклонять обычный текст, начинающийся с '@'.
_ADDR_ARGS = ('key', 'handle', 'root_key')

HINT = ("\n\nResponse format is TOON. Addresses are compacted: a `key` starting with `@p:N` expands "
        "to base_prefixes[\"@p:N\"] + the rest of the key, and a `handle` of `@h:N` expands to "
        "handles[\"@h:N\"]. A base_prefixes value may itself start with `@p:N` — repeat the "
        "substitution until it does not, at most 3 times. These dictionaries belong to THIS "
        "response only — the same number in another response means something else. Always expand "
        "key and handle to their full form before passing them to other tools.\n"
        "Element name: if a `name` column is present, use its value as is. If it is absent, the "
        "name is the text inside the brackets of the last segment of the FULLY EXPANDED key "
        "(`Class[Name]`); a last segment without brackets means the element has no name.")


def _is_hier_key(v):
    """Значение — адрес объекта. Грамматика одна на проект и живёт в декодере: иначе
    определения расходятся и обычная строка ячейки принимается за адрес."""
    return _collection.is_object_key(v)


def _is_guid(v):
    if not isinstance(v, str):
        return False
    try:
        uuid.UUID(v)
        return True
    except Exception:
        return False


def _parent(key):
    """Родительский путь БЕЗ завершающей точки ('' если родителя нет).

    Точка остаётся в остатке ключа, поэтому ссылка выглядит как '@p:1.EditField[Поле]', а не
    слипается в '@p:1EditField[Поле]'. Разворачивание при этом остаётся простой конкатенацией,
    а точка заодно однозначно ограничивает номер ссылки."""
    i = key.rfind('].')
    return key[:i + 1] if i >= 0 else ''


def _norm_rows(rows):
    """Однородная таблица для TOON: объединение РЕАЛЬНО присутствующих полей (в порядке первого
    появления). '' подставляется только в ОТСУТСТВУЮЩИЕ ячейки; присутствующие значения, включая
    None, передаются как есть — TOON различает null и пустую строку, а `type`/`title` штатно
    бывают None (у класса без перечня платформы типа нет вовсе)."""
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    out = []
    for r in rows:
        out.append({c: (r[c] if c in r else '') for c in cols})
    return out


def _is_row_list(v):
    return isinstance(v, list) and v and all(isinstance(x, dict) for x in v)


def _name_is_redundant(rows):
    """Дублирует ли колонка `name` данные ключа У ВСЕХ строк.

    Удаляем колонку только при полном совпадении: если хоть одна строка расходится, переданное
    значение является истинным и должно дойти до потребителя. Деривация берётся из декодера —
    второй реализации этого правила в проекте быть не должно."""
    seen = False
    for r in rows:
        if 'name' not in r:
            continue
        seen = True
        if not _is_hier_key(r.get('key')) or r.get('name') != _collection.object_name(r['key']):
            return False
    return seen


def _drop_name(rows):
    return [{k: v for k, v in r.items() if k != 'name'} for r in rows]


def _squash_legend(pmap, max_depth=3):
    """Сжать сам словарь префиксов: значения записей выражаются через уже заведённые ссылки.

    Глубина ограничена жёстко (по умолчанию 3), поэтому агент делает не более трёх подстановок
    подряд. Ключи строк к этому моменту уже подставлены по ИСХОДНЫМ путям, поэтому сжатие словаря
    на них не влияет."""
    if max_depth <= 1 or not pmap:
        return {ref: path for path, ref in pmap.items()}
    depth, out = {}, {}
    for path, ref in sorted(pmap.items(), key=lambda kv: len(kv[0])):
        base = None
        for other, oref in pmap.items():
            if oref in out and depth[oref] < max_depth and path.startswith(other + '.'):
                if base is None or len(other) > len(base[0]):
                    base = (other, oref)
        if base:
            other, oref = base
            out[ref] = oref + path[len(other):]
            depth[ref] = depth[oref] + 1
        else:
            out[ref] = path
            depth[ref] = 1
    return out


def _is_node_list(rows):
    """Коллекция UI-объектов: хотя бы у одной строки `key` — иерархический адрес.
    Строка табличных данных под это не подходит, даже если у неё есть колонка `handle` с GUID,
    поэтому её значения никогда не подменяются ссылками."""
    return any(_is_hier_key(r.get('key')) for r in rows)


def _compact_payload(payload):
    """Заменить повторяющиеся адреса ссылками. Легенды общие на ответ: base_prefixes/handles."""
    if not isinstance(payload, dict):
        return payload
    lists = [(k, v) for k, v in payload.items() if _is_row_list(v)]
    if not lists:
        return payload
    # внутри адресного ответа значение всё равно проверяется по грамматике: в коллекции узлов
    # попадаются поля, адресами не являющиеся, и их подменять нельзя
    nodes = [(k, v) for k, v in lists if _is_node_list(v)]
    pcount, hcount = {}, {}
    for _, rows in nodes:
        for r in rows:
            k = r.get('key')
            if _is_hier_key(k):
                p = _parent(k)
                if p:
                    pcount[p] = pcount.get(p, 0) + 1
            h = r.get('handle')
            if _is_guid(h):
                hcount[h] = hcount.get(h, 0) + 1
    # ссылку заводим только если она реально экономит: значение встречается больше одного раза
    pmap, hmap = {}, {}
    for p, n in pcount.items():
        if n > 1:
            pmap[p] = '@p:%d' % (len(pmap) + 1)
    for h, n in hcount.items():
        if n > 1:
            hmap[h] = '@h:%d' % (len(hmap) + 1)

    out = dict(payload)
    node_names = {n for n, _ in nodes}
    for name, rows in lists:
        if name in node_names and _name_is_redundant(rows):
            rows = _drop_name(rows)     # ДО нормализации, иначе колонка вернётся пустыми ячейками
        norm = _norm_rows(rows)
        if name not in node_names:      # табличные данные: только однородная форма, без ссылок
            out[name] = norm
            continue
        # от длинного к короткому: узел в цепочке одиночных групп сам ссылкой не становится,
        # но выражается через уже заведённую ссылку выше по дереву
        order = sorted(pmap, key=len, reverse=True)
        new = []
        for r in norm:
            d = dict(r)
            k = d.get('key')
            if _is_hier_key(k):
                for p in order:
                    if k.startswith(p + '.'):
                        d['key'] = pmap[p] + k[len(p):]
                        break
            h = d.get('handle')
            if h in hmap:
                d['handle'] = hmap[h]
            new.append(d)
        out[name] = new
    # Словари идут ПЕРЕД таблицами узлов: длинный ответ читают с начала и часто только начало,
    # поэтому расшифровка должна встретиться раньше первой ссылки на неё.
    if pmap or hmap:
        legend = {}
        if pmap:
            legend['base_prefixes'] = _squash_legend(pmap)
        if hmap:
            legend['handles'] = {v: k for k, v in hmap.items()}
        legend.update(out)
        out = legend
    return out


def _norm_payload(payload):
    """Только приведение списков к однородной форме, без ссылок."""
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    for k, v in payload.items():
        if _is_row_list(v):
            out[k] = _norm_rows(v)
    return out


def respond(payload, addrs=False):
    """Ответ инструмента наружу. В json-режиме — без изменений.

    addrs=True только для инструментов из ADDR_TOOLS: лишь их значения могут стать ссылками."""
    if FORMAT != 'toon':
        return payload
    # не-dict/list возвращаем как есть: у tc_connect/tc_disconnect объявлен '-> str' и есть
    # output schema, подмена типа сломала бы валидацию структурированного вывода
    if not isinstance(payload, (dict, list)):
        return payload
    data = _compact_payload(payload) if (COMPACT and addrs) else _norm_payload(payload)
    return _toon_encode(data)


def check_ref_args(kwargs):
    """Отклонить неразвёрнутую ссылку до отправки команды.

    Без этой проверки поломка тихая: адрес вида '@p:1.EditField[X]' уходит в 1С как есть и
    попадает в журнал команд, из которого собирается сценарий, — записанный сценарий портится."""
    if not COMPACT:
        return
    for name in _ADDR_ARGS:
        v = kwargs.get(name)
        if isinstance(v, str) and _REF_RE.match(v):
            raise ValueError(
                '%s=%r: ссылка не развёрнута. Подставь значение из base_prefixes/handles ТОГО ЖЕ '
                'ответа и передай полный адрес.' % (name, v))
