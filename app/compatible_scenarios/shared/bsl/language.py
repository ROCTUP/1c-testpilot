"""Parsed, explicitly bounded BSL for compatible scenarios (no Python eval)."""
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
import operator
import re
from compatible_scenarios.errors import Failure


class _Null:
    def __repr__(self): return 'Null'


NULL = _Null()


@dataclass
class Node:
    kind: str
    args: tuple
    line: int


WORDS = {}
for pair in ('if если', 'then тогда', 'elsif иначеесли', 'else иначе', 'endif конецесли',
             'for для', 'each каждого', 'in из', 'to по', 'do цикл', 'enddo конеццикла',
             'while пока', 'break прервать', 'continue продолжить', 'return возврат',
             'function функция', 'procedure процедура', 'endfunction конецфункции',
             'endprocedure конецпроцедуры', 'val знач', 'export экспорт', 'var перем',
             'new новый', 'true истина', 'false ложь', 'undefined неопределено',
             'and и', 'or или', 'not не', 'raise вызватьисключение',
             'try попытка', 'except исключение', 'endtry конецпопытки'):
    en, ru = pair.split(); WORDS[en] = WORDS[ru] = en


def lex(source, path):
    # Preserve line numbers, including lone CR, and follow BSL multiline literals.
    source = source.replace('\r\n', '\n').replace('\r', '\n')
    out, pos, line, regions = [], 0, 1, []
    pattern = re.compile(r'\d+(?:\.\d+)?|[^\W\d]\w*|<>|<=|>=|[+*/%=<>.,;()\[\]?\-]', re.UNICODE)
    while pos < len(source):
        ch = source[pos]
        if ch.isspace():
            line += ch == '\n'; pos += 1; continue
        if source.startswith('//', pos):
            end = source.find('\n', pos)
            pos = len(source) if end < 0 else end
            continue
        if ch == '#' and not source[source.rfind('\n', 0, pos)+1:pos].strip():
            end = source.find('\n', pos)
            directive = source[pos:len(source) if end < 0 else end].split('//', 1)[0].strip()
            opening = re.fullmatch(r'#(?:Область|Region)\s+([^\W\d]\w*)', directive, re.I)
            closing = re.fullmatch(r'#(?:КонецОбласти|EndRegion)', directive, re.I)
            if opening: regions.append(line)
            elif closing and regions: regions.pop()
            else:
                raise Failure('unsupported_scenario', 'Unsupported or unmatched region directive.', path=path, line=line)
            pos = len(source) if end < 0 else end
            continue
        start_line = line
        if ch == "'":
            end = source.find("'", pos + 1)
            if end < 0 or '\n' in source[pos:end]:
                raise Failure('unsupported_scenario', 'Unterminated date literal.', path=path, line=line)
            from compatible_scenarios.shared.bsl.dates import literal
            try: value = literal(source[pos+1:end])
            except Failure as exc:
                exc.result.update(path=str(path), line=line); raise
            out.append(('date', value, line)); pos = end + 1; continue
        if ch == '"':
            pos += 1; parts = []
            while True:
                if pos >= len(source):
                    raise Failure('unsupported_scenario', 'Unterminated BSL string.', path=path, line=start_line)
                ch = source[pos]; pos += 1
                if ch == '"':
                    if pos < len(source) and source[pos] == '"':
                        parts.append('"'); pos += 1; continue
                    break
                if ch == '\n':
                    line += 1
                    while True:
                        while pos < len(source) and source[pos] in ' \t\v\f': pos += 1
                        if source.startswith('//', pos):
                            end = source.find('\n', pos)
                            pos = len(source) if end < 0 else end
                        if pos < len(source) and source[pos] == '\n':
                            line += 1; pos += 1; continue
                        break
                    if pos >= len(source) or source[pos] != '|':
                        raise Failure('unsupported_scenario', 'Expected | in a multiline BSL string.', path=path, line=line)
                    pos += 1; parts.append('\n')
                else: parts.append(ch)
            out.append(('string', ''.join(parts), start_line)); continue
        match = pattern.match(source, pos)
        if match is None:
            raise Failure('unsupported_scenario', f'Unsupported syntax: {source[pos:pos+30]!r}.', path=path, line=line)
        value = match.group(); pos = match.end()
        if value[0].isdigit(): kind = 'number'
        elif value[0].isalpha() or value[0] == '_':
            kind = 'name'; value = WORDS.get(value.casefold(), value.casefold())
        else: kind = 'symbol'
        out.append((kind, value, line))
    if regions:
        raise Failure('unsupported_scenario', 'Region has no EndRegion.', path=path, line=regions[-1])
    out.append(('eof', '', line))
    return out


