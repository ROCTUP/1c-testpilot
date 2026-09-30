"""Read additional form properties in a single batch."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

import _code_execution as execution

_suppressed = ContextVar('form_details_suppressed', default=False)


@contextmanager
def suppress():
    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)


def enrich(runtime, client, result):
    mode = execution.SETTINGS.form_details
    if mode == 'false' or _suppressed.get():
        return
    service = vars(client).get('_testpilot_service')
    if not service and mode == 'auto':
        return
    status = result['form_details'] = dict(complete=False)
    try:
        if not service:
            raise execution.Failure('helper_not_ready', 'Launch the client with Testpilot.epf to read form details.')
        if 'form_details' not in service.get('capabilities', []):
            raise execution.Failure('helper_version_mismatch', 'Update Testpilot.epf to read form details.')
        pending = vars(client).get('_service_pending')
        if pending and pending.get('mode') != 'form_details':
            raise execution.Failure('execution_pending', 'Read the pending execution result before requesting form details.')
        timeout = 30
        deadline = vars(client).get('_io_deadline')
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        max_action = vars(client).get('_max_action_time')
        if max_action and max_action > 0:
            timeout = min(timeout, max_action)
        if timeout <= 0:
            raise execution.Failure('execution_timeout', 'No time remains to read form details.')
        form = result['form']
        form_id = form['key'].rsplit('.ManagedForm[', 1)[-1].removesuffix(']')
        names = list(dict.fromkeys(o['name'] for o in result['elements']
                                  if o.get('name') and o['key'] != form['key']))
        requested = set(names)
        expires = time.monotonic() + timeout
        response = execution.execute(runtime, client, mode='form_details', context='client',
            options=dict(form_id=form_id, items=names), timeout=timeout, check_permissions=False)
        if response.get('code') == 'previous_execution_completed':
            # The previous snapshot may describe another form or obsolete properties.
            # Drain it, then request fresh details for the context being returned now.
            timeout = expires - time.monotonic()
            if timeout <= 0:
                raise execution.Failure('execution_timeout', 'No time remains to read current form details.')
            response = execution.execute(runtime, client, mode='form_details', context='client',
                options=dict(form_id=form_id, items=names), timeout=timeout, check_permissions=False)
        if not response.get('ok'):
            status.update({k: v for k, v in response.items() if k != 'ok'})
            return
        data = response.get('result')
        if not isinstance(data, dict) or data.get('form_id') != form_id:
            raise execution.Failure('helper_protocol_error', 'The helper returned properties of a different form.')
        if data.get('found') is False:
            raise execution.Failure('context_unstable', 'The described form is no longer open.')
        if data.get('found') is not True or not isinstance(data.get('items'), list):
            raise execution.Failure('helper_protocol_error', 'Invalid form details response.')
        by_name = {}
        errors = []
        for item in data['items']:
            if (not isinstance(item, dict) or not isinstance(item.get('name'), str)
                    or item['name'] not in requested or item['name'] in by_name
                    or not isinstance(item.get('type'), str)
                    or not isinstance(item.get('errors', []), list)
                    or any(not isinstance(e, dict) or not isinstance(e.get('property'), str)
                           or not isinstance(e.get('error'), str) for e in item.get('errors', []))
                    or any(k in item and not isinstance(item[k], str) for k in ('input_hint', 'control_type'))
                    or any(k in item and type(item[k]) is not bool for k in ('text_edit', 'choice_mode', 'choice_list_truncated'))
                    or any(k in item and not isinstance(item[k], list) for k in ('type_restriction', 'choice_parameters', 'choice_links', 'choice_list'))):
                raise execution.Failure('helper_protocol_error', 'Invalid element details response.')
            details = {k: v for k, v in item.items() if k not in ('name', 'errors')}
            if 'choice_parameters' in details:
                parameters = details['choice_parameters']
                if any(not isinstance(p, dict) or not isinstance(p.get('name'), str)
                       or 'value' not in p for p in parameters):
                    raise execution.Failure('helper_protocol_error', 'Invalid form choice parameters.')
                details['choice_parameters'] = [dict(name=p['name'], value=p['value']) for p in parameters]
            if 'choice_list' in details:
                choices = details['choice_list']
                if (len(choices) > 50 or any(not isinstance(c, dict) or 'value' not in c
                        or not isinstance(c.get('presentation'), str) for c in choices)):
                    raise execution.Failure('helper_protocol_error', 'Invalid form choice list.')
                # A scalar first property keeps nested typed values round-trippable in TOON.
                details['choice_list'] = [dict(presentation=c['presentation'], value=c['value']) for c in choices]
            by_name[item['name']] = details
            errors.extend(dict(name=item['name'], **error) for error in item.get('errors', []))
        for name in names:
            if name not in by_name:
                errors.append(dict(name=name, code='form_properties_unavailable',
                                   error="No matching element in the form's element collection."))
        for element in result['elements']:
            element['details'] = by_name.get(element.get('name')) if element['key'] != form['key'] else None
        status.update(complete=not errors, errors=errors)
    except execution.Failure as exc:
        status.update({k: v for k, v in exc.result.items() if k != 'ok'})
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        status.update(code='form_details_failed', error=str(exc))
