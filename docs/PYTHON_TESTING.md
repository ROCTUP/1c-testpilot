# Python API и pytest

Агент может исследовать форму через MCP, а затем написать Python-тест и повторно
запускать его через pytest. Python API обращается к клиенту тестирования 1С напрямую:
MCP-сервер запускать не нужно. Оба интерфейса используют общую реализацию операций.

Полноценный пример для демобазы УТ: [повторный заказ клиента](../examples/pytest/ut/README.md)
с копированием документа, групповым заполнением, проверкой сумм и повторным открытием.

Пример для демобазы ЗУП: [приём нового сотрудника](../examples/pytest/zup/README.md)
с созданием физлица и сотрудника, назначением оклада, проведением приёма
и проверкой сохранённых кадровых данных.

Поддерживаемые BSL-сценарии Тестера можно запускать методом
[`client.run_compatible_scenario()`](compatible_scenarios/README.md) из обычного Python-кода
и из pytest. Настройка видимости этого инструмента в MCP на Python API не влияет.

## Установка и запуск

Из каталога проекта:

```bash
python -m pip install ".[test]"
```

Задайте `TC1C_PROFILES_FILE` с путём к YAML-файлу профилей. Профили и переменные
`password_env` используются так же, как при работе через MCP. Затем:

```bash
python -m pytest tests/test_document.py --tc-profile ut_admin --tc-artifacts test-results -v
```

Плагин pytest регистрируется при установке пакета. При работе из исходников без
установки добавьте `app` в `PYTHONPATH` и передайте `-p testpilot_pytest`.
Настройки `TC1C_*` должны быть заданы до импорта `testpilot`; pytest сам не читает `.env`.

## Первый тест

`testpilot` — fixture, которая для каждого теста создаёт отдельное подключение.
Профиль с `base` запускает клиент; профиль с `host`/`port` подключает уже запущенный.
После теста запущенный клиент завершается штатно с подтверждением известных вопросов выхода.
Если за 15 секунд он не завершился, процесс останавливается принудительно. Это также
действует для `Client.close()`. Для явной остановки можно вызвать
`client.stop_client(graceful_timeout=30)`; допустимо 0–120 секунд, 0 — сразу принудительно.
Ответ содержит `shutdown`: `graceful`, `forced` или `already_exited`; при `forced`
поле `shutdown_reason` объясняет причину. Несохранённые изменения могут быть потеряны.

Следующий пример рассчитан на конфигурацию с документом `ЗаказКлиента` и полем
`Комментарий`. Имена нужно предварительно проверить в вашей базе через MCP.

```python
import pytest


@pytest.fixture
def document(testpilot):
    testpilot.execute_command(command="e1cib/data/Документ.ЗаказКлиента")
    form = testpilot.find_object(cls="ManagedForm", timeout=30)
    context = form.get_context()
    assert context["form"]["form_name"].startswith("Документ.ЗаказКлиента.")
    return form


def test_comment(testpilot, document):
    comment = document.find_object(name="Комментарий")
    comment.input_text(text="Проверка заполнения")
    assert comment.get_data_presentation() == "Проверка заполнения"
```

Проверка имени формы отличает целевую карточку от неожиданного диалога при открытии.
В примере документ не записывается. Подготовку обязательных данных, запись и очистку
созданных документов определяет конкретный тест.

## Интерфейс Python

```python
from testpilot import Client, ActionError

with Client(profile="ut_admin") as client:
    window = client.get_active_window()
    field = client.find_object(name="Комментарий")
    field.input_text(text="Проверка")
    assert field.get_text() == "Проверка"
```

Также можно создать `Client()` и явно вызвать `connect(...)` или `launch_client(...)`.
Вызовы синхронные. Действия одного клиента выполняются последовательно; для
параллельных тестов нужны отдельные клиенты и подходящие тестовые данные.
Два теста не должны одновременно управлять одним и тем же внешним клиентом 1С.

- `client.call("действие", **параметры)` предоставляет все действия из
  [справочника инструментов](TOOLS.md). Префикс группы `tc_field` не нужен.
