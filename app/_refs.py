"""Bounded, connection-owned UI references. No UI data or scenario contents live here."""
from collections import OrderedDict
import os
import uuid


def _limit():
    try:
        value = int(os.environ.get('TC1C_REF_LIMIT', '100000'))
        if value > 0:
            return value
    except ValueError:
        pass
    raise ValueError('TC1C_REF_LIMIT must be a positive integer')


LIMIT = _limit()


class RefError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Registry:
    def __init__(self, limit=LIMIT):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('reference limit must be a positive integer')
        self.limit = limit
        self._entries = OrderedDict()  # key -> (ref, handle), also replaces the handle cache
        self._keys = {}                # ref -> same key string
        self._namespace = uuid.uuid4().hex[:12]
        self._counter = 0

    def __len__(self):
        return len(self._entries)

    def get(self, key, default=None):
        entry = self._entries.get(key)
        if entry is None:
            return default
        self._entries.move_to_end(key)
        return entry[1]

    def __setitem__(self, key, handle):
        self.remember(key, handle)

    def discard(self, key):
        entry = self._entries.pop(key, None)
        if entry:
            del self._keys[entry[0]]

    def remember(self, key, handle=None):
        entry = self._entries.get(key)
        if entry and (handle is None or entry[1] is None or entry[1] == handle):
            self._entries[key] = (entry[0], handle if handle is not None else entry[1])
            self._entries.move_to_end(key)
            return entry[0]
        self.discard(key)
        while len(self) >= self.limit:
            _, old = self._entries.popitem(last=False)
            del self._keys[old[0]]
        self._counter += 1
        ref = 'e' + self._namespace + '-' + format(self._counter, 'x')
        self._entries[key] = (ref, handle)
        self._keys[ref] = key
        return ref

    def publish(self, pairs):
        """Keep EVERY reference in one response valid, or fail before changing the registry."""
        if len(pairs) > self.limit:
            raise RefError('ref_limit_exceeded',
                           'Too many elements for one response. Narrow the search by name, class or root_ref, '
                           'or increase TC1C_REF_LIMIT.')
        # Evict only entries outside this response, including when existing entries are LRU.
        for key in pairs:
            if key in self._entries:
                self._entries.move_to_end(key)
        excess = len(self) + sum(k not in self._entries for k in pairs) - self.limit
        while excess > 0:
            key = next(iter(self._entries))
            self.discard(key)
            excess -= 1
        return {key: self.remember(key, handle) for key, handle in pairs.items()}

    def resolve(self, ref):
        key = self._keys.get(ref)
        if key is None:
            raise RefError('stale_ref', 'Unknown or expired element reference. Find the element again.')
        self._entries.move_to_end(key)
        return key, self._entries[key][1]


def for_client(client):
    if client is None:
        raise RefError('not_connected', 'Connect to the test client, then find the element again.')
    registry = getattr(client, '_object_handles', None)
    if not isinstance(registry, Registry):
        previous = registry or {}
        registry = Registry()
        for key, handle in previous.items():
            registry.remember(key, handle)
        client._object_handles = registry
    return registry


# Only these response slots contain UI objects. Never rewrite table values, document
# cells, text, HTML, attachments, criteria or recorded scenarios, even if they look like keys.
OBJECT_SLOTS = {
    'click': 'window',
    'get_child_objects': 'children', 'find_objects': 'objects', 'find_object': 'object',
    'wait_for_object_displayed': 'object',
    'get_context_menu': 'menu', 'get_parent': 'parent', 'get_command_bar': 'commandbar',
    'get_command_interface': 'commands', 'get_current_page': 'page',
    'find_default_button': 'button', 'get_current_item': 'item', 'get_current_element': 'item',
    'get_linked_window': 'window', 'get_current_area_field': 'field',
    'start_choosing': 'window', 'execute_command': 'window',
    'set_cell_text': 'current_item',
    'set_row_values': 'page',
}


_CONTEXT_LISTS = ('elements', 'tables', 'documents', 'pages', 'errors', 'table_rows')
_CONTEXT_RELATIONS = ('parent_key', 'current_column_key', 'current_page_key', 'field_key')


def context_pairs(payload, registry):
    """Only declared context addresses; field values and choice text are never addresses."""
    import _collection
    nodes = [payload.get('form'), payload.get('input')]
    nodes.extend(v for field in _CONTEXT_LISTS for v in payload.get(field, []))
    pairs = {v['key']: v.get('handle') or registry.get(v['key']) for v in nodes
             if isinstance(v, dict) and _collection.is_object_key(v.get('key'))}
    for obj in [payload, *nodes, *payload.get('choices', [])]:
        if not isinstance(obj, dict):
            continue
        for field in ('current_key', *_CONTEXT_RELATIONS):
            key = obj.get(field)
            if _collection.is_object_key(key) and key not in pairs:
                pairs[key] = registry.get(key)
    return pairs


