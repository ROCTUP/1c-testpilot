"""Second-precision BSL dates and regional UI presentations."""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
import calendar
import re
from functools import lru_cache

EMPTY = datetime(1, 1, 1)
FORMATS = ('dd.MM.yyyy H:mm:ss', 'M/d/yyyy h:mm:ss tt')


def fail(message):
    from compatible_scenarios.shared.bsl.language import Failure
    raise Failure('invalid_date', message)


def checked(value):
    if type(value) is not datetime or value.tzinfo is not None or value.microsecond:
        fail('A date without timezone or fractional seconds is required.')
    return value


def numeric(value):
    if not isinstance(value, Decimal) or not value.is_finite(): fail('A finite number is required.')
    return int(value)


def make(*args):
    try:
        if len(args) == 1:
            value = args[0]
            if type(value) is datetime: return checked(value)
            if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9]{8}|[0-9]{14})', value):
                fail('Date text must contain YYYYMMDD or YYYYMMDDhhmmss.')
            if not value.strip('0'): return EMPTY
            parts = [int(value[:4]), int(value[4:6]), int(value[6:8])]
            if len(value) == 14: parts += [int(value[i:i+2]) for i in (8, 10, 12)]
            return datetime(*parts)
        if len(args) not in (3, 6): fail('Date takes 1, 3 or 6 arguments.')
        return datetime(*(numeric(x) for x in args))
    except (ValueError, OverflowError) as exc: fail(str(exc))


def literal(text):
    # BSL permits punctuation between the components of a date literal.
    if re.search(r'[^0-9\s./:\-]', text): fail('Unsupported character in a date literal.')
    digits = re.sub(r'[^0-9]', '', text)
    if len(digits) not in (8, 10, 12, 14): fail('A date literal needs a date and optional time components.')
    return make(digits.ljust(14, '0'))


def from_wire(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}', value):
        fail('A typed date must use YYYY-MM-DDThh:mm:ss without timezone.')
    try: return checked(datetime.fromisoformat(value))
    except ValueError as exc: fail(str(exc))


def to_wire(value):
    checked(value)
    return {'$type':'date', 'value':f'{value.year:04}-{value.month:02}-{value.day:02}T{value.hour:02}:{value.minute:02}:{value.second:02}'}


def arithmetic(op, left, right):
    checked(left)
    if op == '-' and type(right) is datetime:
        delta = left - checked(right)
        return Decimal(delta.days * 86400 + delta.seconds)
    if op not in ('+', '-') or not isinstance(right, Decimal) or not right.is_finite():
        fail('A date supports adding/subtracting seconds or subtracting another date.')
    seconds = right if op == '+' else -right
    try: return left + timedelta(seconds=int(seconds.to_integral_value(rounding=ROUND_FLOOR)))
    except (ValueError, OverflowError) as exc: fail(str(exc))


def date_function(name, args):
    if name == 'date': return make(*args)
    if name == 'currentdate': return datetime.now().replace(microsecond=0)
    value = checked(args[0])
    if name in ('year', 'month', 'day', 'hour', 'minute', 'second'): return Decimal(getattr(value, name))
    if name == 'begofday': return value.replace(hour=0, minute=0, second=0)
    if name == 'endofday': return value.replace(hour=23, minute=59, second=59)
    if name == 'begofmonth': return value.replace(day=1, hour=0, minute=0, second=0)
    if name == 'endofmonth': return value.replace(day=calendar.monthrange(value.year, value.month)[1], hour=23, minute=59, second=59)
    if name == 'weekday': return Decimal(value.isoweekday())
    if name == 'dayofyear': return Decimal(value.timetuple().tm_yday)
    if name in ('begofyear', 'endofyear', 'begofquarter', 'endofquarter'):
        end = name.startswith('end')
        month = (12 if end else 1) if name.endswith('year') else ((value.month-1)//3*3 + (3 if end else 1))
        return value.replace(month=month, day=calendar.monthrange(value.year, month)[1] if end else 1,
                             hour=23 if end else 0, minute=59 if end else 0, second=59 if end else 0)
    if name in ('begofweek', 'endofweek'):
        try:
            day = value + timedelta(days=(6-value.weekday()) if name == 'endofweek' else -value.weekday())
            return day.replace(hour=23, minute=59, second=59) if name == 'endofweek' else day.replace(hour=0, minute=0, second=0)
        except OverflowError: return datetime(9999,12,31,23,59,59) if name == 'endofweek' else EMPTY
    if name == 'addmonth':
        months = (value.year - 1) * 12 + value.month - 1 + numeric(args[1])
        year, month = divmod(months, 12); year += 1; month += 1
        if not 1 <= year <= 9999: fail('The resulting year is outside 1..9999.')
        return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))
    fail('Unsupported date function.')


def pattern(fmt):
    if not isinstance(fmt, str) or not fmt or len(fmt) > 100: fail('Invalid TesterDateFormat.')
    return _pattern(fmt)


@lru_cache(maxsize=128)
def _pattern(fmt):
    parts = re.findall(r"'[^']*'|yyyy|yy|MMMM|MMM|dd|MM|HH|hh|mm|ss|tt|[dMHhms]|[ ./T,:\-]+", fmt)
    if ''.join(parts) != fmt: fail('Unsupported TesterDateFormat token.')
    fields = [p[0] for p in parts if p in FIELDS]
    date_fields = [k for k in fields if k in ('y','M','d')]
    if len(fields) != len(set(fields)) or date_fields and set(date_fields) != {'y','M','d'}:
        fail('TesterDateFormat requires a complete date or time.')
    if ('H' in fields and 'h' in fields) or ('h' in fields) != ('t' in fields): fail('12-hour time requires h and tt.')
    if (('H' in fields or 'h' in fields) != ('m' in fields)) or ('s' in fields and 'm' not in fields):
        fail('Time requires hours and minutes.')
    if date_fields and fields[:3] not in (['y','M','d'],['d','M','y'],['M','d','y']):
        fail('TesterDateFormat requires the date before the time.')
    if not date_fields and not any(k in fields for k in ('H','h')): fail('TesterDateFormat requires a date or time.')
    return tuple(parts)


