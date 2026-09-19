"""ЗУП: физлицо → сотрудник → проведённый приём с окладом 45 000 рублей.

Каждый запуск оставляет новые тестовые карточки и проведённый документ в демобазе.
"""
from decimal import Decimal
import json
import time
from uuid import uuid4
from testpilot import ActionError


PERSON = 'Справочник.ФизическиеЛица.Форма.ФормаЭлемента'
EMPLOYEE = 'Справочник.Сотрудники.Форма.ФормаЭлемента'
HIRING = 'Документ.ПриемНаРаботу.Форма.ФормаДокумента'
LABOR = 'Справочник.ТрудовыеФункции.Форма.ФормаЭлемента'
ORG = 'Крон-Ц'
DEPARTMENT = 'Отдел по работе с персоналом'
POSITION = 'Начальник отдела'
STAFF_POSITION = f'{POSITION} /{DEPARTMENT}/'
HIRE_DATE = '01.10.2021'
SALARY = Decimal('45000')


def artifact(directory, name, value):
    (directory / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def number(text):
    return Decimal(''.join(text.split()).replace(',', '.'))


def date_part(text):
    return text.split()[0]


def wait_for_start(client):
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


def fill(client, form, values):
    answer = client.set_fields({form.find_object(name=k): v for k, v in values.items()})
    assert answer['completed'] == len(values), answer


def choose(table, criteria):
    # На 8.3 чтение строк может менять выделение. Перед выбором задаём текущую строку.
    table.goto_first_row()
    found = table.goto_row(fields=criteria)
    assert found['found'] is True, f'Не найдена строка {criteria}: {found}'
    table.choose_row()


def reference(client, form, field_name, catalog, criteria):
    field = form.find_object(name=field_name)
    field.activate()
    field.start_choosing()
    selection = active_form(client, f'Справочник.{catalog}.Форма.ФормаСписка')
    choose(selection.find_object(name='Список', cls='Table'), criteria)
    return active_form(client, form.info['form_name'])


def saved_url(client, form, kind):
    assert form.current_modified()['modified'] is False, 'Форма не записана'
    window = client.get_active_window()
    url = window.get('url', '')
    assert url.startswith(f'e1cib/data/{kind}?ref='), window
    return url


def create_labor_function(client, hire, name):
    """В исходной демобазе нет подходящей функции: создаём свою через формы 1С."""
    field = hire.find_object(name='ТрудоваяФункция')
    field.activate()
    field.start_choosing()
    selection = active_form(client, 'Справочник.ТрудовыеФункции.Форма.ФормаСписка')
    selection.find_object(name='ФормаСоздать').click()
    labor = active_form(client, LABOR)
    fill(client, labor, {'Наименование': name})
    code = labor.find_object(name='КодПоОКЗ')
    code.activate()
    code.start_choosing()
    classifier = active_form(client, 'Справочник.КлассификаторЗанятий.Форма.ФормаСписка')
    table = classifier.find_object(name='Список', cls='Table')
    # Папки классификатора открываются выбором строки; запятая отделяет контрольное число.
    for value in ('1', '12, 5', '121, 8', '1212, 5'):
        choose(table, {'Код': value})
    labor = active_form(client, LABOR)
    assert labor.find_object(name='КодПоОКЗ').get_data_presentation() == '1212'
    labor.find_object(name='ФормаЗаписать').click(diagnostics=True)
    url = saved_url(client, labor, 'Справочник.ТрудовыеФункции')
    client.close_window()
    selection = active_form(client, 'Справочник.ТрудовыеФункции.Форма.ФормаСписка')
    choose(selection.find_object(name='Список', cls='Table'), {'Наименование': name})
    active_form(client, HIRING)
    return url


def read_hiring(client, form):
    form.find_object(name='ГлавноеСтраница').activate()
    header = read_values(client, form, [
        'Номер', 'Дата', 'Организация', 'Сотрудник', 'ДатаПриема', 'Подразделение',
        'ПозицияШтатногоРасписания', 'График', 'ТрудоваяФункция', 'КоличествоСтавок',
        'ВидЗанятости', 'ДлительностьИспытательногоСрока', 'Комментарий'])
    form.find_object(name='ОплатаТрудаСтраница').activate()
    rows = form.find_object(name='Начисления', cls='Table').read_rows(max_rows=10)
    assert not rows['truncated'], rows
    return {'header': header, 'rows': rows['rows'],
            'total': form.find_object(name='НачисленияИтогРазмер').get_data_presentation()}


def assert_hiring(data, full_name, labor_name, marker):
    header = data['header']
    assert header['Организация'] == ORG, header
    assert header['Сотрудник'] == full_name, header
    assert date_part(header['Дата']) == HIRE_DATE, header
    assert date_part(header['ДатаПриема']) == HIRE_DATE, header
    assert header['Подразделение'] == DEPARTMENT, header
    assert header['ПозицияШтатногоРасписания'] == STAFF_POSITION, header
    assert header['График'] == 'Пятидневка', header
    assert header['ТрудоваяФункция'] == labor_name, header
    assert header['ВидЗанятости'] == 'Основное место работы', header
    assert number(header['КоличествоСтавок']) == 1, header
    assert number(header['ДлительностьИспытательногоСрока']) == 3, header
    assert header['Комментарий'] == marker, header
    assert len(data['rows']) == 1, data
    row = data['rows'][0]
    assert row['Начисление'] == 'Оплата по окладу', row
    assert row['Показатель1'] == 'Оклад', row
    assert number(row['Значение1']) == SALARY, row
    assert number(row['Вклад в ФОТ']) == SALARY, row
    assert number(data['total']) == SALARY, data


def test_hire_employee_with_salary(testpilot, testpilot_artifacts):
    client, out = testpilot, testpilot_artifacts
    suffix = ''.join('абвгдежзиклмнопр'[int(c, 16)] for c in uuid4().hex[:8])
    surname = 'Тестпилотов' + suffix
    full_name = surname + ' Алексей Сценариевич'
    marker = f'Testpilot pytest — приём {full_name}'
    labor_name = f'Testpilot — начальник отдела кадров {suffix}'
    created = {'full_name': full_name, 'comment': marker}
    artifact(out, 'created.json', created)
    client.set_max_action_time(seconds=30)
    wait_for_start(client)

    print(f'\n1. Создаём физическое лицо: {full_name}.')
    client.execute_command(command='e1cib/data/Справочник.ФизическиеЛица')
    person = active_form(client, PERSON)
    person.find_object(name='ИзменитьФИО').click()
    fio = active_form(client, 'ОбщаяФорма.СменаФИО')
    fill(client, fio, {'Фамилия': surname, 'Имя': 'Алексей',
                      'Отчество': 'Сценариевич', 'ДатаИзменения': HIRE_DATE})
    fio.find_object(name='ОК').click()
    person = active_form(client, PERSON)
    fill(client, person, {'ФизлицоДатаРождения': '17.04.1990', 'ФизлицоПол': 'Мужской'})
    person.find_object(name='КомандаЗаписать').click(diagnostics=True)
    created['person_url'] = saved_url(client, person, 'Справочник.ФизическиеЛица')
    artifact(out, 'created.json', created)
    client.close_window()
    client.execute_command(command=created['person_url'])
    person = active_form(client, PERSON)
    personal = read_values(client, person, ['ФИО', 'ФизлицоДатаРождения', 'ФизлицоПол'])
    assert personal['ФИО'] == full_name, personal
    assert date_part(personal['ФизлицоДатаРождения']) == '17.04.1990', personal
    assert personal['ФизлицоПол'] == 'Мужской', personal
    artifact(out, 'person-reopened.json', personal)

    print('2. Из карточки физлица создаём сотрудника организации Крон-Ц.')
    person.find_object(name='НовоеМестоРаботы').click()
    employee = active_form(client, EMPLOYEE)
    fill(client, employee, {'ГоловнаяОрганизация': ORG})
    employee.find_object(name='КомандаЗаписать').click(diagnostics=True)
    created['employee_url'] = saved_url(client, employee, 'Справочник.Сотрудники')
    artifact(out, 'created.json', created)
    assert employee.find_object(name='ФИО').get_data_presentation() == full_name
    employee.find_object(name='КнопкаНовогоОформитьПриемНаРаботу').click()
    hire = active_form(client, HIRING)

    print('3. Оформляем приём начальника отдела: дата, подразделение, должность и график.')
    fill(client, hire, {'Дата': HIRE_DATE, 'ДатаПриема': HIRE_DATE})
    hire = reference(client, hire, 'Подразделение', 'ПодразделенияОрганизаций',
                     {'Наименование': DEPARTMENT})
    hire = reference(client, hire, 'ПозицияШтатногоРасписания', 'ШтатноеРасписание',
                     {'Должность': POSITION, 'Подразделение': DEPARTMENT})
    created['labor_function_url'] = create_labor_function(client, hire, labor_name)
    artifact(out, 'created.json', created)
    hire = active_form(client, HIRING)
    hire = reference(client, hire, 'График', 'ГрафикиРаботыСотрудников',
                     {'Наименование': 'Пятидневка'})
    # Сначала выбираем кадровые условия, затем заполняем оставшиеся поля приёма.
    fill(client, hire, {'ДлительностьИспытательногоСрока': '3,0', 'Комментарий': marker})

    print('4. Назначаем оклад 45 000 рублей и проверяем начисление и ФОТ.')
    hire.find_object(name='ОплатаТрудаСтраница').activate()
    hire.find_object(name='РедактироватьФОТ').click()
    table = hire.find_object(name='Начисления', cls='Table')
    table.goto_first_row()
    assert table.goto_row(column='Начисление', value='Оплата по окладу')['found'] is True
    written = table.set_row_values(cells=[{'column': 'НачисленияЗначение1', 'text': str(SALARY)}])
    assert written['completed'] == 1 and written['edit_finished'], written
    before = read_hiring(client, hire)
    artifact(out, 'hiring-before-post.json', before)
    assert_hiring(before, full_name, labor_name, marker)

    print('5. Проводим приём, закрываем документ и проверяем его после открытия.')
    posted = hire.find_object(name='КомандаПровести').click(diagnostics=True)
    artifact(out, 'post-response.json', posted)
    created['hiring_url'] = saved_url(client, hire, 'Документ.ПриемНаРаботу')
    created['hiring_title'] = client.get_active_window()['title']
    artifact(out, 'created.json', created)
    client.close_window()
    # Карточка сотрудника осталась под документом. Закрываем её, чтобы финальная
    # проверка загрузила кадровые сведения заново, а не читала прежнюю форму.
    active_form(client, EMPLOYEE)
    client.close_window()
    client.execute_command(command=created['hiring_url'])
    hire = active_form(client, HIRING)
    reopened = read_hiring(client, hire)
    artifact(out, 'hiring-reopened.json', reopened)
    assert_hiring(reopened, full_name, labor_name, marker)
    assert reopened['header']['Номер'].strip(), reopened
    assert hire.current_modified()['modified'] is False
    client.close_window()

    print('6. Проверяем результат приёма в заново открытой карточке сотрудника.')
    # Эти сведения заполняются результатом проведения; одного ok у кнопки недостаточно.
    client.execute_command(command=created['employee_url'])
    employee = active_form(client, EMPLOYEE)
    state = read_values(client, employee, [
        'ФИО', 'ДатаПриема', 'ТекущееПодразделение', 'ТекущаяДолжность',
        'ТекущаяДолжностьПоШтатномуРасписанию', 'ТекущаяТарифнаяСтавка', 'ТекущийФОТ',
        'ГрафикРаботы', 'ТекущийВидЗанятости', 'КоличествоСтавокПредставление'])
    artifact(out, 'employee-after-hiring.json', state)
    assert state['ФИО'] == full_name, state
    assert date_part(state['ДатаПриема']) == HIRE_DATE, state
    assert state['ТекущееПодразделение'] == DEPARTMENT, state
    assert state['ТекущаяДолжность'] == POSITION, state
    assert state['ТекущаяДолжностьПоШтатномуРасписанию'] == STAFF_POSITION, state
    assert state['ГрафикРаботы'] == 'Пятидневка', state
    assert state['ТекущийВидЗанятости'] == 'Основное место работы', state
    assert number(state['КоличествоСтавокПредставление']) == 1, state
    assert number(state['ТекущаяТарифнаяСтавка']) == SALARY, state
    assert number(state['ТекущийФОТ']) == SALARY, state
    assert employee.current_modified()['modified'] is False
    client.close_window()
    print(f'Готово: {created["hiring_title"]}; {full_name}; оклад 45 000 руб.')
