"""Bounded, rolling form contexts built from the already completed form read."""
import json

import _snapshots as snapshots


def present(S, c, result, signature, mode):
    owner, generation, connection_id = snapshots.identity(S)
    store = S._snapshot_storage()
    form = result['form']
    entry = store.context(owner, form['key'])
    previous = json.loads(entry['payload']) if entry else None
    # Explicit snapshots and elapsed reading time do not describe form state.
    state = {k: v for k, v in result.items() if not k.startswith('snapshot_') and k != 'seconds'}
    complete = (result['complete'] and result.get('form_details', {}).get('complete', True)
                and all(t.get('status') in ('read', 'skipped') and t.get('complete', True) and not t.get('truncated')
                        for t in result.get('table_rows', [])))
    refs = {}
    if S._address_mode() == 'id':
        registry = S._refs.for_client(c)
        refs = registry.publish(S._refs.context_pairs(result, registry))
    reason = ('requested' if mode == 'full' else
              'incomplete_read' if not complete else
              'baseline_unavailable' if entry is None else
              'form_reopened' if entry['generation'] != generation or entry['handle'] != form.get('handle') else
              'options_changed' if previous['auto_signature'] != signature else None)
    if reason is None and any(refs[k] != ref for k, ref in previous['auto_refs'].items() if k in refs):
        reason = 'references_changed'
    result.update(result_mode='full', full_reason=reason, context_id=None)
    if reason is None:
        old = {e['key']: e for e in previous['elements']}
        new = {e['key']: e for e in state['elements']}
        result['elements'] = [e for key, e in new.items() if old.get(key) != e]
        removed = [e for key, e in old.items() if key not in new or new[key].get('handle') != e.get('handle')]
        result['removed'] = [{k: e.get(k) for k in ('key', 'handle', 'name', 'title', 'class', 'type')}
                             for e in removed]
        if refs:
            for e in result['removed']:
                e['ref'] = previous['auto_refs'].get(e['key'])
        if 'table_rows' in state:
            old_tables = {t['key']: t for t in previous.get('table_rows', [])}
            result['table_rows'] = [t for t in state['table_rows'] if old_tables.get(t['key']) != t]
        before = {k: v for k, v in previous.items() if k not in ('auto_signature', 'auto_refs')}
        result.update(result_mode='changes', compared_to=entry['snapshot_id'], changed=before != state)
        result.pop('full_reason')
    if complete:
        try:
            saved = store.add(owner, generation, connection_id, form,
                dict(state, auto_signature=signature, auto_refs=refs), automatic=True,
                protected=(result.get('snapshot_id'),))
            result['context_id'] = saved['snapshot_id']
        except snapshots.Failure as exc:
            # No new baseline: retrying compares with the same previously delivered state.
            result['context_error'] = dict(code=exc.code, message=str(exc))
    else:
        result['context_error'] = dict(code='incomplete_context', message='The automatic baseline was not updated.')
    return result