def _present_context(payload, registry):
    refs = registry.publish(context_pairs(payload, registry))
    def node(obj):
        if not isinstance(obj, dict):
            return obj
        out = {}
        for field, value in obj.items():
            if field == 'handle':
                continue
            if field == 'key' or field in ('current_key', *_CONTEXT_RELATIONS):
                name = 'ref' if field == 'key' else field[:-3] + 'ref'
                out[name] = refs.get(value) if value is not None else None
            else:
                out[field] = value
        return out
    out = dict(payload)
    if 'current_key' in out:
        out['current_ref'] = refs.get(out.pop('current_key'))
    for field in ('form', 'input'):
        if field in out:
            out[field] = node(out[field])
    for field in (*_CONTEXT_LISTS, 'choices'):
        if field in out:
            out[field] = [node(v) for v in out[field]]
    return out


def _failure_objects(context):
    nodes = [context.get('target'), context.get('focused_element'), context.get('table')]
    nodes.extend(context.get('ancestors', []))
    table = context.get('table')
    if isinstance(table, dict):
        nodes.append(table.get('current_column'))
    unique = {}
    for obj in nodes:
        if isinstance(obj, dict) and obj.get('key'):
            unique[id(obj)] = obj
    return list(unique.values())


def _envelopes(payload, action):
    """Declared result containers only; never inspect arbitrary cell text or UI values."""
    yield payload
    if action in ('set_fields', 'set_row_values', 'add_rows'):
        for row in payload.get('results', []):
            if isinstance(row, dict):
                yield row
                if action == 'add_rows' and isinstance(row.get('fill'), dict):
                    yield from _envelopes(row['fill'], 'set_row_values')


