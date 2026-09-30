"""Bounded string operations with the platform's UTF-16 positions."""
from decimal import Decimal, InvalidOperation
from enum import Enum

MISSING = object()


class SearchDirection(Enum):
    BEGIN = 1
    END = 2


class Constants:
    def __init__(self, values):
        self._values = {key.casefold(): value for key, value in values.items()}

    def get(self, name):
        if name not in self._values: fail(f'Unknown constant {name!r}.')
        return self._values[name]


def fail(message):
    from compatible_scenarios.shared.bsl.language import Failure
    raise Failure('scenario_failed', message)


def integer(value):
    if type(value) not in (str, bool, Decimal): fail('A number is required.')
    try:
        number = Decimal(value)
        if not number.is_finite(): fail('A finite number is required.')
        return int(number)
    except (ValueError, InvalidOperation): fail('Cannot convert the value to a number.')


def units(text):
    raw = text.encode('utf-16-le', errors='surrogatepass')
    return ''.join(chr(raw[i] | raw[i+1] << 8) for i in range(0, len(raw), 2))


def from_units(text):
    # Reassemble adjacent surrogate halves, retaining isolated units for BSL slicing.
    return text.encode('utf-16-le', errors='surrogatepass').decode('utf-16-le', errors='surrogatepass')


def limit(size, maximum, kind):
    if size > maximum:
        from compatible_scenarios.shared.bsl.language import Failure
        raise Failure('scenario_limit', f'{kind} exceeds {maximum} items.')


def call(name, args, text, tick):
    if name == 'strreplace':
        if not all(isinstance(value, str) for value in args): fail('Strings are required.')
        source, old, new = map(units, args)
        return from_units(source.replace(old, new))
    if name in ('strlen', 'left', 'right', 'mid'):
        if not isinstance(args[0], str): fail('A string is required.')
        source = units(args[0])
        if name == 'strlen': return Decimal(len(source))
        if name == 'left': return from_units(source[:max(0, int(args[1]))])
        if name == 'right': return from_units(source[-int(args[1]):]) if args[1] > 0 else ''
        start = max(0, int(args[1])-1)
        count = args[2] if len(args) > 2 else None
        return from_units(source[start:None if count is None else start+max(0, int(count))])
    if name == 'char':
        code = integer(args[0]) % (1 << 32)
        return chr(code) if code <= 65535 else ''
    if name in ('triml', 'trimr', 'strlinecount', 'strgetline', 'charcode', 'strstartswith', 'strendswith'):
        source = text(args[0])
        if name == 'triml': return source.lstrip()
        if name == 'trimr': return source.rstrip()
        if name in ('strstartswith', 'strendswith'):
            needle = text(args[1])
            if not needle: fail('The second parameter must be nonempty.')
            source, needle = units(source), units(needle)
            return source.startswith(needle) if name == 'strstartswith' else source.endswith(needle)
        lines = source.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        if len(lines) > 1 and not lines[-1]: lines.pop()
        if name == 'strlinecount': return Decimal(len(lines))
        position = 1 if len(args) < 2 else integer(args[1])
        if name == 'charcode':
            source = units(source)
            if not 1 <= position <= len(source): return Decimal(-1)
            return Decimal(ord(source[position-1]))
        return lines[position-1] if 1 <= position <= len(lines) else ''
    if name == 'strconcat':
        if not isinstance(args[0], list): fail('StrConcat requires an Array.')
        separator = text(args[1]) if len(args) > 1 else ''
        pieces, size = [], 0
        for value in args[0]:
            tick()
            part = text(value)
            size += len(part) + (len(separator) if pieces else 0)
            limit(size, 1000000, 'String')
            pieces.append(part)
        return from_units(separator.join(pieces))
    source = units(text(None if args[0] is MISSING else args[0]))
    needle = units(text(None if args[1] is MISSING else args[1]))
    if name == 'strsplit':
        include = args[2] if len(args) > 2 else True
        if include is MISSING: include = True
        if include is None: include = False
        if type(include) is not bool: fail('IncludeEmpty must be Boolean.')
        separators, pieces, start = set(needle), [], 0
        for index, char in enumerate(source):
            if index % 256 == 0: tick()
            if char not in separators: continue
            if include or index > start: pieces.append(from_units(source[start:index]))
            limit(len(pieces), 10000, 'Array')
            start = index + 1
        if include or start < len(source): pieces.append(from_units(source[start:]))
        limit(len(pieces), 10000, 'Array')
        return pieces
    if name == 'find': return Decimal(source.find(needle) + 1)
    direction, start, occurrence = (args[2:] + [MISSING] * 3)[:3]
    if direction is MISSING: direction = SearchDirection.BEGIN
    if not isinstance(direction, SearchDirection): fail('A SearchDirection value is required.')
    backward = direction is SearchDirection.END
    if start is MISSING or start is None: start = len(source) if backward else 1
    else:
        start = integer(start)
        if not 1 <= start <= len(source): fail('Start position is outside the string.')
    occurrence = 1 if occurrence is MISSING else (0 if occurrence is None else integer(occurrence))
    if occurrence <= 0: fail('Occurrence must be greater than zero.')
    if not needle: return Decimal(1)
    position = start - 1
    for _ in range(min(occurrence, len(source) + 1)):
        tick()
        found = source.rfind(needle, 0, position + len(needle)) if backward else source.find(needle, position)
        if found < 0: return Decimal(0)
        position = found - len(needle) if backward else found + len(needle)
        if backward and position < 0 and _ + 1 < occurrence: return Decimal(0)
    return Decimal(found + 1)
