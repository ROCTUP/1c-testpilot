"""Scalar platform functions, constants and error values."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_UP, ROUND_HALF_DOWN
from enum import Enum

from compatible_scenarios.shared.bsl.language import Failure, Structure, Map, NULL, COLLECTIONS
from compatible_scenarios.shared.bsl.strings import Constants, MISSING, integer


class MessageStatus(Enum):
    WITHOUT_STATUS = 'WithoutStatus'
    ORDINARY = 'Ordinary'
    INFORMATION = 'Information'
    ATTENTION = 'Attention'
    IMPORTANT = 'Important'
    VERY_IMPORTANT = 'VeryImportant'


class RoundMode(Enum):
    DOWN = 0
    UP = 1


@dataclass(frozen=True)
class ErrorInfo:
    description: str
    details: tuple = ()

    def get(self, name):
        if name in ('description', 'описание'): return self.description
        raise Failure('unsupported_scenario', f'Unsupported error information property {name!r}.')

    def detailed(self):
        return '\n'.join(dict.fromkeys((self.description, *self.details)))


def constants():
    from compatible_scenarios.shared.bsl.collections import SortDirection
    sorting = Constants({'Asc':SortDirection.ASC, 'Возр':SortDirection.ASC,
                         'Desc':SortDirection.DESC, 'Убыв':SortDirection.DESC})
    status = Constants(dict(zip(
        ('WithoutStatus','БезСтатуса','Ordinary','Обычное','Information','Информация',
         'Attention','Внимание','Important','Важное','VeryImportant','ОченьВажное'),
        (v for item in MessageStatus for v in (item, item)))))
    rounding = Constants({'Round15as10':RoundMode.DOWN, 'Окр15как10':RoundMode.DOWN,
                          'Round15as20':RoundMode.UP, 'Окр15как20':RoundMode.UP})
    return dict(messagestatus=status, статуссообщения=status, roundmode=rounding, режимокругления=rounding,
                sortdirection=sorting, направлениесортировки=sorting)


def number(value):
    if type(value) not in (Decimal, str, bool):
        raise Failure('scenario_failed', 'Cannot convert the value to a number.')
    try: result = Decimal(value)
    except InvalidOperation as exc:
        raise Failure('scenario_failed', 'Cannot convert the value to a number.') from exc
    if not result.is_finite(): raise Failure('scenario_failed', 'A finite number is required.')
    return result


def filled(value):
    if value is None or value is NULL: return False
    if isinstance(value, str): return bool(value.strip())
    if isinstance(value, Decimal): return value != 0
    if type(value) is datetime: return value != datetime(1, 1, 1)
    if isinstance(value, COLLECTIONS): return bool(len(value))
    return True


def numeric(name, args):
    if name in ('min', 'max'):
        if not args: raise Failure('scenario_failed', 'At least one value is required.')
        numeric_types = (Decimal, bool)
        if len(args) > 1 and (type(args[0]) not in (Decimal, str, bool, datetime) or
                             any(type(a) is not type(args[0]) and not (type(a) in numeric_types and type(args[0]) in numeric_types) for a in args)):
            raise Failure('scenario_failed', 'Comparison requires matching primitive types.')
        try: return (min if name == 'min' else max)(args)
        except TypeError as exc: raise Failure('scenario_failed', 'Values cannot be compared.') from exc
    value = number(args[0])
    places = 0 if len(args) < 2 or args[1] is MISSING else integer(args[1])
    mode = RoundMode.UP if len(args) < 3 or args[2] is MISSING else args[2]
    if not isinstance(mode, RoundMode): mode = RoundMode.UP if integer(mode) != 0 else RoundMode.DOWN
    if abs(places) > 1000 or len(value.as_tuple().digits) + abs(value.adjusted()) > 1000:
        raise Failure('scenario_limit', 'Rounding exceeds the supported numeric precision.')
    with localcontext() as ctx:
        ctx.prec = max(38, len(value.as_tuple().digits) + abs(value.adjusted()) + abs(places) + 2)
        return value.quantize(Decimal(1).scaleb(-places),
                              rounding=ROUND_HALF_UP if mode is RoundMode.UP else ROUND_HALF_DOWN)
