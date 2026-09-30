"""Tester table content, text comparisons and user-message templates."""
from decimal import Decimal, InvalidOperation
from datetime import datetime
import re

from compatible_scenarios.shared.bsl.language import Failure, Structure, string


def cells(line):
    result, current, quoted, index = [], [], False, 0
    while index < len(line):
        char = line[index]
        if char == '\\' and index + 1 < len(line):
            following = line[index + 1]
            if following == "'": current.append('\x01')
            elif following == '\\' or (not quoted and following in ',|'): current.append(following)
            else: current.extend(('\\', following))
            index += 2
            continue
        if char == "'": quoted = not quoted
        if char in ',|' and not quoted:
            result.append(''.join(current)); current = []
        else: current.append(char)
        index += 1
    if quoted: raise Failure('invalid_table_template', 'Unclosed quote in a table row.')
    result.append(''.join(current))
    values = []
    for cell in result[1:]:  # The first column is a display row number, not table data.
        cell = cell.strip()
        if len(cell) >= 2 and cell.startswith("'") and cell.endswith("'"): cell = cell[1:-1]
        values.append(cell.replace('\x01', "'"))
    return values


def substitute(text, parameters):
    text = text.replace('\\%', '\x02')
    for key, value in sorted(parameters.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace('%'+key, string(value))
    return text.replace('\x02', '%')


def parse(text, parameters):
    if not isinstance(text, str): raise Failure('invalid_table_template', 'CheckTable expects table text.')
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2: raise Failure('invalid_table_template', 'The table selector and column header are required.')
    header = [value if value.startswith(('!', '#')) else substitute(value, parameters) for value in cells(lines[1])]
    if not header or any(not value for value in header):
        raise Failure('invalid_table_template', 'Table columns must have names or titles after the row-number column.')
    body = [cells(line) for line in lines[2:]]
    for row, values in enumerate(body, 1):
        if len(values) != len(header):
            raise Failure('invalid_table_template', f'Row {row}: expected {len(header)} cells, got {len(values)}.', row=row)
    return lines[0].strip(), header, body


def matches(actual, expected, parameters, tick=lambda: None, date_format='dd.MM.yyyy H:mm:ss', deadline=None):
    if expected.startswith('%') and parameters.actual(expected[1:]) in parameters:
        value = parameters[expected[1:]]
        if type(value) is datetime:
            from compatible_scenarios.shared.bsl.dates import read_presentation
            try: return read_presentation(actual, date_format() if callable(date_format) else date_format) == value
            except Failure as exc:
                if exc.result['code'] != 'invalid_date': raise
                return False
        if isinstance(value, Decimal):
            try: return Decimal(actual.replace(' ', '').replace('\u00a0', '').replace(',', '.') or '0') == value
            except InvalidOperation: return False
        return actual == value
    expected = substitute(expected, parameters)
    # TableProcessor.compareValues protects escaped wildcards, then escapes
    # backslashes and dots. Other regex operators retain their meaning.
    parts, index = [], 0
    wildcard = False
    while index < len(expected):
        char = expected[index]
        if char == '\\' and index + 1 < len(expected) and expected[index+1] in '*?':
            parts.append(('literal', expected[index+1])); index += 2; continue
        if char in '*?': wildcard = True; parts.append((char, ''))
        else: parts.append(('literal', char))
        index += 1
    if not wildcard: return actual == ''.join(value for _, value in parts)
    pattern = ''.join('.+' if kind == '*' else '.' if kind == '?' else
                      '\\'+value if value in '\\.*?' else value for kind, value in parts)
    import regex
    import time
    tick()
    try: compiled = regex.compile(pattern, regex.IGNORECASE | regex.VERSION0)
    except (regex.error, RecursionError, OverflowError) as exc:
        raise Failure('invalid_table_template', f'Invalid table pattern: {exc}') from exc
    tick()
    remaining = .25 if deadline is None else min(.25, deadline-time.monotonic())
    if remaining <= 0: raise Failure('scenario_timeout', 'Scenario deadline expired.')
    try: result = compiled.search(actual, timeout=remaining) is not None
    except TimeoutError as exc:
        tick()
        if deadline is not None and (remaining < .25 or time.monotonic() >= deadline):
            raise Failure('scenario_timeout', 'Scenario deadline expired.') from exc
        raise Failure('scenario_limit', 'The table pattern exceeded its matching time limit.') from exc
    tick()
    return result


def next_table_row(adapter, table):
    """Tester treats a received refusal to move as the end of iteration."""
    try: adapter.action('goto_next_row', table)
    except Failure as exc:
        if adapter.platform_refusal(exc): return False
        raise
    return bool(adapter.action('get_selected_rows', table)['rows'])


def check_table(adapter, text, parameters=None, options=None, source=None):
    if parameters is None: parameters = Structure()
    if not isinstance(parameters, Structure):
        raise Failure('invalid_table_template', 'Table parameters must be a Structure.')
    strict = False
    if options is not None:
        if not isinstance(options, Structure): raise Failure('invalid_table_template', 'Table options must be a Structure.')
        for name, value in options.items():
            if name.casefold() != 'strictly' or type(value) is not bool:
                raise Failure('invalid_table_template', 'The supported table option is Strictly (Boolean).')
            strict = value
    selector, header, body = parse(text, parameters)
    table = adapter.field(selector, source, 'table')
    columns = adapter.action('find_objects', table, root_key=table.data['key'], cls='EditField')['objects']
    selected = []
    for column in header:
        named = column.startswith(('!', '#'))
        title = column[1:] if named or column.startswith(('\\!', '\\#')) else column
        found = [obj for obj in columns if obj.get('name' if named else 'title') == title]
        if len(found) != 1:
            raise Failure('assertion_failed', f'Column {column!r}: expected one match, got {len(found)}.', column=column)
        selected.append(found[0])
    if strict and [obj['key'] for obj in selected] != [obj['key'] for obj in columns]:
        raise Failure('assertion_failed', 'The table columns or their order differ from the template.',
                      expected_columns=header, actual_columns=[obj.get('title') for obj in columns])
    adapter.finish_row(table)
    def tick():
        host = adapter.host
        host.tick(host.location['path'], host.location['line'])
    count = adapter.action('read_rows', table, max_rows=0).get('row_count')
    if type(count) is not int or count < 0:
        raise Failure('table_state_unavailable', 'The table row count is unavailable.')
    if count: adapter.action('goto_first_row', table)
    # More than one selected row proves multiple selection works. A single
    # selected row does not tell us whether the table itself has only one row.
    single_selection = count == 1
    for row in range(len(body) if single_selection else min(count, len(body))):
        if row:
            if single_selection:
                if not next_table_row(adapter, table):
                    raise Failure('assertion_failed', f'Table {selector!r}: expected {len(body)} rows, got {row}.',
                                  expected_rows=len(body), actual_rows=row)
            else: adapter.action('goto_next_row', table)
        for col, obj in enumerate(selected):
            actual = adapter.action('get_cell_text', table, column=obj['name']).get('text')
            if not isinstance(actual, str): raise Failure('table_state_unavailable', 'The table cell text is unavailable.')
            actual = plain_cell_text(actual)
            from compatible_scenarios.tester.regional import format_for
            if not matches(actual, body[row][col], parameters, tick, lambda: format_for(adapter.host),
                           deadline=adapter.host.deadline):
                expected = body[row][col]
                if expected.startswith('%') and parameters.actual(expected[1:]) in parameters:
                    expected = adapter.host.text(parameters[expected[1:]])
                else: expected = substitute(expected, parameters)
                raise Failure('assertion_failed', f'Table {selector!r}, row {row+1}, column {header[col]!r}: '
                              f'expected {expected!r}, got {actual!r}.',
                              row=row+1, column=header[col], expected=expected, actual=actual)
    if single_selection and body:
        if next_table_row(adapter, table):
            raise Failure('table_end_unavailable', 'The end of the table could not be confirmed after the expected rows.')
        return None
    if count != len(body):
        raise Failure('assertion_failed', f'Table {selector!r}: expected {len(body)} rows, got {count}.',
                      expected_rows=len(body), actual_rows=count)
    return None
def find_messages(adapter, template, messages):
    """RuntimeSrv.FindErrors + addon's case-insensitive regex_search."""
    import regex
    import time
    if not isinstance(template, str): raise Failure('scenario_failed', 'The message template must be a string.')
    if len(template) > 4096: raise Failure('scenario_limit', 'The message template exceeds 4096 characters.')
    if not messages: return []
    try: pattern = regex.compile(template.replace('*', '.+').replace('?', '.'), regex.IGNORECASE | regex.VERSION0)
    except regex.error as exc: raise Failure('invalid_template', str(exc)) from exc
    result = []
    for message in messages:
        adapter.host.tick(**adapter.host.location)
        if not isinstance(message, str): raise Failure('state_unavailable', 'A user message could not be read.')
        try:
            if pattern.search(message, timeout=max(.001, min(.25, adapter.host.deadline-time.monotonic()))):
                result.append(message)
        except TimeoutError as exc:
            adapter.host.tick(**adapter.host.location)
            raise Failure('scenario_limit', 'The message template took too long to evaluate.') from exc
    return result


def plain_cell_text(value):
    """Tester RemoveSeachingTags: remove <[^>]*> fragments in linear time."""
    if not isinstance(value, str): return value
    pieces, offset = [], 0
    while True:
        start = value.find('<', offset)
        if start < 0: break
        end = value.find('>', start + 1)
        if end < 0: break
        pieces.append(value[offset:start])
        offset = end + 1
    pieces.append(value[offset:])
    return ''.join(pieces)


def number_separators(adapter):
    from compatible_scenarios.tester.regional import get
    value = get(adapter.host)
    return value['Fractions'], value['Groups']


def number_value(value, separators):
    """Convert numeric text when the scenario has configured its separators."""
    if separators is None: return value
    fractions, groups = separators
    text = value.strip()
    sign = ''
    if text.startswith('-'):
        sign, text = '-', text[1:]
        if text[:1].isspace(): text = text[1:]
    parts = text.split(fractions) if fractions else [text]
    if len(parts) > 2 or len(parts) == 2 and not re.fullmatch(r'[0-9]+', parts[1]): return value
    integer = parts[0]
    # 1C uses several Unicode spaces for digit grouping. They are only grouping
    # separators when the scenario has explicitly declared a space separator.
    if groups and groups.isspace():
        integer = re.sub('[ \u00a0\u202f\u2003\u2002]', groups, integer)
    if groups and groups in integer:
        chunks = integer.split(groups)
        if not re.fullmatch(r'[0-9]{1,3}', chunks[0]) or any(not re.fullmatch(r'[0-9]{3}', s) for s in chunks[1:]): return value
        integer = ''.join(chunks)
    if not re.fullmatch(r'[0-9]+', integer): return value
    return Decimal(sign + integer + ('.' + parts[1] if len(parts) == 2 else ''))


def get_table_content(adapter, name, source=None):
    """Fields.FetchTableContent: at most 100 rows, keyed by column element name."""
    separators = number_separators(adapter)
    table = adapter.field(name, source, 'table')
    adapter.finish_row(table)
    adapter.action('goto_first_row', table)
    adapter.action('select_all_rows', table)
    rows = adapter.action('get_selected_rows', table)['rows']
    if len(rows) > 100: raise Failure('table_too_large', 'GetTableContent supports at most 100 rows.')
    if not rows: return []
    columns = adapter.action('find_objects', table, root_key=table.data['key'], cls='EditField')['objects']
    if not columns: raise Failure('table_state_unavailable', 'The table columns could not be read.')
    titles = [column.get('title') for column in columns]
    ambiguous = {title for title in titles if titles.count(title)>1}
    result = []
    def append(row):
        adapter.host.tick(**adapter.host.location)
        data = Structure()
        for column in columns:
            title, key = column.get('title'), column.get('name')
            if title in ambiguous and title in row:
                raise Failure('ambiguous_columns', 'Selected rows cannot distinguish columns with identical titles.')
            value = row.get(title)
            if not key or (value is not None and not isinstance(value, str)):
                raise Failure('table_state_unavailable', f'Column {key!r} could not be read.')
            # The platform omits some columns even when CurrentVisible is true.
            # Preserve their identity and distinguish unread values from empty text.
            if value is not None:
                value = plain_cell_text(value)
                value = number_value(value, separators)
            data[key] = Structure(Title=title, Value=value)
        result.append(data)
    if len(rows) > 1:
        for row in rows: append(row)
    else:
        # Single-selection tables need navigation. Never use equal cell texts
        # as an end marker: consecutive rows can contain identical values.
        while rows:
            if len(result) == 100:
                raise Failure('table_end_unavailable', 'The end of the table could not be confirmed within 100 row reads.')
            if len(rows) != 1: raise Failure('table_state_unavailable', 'Selection changed while reading the table.')
            append(rows[0])
            try: adapter.action('goto_next_row', table)
            except Failure as exc:
                if adapter.platform_refusal(exc): break
                raise
            rows = adapter.action('get_selected_rows', table)['rows']
        adapter.action('goto_first_row', table)
    return result
