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


def present(payload, action, registry):
    import _collection
    if not isinstance(payload, dict):
        return payload
    pairs = {}
    objects = []
    slot = OBJECT_SLOTS.get(action)
    value = payload.get(slot) if slot else None
    if isinstance(value, list):
        objects = [v for v in value if isinstance(v, dict) and _collection.is_object_key(v.get('key'))]
    elif isinstance(value, dict) and _collection.is_object_key(value.get('key')):
        objects = [value]
    if action == 'set_row_values':
        objects.extend(v['page'] for v in payload.get('results', [])
                       if isinstance(v, dict) and isinstance(v.get('page'), dict)
                       and _collection.is_object_key(v['page'].get('key')))
    snapshot_slots = ('changes', 'observed', 'added', 'errors') if action in ('create_snapshot', 'compare_snapshot') else ()
    for field in snapshot_slots:
        objects.extend(v for v in payload.get(field, []) if isinstance(v, dict)
                       and _collection.is_object_key(v.get('key')))
    if action == 'get_active_window' and _collection.is_object_key(payload.get('key')):
        objects.append(payload)
    for obj in objects:
        pairs[obj['key']] = obj.get('handle') or registry.get(obj['key'])
    # Address echoes at the top level have defined semantics, unlike arbitrary UI data.
    echoes = {}
    echo_fields = ['target', 'parent', 'window']
    if action == 'table_add_row':
        echo_fields.append('table')
    if action == 'close_window' and not payload.get('native'):
        echo_fields.append('closed')
    if isinstance(payload.get('observed'), dict) and payload['observed'].get('kind') == 'active_window':
        echo_fields.extend(('value_before', 'value_after'))
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
    refs = registry.publish(pairs)
    def node(obj):
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
    for field, key in echoes.items():
        out[field] = refs[key]
    if argument_targets:
        converted = dict(args)
        for field, key in argument_targets.items():
            converted.pop(field)
            converted['root_ref' if field == 'root_key' else 'ref'] = refs[key]
        converted.pop('handle', None)
        out['suggested_call'] = {**suggestion, 'arguments': converted}
    return out
