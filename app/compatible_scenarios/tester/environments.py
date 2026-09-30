"""Environment records owned by a testing session, separate from application data."""
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from _connections import database_key

from compatible_scenarios.shared.bsl.language import Failure, Structure, Map, NULL
from compatible_scenarios.shared.bsl.language import FixedArray, FixedStructure, FixedMap, ValueList, ValueListItem


KINDS = {list:'array', Structure:'structure', Map:'map', FixedArray:'fixedarray',
         FixedStructure:'fixedstructure', FixedMap:'fixedmap', ValueList:'valuelist', ValueListItem:'valuelistitem'}


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.records = {}


_creation_lock = threading.Lock()


def path_argument(value):
    if value is None: return None
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip() or '\x00' in str(value):
        raise Failure('invalid_environments_path', 'environments_path must be a SQLite file path.')
    path = Path(value).expanduser().resolve()
    if path.is_dir(): raise Failure('invalid_environments_path', 'environments_path points to a directory.')
    return path


def encode(value, host):
    nodes, memo = [], {}
    def visit(item, depth=0):
        host.tick(**host.location)
        if depth > 100 or len(nodes) > 10000:
            raise Failure('unsupported_environment_data', 'Environment data exceeds its nesting or collection limit.')
        if item is None: return ['undefined']
        if item is NULL: return ['null']
        if type(item) is str: return ['string', item]
        if type(item) is bool: return ['boolean', item]
        if type(item) is Decimal:
            if not item.is_finite(): raise Failure('unsupported_environment_data', 'Environment numbers must be finite.')
            return ['number', str(item)]
        if type(item) is datetime: return ['date', item.isoformat()]
        if type(item) not in KINDS:
            raise Failure('unsupported_environment_data', 'Environment data must contain supported BSL values.')
        if id(item) in memo: return ['ref', memo[id(item)]]
        index = memo[id(item)] = len(nodes)
        node = [KINDS[type(item)], []]
        nodes.append(node)
        if isinstance(item, ValueListItem):
            node[1] = [visit(v, depth+1) for v in (item.value, item.presentation, item.check, Decimal(item.identifier))]
        elif isinstance(item, ValueList):
            node[1] = [visit(Decimal(item.next_id), depth+1)] + [visit(v, depth+1) for v in item]
        elif isinstance(item, list): node[1] = [visit(v, depth+1) for v in item]
        else: node[1] = [[visit(k, depth+1), visit(v, depth+1)] for k,v in item.items()]
        return ['ref', index]
    root = visit(value)
    payload = json.dumps(dict(version=2, root=root, nodes=nodes), ensure_ascii=True, separators=(',', ':'))
    if len(payload) > 1048576:
        raise Failure('unsupported_environment_data', 'Environment data exceeds 1 MiB.')
    return payload


def decode(payload, host):
    try:
        if not isinstance(payload, str) or len(payload) > 1048576: raise ValueError()
        data = json.loads(payload)
        if data['version'] not in (1, 2) or not isinstance(data['nodes'], list) or len(data['nodes']) > 10000: raise ValueError()
        nodes = data['nodes']
        values = []
        classes = {kind: cls for cls, kind in KINDS.items()}
        for kind, items in nodes:
            if not isinstance(items, list) or kind not in classes or len(items) > 10000: raise ValueError()
            values.append(classes[kind]())
        def read(item):
            host.tick(**host.location)
            if item == ['undefined']: return None
            if item == ['null']: return NULL
            kind, value = item
            if kind == 'string' and type(value) is str: return value
            if kind == 'boolean' and type(value) is bool: return value
            if kind == 'number' and type(value) is str:
                number = Decimal(value)
                if number.is_finite(): return number
            if kind == 'date' and type(value) is str:
                from compatible_scenarios.shared.bsl.dates import checked
                return checked(datetime.fromisoformat(value))
            if kind == 'ref' and type(value) is int and 0 <= value < len(values): return values[value]
            raise ValueError()
        for (kind, items), target in zip(nodes, values):
            if isinstance(target, list): target.extend(read(v) for v in items)
            elif isinstance(target, ValueList):
                counter = read(items[0]) if data['version'] == 2 else Decimal(len(items))
                if type(counter) is not Decimal or counter != int(counter) or counter < 0: raise ValueError()
                target.next_id = int(counter)
                target.items = [read(v) for v in (items[1:] if data['version'] == 2 else items)]
                if any(not isinstance(v, ValueListItem) for v in target): raise ValueError()
            elif isinstance(target, ValueListItem):
                if len(items) != (3 if data['version'] == 1 else 4): raise ValueError()
                target.value, target.presentation, target.check = map(read, items[:3])
                if data['version'] == 2:
                    identifier = read(items[3])
                    if type(identifier) is not Decimal or identifier != int(identifier) or identifier < 0: raise ValueError()
                    target.identifier = int(identifier)
                if type(target.presentation) is not str or type(target.check) is not bool: raise ValueError()
            else:
                for key, value in items:
                    key = read(key)
                    if isinstance(target, Structure) and (type(key) is not str or not key.isidentifier()): raise ValueError()
                    target[key] = read(value)
        for target in values:
            if isinstance(target, ValueList):
                if data['version'] == 1:
                    for i, item in enumerate(target): item.identifier = i
                ids = [item.identifier for item in target]
                if len(set(ids)) != len(ids): raise ValueError()
                target.next_id = max(target.next_id, max(ids, default=-1)+1)
        return read(data['root'])
    except Failure as exc:
        if exc.result.get('code') in ('scenario_timeout','scenario_limit','connection_closed'): raise
        raise Failure('environment_store_invalid', 'Stored environment data is invalid.') from exc
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError, RecursionError) as exc:
        raise Failure('environment_store_invalid', 'Stored environment data is invalid.') from exc


