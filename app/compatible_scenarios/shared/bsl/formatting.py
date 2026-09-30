"""Scalar BSL formatting and language strings for compatible scenarios."""
from datetime import datetime
from decimal import Decimal, localcontext, ROUND_HALF_UP
import re

from compatible_scenarios.shared.bsl.language import Failure, NULL
from compatible_scenarios.shared.bsl.strings import from_units, units

ALIASES = dict(zip('л чц чдц чс чрд чрг чн чвн чг чо чф дф длф дп бл би'.split(),
                   'l nd nfd ns nds ngs nz nlz ng nn nf df dlf de bf bt'.split()))


def pairs(text):
    if not isinstance(text, str): raise Failure('scenario_failed', 'A format or language string is required.')
    if len(text) > 1000000: raise Failure('scenario_limit', 'The format string is too large.')
    i = 0; result = []
    while i < len(text):
        start = i
        while i < len(text) and text[i] not in '=;': i += 1
        if i == len(text): return [] if text[start:i].strip() else result
        if text[i] == ';':
            if text[start:i].strip(): return []
            i += 1; continue
        key = text[start:i].strip().casefold(); i += 1
        while i < len(text) and text[i].isspace(): i += 1
        value = []
        if i < len(text) and text[i] in '\'"':
            quote = text[i]; i += 1
            while i < len(text):
                if text[i] == quote:
                    i += 1
                    if i < len(text) and text[i] == quote: value.append(quote); i += 1
                    else: break
                else: value.append(text[i]); i += 1
            else: return []
            while i < len(text) and text[i].isspace(): i += 1
            if i < len(text) and text[i] != ';': return []
        else:
            start = i
            while i < len(text) and text[i] != ';': i += 1
            value.append(text[start:i].strip())
            if any(c.isspace() for c in value[0]): return []
        result.append((key, from_units(''.join(value))))
        i += 1
    return result


def culture(host, options):
    name = options.get('l') or host.language_context()[1]
    name = name.replace('-', '_').casefold()
    if name in ('ru', 'ru_ru'): return 'ru'
    if name in ('en', 'en_us'): return 'en'
    if name == 'en_gb': return 'en_gb'
    raise Failure('unsupported_format_locale', 'Format supports ru_RU, en_US and en_GB; specify L explicitly.', locale=name)


def integer(options, name, default=0):
    if name not in options: return default
    try: value = int(options[name])
    except ValueError: return 0
    if abs(value) > 1000: raise Failure('scenario_limit', 'Format precision or shift exceeds 1000.')
    return value


def template(pattern, replace, *, escaped_quotes=False):
    result = []; i = 0; size = 0
    while i < len(pattern):
        char = pattern[i]
        if escaped_quotes and char in '\'"' and pattern[i:i+2] == char * 2:
            result.append(char); i += 2
        elif char in '\'"':
            quote = char; i += 1; literal = []
            while i < len(pattern):
                if pattern[i] == quote:
                    i += 1
                    if i < len(pattern) and pattern[i] == quote: literal.append(quote); i += 1
                    else: break
                else: literal.append(pattern[i]); i += 1
            result.append(''.join(literal))
        else:
            end = i+1
            while end < len(pattern) and pattern[end] == char: end += 1
            result.append(replace(pattern[i:end])); i = end
        size += len(result[-1])
        if size > 1000000:
            raise Failure('scenario_limit', 'Formatted text is too large.')
    text = ''.join(result)
    if len(text) > 1000000: raise Failure('scenario_limit', 'Formatted text is too large.')
    return from_units(text)


def number(value, options, host):
    if not value.is_finite(): raise Failure('scenario_failed', 'Format requires a finite number.')
    if len(value.as_tuple().digits)+abs(value.adjusted()) > 1000:
        raise Failure('scenario_limit', 'Format number exceeds 1000 digits.')
    if value == 0 and options.get('nz') != '': return options.get('nz', '')
    lang = culture(host, options)
    if options.get('l'):
        fractions, groups = (',', '\u00a0') if lang == 'ru' else ('.', ',')
    else:
        seps = host.number_separators(); fractions, groups = seps['Fractions'], seps['Groups']
    # 1C uses one UTF-16 code unit, including when the separator is a surrogate pair.
    fractions = units(options.get('nds', fractions) or fractions)[:1]
    groups = units(options.get('ngs', groups) or groups)[:1]
    digits = max(0, integer(options, 'nd'))
    places = max(0, integer(options, 'nfd')) if 'nfd' in options or 'nd' in options else None
    if digits and places is not None: places = min(places, digits)
    with localcontext() as ctx:
        ctx.prec = 3100
        amount = abs(value).scaleb(-integer(options, 'ns'))
        if places is not None: amount = amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if digits:
            maximum = Decimal(10)**(digits-(places or 0))-Decimal(1).scaleb(-(places or 0))
            amount = min(amount, maximum)
        text = format(amount, 'f')
    whole, dot, part = text.partition('.')
    if places is None: part = part.rstrip('0')
    if digits and digits == places: whole = ''
    if digits and 'nlz' in options: whole = whole.zfill(max(0, digits-(places or 0)))
    grouping = options.get('ng', '3,0')
    widths = [int(x.strip()) if re.fullmatch(r'\s*[0-9]{1,4}\s*',x) else 0 for x in grouping.split(',')[:2]]
    if widths[0] > 0:
        chunks = []; width = widths[0]
        while width and len(whole) > width:
            chunks.append(whole[-width:]); whole = whole[:-width]
            width = (widths[1] or widths[0]) if len(widths) > 1 else 0
        whole = groups.join([whole, *reversed(chunks)])
    text = whole+(fractions+part if part else '')
    if value < 0 and amount:
        mode = integer(options, 'nn', 1)
        text = {0:'('+text+')', 1:'-'+text, 2:'- '+text, 3:text+'-', 4:text+' -'}.get(mode, '-'+text)
    if 'nf' in options:
        if len(text)*sum(c in 'NЧ' for c in options['nf'])+len(options['nf']) > 1000000:
            raise Failure('scenario_limit', 'Formatted text is too large.')
        text = template(options['nf'], lambda token: text*len(token) if token[0] in 'NЧ' else token,
                        escaped_quotes=True)
    return from_units(text)


