"""Text comparison contract of Tester's addon JSON::compare (addon/json.cpp)."""
import json


def representation(value):
    if isinstance(value, str):
        quote = '"' if "'" in value and '"' not in value else "'"
        result = []
        for char in value:
            if char in ('\\', quote): result.append('\\' + char)
            elif char in ('\t', '\n', '\r'): result.append({'\t': '\\t', '\n': '\\n', '\r': '\\r'}[char])
            elif ord(char) < 32 or ord(char) == 127: result.append(f'\\x{ord(char):02x}')
            else: result.append(char)
        return quote + ''.join(result) + quote
    if isinstance(value, list): return '[' + ', '.join(representation(v) for v in value) + ']'
    if isinstance(value, dict): return '{' + ', '.join(representation(k) + ': ' + representation(v) for k, v in value.items()) + '}'
    if value is None: return 'None'
    if type(value) is bool: return 'True' if value else 'False'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def compare(before, after, tick=lambda: None):
    changes = []

    def same(a, b):
        tick()
        if isinstance(a, str) and isinstance(b, str): return a.strip() == b.strip()
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        if isinstance(a, dict) and isinstance(b, dict):
            return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
        return a == b

    def list_key(items):
        if not items or not all(isinstance(i, dict) for i in items): return None
        for key in ['ID'] + [k for k in items[0] if k.endswith('LineNumber')]:
            seen = []
            for item in items:
                if key not in item or any(same(item[key], old) for old in seen): break
                seen.append(item[key])
            else: return key
        return None

    def keyed(items, key):
        pairs = []
        for item in items:
            if not isinstance(item, dict) or key not in item: continue
            for i, (old, _) in enumerate(pairs):
                if same(old, item[key]):
                    pairs[i] = (old, item); break
            else: pairs.append((item[key], item))
        return pairs

    def find(pairs, key):
        return next((item for old, item in pairs if same(old, key)), None)

    def diff(a, b, path):
        tick()
        if isinstance(a, dict) and isinstance(b, dict):
            for key, value in a.items():
                sub = path + '.' + key if path else key
                if key in b: diff(value, b[key], sub)
                else: changes.append((sub, '-', value, None))
            for key in b.keys() - a.keys():
                changes.append((path + '.' + key if path else key, '+', None, b[key]))
        elif isinstance(a, list) and isinstance(b, list):
            key = list_key(a) or list_key(b)
            if key is None:
                for i in range(max(len(a), len(b))):
                    sub = f'{path}[{i}]'
                    if i >= len(b): changes.append((sub, '-', a[i], None))
                    elif i >= len(a): changes.append((sub, '+', None, b[i]))
                    else: diff(a[i], b[i], sub)
                return
            old, new = keyed(a, key), keyed(b, key)
            for ident, item in old:
                sub = path + '[' + (ident if isinstance(ident, str) else representation(ident)) + ']'
                current = find(new, ident)
                if current is None: changes.append((sub, '-', item, None))
                else: diff(item, current, sub)
            for ident, item in new:
                if find(old, ident) is None:
                    changes.append((path + '[' + (ident if isinstance(ident, str) else representation(ident)) + ']', '+', None, item))
            order_a = [i[key] for i in a if isinstance(i, dict) and key in i and find(new, i[key]) is not None]
            order_b = [i[key] for i in b if isinstance(i, dict) and key in i and find(old, i[key]) is not None]
            if not same(order_a, order_b): changes.append((path, '*', order_a, order_b))
        elif not same(a, b): changes.append((path, '~', a, b))

    if (isinstance(before, list) and isinstance(after, list) and len(before) == len(after) == 1
            and isinstance(before[0], dict) and isinstance(after[0], dict)
            and 'ID' in before[0] and 'ID' in after[0] and same(before[0]['ID'], after[0]['ID'])):
        before, after = before[0], after[0]
    diff(before, after, '')
    if not changes: return 'No changes.'
    lines, size = [], 0
    def encoded(value): return json.dumps(value, ensure_ascii=False, allow_nan=False)
    for path, op, old, new in sorted(changes, key=lambda c: c[0]):
        tick()
        if op == '~': line = f'~ {path}: {encoded(old)} -> {encoded(new)}'
        elif op == '*': line = f'* {path}: order {representation(old)} -> {representation(new)}'
        else: line = f'{op} {path}: {encoded(new if op == "+" else old)}'
        size += len(line) + 1
        if size > 1000000:
            from compatible_scenarios.shared.bsl.language import Failure
            raise Failure('scenario_limit', 'The window difference exceeds 1000000 characters.')
        lines.append(line)
    return '\n'.join(lines)
