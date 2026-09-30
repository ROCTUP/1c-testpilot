"""Search displayed table values, with explicit scope and completeness."""
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, TypeAdapter, ValidationError


class RowCondition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    column: Annotated[StrictStr, Field(min_length=1)]
    text: StrictStr
    match: Literal['exact', 'contains', 'starts_with', 'ends_with'] = 'exact'


Conditions = Annotated[list[RowCondition], Field(min_length=1, max_length=32)]
Columns = Annotated[list[Annotated[StrictStr, Field(min_length=1)]], Field(min_length=1, max_length=100)]
SCOPE_NOTE = ('Search is limited to selectable rows of the current table, respecting filters and '
              'collapsed groups. This is not a database-wide search.')
_HIGHLIGHT = re.compile(r'<b><colorstyle -46>(.*?)</></>', re.DOTALL)


class OutputError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def output_options(columns, text_format):
    if text_format not in ('plain', 'raw'):
        raise OutputError('invalid_text_format', 'text_format must be plain or raw.')
    try:
        columns = TypeAdapter(Columns).validate_python(columns) if columns is not None else None
    except ValidationError as exc:
        raise OutputError('invalid_columns', 'columns must be a nonempty list of column titles.') from exc
    if columns is not None and len(set(columns)) != len(columns):
        raise OutputError('invalid_columns', 'Return each column title only once.')
    return columns


def output_titles(S, c, key, columns):
    if columns is None:
        return []
    titles = [o.get('title') for o in S._table_columns(c, key)]
    if any(titles.count(column) > 1 for column in columns):
        raise OutputError('ambiguous_column', 'Several table columns have a requested title.')
    return titles


def project(rows, columns, text_format, titles=()):
    if columns is not None:
        if not rows and any(column not in titles for column in columns):
            raise OutputError('column_unverified', 'The empty table does not confirm the requested column titles.')
        if any(column not in row for row in rows for column in columns):
            raise OutputError('column_not_returned', 'A requested column was not returned by the table.')
    def text(value):
        return _HIGHLIGHT.sub(r'\1', value) if text_format == 'plain' and isinstance(value, str) else value
    return [{k: text(row[k]) for k in (columns if columns is not None else row)} for row in rows]


def search(S, c, key, handle, conditions, columns, case_sensitive, max_rows, max_matches, text_format='plain'):
    out = dict(ok=False, target=key, scope='current_table', scope_note=SCOPE_NOTE,
               comparison='displayed_text', rows_checked=0, row_count=None,
               complete=False, matches=[], match_count=None, returned_matches=0,
               matches_truncated=False)

    def fail(code, error, **extra):
        return dict(out, code=code, error=error, **extra)

    if S._key_class(key) != 'Table':
        return fail('invalid_table', 'Find rows requires a table.')
    try:
        columns = output_options(columns, text_format)
    except OutputError as exc:
        return fail(exc.code, str(exc))
    try:
        conditions = TypeAdapter(Conditions).validate_python(conditions)
    except ValidationError as exc:
        return fail('invalid_search', 'Supply conditions as {column, text, match} objects and optional column titles.',
                    details=exc.errors(include_url=False, include_context=False, include_input=False))
    if type(case_sensitive) is not bool:
        return fail('invalid_search', 'case_sensitive must be a boolean.')
    if type(max_rows) is not int or not 1 <= max_rows <= 10000:
        return fail('invalid_row_limit', 'max_rows must be an integer from 1 to 10000.')
    if type(max_matches) is not int or not 1 <= max_matches <= 1000:
        return fail('invalid_match_limit', 'max_matches must be an integer from 1 to 1000.')
    required = list(dict.fromkeys([r.column for r in conditions] + (columns or [])))
    # Metadata can omit inherited captions (e.g. N); the returned row keys remain
    # authoritative. Detect known duplicate titles before selection changes.
    try:
        metadata = S._table_columns(c, key)
    except Exception as exc:
        return fail('columns_unavailable', 'The table columns could not be checked: ' + str(exc))
    titles = [o.get('title') for o in metadata]
    returned_titles = required if columns is not None else list(dict.fromkeys(required + [t for t in titles if t]))
    for column in returned_titles:
        if titles.count(column) > 1:
            return fail('ambiguous_column', 'Several table columns have this title.', column=column)
    try:
        read = S.tc_read_rows(key, handle, max_rows=max_rows, text_format='raw')
    except Exception as exc:
        return fail('table_read_failed', 'The table could not be read: ' + str(exc))
    # Preserve selection/recording failure details. A failed read is not a negative search.
    out.update({k: v for k, v in read.items() if k not in
                ('ok', 'target', 'scope', 'rows', 'returned_rows', 'truncated')})
    if not read.get('ok'):
        return dict(out, code=read.get('code', 'table_read_failed'),
                    message='Search could not be completed. No conclusion about absent rows can be drawn.')
    rows = read.get('rows')
    count = read.get('row_count')
    if (not isinstance(rows, list) or type(count) is not int or count < len(rows)
            or any(not isinstance(r, dict) for r in rows)):
        return fail('rows_unavailable', 'The table read did not return a usable row set.')
    available = list(dict.fromkeys(k for r in rows for k in r))
    for column in required:
        if rows and column not in available:
            return fail('column_not_returned', 'The requested column was not returned by the table.',
                        column=column, available_columns=available)
        if not rows and column not in titles:
            return fail('column_unverified', 'The empty table does not confirm this column title.', column=column)
        if any(column not in row or not isinstance(row[column], str) for row in rows):
            return fail('column_values_unavailable', 'Some rows have no readable text for this column.', column=column)

    def matches(row):
        for condition in conditions:
            # Strip only 1C's known search-match wrapper, not arbitrary HTML-like
            # user text. Matching is independent of the requested output format.
            value, text = _HIGHLIGHT.sub(r'\1', row[condition.column]), condition.text
            if not case_sensitive:
                value, text = value.casefold(), text.casefold()
            matched = (value == text if condition.match == 'exact' else
                       text in value if condition.match == 'contains' else
                       value.startswith(text) if condition.match == 'starts_with' else value.endswith(text))
            if not matched:
                return False
        return True

    found = [row for row in rows if matches(row)]
    result_rows = project(found[:max_matches], columns, text_format, required)
    # Limits on scanned rows and returned matches are independent. No ordering or
    # stable row identity is implied by this selection-based native reader.
    complete = len(rows) == count and not read.get('truncated')
    out.update(ok=True, complete=complete, rows_checked=len(rows), matches=result_rows,
               match_count=len(found), returned_matches=len(result_rows),
               matches_truncated=len(found) > max_matches)
    if not complete:
        out['message'] = 'Only part of the table was checked. Matches and match_count cover checked rows only.'
    elif not found:
        out['message'] = 'No matches in the checked table rows; this does not establish absence from the database.'
    return out
