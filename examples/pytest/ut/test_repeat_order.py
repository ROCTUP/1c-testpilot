"""Повторный заказ клиента: копирование, пересчёт, запись и повторное открытие.

Демобаза УТ 11.5.27.88. Каждый прогон оставляет новый непроведённый заказ
с уникальным комментарием. Исходный заказ не редактируется.
"""
from datetime import datetime
from decimal import Decimal
import json
import time
from uuid import uuid4
from testpilot import ActionError

LIST_FORM = 'Документ.ЗаказКлиента.Форма.ФормаСпискаДокументов'
ORDER_FORM = 'Документ.ЗаказКлиента.Форма.ФормаДокумента'
SOURCE = {'Номер': 'ТД00-000001', 'Дата': '06.04.2022', 'Клиент': 'Протон'}
PRODUCT = 'Телевизор "SHARP"'


def number(text):
    """Сравниваем числа, включая неразрывные пробелы и десятичную запятую 1С."""
    return Decimal(''.join(text.split()).replace(',', '.'))


def artifact(directory, name, value):
    (directory / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def wait_for_start(client):
    # Подключение к тест-клиенту устанавливается раньше окончания запуска БСП.
    def probe():
        result = client.call('get_active_window', check=False)
        if result.get('error') or result.get('exception'):
            raise ActionError('get_active_window', result)
        return result
    return client.wait_until(
        'Ожидание готовности клиента',
        probe,
        condition=lambda result: bool(result.get('ok') and result.get('key')),
        timeout=120, interval=1)


def active_form(client, expected):
    deadline = time.monotonic() + 60
    last = []
    while time.monotonic() < deadline:
        forms = client.find_objects(cls='ManagedForm')
        last = [form.info.get('form_name') for form in forms]
        if len(forms) == 1 and last == [expected]:
            return forms[0]
        if last == ['ОбщаяФорма.ПодключениеИнтернетПоддержки']:
            client.close_window()
        else:
            time.sleep(.5)
    raise AssertionError(f'Ожидалась форма {expected}, открыты: {last}')


def read_values(client, form, names):
    answer = client.read_fields(
        [form.find_object(name=name) for name in names], properties=['presentation'])
    assert len(answer['results']) == len(names), answer
    values = {}
    for name, item in zip(names, answer['results']):
        assert item['status'] == 'read', item
        value = item['values']['presentation']
        assert value is not None, f'Не прочитано поле {name}: {item}'
        values[name] = value
    return values


def read_order(client, form):
    form.find_object(name='ГруппаОсновное').activate()
    header = read_values(client, form, ['Номер', 'Партнер', 'Организация', 'Комментарий'])
    form.find_object(name='ГруппаТовары').activate()
    answer = form.find_object(name='Товары', cls='Table').read_rows(max_rows=10)
    assert not answer['truncated'], answer
    total = form.find_object(name='СуммаВсегоСНДС').get_data_presentation()
    return {'header': header, 'rows': answer['rows'], 'total': total}


def assert_order(order, quantities):
    assert order['header']['Партнер'] == 'Протон', order['header']
    assert order['header']['Организация'] == 'Торговый дом "Комплексный"', order['header']
    assert len(order['rows']) == 2, order['rows']
    for index, (row, quantity) in enumerate(zip(order['rows'], quantities), start=1):
        assert row['N'] == str(index), row
        assert row['Номенклатура'] == PRODUCT, row
        assert number(row['Количество']) == quantity, row
        assert number(row['Цена']) == Decimal('14000'), row
        assert number(row['Сумма']) == quantity * Decimal('14000'), row
        assert number(row['Сумма с НДС']) == quantity * Decimal('14000'), row
    assert number(order['total']) == sum(quantities) * Decimal('14000'), order


def source_row(list_form):
    table = list_form.find_object(name='Список', cls='Table')
    table.goto_first_row()
    found = table.goto_row(fields=SOURCE)
    assert found['found'] is True, f'Не найден исходный демозаказ: {found}'
    return table


def test_repeat_customer_order(testpilot, testpilot_artifacts):
    client, out = testpilot, testpilot_artifacts
    marker = f'Testpilot pytest {datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}'
    customer_number = f'TP-{uuid4().hex[:10]}'
    client.set_max_action_time(seconds=30)
    wait_for_start(client)

    print('\n1. Открываем исходный заказ Протон от 06.04.2022 и проверяем образец.')
    client.execute_command(command='e1cib/list/Документ.ЗаказКлиента')
    listing = active_form(client, LIST_FORM)
    source_row(listing).choose_row()
    original = active_form(client, ORDER_FORM)
    source_url = client.get_active_window()['url']
    source_before = read_order(client, original)
    assert_order(source_before, [2, 1])
    artifact(out, 'source-before.json', source_before)
    assert original.current_modified()['modified'] is False
    client.close_window()

    print('2. Создаём копию, заполняем номер заказа клиента и комментарий.')
    listing = active_form(client, LIST_FORM)
    source_row(listing)
    listing.find_object(name='СписокСкопировать').click()
    order = active_form(client, ORDER_FORM)
    assert not client.get_active_window().get('url'), 'Ожидался новый незаписанный заказ'
    order.find_object(name='ГруппаДополнительно').activate()
    written = client.set_fields({
        order.find_object(name='НомерПоДаннымКлиента'): customer_number,
        order.find_object(name='Комментарий'): marker,
    })
    assert written['completed'] == 2, written

    print('3. Меняем количества с 2 и 1 на 3 и 2. Проверяем 42 000 + 28 000 = 70 000.')
    order.find_object(name='ГруппаТовары').activate()
    table = order.find_object(name='Товары', cls='Table')
    baseline = order.create_snapshot(include_tables=True, max_rows=10)
    artifact(out, 'snapshot.json', baseline)
    assert any(t['name'] == 'Товары' and t['status'] == 'read' for t in baseline['tables']), baseline
    for row_number, quantity in ((1, '3'), (2, '2')):
        # Снимок на 8.3 оставляет выделение; перед записью явно выбираем нужную строку.
        table.goto_first_row()
        assert table.goto_row(column='N', value=str(row_number))['found'] is True
        written = table.set_row_values(cells=[
            {'column': 'ТоварыКоличествоУпаковок', 'text': quantity},
        ])
        assert written['completed'] == 1 and written['edit_finished'], written

    diff = client.compare_snapshot(snapshot_id=baseline['snapshot_id'])
    artifact(out, 'changes.json', diff)
    changed = next(t for t in diff['table_changes'] if t['name'] == 'Товары')
    assert changed['status'] == 'changed', changed
    quantities = {cell['row']: (number(cell['before']), number(cell['after']))
                  for cell in changed['cells'] if cell['column'] == 'Количество'}
    assert quantities == {1: (2, 3), 2: (1, 2)}, changed
    before_save = read_order(client, order)
    assert_order(before_save, [3, 2])
    assert before_save['header']['Комментарий'] == marker
    artifact(out, 'order-before-save.json', before_save)

    print('4. Записываем заказ обычной кнопкой «Записать» и закрываем форму.')
    saved = order.find_object(name='ФормаЗаписать').click(diagnostics=True)
    artifact(out, 'save-response.json', saved)
    assert order.current_modified()['modified'] is False, saved
    window = client.get_active_window()
    order_url = window.get('url', '')
    assert order_url.startswith('e1cib/data/Документ.ЗаказКлиента?ref='), window
    assert order_url != source_url
    created = {'url': order_url, 'title': window['title'], 'comment': marker,
               'customer_number': customer_number}
    artifact(out, 'created-order.json', created)
    client.close_window()

    print('5. Повторно открываем сохранённый заказ и сверяем реквизиты, строки и итог.')
    client.execute_command(command=order_url)
    reopened = active_form(client, ORDER_FORM)
    actual = read_order(client, reopened)
    artifact(out, 'order-reopened.json', actual)
    assert_order(actual, [3, 2])
    assert actual['header']['Комментарий'] == marker
    assert actual['header']['Номер'].strip()
    assert actual['rows'] == before_save['rows']
    reopened.find_object(name='ГруппаДополнительно').activate()
    assert reopened.find_object(name='НомерПоДаннымКлиента').get_data_presentation() == customer_number
    assert reopened.current_modified()['modified'] is False
    client.close_window()

    print('6. Открываем исходный заказ и подтверждаем, что он не изменился.')
    client.execute_command(command=source_url)
    original = active_form(client, ORDER_FORM)
    source_after = read_order(client, original)
    assert source_after == source_before
    assert original.current_modified()['modified'] is False
    artifact(out, 'source-after.json', source_after)
    client.close_window()
    print(f'Готово: {actual["header"]["Номер"]}, 70 000 RUB; комментарий: {marker}')