def present(payload, action, registry):
    import _collection
    if not isinstance(payload, dict):
        return payload
    if action == 'get_context':
        return _present_context(payload, registry)
    pairs = {}
    objects = []
    slot = OBJECT_SLOTS.get(action)
    value = payload.get(slot) if slot else None
    if isinstance(value, list):
        objects = [v for v in value if isinstance(v, dict) and _collection.is_object_key(v.get('key'))]
    elif isinstance(value, dict) and _collection.is_object_key(value.get('key')):
        objects = [value]
    if action == 'set_row_values' and isinstance(value, dict):
        # The page is failure diagnostics; the table target takes priority.
        objects = []
    snapshot_slots = ('changes', 'observed', 'added', 'errors', 'tables', 'table_changes') if action in ('create_snapshot', 'compare_snapshot') else ()
    for field in snapshot_slots:
        objects.extend(v for v in payload.get(field, []) if isinstance(v, dict)
                       and _collection.is_object_key(v.get('key')))
    if action == 'get_active_window' and _collection.is_object_key(payload.get('key')):
        objects.append(payload)
    for obj in objects:
        pairs[obj['key']] = obj.get('handle') or registry.get(obj['key'])
    envelopes = list(_envelopes(payload, action))
    extra_pages = [v['page'] for v in envelopes if isinstance(v.get('page'), dict) and v['page'].get('key')]
    # Address echoes at the top level have defined semantics, unlike arbitrary UI data.
    echoes = {}
    echo_fields = ['target', 'parent', 'window']
    if action == 'table_add_row':
        echo_fields.append('table')
    if action == 'close_window' and not payload.get('native'):
        echo_fields.append('closed')
    for field in echo_fields:
        key = payload.get(field)
        if _collection.is_object_key(key):
            pairs.setdefault(key, registry.get(key))
            echoes[field] = key
    suggestion = payload.get('suggested_call') if action in ('set_cell_text', 'get_cell_text') else None
    args = suggestion.get('arguments') if isinstance(suggestion, dict) else None
    argument_targets = {}
    if isinstance(args, dict):
        for field in ('key', 'root_key'):
            key = args.get(field)
            if _collection.is_object_key(key):
                pairs.setdefault(key, args.get('handle') or registry.get(key))
                argument_targets[field] = key
    missing_cell_refs = set()
    if action == 'set_cell_text' and len(pairs) > registry.limit:
        # The edit has already happened. Keep its result and target even when
        # the current editor or a suggested call cannot receive another reference.
        optional = dict.fromkeys([o['key'] for o in objects] + list(argument_targets.values()))
        for key in reversed(optional):
            if len(pairs) <= registry.limit:
                break
            if key not in echoes.values():
                pairs.pop(key, None)
                missing_cell_refs.add(key)
    if action in ('click', 'start_choosing', 'execute_command') and len(pairs) > registry.limit and isinstance(value, dict):
        # The action has already happened. Losing an optional window ref must not
        # turn its response into a failed action that the caller might repeat.
        window_key = value.get('key')
        if window_key not in echoes.values():
            pairs.pop(window_key, None)
            value = {**value, 'key': None, 'reference_status': 'unavailable',
                     'code': 'ref_limit_exceeded',
                     'message': 'The window reference exceeds TC1C_REF_LIMIT. Find the window separately.'}
    missing_page_refs = set()
    for obj in extra_pages:
        key = obj['key']
        if key in pairs or len(pairs) < registry.limit:
            pairs[key] = obj.get('handle') or registry.get(key)
        else:
            missing_page_refs.add(key)
    # Window observations are optional readback of an already attempted action.
    # Keep the target usable; prefer the current window when only one more ref fits.
    missing_window_refs = []
    if isinstance(payload.get('observed'), dict) and payload['observed'].get('kind') == 'active_window':
        for field in ('value_after', 'value_before'):
            key = payload.get(field)
            if not _collection.is_object_key(key):
                continue
            if key in pairs or len(pairs) < registry.limit:
                pairs.setdefault(key, registry.get(key))
                echoes[field] = key
            else:
                missing_window_refs.append(field)
    # Optional diagnostics must not turn an already attempted mutation into ref_limit_exceeded.
    for envelope in envelopes:
        for obj in _failure_objects(envelope.get('failure_context', {})):
            if obj['key'] in pairs or len(pairs) < registry.limit:
                pairs[obj['key']] = obj.get('handle') or registry.get(obj['key'])
    refs = registry.publish(pairs)
    def node(obj):
        if isinstance(obj, dict) and obj.get('key') in missing_page_refs:
            return {'ref': None, **{k: v for k, v in obj.items() if k not in ('key', 'handle')},
                    'reference_status': 'unavailable', 'code': 'ref_limit_exceeded',
                    'message': 'The page reference exceeds TC1C_REF_LIMIT. Find the page separately.'}
        if isinstance(obj, dict) and obj.get('key') in missing_cell_refs:
            return {'ref': None, **{k: v for k, v in obj.items() if k not in ('key', 'handle')},
                    'reference_status': 'unavailable', 'code': 'ref_limit_exceeded',
                    'message': 'The editor reference exceeds TC1C_REF_LIMIT. Find the column by name to obtain its reference.'}
        if not isinstance(obj, dict) or obj.get('key') not in refs:
            return obj
        return {'ref': refs[obj['key']], **{k: v for k, v in obj.items() if k not in ('key', 'handle')}}
    out = node(payload) if action == 'get_active_window' and objects else dict(payload)
    if action == 'get_active_window' and 'key' in out:
        out['ref'] = out.pop('key')  # unavailable window: null stays null
    if slot and slot in payload:
        out[slot] = [node(v) for v in value] if isinstance(value, list) else node(value)
        if slot == 'window' and isinstance(value, dict) and 'key' in value and value['key'] is None:
            out[slot] = {'ref': None, **{k: v for k, v in value.items() if k not in ('key', 'handle')}}
    for field in snapshot_slots:
        if field in payload:
            out[field] = [node(v) for v in payload[field]]
    if action == 'set_row_values' and 'results' in payload:
        out['results'] = [{**v, 'page': node(v['page'])} if isinstance(v, dict) and 'page' in v else v
                          for v in payload['results']]
    if extra_pages or any('failure_context' in v for v in envelopes):
        import copy
        out = copy.deepcopy(out)
    for envelope in _envelopes(out, action):
        if isinstance(envelope.get('page'), dict):
            envelope['page'] = node(envelope['page'])
        for obj in _failure_objects(envelope.get('failure_context', {})):
            key = obj.pop('key')
            obj.pop('handle', None)
            obj['ref'] = refs.get(key)
            if obj['ref'] is None:
                obj['reference_status'] = 'unavailable'
    for field, key in echoes.items():
        out[field] = refs[key]
    if missing_window_refs:
        for field in missing_window_refs:
            out[field] = None
        out['observed'] = {**payload['observed'], 'reference_status': 'unavailable',
                           'code': 'ref_limit_exceeded', 'missing_refs': missing_window_refs,
                           'message': 'Window references exceed TC1C_REF_LIMIT. Find the window separately.'}
    if any(key in missing_cell_refs for key in argument_targets.values()):
        out['suggested_call'] = None
    elif argument_targets:
        converted = dict(args)
        for field, key in argument_targets.items():
            converted.pop(field)
            converted['root_ref' if field == 'root_key' else 'ref'] = refs[key]
        converted.pop('handle', None)
        out['suggested_call'] = {**suggestion, 'arguments': converted}
    return out