class Parser:
    def __init__(self, source, path='<scenario>'):
        self.path, self.items, self.pos = str(path), lex(source, path), 0
        self.functions = {}
        self.loops = 0

    def peek(self):
        kind, value, _ = self.items[self.pos]
        # A literal comma/parenthesis is a value, never an argument separator.
        return '<literal>' if kind in ('string', 'number', 'date') else value

    def take(self, expected=None):
        item = self.items[self.pos]
        if expected is not None and item[1] != expected:
            self.fail(f'Expected {expected!r}, got {item[1]!r}.')
        if item[0] == 'eof': self.fail('Unexpected end of scenario.')
        self.pos += 1
        return item

    def accept(self, word):
        if self.peek() == word:
            self.take(); return True
        return False

    def fail(self, message):
        raise Failure('unsupported_scenario', message, path=self.path, line=self.items[self.pos][2])

    def name(self):
        if self.items[self.pos][0] != 'name': self.fail('Expected an identifier.')
        return self.take()[1]

    def parse(self):
        tree = self.block(top=True)
        return tree, self.functions

    def block(self, stops=(), top=False):
        nodes = []
        while self.peek() and self.peek() not in stops:
            word, line = self.peek(), self.items[self.pos][2]
            if self.accept(';'): continue
            if word in ('function', 'procedure'):
                if not top: self.fail('Nested function declarations are not supported.')
                self.take(); name = self.name(); self.take('('); params = []
                if self.peek() != ')':
                    while True:
                        byval = self.accept('val'); arg = self.name()
                        default = self.expr() if self.accept('=') else None
                        params.append((arg, byval, default))
                        if not self.accept(','): break
                self.take(')'); self.accept('export')
                body = self.block(('end' + word,)); self.take('end' + word); self.accept(';')
                if name in self.functions: self.fail(f'Duplicate function {name}.')
                self.functions[name] = Node('function', (params, body), line)
                continue
            if self.accept('if'):
                branches = []
                while True:
                    cond = self.expr(); self.take('then')
                    branches.append((cond, self.block(('elsif', 'else', 'endif'))))
                    if not self.accept('elsif'): break
                otherwise = self.block(('endif',)) if self.accept('else') else []
                self.take('endif'); node = Node('if', (branches, otherwise), line)
            elif self.accept('try'):
                body = self.block(('except',)); self.take('except')
                handler = self.block(('endtry',)); self.take('endtry')
                node = Node('try', (body, handler), line)
            elif word in ('for', 'while'):
                self.take()
                if word == 'while': args = (self.expr(),); kind = 'while'
                elif self.accept('each'):
                    name = self.name(); self.take('in'); args = (name, self.expr()); kind = 'each'
                else:
                    name = self.name(); self.take('='); start = self.expr(); self.take('to')
                    args = (name, start, self.expr()); kind = 'for'
                self.take('do'); self.loops += 1
                body = self.block(('enddo',)); self.loops -= 1; self.take('enddo')
                node = Node(kind, (*args, body), line)
            elif word in ('break', 'continue'):
                if not self.loops: self.fail(f'{word} outside a loop.')
                self.take(); node = Node(word, (), line)
            elif self.accept('var'):
                names = [self.name()]
                while self.accept(','): names.append(self.name())
                node = Node('var', (names,), line)
            elif word in ('return', 'raise'):
                self.take()
                empty = self.peek() in (';', '', *stops)
                value = None if empty and word == 'raise' else Node('value', (None,), line) if empty else self.expr()
                node = Node(word, (value,), line)
            else:
                # '=' is comparison within expressions, assignment only here.
                left = self.expr(4)
                if self.accept('='):
                    if left.kind not in ('name', 'attr', 'index'): self.fail('Invalid assignment target.')
                    node = Node('set', (left, self.expr()), line)
                else:
                    if left.kind != 'call': self.fail('Expected an assignment or procedure call.')
                    node = Node('expr', (left,), line)
            nodes.append(node)
            if not self.accept(';') and self.peek() not in ('', *stops):
                self.fail('Expected ; between statements.')
        return nodes

    def arguments(self):
        args = []; self.take('(')
        if self.peek() != ')':
            while True:
                args.append(Node('omitted', (), self.items[self.pos][2]) if self.peek() in (',', ')') else self.expr())
                if not self.accept(','): break
        self.take(')'); return args

    def expr(self, minimum=0):
        kind, word, line = self.take()
        if kind == 'string': left = Node('value', (word,), line)
        elif kind == 'date': left = Node('value', (word,), line)
        elif kind == 'number': left = Node('value', (Decimal(word),), line)
        elif word in ('true', 'false', 'undefined', 'null'):
            left = Node('value', ({'true': True, 'false': False, 'null': NULL}.get(word),), line)
        elif word in ('not', '+', '-'):
            left = Node('unary', (word, self.expr(3 if word == 'not' else 6)), line)
        elif word == '(':
            left = self.expr(); self.take(')')
        elif word == '?':
            args = self.arguments()
            if len(args) != 3: self.fail('?(condition, yes, no) requires three arguments.')
            left = Node('conditional', tuple(args), line)
        elif word == 'new':
            name = self.name(); args = self.arguments() if self.peek() == '(' else []
            left = Node('new', (name, args), line)
        elif kind == 'name': left = Node('name', (word,), line)
        else: self.fail(f'Unsupported expression {word!r}.')
        while self.peek() in ('.', '[', '('):
            if self.accept('.'):
                left = Node('attr', (left, self.name()), line)
            elif self.accept('['):
                index = self.expr(); self.take(']'); left = Node('index', (left, index), line)
            else: left = Node('call', (left, self.arguments()), line)
        priorities = {'or': 1, 'and': 2, '=': 3, '<>': 3, '<': 3, '>': 3, '<=': 3, '>=': 3,
                      '+': 4, '-': 4, '*': 5, '/': 5, '%': 5}
        while self.peek() in priorities and priorities[self.peek()] >= minimum:
            op = self.take()[1]
            left = Node('binary', (op, left, self.expr(priorities[op] + 1)), line)
        return left


