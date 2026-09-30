"""Local collection operations for compatible BSL scenarios."""
from decimal import Decimal
from datetime import datetime
from enum import Enum

from compatible_scenarios.shared.bsl.language import (Failure, FixedArray, FixedStructure, FixedMap, Structure,
                         Map, ValueList, ValueListItem, value_equal, NULL)
from compatible_scenarios.shared.bsl.strings import integer


CONSTRUCTORS = {
    'fixedarray': FixedArray, 'фиксированныймассив': FixedArray,
    'fixedstructure': FixedStructure, 'фиксированнаяструктура': FixedStructure,
    'fixedmap': FixedMap, 'фиксированноесоответствие': FixedMap,
    'valuelist': ValueList, 'списокзначений': ValueList,
}
TYPE_NAMES = {FixedArray:'fixedarray', FixedStructure:'fixedstructure', FixedMap:'fixedmap',
              ValueList:'valuelist', ValueListItem:'valuelistitem'}


class SortDirection(Enum):
    ASC = 0
    DESC = 1


def sort_values(vm, obj, presentation, direction):
    if direction is None: direction = SortDirection.ASC
    if not isinstance(direction, SortDirection): fail('Sort direction must be SortDirection.Asc or Desc.')
    from contextlib import ExitStack
    from compatible_scenarios.shared.bsl.collation import Collation
    with ExitStack() as stack:
        collation = None
        def key(item):
            nonlocal collation
            vm.host.tick(vm.program.path, vm.line)
            value = item.presentation if presentation else item.value
            if value is None: return (0, 0)
            if value is NULL: return (1, 0)
            if type(value) is bool: return (2, value)
            if type(value) is Decimal: return (3, value)
            if type(value) is str:
                if collation is None: collation = stack.enter_context(Collation())
                return (4, collation.key(value))
            if type(value) is datetime: return (5, value)
            ranks = {ValueList:6, list:7, Map:8, Structure:9, FixedStructure:10, FixedMap:11, FixedArray:12}
            if type(value) in ranks: return (ranks[type(value)], 0)
            fail('Sorting supports Undefined, Null, Boolean, number, string and date values.')
        # Prepare the entire result before replacing the list on an error or deadline.
        items = sorted(obj.items, key=key, reverse=direction is SortDirection.DESC)
        vm.host.tick(vm.program.path, vm.line)
        obj.items = items


def fail(message): raise Failure('scenario_failed', message)


def construct(name, values):
    cls = CONSTRUCTORS[name]
    if cls is ValueList:
        if values: fail('ValueList does not accept constructor arguments.')
        return ValueList()
    if cls is FixedStructure and (not values or type(values[0]) is str):
        if not values: return FixedStructure()
        keys = [key.strip() for key in values[0].split(',')] if values[0].strip() else []
        if len(values)-1 > len(keys) or any(not key.isidentifier() for key in keys):
            fail('Invalid FixedStructure property names or values.')
        return FixedStructure({key: values[i+1] if i+1 < len(values) else None for i, key in enumerate(keys)})
    source_type = {FixedArray:list, FixedStructure:Structure, FixedMap:Map}[cls]
    if len(values) != 1 or type(values[0]) is not source_type:
        fail('A fixed collection requires one corresponding collection argument.')
    source = values[0]
    if cls is FixedMap:
        result = FixedMap()
        for key, value in source.items(): result[key] = value
        return result
    return cls(source)


def item_field(name):
    fields = {'value':'value', 'значение':'value', 'presentation':'presentation',
              'представление':'presentation', 'check':'check', 'пометка':'check'}
    field = fields.get(name.casefold()) if isinstance(name, str) else None
    if field is None: fail('ValueListItem supports Value, Presentation and Check.')
    return field


def set_item(item, field, value):
    if field == 'presentation' and type(value) is not str: fail('Presentation must be a string.')
    if field == 'check' and type(value) is not bool: fail('Check must be Boolean.')
    setattr(item, field, value)