FIELDS = {p:p[0] for p in ('yyyy','yy','d','dd','M','MM','MMM','MMMM','H','HH','h','hh','m','mm','s','ss','tt')}


@lru_cache(maxsize=1)
def month_names():
    from compatible_scenarios.shared.bsl.formatting import MONTHS
    result = {}
    for key, names in MONTHS.items():
        for number, name in enumerate(names, 1):
            result[name.casefold()] = number
            if key == 'en': result[name[:3].casefold()] = number
    return result


def _literal(part):
    return part[1:-1] if part.startswith("'") else part


def _numeric_presentation(value, fmt, padded=False, locale='ru'):
    checked(value)
    values = dict(y=value.year, M=value.month, d=value.day, H=value.hour,
                  h=value.hour % 12 or 12, m=value.minute, s=value.second)
    result = []
    for part in pattern(fmt):
        if part == 'tt': result.append('AM' if value.hour < 12 else 'PM')
        elif part in ('MMM','MMMM'):
            from compatible_scenarios.shared.bsl.formatting import MONTHS
            language = locale.replace('-', '_').split('_')[0].casefold()
            if language not in ('ru','en'): fail('Month names support Russian and English.')
            if language == 'ru': text = MONTHS['ru_short' if part == 'MMM' else 'ru_gen'][value.month-1]
            else:
                text = MONTHS['en'][value.month-1]
                if part == 'MMM': text = text[:3]
            result.append(text)
        elif part == 'yy': result.append(f'{value.year % 100:02}')
        elif part[0] in values and part in ('yyyy','d','dd','M','MM','H','HH','h','hh','m','mm','s','ss'):
            width = 4 if part == 'yyyy' else 2 if len(part)==2 or padded and part in ('d','M') else 1
            result.append(str(values[part[0]]).zfill(width))
        else: result.append(_literal(part))
    return ''.join(result)


def _numeric_read(text, fmt):
    parts = pattern(fmt)
    fields = FIELDS
    variants = [parts]
    hour = next((i for i,p in enumerate(parts) if p in ('H','HH','h','hh')), None)
    if hour is not None:
        date = parts[:hour]
        while date and date[-1] not in fields: date = date[:-1]
        if date and set(fields[p] for p in date if p in fields) != {'y','M','d'}:
            fail('TesterDateFormat requires the date before the time.')
        seconds = next((i for i,p in enumerate(parts) if p in ('s','ss')), None)
        if seconds is not None and seconds and parts[seconds-1] not in fields:
            variants.append(parts[:seconds-1]+parts[seconds+1:])
        if date: variants.append(date)
    for variant in variants:
        regex = ''
        mask = ''
        for part in variant:
            if part in fields:
                kind = fields[part]
                choices = ('[0-9]{4}' if part=='yyyy' else '[0-9]{2}(?:[0-9]{2})?' if part=='yy'
                           else 'AM|PM' if kind=='t' else '|'.join(re.escape(n) for n in month_names())
                           if part in ('MMM','MMMM') else '[0-9]{1,2}')
                regex += r'\s*(?P<'+kind+'>'+choices+')'
                if kind == 't': mask += 'AM'
            else:
                literal = _literal(part)
                regex += ''.join(r'\s+' if ch.isspace() else re.escape(ch) for ch in literal)
                mask += literal
        if text and re.sub(r'\s','',text).casefold() == re.sub(r'\s','',mask).casefold(): return EMPTY
        match = re.fullmatch(regex+r'\s*', text, re.I)
        if match:
            v = match.groupdict()
            hour_value = int(v.get('H') or v.get('h') or 0)
            if 'h' in v:
                if not 1 <= hour_value <= 12: fail('Invalid 12-hour time.')
                hour_value = hour_value % 12 + (12 if v['t'].upper()=='PM' else 0)
            year = int(v.get('y') or 1)
            if len(v.get('y','')) == 2: year += 2000 if year < 50 else 1900
            month = v.get('M') or '1'
            month = month_names().get(month.casefold(), month)
            return make(*(Decimal(x) for x in (year,month,v.get('d') or 1,hour_value,v.get('m') or 0,v.get('s') or 0)))
    fail('The field text does not match TesterDateFormat.')


# Keep the public helpers shared by UI checks and BSL String/Message.
def presentation(value, fmt=FORMATS[0], locale='ru'):
    return _numeric_presentation(value, fmt, locale=locale)


def input_text(value, fmt=FORMATS[0], locale='ru'):
    return _numeric_presentation(value, fmt, padded=True, locale=locale)


def read_presentation(text, fmt=FORMATS[0]):
    if type(text) is datetime: return checked(text)
    if not isinstance(text, str): fail('Expected a displayed date.')
    return _numeric_read(text.strip(), fmt)


def sample_format(sample):
    if not isinstance(sample, str): fail('Cannot determine the regional date format; set TesterDateFormat.')
    replacements = {'2004':'yyyy','04':'yy','22':'dd','11':'MM','17':'H','5':'h','05':'hh','48':'mm','59':'ss'}
    parts = re.split(r'([0-9]+|AM|PM)', sample.strip(), flags=re.I)
    result = ''.join('tt' if p.upper() in ('AM','PM') else replacements.get(p,p) for p in parts)
    if result == 'MM/dd/yyyy h:mm:ss tt': result = FORMATS[1]
    pattern(result)
    return result


