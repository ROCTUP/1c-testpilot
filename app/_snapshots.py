"""Bounded form observations. Stored baselines never own clients or live element refs."""
from collections import OrderedDict
from datetime import datetime, timezone
import json
import os
import sys
import threading
import time
import uuid
import _snapshot_tables as table_rows


PROPERTIES = ('visible', 'enabled', 'readonly', 'presentation')
FLAGS = frozenset(PROPERTIES[:3])
FLAG_CLASSES = {
    'visible': {'Group', 'Decoration', 'Button', 'Table', 'Additional', 'EditField'},
    'enabled': {'Group', 'Decoration', 'Button', 'Table', 'Additional', 'EditField'},
    'readonly': {'Group', 'Decoration', 'Table', 'Additional', 'EditField'},
}


class Failure(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def positive_env(name, default):
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        raise ValueError(name + ' must be a positive integer') from None
    if value < 1:
        raise ValueError(name + ' must be a positive integer')
    return value


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def deep_size(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(deep_size(k, seen) + deep_size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple)):
        size += sum(deep_size(v, seen) for v in value)
    return size


class Store:
    def __init__(self, limit=None, memory_bytes=None):
        self.limit = positive_env('TC1C_SNAPSHOT_LIMIT', '100') if limit is None else limit
        self.memory_bytes = (positive_env('TC1C_SNAPSHOT_MEMORY_MB', '128') * 1024**2
                             if memory_bytes is None else memory_bytes)
        if type(self.limit) is not int or type(self.memory_bytes) is not int or min(self.limit, self.memory_bytes) < 1:
            raise ValueError('Snapshot limits must be positive integers')
        self.entries = OrderedDict()
        self.size = 0
        self.lock = threading.RLock()

    def info(self, entry):
        return {k: entry[k] for k in ('snapshot_id', 'connection_id', 'form_title',
                'created_at', 'last_used_at', 'element_count', 'size_bytes', 'include_tables', 'max_rows')}

    def stats(self):
        with self.lock:
            return dict(count=len(self.entries), limit=self.limit,
                        size_bytes=self.size, limit_bytes=self.memory_bytes)

    def add(self, owner, generation, connection_id, form, capture):
        # Immutable UTF-8 payload avoids retaining mutable live objects and makes storage
        # accounting independent of shared strings in the discovery registry.
        payload = json.dumps(capture, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        stamp = now()
        entry = dict(snapshot_id='s' + uuid.uuid4().hex, owner=owner, generation=generation,
                     connection_id=connection_id, key=form['key'], handle=form.get('handle'),
                     form_title=form.get('title'), created_at=stamp, last_used_at=stamp,
                     element_count=len(capture['elements']), payload=payload, size_bytes=0)
        entry.update(capture.get('options', table_rows.options(False, 500)))
        # Separate equal timestamps for conservative ownership accounting; include map overhead.
        entry['last_used_at'] = stamp.encode().decode()
        entry['size_bytes'] = deep_size(entry) + 256
        with self.lock:
            if entry['size_bytes'] > self.memory_bytes:
                raise Failure('snapshot_too_large', 'The snapshot exceeds TC1C_SNAPSHOT_MEMORY_MB; no snapshot was stored.')
            while self.entries and (len(self.entries) >= self.limit or self.size + entry['size_bytes'] > self.memory_bytes):
                _, old = self.entries.popitem(last=False)
                self.size -= old['size_bytes']
            self.entries[entry['snapshot_id']] = entry
            self.size += entry['size_bytes']
            return self.info(entry)

    def get(self, snapshot_id, owner, touch=False):
        with self.lock:
            entry = self.entries.get(snapshot_id)
            if entry is None:
                raise Failure('snapshot_unavailable', 'The snapshot is unknown, deleted or evicted. Create a new snapshot.')
            if entry['owner'] != owner:
                raise Failure('snapshot_connection_mismatch', 'The snapshot belongs to another connection.')
            if touch:
                entry['last_used_at'] = now()
                self.entries.move_to_end(snapshot_id)
            return dict(entry)

    def list(self, owner, key=None, generation=None):
        with self.lock:
            return [self.info(e) for e in reversed(self.entries.values()) if e['owner'] == owner
                    and (key is None or (e['key'] == key and e['generation'] == generation))]

    def delete(self, snapshot_id, owner):
        with self.lock:
            entry = self.get(snapshot_id, owner)
            del self.entries[snapshot_id]
            self.size -= entry['size_bytes']


def identity(S):
    owner = S._state.get('_snapshot_owner')
    if owner is None:
        owner = uuid.uuid4().hex
        S._state['_snapshot_owner'] = owner
    c = S._state.get('client')
    generation = None
    if c is not None:
        generation = getattr(c, '_snapshot_generation', None)
        if generation is None:
            generation = c._snapshot_generation = uuid.uuid4().hex
    return owner, generation, S._state.get('connection_id')


def form_object(S, c, key=None):
    if key is None:
        window = S._window(c)
        if not window.get('key'):
            raise Failure('active_window_unavailable', 'The active form cannot be identified. Pass a form ref.')
        candidates = [o for o in S._search_objects(c, window['key']) if o.get('class') == 'ManagedForm']
        if len(candidates) != 1:
            raise Failure('form_required', 'Pass the ref of one ManagedForm.')
        key = candidates[0]['key']
    if S._key_class(key) != 'ManagedForm':
        raise Failure('unsupported_receiver', 'Pass a ManagedForm ref.')
    try:
        obj = S._ref_live_object(c, key)
    except S.tc1c.OperationError as exc:
        if exc.result().get('code') != 'target_unavailable':
            raise
        obj = None
    if obj is None:
        raise Failure('snapshot_form_unavailable', 'The form is no longer available. Find the form and create a new snapshot.')
    return obj


def capture(S, c, form, inspect=None, include_tables=False, max_rows=500):
    start = time.perf_counter()
    track = getattr(c, '_track', None)
    c._track = None
    try:
        prepared = None
        if include_tables:
            objects = S._search_objects(c, form['key'])
            native = (S._state.get('rec_active') and S._state.get('rec_mode') == 'native' and track is not None)
            def recording(middle, message):
                try:
                    r = c.send_cmd(S.G.UILOG, None, kind='read818', middle=middle)
                    if not r.get('ok'):
                        raise Failure('recording_incomplete', message)
                except Exception:
                    S._state.setdefault('rec_native_errors', []).append('snapshot_tables')
                    raise
            if native:
                recording(b'\xe2\x81', 'Recording could not be paused for table preparation.')
            try:
                prepared = table_rows.prepare(S, c, form, objects)
            finally:
                if native:
                    recording(b'\xe3\x81', 'Recording could not be resumed after table preparation.')
        return _capture(S, c, form, start, inspect, prepared, max_rows)
    finally:
        c._track = track


def _capture(S, c, form, start, inspect=None, prepared=None, max_rows=500):
    errors = []

    def read(obj, prop):
        fn = S.tc_get_data_presentation if prop == 'presentation' else getattr(S, 'tc_is_' + prop)
        result = fn(obj['key'], obj.get('handle'))
        value = result.get(prop)
        valid = type(value) is bool if prop in FLAGS else isinstance(value, str)
        if result.get('ok') and valid:
            return value, 'read'
        errors.append(dict(key=obj['key'], handle=obj.get('handle'), title=obj.get('title'), property=prop,
                           code=result.get('code', 'value_unavailable'),
                           error=result.get('error') or result.get('message') or 'The property could not be read.'))
        return ('unknown' if prop in FLAGS else None), 'error'

    def guard():
        modified = S.tc_current_modified(form['key'], form.get('handle'))
        focus = S.tc_get_current_element(form['key'], form.get('handle'))
        if (not modified.get('ok') or type(modified.get('modified')) is not bool
                or not focus.get('ok') or focus.get('current_status') == 'unavailable'):
            raise Failure('snapshot_state_unavailable', 'The form state could not be checked. Retry after the form finishes updating.')
        return modified['modified'], [o['key'] for o in focus.get('item', [])]

    before = guard()
    objects = S._search_objects(c, form['key'])
    if prepared is not None:
        tables, preparation_errors = prepared
        current_tables = {o['key']: o.get('handle') for o in objects if o.get('class') == 'Table'}
        if current_tables != {k: t.get('handle') for k, t in tables.items()}:
            raise Failure('snapshot_unstable', 'The form tables changed during preparation. Retry.')
        errors.extend(preparation_errors)
        errors.extend(table_rows.read(S, tables, max_rows))
    data, inherited = {}, {}
    # Parents precede children, independent of the order of the native result.
    def depth(obj):
        key, n = obj['key'], 0
        while key:
            key, n = S._collection_parent(key), n + 1
        return n
    for obj in sorted(objects, key=depth):
        key, cls = obj['key'], obj.get('class')
        parent = S._collection_parent(key)
        hidden, disabled = inherited.get(parent, (False, False))
        # Keep the baseline's standard columns even when discovery omits a value,
        # and preserve additional attributes such as form_name for the overview.
        item = {k: obj.get(k) for k in ('key', 'handle', 'name', 'title', 'class', 'type')}
        item.update(obj)
        status = {}
        for prop in PROPERTIES[:3]:
            item[prop], status[prop] = '-', 'skipped'
        item['presentation'], status['presentation'] = None, 'skipped'
        if not hidden and cls in FLAG_CLASSES['visible']:
            item['visible'], status['visible'] = read(obj, 'visible')
        hidden = hidden or item['visible'] is False
        if not hidden and not disabled and cls in FLAG_CLASSES['enabled']:
            item['enabled'], status['enabled'] = read(obj, 'enabled')
        disabled = disabled or item['enabled'] is False
        if not hidden and not disabled and cls in FLAG_CLASSES['readonly']:
            item['readonly'], status['readonly'] = read(obj, 'readonly')
        if (not hidden and cls == 'EditField' and obj.get('type') in S._batches.SCALAR_KINDS
                and not S._table_owner(key)):
            item['presentation'], status['presentation'] = read(obj, 'presentation')
        item['_status'] = status
        data[key] = item
        inherited[key] = hidden, disabled

    context = inspect(data, before) if inspect is not None else None
    after = guard()
    current = form_object(S, c, form['key'])
    again = S._search_objects(c, form['key'])
    def structure(values):
        return {o['key']: tuple(o.get(p) for p in ('handle', 'class', 'type', 'name', 'title')) for o in values}
    if before != after or current.get('handle') != form.get('handle') or structure(objects) != structure(again):
        raise Failure('snapshot_unstable', 'The form changed while it was being read. Retry after the form finishes updating.')
    result = dict(elements=data, errors=errors, complete=not errors,
                  captured_at=now(), seconds=round(time.perf_counter() - start, 4))
    if prepared is not None:
        result.update(tables=tables, options=table_rows.options(True, max_rows))
    if inspect is not None:
        result.update(context=context, modified=before[0])
    return result


def difference(before, after):
    old, new = before['elements'], after['elements']
    changes, observed = [], []
    for key, item in new.items():
        if key not in old:
            continue
        previous = old[key]
        for prop in ('title', 'type') + PROPERTIES:
            prev_status = previous['_status'].get(prop, 'read')
            status = item['_status'].get(prop, 'read')
            target = {p: item[p] for p in ('key', 'handle', 'name', 'title')}
            if status == 'read' and prev_status == 'read' and previous[prop] != item[prop]:
                changes.append(dict(target, property=prop, before=previous[prop], after=item[prop]))
            elif status == 'read' and prev_status != 'read':
                observed.append(dict(target, property=prop, value=item[prop]))
    def public(item):
        return {k: v for k, v in item.items() if k != '_status'}
    # Removed elements must not mint usable refs for objects that no longer exist.
    removed = [{p: old[k][p] for p in ('name', 'title', 'class', 'type')} for k in old if k not in new]
    result = dict(changes=changes, observed=observed,
                added=[public(new[k]) for k in new if k not in old], removed=removed,
                errors=after['errors'], baseline_complete=before['complete'], complete=after['complete'])
    if before.get('options', {}).get('include_tables'):
        result.update(table_changes=table_rows.difference(before.get('tables', {}), after.get('tables', {})),
                      tables=table_rows.summary(after.get('tables', {})))
    return result


def run(S, action, key=None, snapshot_id=None, include_tables=False, max_rows=500):
    try:
        try:
            options = table_rows.options(include_tables, max_rows)
        except ValueError as exc:
            return dict(ok=False, code='invalid_argument', error=str(exc))
        owner, generation, connection_id = identity(S)
        store = S._snapshot_store
        if action == 'delete_snapshot':
            store.delete(snapshot_id, owner)
            return dict(ok=True, deleted_snapshot_id=snapshot_id, storage=store.stats())
        if action == 'list_snapshots':
            if key is not None:
                form_object(S, S._need(), key)
            return dict(ok=True, snapshots=store.list(owner, key, generation), storage=store.stats())
        c, error = S._need_ver('8.3.3')
        if error:
            return error
        if action == 'compare_snapshot':
            entry = store.get(snapshot_id, owner)
            if entry['generation'] != generation:
                raise Failure('snapshot_connection_changed', 'The connection was re-established. Create a new snapshot.')
            if key is not None and key != entry['key']:
                raise Failure('snapshot_form_mismatch', 'The snapshot belongs to another form instance.')
            form = form_object(S, c, entry['key'])
            if form.get('handle') != entry['handle']:
                raise Failure('snapshot_form_unavailable', 'This is a different form instance. Create a new snapshot.')
            baseline = json.loads(entry['payload'])
            options = baseline.get('options', table_rows.options(False, 500))
            if options['include_tables']:
                _, error = S._need_ver('8.3.6')
                if error:
                    return error
            current = capture(S, c, form, **options)
            result = difference(baseline, current)
            # Another connection can evict the baseline during the read. The local immutable
            # copy is still valid; do not resurrect it or fail an already completed comparison.
            try:
                store.get(snapshot_id, owner, touch=True)
            except Failure:
                pass
            return dict(ok=True, compared_to=snapshot_id, form_title=form.get('title'),
                        seconds=current['seconds'], **result)
        form = form_object(S, c, key)
        if include_tables:
            _, error = S._need_ver('8.3.6')
            if error:
                return error
        current = capture(S, c, form, **options)
        if (S._response.REF_MODE == 'id'
                and len({e['key'] for e in current['errors']} | set(current.get('tables', {}))) > S._refs.for_client(c).limit):
            raise Failure('ref_limit_exceeded', 'The snapshot result exceeds TC1C_REF_LIMIT; no snapshot was stored.')
        info = store.add(owner, generation, connection_id, form, current)
        result = dict(ok=True, **info, complete=current['complete'], errors=current['errors'], seconds=current['seconds'])
        if include_tables:
            result['tables'] = table_rows.summary(current['tables'])
        return result
    except Failure as exc:
        return dict(ok=False, code=exc.code, error=str(exc))
    except (OSError, RuntimeError) as exc:
        # No partial baseline after a transport failure or incomplete discovery.
        return dict(ok=False, code='snapshot_read_failed', error=str(exc))