def persistent(host, name, identifier, arguments, path):
    record = host.connections.current or host.connections.root
    base = getattr(record[1], 'environment_base', None) if record else None
    if not base:
        raise Failure('environment_base_unknown', 'Persistent environments require a client launched by Testpilot; the connected database is unknown.')
    # A String(10) cut can end on a UTF-16 surrogate; keep such IDs representable in SQLite.
    key = (base, host.repository.application_name, json.dumps(identifier, ensure_ascii=True))
    writing = name == 'registerenvironment'
    payload = encode(arguments[1] if len(arguments) > 1 else None, host) if writing else None
    if not writing and not path.exists(): return False if name == 'environmentexists' else None
    connection = None
    try:
        remaining = host.deadline-time.monotonic()
        if remaining <= 0: raise Failure('scenario_timeout', 'Scenario deadline expired.')
        if writing: path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path if writing else path.as_uri()+'?mode=ro', uri=not writing,
                                     timeout=min(remaining, 5), isolation_level=None)
        connection.execute('BEGIN IMMEDIATE' if writing else 'BEGIN')
        application_id = connection.execute('PRAGMA application_id').fetchone()[0]
        if application_id != 0x54434531:
            if not writing and application_id == 0 and not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                return False if name == 'environmentexists' else None
            if not writing or application_id or connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                raise Failure('environment_store_invalid', 'The file is not a Testpilot environment store.')
            connection.execute('PRAGMA application_id=1413694769')
            connection.execute('CREATE TABLE environments (base TEXT NOT NULL, application TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(base,application,id))')
        if writing:
            connection.execute('INSERT INTO environments VALUES (?,?,?,?) ON CONFLICT(base,application,id) DO UPDATE SET data=excluded.data', (*key, payload))
            if time.monotonic() >= host.deadline: raise Failure('scenario_timeout', 'Scenario deadline expired.')
            connection.execute('PRAGMA busy_timeout='+str(max(0,int(min(5,host.deadline-time.monotonic())*1000))))
            connection.execute('COMMIT')
            return None
        row = connection.execute('SELECT data FROM environments WHERE base=? AND application=? AND id=?', key).fetchone()
        if time.monotonic() >= host.deadline: raise Failure('scenario_timeout', 'Scenario deadline expired.')
        if name == 'environmentexists': return row is not None
        return decode(row[0], host) if row else None
    except (OSError, sqlite3.Error) as exc:
        code = 'scenario_timeout' if time.monotonic() >= host.deadline else 'environment_store_unavailable'
        raise Failure(code, 'Cannot access environment store: '+str(exc)) from exc
    finally:
        if connection is not None: connection.close()


def store(host):
    owner = host.connections.root[0] if host.connections.root else host.R
    with _creation_lock:
        if not hasattr(owner, '_tester_environments'):
            owner._tester_environments = Store()
        return owner._tester_environments


def clone(value, host, memo=None):
    host.tick(**host.location)
    if value is None or value is NULL or type(value) in (str, bool, Decimal, datetime): return value
    memo = {} if memo is None else memo
    if id(value) in memo: return memo[id(value)]
    if type(value) not in KINDS:
        raise Failure('unsupported_environment_data', 'Environment data must contain supported BSL values.')
    result = type(value)()
    memo[id(value)] = result
    if isinstance(value, ValueListItem):
        result.value = clone(value.value, host, memo)
        result.presentation, result.check = value.presentation, value.check
        result.identifier = value.identifier
    elif isinstance(value, ValueList):
        result.items = [clone(item, host, memo) for item in value]
        result.next_id = value.next_id
    elif isinstance(value, list):
        result.extend(clone(item, host, memo) for item in value)
    else:
        for key, item in value.items(): result[key] = clone(item, host, memo)
    return result


def call(host, name, arguments):
    identifier = arguments[0]
    if not isinstance(identifier, str):
        raise Failure('invalid_environment_id', 'The environment ID must be a string.')
    # Tester stores ID in a String(10) register dimension; comparisons ignore case.
    from compatible_scenarios.shared.bsl.strings import units, from_units
    identifier = from_units(units(identifier)[:10]).rstrip().casefold()
    path = getattr(host.repository, 'environments_path', None)
    if path is not None: return persistent(host, name, identifier, arguments, path)
    key = (host.repository.application_name, identifier)
    storage = store(host)
    with storage.lock:
        if name == 'environmentexists': return key in storage.records
        if name == 'environmentdata': return clone(storage.records.get(key), host)
        value = clone(arguments[1] if len(arguments) > 1 else None, host)
        storage.records[key] = value
    return None