def walk(value):
    if isinstance(value, Node):
        yield value
        for arg in value.args: yield from walk(arg)
    elif isinstance(value, (list, tuple)):
        for arg in value: yield from walk(arg)


class Structure(dict):
    def actual(self, key):
        return next((k for k in self if k.casefold() == str(key).casefold()), key)

    def __getitem__(self, key): return super().__getitem__(self.actual(key))
    def __setitem__(self, key, value): super().__setitem__(self.actual(key), value)


class Map:
    """BSL correspondence: keys are case-sensitive and retain their scalar type."""
    def __init__(self): self.values = {}
    def key(self, key):
        if key is not None and key is not NULL and type(key) not in (str, bool, Decimal, datetime):
            raise Failure('unsupported_scenario', 'Map keys must be scalar values.')
        return (type(key), key)
    def __getitem__(self, key): return self.values.get(self.key(key))
    def __setitem__(self, key, value): self.values[self.key(key)] = value
    def __len__(self): return len(self.values)
    def items(self): return [(key[1], value) for key, value in self.values.items()]
    def clear(self): self.values.clear()
    def delete(self, key): self.values.pop(self.key(key), None)


# Mutation is checked by the VM; private storage remains writable for graph restoration.
class FixedArray(list):
    def __eq__(self, other): return self is other
    def __ne__(self, other): return self is not other


class FixedStructure(Structure):
    def __eq__(self, other): return self is other
    def __ne__(self, other): return self is not other