MONTHS = {
    'ru': 'январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь'.split(),
    'ru_gen': 'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split(),
    'ru_short': 'янв. февр. март апр. май июнь июль авг. сент. окт. нояб. дек.'.split(),
    'en': 'January February March April May June July August September October November December'.split(),
}
DAYS = {
    'ru': 'понедельник вторник среда четверг пятница суббота воскресенье'.split(),
    'ru_short': 'Пн Вт Ср Чт Пт Сб Вс'.split(),
    'en': 'Monday Tuesday Wednesday Thursday Friday Saturday Sunday'.split(),
}


def date(value, options, host):
    if value == datetime(1,1,1): return options.get('de', '')
    lang = culture(host, options)
    russian = lang == 'ru'
    short = 'dd.MM.yyyy' if russian else 'dd/MM/yyyy' if lang == 'en_gb' else 'M/d/yyyy'
    clock = 'H:mm:ss' if russian or lang == 'en_gb' else 'h:mm:ss tt'
    long = "d MMMM yyyy'\u00a0г.'" if russian else 'd MMMM yyyy' if lang == 'en_gb' else 'MMMM d, yyyy'
    local = options.get('dlf', '').upper().translate(str.maketrans({'Д':'D','В':'T'}))
    defaults = {'D':short,'DD':long,'T':clock,'DT':short+' '+clock,'DDT':long+' '+clock}
    pattern = options.get('df') or defaults.get(local, short+' '+clock)
    genitive = bool(re.search(r'(?<![dд])[dд]{1,2}(?![dд])', pattern))
    def token(part):
        char = part[0].translate(str.maketrans({'г':'y','д':'d','к':'q','М':'M','м':'m','с':'s','ч':'h','Ч':'H','в':'t'}))
        count = len(part)
        if char == 'y': return str(value.year % 100) if count == 1 else f'{value.year%100:02}' if count == 2 else f'{value.year:04}'
        if char == 'M' and count >= 3:
            if russian:
                result = MONTHS['ru_short' if count == 3 else 'ru_gen' if genitive else 'ru'][value.month-1]
                return result.capitalize() if count > 3 and not genitive else result
            return MONTHS['en'][value.month-1][:3] if count == 3 else MONTHS['en'][value.month-1]
        if char == 'd' and count >= 3:
            if russian: return DAYS['ru_short' if count == 3 else 'ru'][value.weekday()]
            return DAYS['en'][value.weekday()][:3] if count == 3 else DAYS['en'][value.weekday()]
        if char == 't': return 'AM' if value.hour < 12 else 'PM'
        values = {'d':value.day, 'M':value.month,'m':value.minute,'s':value.second,'h':value.hour%12 or 12,'H':value.hour,'q':(value.month-1)//3+1}
        if char in values: return str(values[char]).zfill(2 if count > 1 else 1)
        return part
    return template(pattern, token)


def call(host, name, args):
    if name == 'nstr':
        language = args[1] if len(args) > 1 else ''
        if language is None: language = ''
        if not isinstance(language, str): raise Failure('scenario_failed', 'NStr language must be a string.')
        if not language:
            language = host.scenario_language()
        for key, value in pairs(args[0]):
            if key == language.casefold(): return value
        return ''
    value = args[0]
    options = {}
    for key, item in pairs(args[1] if len(args)>1 and args[1] is not None else ''):
        key = ALIASES.get(key, key)
        if key != 'df' or key not in options: options[key] = item
    if value is None or value is NULL: return ''
    if isinstance(value, str): return from_units(value)
    if type(value) is bool:
        key = 'bt' if value else 'bf'
        if key in options: return options[key]
        if not options.get('l'):
            language = host.system_language()
            options = dict(options, l=language)
        return ('Да' if value else 'Нет') if culture(host, options) == 'ru' else ('Yes' if value else 'No')
    if type(value) is Decimal: return number(value, options, host)
    if type(value) is datetime: return date(value, options, host)
    raise Failure('unsupported_format_value', 'Format supports numbers, dates, Booleans and strings.')