- `client.input_text(...)` и другие методы с именем действия эквивалентны `call`.
- `client.find_objects(...)` возвращает список `Element`;
  `client.find_object(...)` требует **ровно одно** совпадение. Если нужно штатное
  поведение поиска первого объекта, используйте `client.call("find_object", ...)`.
- `element.call("действие", ...)` и `element.input_text(...)` сами передают адрес
  элемента. `element.find_object(...)` ищет внутри него.
- `element.get_text()` и `element.get_data_presentation()` возвращают строку.
  Недоступное значение вызывает `ActionError`, прочитанное пустое значение — `""`.
- Остальные действия возвращают исходные словари и списки Python. Полные адреса
  находятся в `key`/`handle`, независимо от настроек JSON/TOON и коротких ссылок MCP.
- `get_screenshot()` возвращает `Screenshot` с полями `png: bytes` и `metadata: dict`.

Сохраните в сценарии имена элементов и условия поиска. Получайте `Element` заново
при каждом прогоне; после переподключения прежние объекты недействительны.

Без лимита `find_objects` возвращает весь список. С `limit` — список `SearchPage`
с полями `total`, `offset`, `has_more`, `next_cursor`:

```python
page = client.find_objects(cls="EditField", limit=20)
while True:
    for element in page:
        print(element.info["name"])
    if not page.has_more:
        break
    page = client.find_objects(cursor=page.next_cursor)
```

Курсор продолжает исходный поиск, без повторного обхода интерфейса. Действует до 5 минут
в том же подключении; может быть вытеснен новыми поисками. Для результата в виде словаря
используйте `client.call("find_objects", limit=20)`.

`input_text(..., finish=False)` оставляет текст в буфере поля. Перед последующими
действиями проверяется, сохранился ли прочитанный после ввода текст в буфере или
принятом значении. Если он изменился, действие останавливается с
`ActionError` с кодом `pending_input_changed`; в `result` находятся `expected_text`,
`edit_text` и `data_presentation`. Можно повторно ввести текст, отменить ввод через
`cancel_edit()` или явно выбрать нужное значение. Эквивалентное числовое
форматирование допускается. Проверка действует также при воспроизведении сценария.

## Несколько полей, таблицы и снимки

```python
# Найдите элементы по именам из своей формы.
client.set_fields({comment: "Тест", enabled_checkbox: True})
# Выбор значения, ввод текста и флажок в одном вызове:
client.set_fields({
    counterparty: {"select": {"match": {"Наименование": "Ромашка", "ИНН": "7701234567"}}},
    comment: "Тест",
    enabled_checkbox: True,
})
result = client.read_fields([comment], properties=["presentation", "readonly"])
assert result["results"][0]["values"]["presentation"] == "Тест"

table.set_row_values(cells=[{"column": "ИмяКолонки", "text": "Значение"}])
table.add_rows(rows=[{"cells": [{"column": "ИмяКолонки", "text": "Ещё значение"}]}])
rows = table.read_rows()
assert rows["row_count"] == 2

baseline = form.create_snapshot(include_tables=True)
# Действие, результат которого проверяем.
changes = client.compare_snapshot(snapshot_id=baseline["snapshot_id"])
```

Пример с таблицей предполагает одну существующую строку и возможность ввести эти
значения. Строки читаются с текущими отборами и свёрнутыми узлами. Семантика действий,
включая завершение ввода и подготовку таблицы, совпадает с MCP.

В `read_rows`, `find_rows` и `search` можно передать `columns=["Код", "Наименование"]`.
По умолчанию текст возвращается без подсветки поиска 1С; `text_format="raw"` сохраняет её.
Для краткого обзора формы используйте
`client.get_context(root_key=group.address["key"], visible_only=True, include_commands=False)`:
поддерево группы, без заведомо скрытых элементов, кнопок и командных групп.
Фильтры сокращают выдачу; чтение и явно сохраняемый снимок формы остаются полными.