class FixedMap(Map): pass


class ValueList:
    def __init__(self): self.items, self.next_id = [], 0
    def __len__(self): return len(self.items)
    def __iter__(self): return iter(self.items)
    def __getitem__(self, index): return self.items[index]
    def new_item(self, value=None, presentation='', check=False):
        item = ValueListItem(value, presentation, check, self.next_id)
        self.next_id += 1
        return item


@dataclass(eq=False)
class ValueListItem:
    value: object = None
    presentation: str = ''
    check: bool = False
    identifier: int = 0


FIXED = (FixedArray, FixedStructure, FixedMap)
COLLECTIONS = (list, Structure, Map, ValueList)


def value_equal(left, right):
    if isinstance(left, (*COLLECTIONS, ValueListItem)) or isinstance(right, (*COLLECTIONS, ValueListItem)):
        return left is right
    return type(left) is type(right) and left == right


@dataclass(frozen=True)
class TypeValue:
    name: str


def value_in(value):
    if isinstance(value, str):
        from compatible_scenarios.shared.bsl.strings import from_units
        return from_units(value)
    if isinstance(value, dict) and value == {'$type': 'null'}: return NULL
    if type(value) is datetime:
        from compatible_scenarios.shared.bsl.dates import checked
        return checked(value)
    if isinstance(value, dict) and value.get('$type') == 'date':
        from compatible_scenarios.shared.bsl.dates import from_wire
        return from_wire(value.get('value'))
    if isinstance(value, dict): return Structure({k: value_in(v) for k, v in value.items()})
    if isinstance(value, list): return [value_in(v) for v in value]
    if type(value) in (int, float): return Decimal(str(value))
    return value


def value_out(value):
    if value is NULL: return {'$type': 'null'}
    if isinstance(value, str):
        from compatible_scenarios.shared.bsl.strings import from_units
        value = from_units(value)
        try: value.encode('utf-8')
        except UnicodeEncodeError:
            raise Failure('unsupported_result', 'A string contains an isolated UTF-16 surrogate. '
                          'Reassemble the character or return its CharCode.') from None
        return value
    if type(value) is datetime:
        from compatible_scenarios.shared.bsl.dates import to_wire
        return to_wire(value)
    from compatible_scenarios.shared.bsl.binary import BinaryData
    if isinstance(value, BinaryData):
        import base64
        return {'$type': 'binary', 'base64': base64.b64encode(value.data).decode('ascii')}
    if isinstance(value, Decimal): return int(value) if value == int(value) else float(value)
    if isinstance(value, ValueList): return [value_out(item) for item in value]
    if isinstance(value, ValueListItem):
        return value_out(dict(value=value.value, presentation=value.presentation, check=value.check))
    if isinstance(value, Map):
        if any(type(key) is not str for key, _ in value.items()):
            raise Failure('unsupported_result', 'A returned Map or UI argument must have string keys.')
        return {value_out(key): value_out(item) for key, item in value.items()}
    if isinstance(value, (dict, list, tuple)):
        return {value_out(k): value_out(v) for k, v in value.items()} if isinstance(value, dict) else [value_out(v) for v in value]
    if value is None or type(value) in (str, bool, int, float): return value
    raise Failure('unsupported_result', 'The scenario returned a UI object; return its value or properties instead.')


def string(value):
    if type(value) is datetime:
        from compatible_scenarios.shared.bsl.dates import presentation
        return presentation(value)
    if value is None or value is NULL: return ''
    if type(value) is bool: return 'Да' if value else 'Нет'
    if isinstance(value, Decimal): return format(value, 'f')
    if isinstance(value, str): return value
    raise Failure('unsupported_scenario', 'String conversion for this type is not supported.')


def boolean(value):
    if type(value) is not bool:
        raise Failure('scenario_failed', 'A Boolean value was expected.')
    return value


class Flow(BaseException):
    def __init__(self, kind, value=None): self.kind, self.value = kind, value


