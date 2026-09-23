import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
for name in ('TC1C_CODE_FORBIDDEN_WORDS', 'TC1C_CODE_ALLOWED_WORDS'):
    os.environ.pop(name, None)

import _code_execution as ce  # noqa: E402


def refused(code):
    with pytest.raises(ce.Failure) as caught:
        ce.validate_code(code)
    return caught.value.result


COMMENT_INSIDE_STRING = 'С = "начало\n// "\n|конец";\n{call}\n// "'


@pytest.mark.parametrize('call, identifier', [
    ('Выполнить("Результат = 42;");', 'Выполнить'),
    ('Результат = Вычислить("42");', 'Вычислить'),
    ('М.Удалить(0);', 'Удалить'),
    ('КомандаСистемы("id");', 'КомандаСистемы'),
])
def test_comment_quote_inside_multiline_string_does_not_hide_code(call, identifier):
    result = refused(COMMENT_INSIDE_STRING.format(call=call))
    assert result['code'] == 'forbidden_code'
    assert result['identifier'] == identifier
    assert result['line'] == 4


@pytest.mark.parametrize('newline', ['\r\n', '\r'])
def test_crlf_and_lone_cr_line_endings(newline):
    code = COMMENT_INSIDE_STRING.format(call='Выполнить("Результат = 42;");').replace('\n', newline)
    result = refused(code)
    assert result['identifier'] == 'Выполнить'
    assert result['line'] == 4


@pytest.mark.parametrize('code', [
    # 1C ends a line at a lone CR: a comment stops there and the rest of the text is code.
    '// комментарий\rВыполнить("Результат = 42;");',
    'С = "начало\n// комментарий\r|конец"; Выполнить("Результат = 42;"); С = "ещё\n|конец";',
])
def test_lone_cr_ends_a_comment(code):
    assert refused(code)['identifier'] == 'Выполнить'


@pytest.mark.parametrize('code', [
    'С = "начало\n// "\n|Выполнить";\n// "',
    'С = "начало\n\n   // комментарий\n\t|Удалить ""в кавычках""\n|Записать";',
    'С = "Выполнить(""x"")"; // Удалить',
    'Результат = "Записать";',
    'С = "начало\n\f|Удалить\n\v|Записать";',
])
def test_string_and_comment_content_is_not_code(code):
    ce.validate_code(code)


@pytest.mark.parametrize('code', [
    'С = "начало\nВыполнить("x");',
    'С = "начало',
    'С = "начало\n|продолжение',
])
def test_unterminated_string_is_rejected(code):
    assert refused(code)['code'] == 'invalid_code'


@pytest.mark.parametrize('word', ['КомандаСистемы', 'System', 'ЗапуститьПриложение', 'RunApp'])
def test_os_commands_are_always_forbidden(monkeypatch, word):
    monkeypatch.setenv('TC1C_CODE_ALLOWED_WORDS', word)
    monkeypatch.setattr(ce, 'SETTINGS', ce.settings())
    assert refused(f'{word}("id");')['identifier'] == word


@pytest.mark.parametrize('query, prepared', [
    ('ВЫБРАТЬ\n\t"строка\nна двух строках" КАК Поле;\nВЫБРАТЬ 2',
     'ВЫБРАТЬ РАЗРЕШЕННЫЕ\n\t"строка\nна двух строках" КАК Поле;\nВЫБРАТЬ РАЗРЕШЕННЫЕ 2'),
    ('SELECT "a;\r\n// b" AS F // SELECT\r\n;SELECT ALLOWED 1',
     'SELECT ALLOWED "a;\r\n// b" AS F // SELECT\r\n;SELECT ALLOWED 1'),
])
def test_query_strings_keep_their_own_rules(query, prepared):
    assert ce.prepare_query(query) == prepared