`client.get_context()` и `form.get_context()` по умолчанию возвращают полное первое
описание, затем новые и изменившиеся элементы в `elements` и удалённые в `removed`.
Каждое возвращённое описание элемента заменяет прежнее; остальные элементы сохраняются.
Для полного описания при каждом вызове используйте `result_mode="full"`.
Фактический режим указан в ответе, `changed=False` означает отсутствие изменений.
Автоматическая точка сравнения обновляется только после полного успешного чтения;
`save_as_snapshot=True` по-прежнему создаёт отдельный фиксированный снимок для
`compare_snapshot`. Подробности и лимиты — в [справочнике действий](TOOLS.md).

Для полного ответа чтения используйте `element.call("get_data_presentation")`.
Для передачи готовых массивов `{key, handle, ...}` используйте
`client.call("set_fields", entries=...)` или `client.call("read_fields", targets=...)`.
В `set_fields` запись `{"select": {...}}` принимает параметры `select_value`.
Поля заполняются в порядке словаря; в конце проверяются все заданные значения.
Отказ останавливает заполнение без отката предыдущих изменений.

Значение поля можно выбрать через выпадающий список или форму выбора:

```python
counterparty = client.find_object(name="Контрагент")
counterparty.select_value(value="Ромашка")
# Для одноимённых элементов — точные значения колонок формы выбора:
counterparty.select_value(match={"Наименование": "Ромашка", "ИНН": "7701234567"})
```