class VM:
    """Private environment per invocation; only explicitly registered calls are exposed."""
    def __init__(self, host, program, parameter=None):
        self.host, self.program = host, program
        self.env = host.initial_locals(parameter)
        self.line = 1
        self.current_error = None

    def fail(self, text):
        raise Failure('scenario_failed', text, path=self.program.path, line=self.line)

    def get(self, name):
        if name in self.env: return self.env[name]
        if name in ('thisobject', 'этотобъект'):
            from compatible_scenarios.shared.bsl.callbacks import ModuleContext
            return ModuleContext(self)
        found, value = self.host.resolve_global(name)
        if found: return value
        self.fail(f'Variable {name!r} is not defined.')

    def assign(self, node, value):
        self.assignment_target(node)(value)

    def assignment_target(self, node):
        """Resolve a writable argument once, including any computed container/index."""
        if node.kind == 'name':
            name = node.args[0]
            def put(value):
                if not self.host.assign_global(name, value): self.env[name] = value
            return put
        elif node.kind in ('attr', 'index'):
            target = self.expr(node.args[0]); key = node.args[1] if node.kind == 'attr' else self.expr(node.args[1])
            if isinstance(target, FIXED): self.fail('A fixed collection cannot be changed.')
            if isinstance(target, ValueListItem):
                from compatible_scenarios.shared.bsl.collections import item_field, set_item
                field = item_field(key)
                return lambda value: set_item(target, field, value)
            if isinstance(target, list): key = self.index(key, len(target))
            if not isinstance(target, (Structure, Map, list)): self.fail('Only array, structure and map values can be assigned.')
            if isinstance(target, Structure) and target.actual(key) not in target:
                self.fail(f'Structure property {key!r} does not exist; use Insert.')
            def put(value):
                self.host.value_changed(target)
                target[key] = value
            return put
        else: self.fail('Invalid assignment.')

    def index(self, value, length):
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or int(value) != value or not 0 <= value < length:
            self.fail('Array index is outside its bounds.')
        return int(value)

    def expr(self, node):
        self.host.tick(self.program.path, node.line)
        kind, args = node.kind, node.args
        if kind == 'omitted': return None
        if kind == 'value': return args[0]
        if kind == 'name': return self.get(args[0])
        if kind == 'attr':
            obj = self.expr(args[0])
            if isinstance(obj, ValueListItem):
                from compatible_scenarios.shared.bsl.collections import item_field
                return getattr(obj, item_field(args[1]))
            if isinstance(obj, Structure): return obj[args[1]]
            return self.host.attribute(obj, args[1])
        if kind == 'index':
            obj, key = self.expr(args[0]), self.expr(args[1])
            if isinstance(obj, (list, str, ValueList)): return obj[self.index(key, len(obj))]
            if isinstance(obj, ValueListItem):
                from compatible_scenarios.shared.bsl.collections import item_field
                return getattr(obj, item_field(key))
            if isinstance(obj, Structure): return obj.get(obj.actual(key))
            if isinstance(obj, Map): return obj[key]
            self.fail('Indexed access is not supported for this value.')
        if kind == 'conditional': return self.expr(args[1] if boolean(self.expr(args[0])) else args[2])
        if kind == 'unary':
            op, value = args[0], self.expr(args[1])
            if value is NULL: self.fail('Null cannot be used as a number or Boolean.')
            return not boolean(value) if op == 'not' else (+value if op == '+' else -value)
        if kind == 'binary':
            op, a = args[0], self.expr(args[1])
            if op == 'and': return boolean(a) and boolean(self.expr(args[2]))
            if op == 'or': return boolean(a) or boolean(self.expr(args[2]))
            b = self.expr(args[2])
            if op in ('=', '<>') and (isinstance(a, (*COLLECTIONS, ValueListItem))
                                     or isinstance(b, (*COLLECTIONS, ValueListItem))):
                equal = value_equal(a, b)
                return equal if op == '=' else not equal
            if op == '+' and isinstance(a, str) and b is NULL: return a
            if op in ('<', '>', '<=', '>=') and (a is NULL or b is NULL):
                self.fail('Null does not support ordered comparisons.')
            if type(a) is datetime and op in ('+', '-', '*', '/', '%'):
                from compatible_scenarios.shared.bsl.dates import arithmetic
                return arithmetic(op, a, b)
            if op in ('+', '-', '*', '/', '%'):
                if op == '+' and isinstance(a, str) and isinstance(b, str):
                    if len(a) + len(b) > 1000000: self.fail('String value exceeds 1000000 characters.')
                    from compatible_scenarios.shared.bsl.strings import from_units
                    return from_units(a + b)
                elif not isinstance(a, Decimal) or not isinstance(b, Decimal):
                    self.fail('Arithmetic requires numbers; concatenation requires strings.')
            return {'=': operator.eq, '<>': operator.ne, '<': operator.lt, '>': operator.gt,
                    '<=': operator.le, '>=': operator.ge, '+': operator.add, '-': operator.sub,
                    '*': operator.mul, '/': operator.truediv, '%': operator.mod}[op](a, b)
        if kind == 'new':
            name, expressions = args; values = [self.expr(a) for a in expressions]
            if name in ('callbackdescription', 'описаниеоповещения'):
                from compatible_scenarios.shared.bsl.callbacks import construct
                return construct(self, values)
            from compatible_scenarios.shared.bsl.collections import CONSTRUCTORS, construct
            if name in CONSTRUCTORS: return construct(name, values)
            if name in ('map', 'соответствие'):
                if len(values) == 1 and isinstance(values[0], FixedMap):
                    result = Map()
                    for key, value in values[0].items(): result[key] = value
                    return result
                if values: self.fail('Map does not accept constructor arguments.')
                return Map()
            if name in ('структура', 'structure'):
                if not values: return Structure()
                if len(values) == 1 and isinstance(values[0], FixedStructure): return Structure(values[0])
                keys = [k.strip() for k in values[0].split(',')]
                if len(values) - 1 > len(keys): self.fail('Too many Structure values.')
                if any(not k.isidentifier() for k in keys): self.fail('Invalid Structure key.')
                return Structure({k: values[i+1] if i+1 < len(values) else None for i, k in enumerate(keys)})
            if name in ('массив', 'array'):
                if not values: return []
                if len(values) == 1 and isinstance(values[0], FixedArray): return list(values[0])
                if len(values) != 1 or values[0] != int(values[0]) or not 0 <= values[0] <= 10000:
                    self.fail('Only one-dimensional arrays up to 10000 items are supported.')
                return [None] * int(values[0])
            self.fail(f'Unsupported type {name}.')
        if kind == 'call':
            target, expressions = args
            if target.kind == 'attr' and target.args[1] in ('getdocumenthtml', 'получитьhtmlдокумента'):
                if len(expressions) != 2: self.fail('GetDocumentHTML requires two output arguments.')
                obj = self.expr(target.args[0])
                outputs = []
                for arg in expressions:
                    if arg.kind in ('name', 'attr', 'index'):
                        if arg.kind == 'name': self.get(arg.args[0])
                        outputs.append(self.assignment_target(arg))
                    else:
                        self.expr(arg)
                        outputs.append(None)
                html, attachments = self.host.method(obj, target.args[1], [])
                for put, value in zip(outputs, (html, attachments)):
                    if put is not None: put(value)
                return None
            if target.kind == 'attr' and target.args[1] in ('property', 'свойство'):
                obj = self.expr(target.args[0])
                if isinstance(obj, Structure):
                    if not 1 <= len(expressions) <= 2: self.fail('Property expects a name and an optional output argument.')
                    key = self.expr(expressions[0])
                    if not isinstance(key, str) or not key.isidentifier(): self.fail('Invalid Structure property name.')
                    output = None
                    if len(expressions) == 2:
                        arg = expressions[1]
                        if arg.kind == 'name':
                            self.get(arg.args[0])
                            output = self.assignment_target(arg)
                        elif arg.kind in ('attr', 'index'):
                            output = self.assignment_target(arg)
                        else:
                            # 1C accepts a temporary value here; there is no caller variable to update.
                            self.expr(arg)
                    actual = obj.actual(key)
                    found = actual in obj
                    if output is not None: output(obj[actual] if found else None)
                    return found
                return self.method(obj, target.args[1], [self.expr(a) for a in expressions])
            values = [self.expr(a) for a in expressions]
            if target.kind == 'name':
                name = target.args[0]
                if name in self.program.functions:
                    return self.invoke_function(name, values)
                if name in ('strfind', 'стрнайти', 'strsplit', 'стрразделить', 'round', 'окр'):
                    from compatible_scenarios.shared.bsl.strings import MISSING
                    values = [MISSING if exp.kind == 'omitted' else val for exp, val in zip(expressions, values)]
                return self.host.call(name, values, self)
            if target.kind == 'attr':
                obj = self.expr(target.args[0]); name = target.args[1]
                return self.method(obj, name, values)
            self.fail('Indirect function calls are not supported.')
        self.fail(f'Unsupported expression {kind}.')

    def invoke_function(self, name, values):
        fn = self.program.functions[name]; params, body = fn.args
        if len(values) > len(params): self.fail(f'Too many arguments for {name}.')
        nested = VM(self.host, self.program)
        self.host.inherit_locals(self.env, nested.env)
        for i, (param, byval, default) in enumerate(params):
            if i < len(values) and values[i] is not None: val = values[i]
            elif default is not None: val = self.expr(default)
            else: val = None
            nested.env[param] = val
        return nested.run(body)

    def method(self, obj, name, values):
        from compatible_scenarios.shared.bsl.collections import collection_method
        if isinstance(obj, (ValueList, ValueListItem)):
            return collection_method(self, obj, name, values)
        if isinstance(obj, FIXED) and name not in ('count', 'количество', 'get', 'получить', 'find', 'найти', 'ubound', 'вграница'):
            self.fail('This method is unavailable for a fixed collection.')
        if name in ('count', 'количество') and isinstance(obj, (list, Structure, Map)) and not values: return Decimal(len(obj))
        if isinstance(obj, Map):
            if name in ('insert', 'вставить') and 1 <= len(values) <= 2:
                obj[values[0]] = values[1] if len(values) == 2 else None; return None
            if name in ('get', 'получить') and len(values) == 1: return obj[values[0]]
            if name in ('clear', 'очистить') and not values: obj.clear(); return None
            if name in ('delete', 'удалить') and len(values) == 1: obj.delete(values[0]); return None
        if isinstance(obj, list):
            if name in ('find', 'найти') and len(values) == 1:
                value = values[0]
                for index, item in enumerate(obj):
                    self.host.tick(self.program.path, self.line)
                    equal = value_equal(item, value)
                    if equal: return Decimal(index)
                return None
            if name in ('insert', 'вставить', 'delete', 'удалить', 'set', 'установить'):
                from compatible_scenarios.shared.bsl.strings import integer, limit
                insert = name in ('insert', 'вставить')
                delete = name in ('delete', 'удалить')
                if not (1 <= len(values) <= 2 if insert else len(values) == (1 if delete else 2)):
                    self.fail('Incorrect Array method arguments.')
                index = integer(values[0])
                if index < 0 or (not insert and index >= len(obj)): self.fail('Array index is outside its bounds.')
                if insert:
                    limit(max(len(obj) + 1, index + 1), 10000, 'Array')
                    if index > len(obj): obj.extend([None] * (index - len(obj)))
                    obj.insert(index, values[1] if len(values) == 2 else None)
                elif delete: del obj[index]
                else: obj[index] = values[1]
                return None
            if name in ('add', 'добавить') and len(values) <= 1:
                if len(obj) >= 10000: self.fail('Array limit exceeded.')
                obj.append(values[0] if values else None); return None
            if name in ('ubound', 'вграница') and not values: return Decimal(len(obj)-1)
            if name in ('get', 'получить') and len(values) == 1: return obj[self.index(values[0], len(obj))]
            if name in ('clear', 'очистить') and not values: obj.clear(); return None
        if isinstance(obj, Structure):
            if name in ('clear', 'очистить') and not values: obj.clear(); return None
            if name in ('delete', 'удалить') and len(values) == 1:
                if not isinstance(values[0], str) or not values[0].isidentifier(): self.fail('Invalid Structure key.')
                obj.pop(obj.actual(values[0]), None); return None
            if name in ('insert', 'вставить') and 1 <= len(values) <= 2:
                if not isinstance(values[0], str) or not values[0].isidentifier(): self.fail('Invalid Structure key.')
                obj[values[0]] = values[1] if len(values) == 2 else None; return None
        return self.host.method(obj, name, values)

    def block(self, nodes):
        for node in nodes:
            self.line = node.line; self.host.tick(self.program.path, node.line)
            kind, args = node.kind, node.args
            if kind == 'set': self.assign(args[0], self.expr(args[1]))
            elif kind == 'expr': self.expr(args[0])
            elif kind == 'var':
                for name in args[0]: self.env.setdefault(name, None)
            elif kind == 'return': raise Flow('return', self.expr(args[0]))
            elif kind == 'raise':
                if args[0] is None:
                    if self.current_error is None: self.fail('Raise without an argument requires an active exception handler.')
                    raise self.current_error
                self.fail(string(self.expr(args[0])))
            elif kind in ('break', 'continue'): raise Flow(kind)
            elif kind == 'try':
                try:
                    self.block(args[0])
                except (Failure, KeyError, TypeError, ValueError, ArithmeticError, IndexError) as exc:
                    if isinstance(exc, Failure):
                        if exc.result['code'] in ('scenario_timeout', 'scenario_limit', 'connection_closed', 'unsupported_scenario'):
                            raise
                        error = exc
                    else:
                        error = Failure('scenario_failed', str(exc))
                    error.result.setdefault('path', str(self.program.path))
                    error.result.setdefault('line', self.line)
                    previous = self.current_error
                    self.current_error = error
                    try: self.block(args[1])
                    finally: self.current_error = previous
            elif kind == 'if':
                for cond, body in args[0]:
                    condition = boolean(self.expr(cond))
                    self.host.after_statement(self.program.path, cond.line)
                    if condition:
                        self.block(body); break
                else: self.block(args[1])
            elif kind in ('each', 'for', 'while'):
                if kind == 'each':
                    seq = self.expr(args[1])
                    if not isinstance(seq, COLLECTIONS): self.fail('For Each requires a supported collection.')
                    iterator = iter(seq if isinstance(seq, (list, ValueList)) else [Structure({'Ключ': k, 'Значение': v, 'Key': k, 'Value': v}) for k, v in seq.items()])
                elif kind == 'for':
                    start, end = self.expr(args[1]), self.expr(args[2])
                    if any(not isinstance(v, Decimal) or v != int(v) for v in (start, end)): self.fail('For bounds must be integers.')
                    iterator = (Decimal(i) for i in range(int(start), int(end)+1))
                while True:
                    self.host.tick(self.program.path, node.line)
                    if kind == 'while':
                        condition = boolean(self.expr(args[0]))
                        self.host.after_statement(self.program.path, node.line)
                        if not condition: break
                    else:
                        try: self.assign(Node('name', (args[0],), node.line), next(iterator))
                        except StopIteration: break
                        self.host.after_statement(self.program.path, node.line)
                    try: self.block(args[-1])
                    except Flow as flow:
                        if flow.kind == 'break': break
                        if flow.kind != 'continue': raise
            if kind in ('set', 'expr', 'if', 'each', 'for', 'while', 'try'):
                self.host.after_statement(self.program.path, node.line)

    def run(self, body=None):
        self.host.depth += 1
        try:
            if self.host.depth > 64: self.fail('Scenario call depth exceeded 64.')
            try: self.block(self.program.tree if body is None else body)
            except Flow as flow:
                if flow.kind == 'return': return flow.value
                raise
        except Failure as exc:
            exc.result.setdefault('path', str(self.program.path)); exc.result.setdefault('line', self.line)
            raise
        except (KeyError, TypeError, ValueError, ArithmeticError, IndexError) as exc:
            self.fail(str(exc))
        finally: self.host.depth -= 1