def collection_method(vm, obj, name, args):
    if isinstance(obj, ValueListItem):
        if name in ('getid', 'получитьидентификатор') and not args: return Decimal(obj.identifier)
        fail(f'Unsupported ValueListItem method {name}.')
    def tick(): vm.host.tick(vm.program.path, vm.line)
    if name in ('sortbyvalue', 'сортироватьпозначению', 'sortbypresentation', 'сортироватьпопредставлению') and len(args) <= 1:
        return sort_values(vm, obj, name in ('sortbypresentation', 'сортироватьпопредставлению'), args[0] if args else None)
    if name in ('findbyid', 'найтипоидентификатору') and len(args) == 1:
        identifier = integer(args[0])
        return next((item for item in obj if item.identifier == identifier), None)
    if name in ('move', 'сдвинуть') and len(args) == 2:
        if isinstance(args[0], ValueListItem):
            index = next((i for i, item in enumerate(obj) if item is args[0]), None)
            if index is None: fail('The item does not belong to this ValueList.')
        else: index = vm.index(args[0], len(obj))
        offset = integer(args[1])
        if not 0 <= index + offset < len(obj): fail('Moving would put the item outside this ValueList.')
        obj.items.insert(index + offset, obj.items.pop(index))
        return None
    if name in ('count', 'количество') and not args: return Decimal(len(obj))
    if name in ('get', 'получить') and len(args) == 1: return obj[vm.index(args[0], len(obj))]
    if name in ('add', 'добавить', 'insert', 'вставить'):
        insert = name in ('insert', 'вставить')
        if not (int(insert) <= len(args) <= 4 + int(insert)): fail('Incorrect ValueList item arguments.')
        index = integer(args[0]) if insert else len(obj)
        if not 0 <= index <= len(obj): fail('ValueList index is outside its bounds.')
        values = args[1:] if insert else args
        if len(values) == 4 and values[3] is not None:
            fail('ValueList item pictures are not supported.')
        if len(obj) >= 10000: fail('ValueList limit exceeded.')
        item = ValueListItem()
        for field, value in zip(('value', 'presentation', 'check'), values):
            if value is None and field != 'value': continue
            set_item(item, field, value)
        item.identifier = obj.next_id
        obj.next_id += 1
        obj.items.insert(index, item)
        return item
    if name in ('findbyvalue', 'найтипозначению') and len(args) == 1:
        for item in obj:
            tick()
            if value_equal(item.value, args[0]): return item
        return None
    if name in ('indexof', 'индекс') and len(args) == 1:
        if not isinstance(args[0], ValueListItem): fail('IndexOf requires a ValueListItem.')
        return Decimal(next((i for i, item in enumerate(obj) if item is args[0]), -1))
    if name in ('delete', 'удалить') and len(args) == 1:
        if isinstance(args[0], ValueListItem):
            index = next((i for i, item in enumerate(obj) if item is args[0]), None)
            if index is None: fail('The item does not belong to this ValueList.')
        else: index = vm.index(args[0], len(obj))
        del obj.items[index]
        return None
    if name in ('clear', 'очистить') and not args: obj.items.clear(); return None
    if name in ('copy', 'скопировать') and not args:
        result = ValueList()
        for item in obj:
            tick()
            result.items.append(result.new_item(item.value, item.presentation, item.check))
        return result
    if name in ('unloadvalues', 'выгрузитьзначения') and not args:
        return [item.value for item in obj]
    if name in ('loadvalues', 'загрузитьзначения') and len(args) == 1 and type(args[0]) is list:
        if len(args[0]) > 10000: fail('ValueList limit exceeded.')
        obj.items = [obj.new_item(value) for value in args[0]]
        return None
    if name in ('fillchecks', 'заполнитьпометки') and len(args) == 1:
        if type(args[0]) is not bool: fail('FillChecks requires a Boolean.')
        for item in obj: tick(); item.check = args[0]
        return None
    fail(f'Unsupported ValueList method {name} or argument count.')