`expected` дополнительно проверяет текст поля после выбора. Неоднозначность и
превышение `max_rows` останавливают действие. Для ячейки таблицы сначала установите
нужную строку; после выбора её редактирование остаётся открытым. Служебная обработка
не требуется. [Параметры выбора](TOOLS.md#tc_field--actions-on-a-form-field-button-group-or-element-addition-choose-action).

Поиск средствами самой 1С запускается одним действием:

```python
table = client.find_object(name="Список", cls="Table")
result = table.search(text="Ромашка", max_rows=100)
rows = result["rows"]
table.search(text="")  # Очистить поиск, сохранив отборы списка.
```

Действие проверяет текст поиска и совпадение двух чтений с интервалом `settle_time=2`
секунды. `truncated` показывает неполную выдачу; `timeout=180` ограничивает всю операцию.
При медленном обновлении списка интервал наблюдения можно увеличить.

Для списка со стандартной командой «Настройка списка» доступны чтение и изменение
отборов и сортировки:

```python
table = client.find_object(name="Список")
fields = table.get_list_settings_fields(section="filters")
settings = table.get_list_settings()
table.set_list_settings(
    filters=[{"field": "Наименование", "comparison": "contains", "value": "Кабель"}],
    orders=[{"field": "Наименование", "direction": "asc"}],
    replace=True,
)
table.set_list_settings(filters=[], replace=True)  # Убрать отбор, оставить сортировку.
```

Названия полей берутся из `get_list_settings_fields`, значения передаются текстом,
как при ручном вводе. По умолчанию настройки дополняются; `replace=True` заменяет
только переданные разделы. При ошибке частичные изменения остаются в открытой форме.
Подробности параметров — в [описании действий таблицы](TOOLS.md#tc_table--read-and-edit-table-or-tree-rows-manage-selection-and-expand-or-collapse-nodes-choose-action).

## Ошибки и отчёты

Ожидание результата можно явно оформить через `wait_until`:

```python
def read_startup_window():
    result = client.call("get_active_window", check=False)
    if result.get("error") or result.get("exception"):
        raise ActionError("get_active_window", result)
    return result

window = client.wait_until(
    "Ожидание готовности клиента",
    read_startup_window,
    condition=lambda result: bool(result.get("ok") and result.get("key")),
    timeout=120,
    interval=1,
)
```

Первый callback читает состояние, `condition` проверяет результат. При выполнении
условия возвращается прочитанный результат. Проверки повторяются только при ложном
условии; исключения сразу прерывают ожидание. По таймауту возникает `ActionError`
с кодом `wait_timeout` и последним ответом в `result["last_result"]`.
Таймаут ограничивает цикл опроса; уже выполняющийся вызов ограничивается собственным
таймаутом операции. Вложенные ожидания не поддерживаются.

В HTML-журнале это один этап с длительностью, количеством проверок и результатом.
Ответы проверок находятся внутри свёрнутого блока подробностей. В фильтр ошибок
попадает неуспешный этап ожидания. Проверки внутри ожидания не создают скриншоты
журнала, в том числе в режиме `all`. В JSONL сохраняются отдельные вызовы с `wait_id`
и события `wait_start`/`wait_finish`. Вне `wait_until` оформление вызовов прежнее:
сам по себе `check=False` не меняет их статус в журнале. Без журналирования
ожидание выполняется так же.

После успешного отключения или остановки клиента журнал также не пытается снять
экран уже освобождённого подключения.

Отказ действия вызывает `ActionError`. В `error.action`, `error.code` и
`error.result` доступны название действия, код и полный результат, включая
успешно выполненную часть групповой операции. Несовпадение ожидаемого значения
проверяется обычным `assert`; отсутствие элемента — отдельная ошибка поиска.

```python
with pytest.raises(ActionError) as error:
    form.find_object(name="НесуществующееПоле")
assert error.value.code == "object_not_found"

# Когда отказ — ожидаемый результат, его можно проверить без исключения.
result = table.call("delete_rows", scope="selected", check=False)
assert result["code"] == "no_selected_rows"
```

После теста fixture останавливает запущенный ею клиент либо отключается от внешнего.
Созданные в базе данные автоматически не удаляются и не откатываются.

В `--tc-artifacts` создаётся отдельный каталог каждого теста: `result.json`, журнал
вызовов с HTML-отчётом при включённом `TC1C_LOGGING`, а при падении — попытка прочитать
контекст формы в `context.json`. Ошибка завершения клиента отражается в отчёте pytest.
Путь к каталогу также включается в JUnit XML, если передан `--junitxml report.xml`.

Скриншоты журнала по умолчанию выключены. Для их включения используйте
`--tc-screenshots actions` или `--tc-screenshots all`; настройки
`TC1C_SCREENSHOTS`, `TC1C_LOGGING` и `TC1C_LOG_SCREENSHOTS` должны разрешать захват.
Это не запрещает самому тесту явно вызвать `get_screenshot`, если функция разрешена.

## Дополнительные свойства формы

`client.get_context()` может дополнять элементы подсказками ввода, ограничениями типов
и настройками выбора. Настройка `TC1C_FORM_DETAILS=auto` включена по умолчанию:
свойства читаются, если клиент запущен с обновлённой Testpilot.epf. Для отключения
укажите `TC1C_FORM_DETAILS=false` до импорта `testpilot`.

В Python API обработка подключается через `code_epf` в аргументах запуска или профиле,
либо через общий путь `TC1C_CODE_EPF`. Явный `code_epf=""` оставляет запуск без обработки.
Разрешать инструменты выполнения кода или запросов в MCP для этого не требуется.

Свойства находятся в `elements[].details`, состояние дополнительного чтения — в
`form_details`. Его ошибка не заменяет основной результат формы и сама по себе не
вызывает `ActionError`. Поля и режимы описаны в [справочнике действий](TOOLS.md#дополнительные-свойства-в-get_context).

## Форматы отчётов

`--tc-reports` выбирает `html`, `allure`, `html,allure` или `none` (только JSONL).
По умолчанию используется `TC1C_LOG_REPORTS`, а если переменная не задана — `html`.
Журнал и скриншоты собираются один раз; HTML и Allure используют одни и те же события.
`TC1C_LOGGING` включает журналирование, `--tc-screenshots` задаёт режим снимков.

Для Allure установите дополнительную зависимость:

```bash
pip install "1c-testpilot[allure]"
python -m pytest examples/pytest/zup/test_hire_employee.py --tc-profile zup_demo --tc-reports html,allure --tc-screenshots actions --tc-artifacts test-results/zup --junitxml test-results/zup.xml
```

При выборе Allure результаты сохраняются в `<tc-artifacts>/allure-results`.
Путь можно переопределить стандартным `--alluredir`. Для сборки и просмотра нужен
[Allure Report](https://allurereport.org/docs/install/):

```bash
allure generate test-results/zup/allure-results -o allure-report
allure open allure-report
```

Каждый pytest-тест имеет собственный результат Allure. Действия Testpilot отображаются
как шаги с параметрами, ответами и скриншотами; `wait_until` группирует проверки ожидания,
а `run_scenario` — шаги воспроизведения. При падении прикладывается полученный контекст формы.
Подготовка и завершение клиента отображаются в соответствующих fixture.
Ожидаемый отказ, прочитанный с `check=False`, остаётся виден в шаге, но итог теста определяет pytest.
Смысловые этапы при необходимости можно объединять стандартным `with allure.step("…")`.

JUnit XML независимо включается параметром `--junitxml`. Каталог результатов Allure
управляется плагином `allure-pytest`; лимит `TC1C_LOG_MAX_MB` относится к журналам Testpilot.

Запись XML-сценариев и `run_scenario` доступны через тот же Python API. Для новых
pytest-тестов действия и проверки можно писать непосредственно на Python.
`client.run_scenario(path="scenario.xml")` по умолчанию возвращает сводку;
`result_mode="full"` добавляет список `steps`. При включённом логировании подробности
сохраняются в журнале в обоих режимах, пути к нему и отчётам находятся в `logging`.

## Код и запросы в текущем сеансе 1С

Для `execute_code` и `execute_query` запустите клиент с
[внешней обработкой Testpilot](../onec/Testpilot/README.md).
В профиле, выбранном через `--tc-profile`, укажите `code_epf`:

```yaml
profiles:
  demo:
    base: 'D:/Bases/Demo'
    code_epf: 'D:/Tools/1c-testpilot/onec/Testpilot/Testpilot.epf'
```

При запуске профиля обработка автоматически открывается и проверяется на готовность.
После этого доступны `execute_code`, `execute_query`, `get_metadata`,
`list_custom_bsl_functions` и `execute_custom_bsl_function`. Настройки публикации
MCP (`TC1C_CODE_EXECUTION`, `TC1C_QUERY_EXECUTION`, `TC1C_METADATA`, `TC1C_FUNCTIONS`)
для Python-тестов не требуются. Пользовательские функции добавляются в
[реестр обработки](../onec/Testpilot/CUSTOM_FUNCTIONS.md).

При запуске из кода путь можно передать непосредственно:

```python
from testpilot import Client

with Client() as client:
    client.launch_client(
        base="D:/Bases/Demo",
        code_epf="D:/Tools/1c-testpilot/onec/Testpilot/Testpilot.epf",
    )
    assert client.execute_query(query="ВЫБРАТЬ 1 КАК Число")["rows"] == [{"Число": 1}]
```

Общий путь можно задать через `TC1C_CODE_EPF` до импорта `testpilot`.
Приоритет: аргумент `code_epf`, затем профиль, затем переменная окружения.
`code_epf=""` в `launch_client` или `start` запускает клиент без обработки даже при
заданном пути в профиле или окружении. При подключении к работающему клиенту
используется уже открытая обработка; если её нет, выполнение сообщает `helper_not_ready`.

Пример теста со стандартной фикстурой и профилем с `code_epf`:

```python
def test_document_was_saved(testpilot):
    # Перед этим тест создаёт документ через обычные действия над формой.
    rows = testpilot.execute_query(
        query="ВЫБРАТЬ Номер, Проведен ИЗ Документ.ЗаказКлиента ГДЕ Номер = &Номер",
        parameters={"Номер": "ТД00-000001"},
        limit=2,
    )
    assert rows["returned_rows"] == 1
    assert rows["rows"][0]["Проведен"] is True

    value = testpilot.execute_code(
        context="client",
        code='Результат = Параметры["текст"] + "!";',
        parameters={"текст": "Проверка"},
    )
    assert value["result"] == "Проверка!"
```

Запросы учитывают текущие права пользователя 1С.
Возвращаемые даты, ссылки и перечисления имеют явное JSON-представление,
описанное в документации обработки. Ошибки возвращаются через `ActionError`.
