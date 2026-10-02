"""Platform methods whose signatures cannot be forwarded as ordinary field actions."""
from decimal import Decimal
import time

from compatible_scenarios.shared.testing import Application, UIObject, METHODS
from compatible_scenarios.shared.bsl.language import Failure, Structure, FixedArray, value_in, boolean

APPLICATION = frozenset(('record_start', 'record_finish', 'record_pause', 'record_resume', 'record_cancel',
    'get_max_action_time', 'set_max_action_time', 'get_performance', 'clear_performance',
    'set_file_dialog_result', 'clear_file_dialog_result', 'wait_for_object_displayed', 'wait_for_condition'))
WINDOW = frozenset(('goto_next_window', 'goto_previous_window', 'goto_start_page',
    'get_user_message_texts', 'choose_user_message', 'close_user_messages_panel'))
SPECIAL = APPLICATION | WINDOW | frozenset(('wait_for_closing', 'get_choice_list', 'get_state_presentation', 'get_html'))


def address(adapter, obj):
    adapter.check_owner(obj)
    live = adapter.host.live(obj.data['key'])
    if not live or live.get('handle') != obj.data.get('handle'):
        raise Failure('element_unavailable', 'The scenario element is no longer available.')
    return dict(key=obj.data['key'], handle=obj.data.get('handle'))


def number(value, label):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise Failure('scenario_failed', f'{label} must be a finite nonnegative number.')
    return value


def native(adapter, action, arguments, invoke):
    host = adapter.host
    with host.connections.bound():
        return host.operation(action, arguments, lambda: invoke(host.R))


def wait(adapter, predicate, timeout, interval=.1):
    host = adapter.host
    seconds = host.deadline-time.monotonic() if timeout is None else float(number(timeout, 'Timeout'))
    deadline = min(host.deadline, time.monotonic()+seconds)
    while True:
        host.tick(host.location['path'], host.location['line'])
        answer = predicate()
        host.tick(host.location['path'], host.location['line'])
        if type(answer) is not bool:
            raise Failure('scenario_failed', 'The wait condition must return Boolean.')
        if answer: return True
        if time.monotonic() >= deadline: return False
        host.pause(min(interval, max(0, deadline-time.monotonic())))


def invoke(adapter, obj, name, args):
    action, params, slot = METHODS[name]
    if action in APPLICATION and not isinstance(obj, Application):
        raise Failure('unsupported_scenario', f'{name} requires App.')
    if action not in APPLICATION and not isinstance(obj, UIObject):
        raise Failure('unsupported_scenario', f'{name} requires a tested object.')
    if action == 'get_html':
        # The VM resolves output argument destinations; the transport gets no output values.
        if args: raise Failure('scenario_failed', 'Unexpected HTML input arguments.')
        import base64
        from compatible_scenarios.shared.bsl.language import Map
        from compatible_scenarios.shared.bsl.pictures import Picture
        kw = address(adapter, obj)
        result = native(adapter, 'get_html', kw, lambda R: R._get_html(**kw, attachments=True))
        pictures = Map()
        for key, encoded in result['attachments'].items():
            pictures[key] = Picture(base64.b64decode(encoded, validate=True))
        return result['html'], pictures
    minimum = {'set_file_dialog_result': 1, 'wait_for_condition': 1,
               'get_performance': 0, 'wait_for_closing': 0, 'wait_for_object_displayed': 0}.get(action, len(params))
    if not minimum <= len(args) <= len(params):
        raise Failure('scenario_failed', f'{name} expects {minimum}..{len(params)} arguments.')
    kw = {key: val for key, val in zip(params, args) if val is not None}
    if action in WINDOW:
        if obj.data.get('class') not in ('MainFrame', 'SecondaryFrame', 'HomePage'):
            raise Failure('unsupported_element_type', f'{name} requires an application window.')
        if action.startswith('goto_') and obj.data.get('class') != 'MainFrame':
            raise Failure('unsupported_element_type', f'{name} requires the main window.')
        if action == 'choose_user_message' and not isinstance(kw.get('text'), str):
            raise Failure('scenario_failed', 'The message text must be a string.')
        result = adapter.action(action, obj, **kw)
        return FixedArray(result['messages']) if slot else None
    if action == 'wait_for_closing':
        if obj.data.get('class') != 'ManagedForm':
            raise Failure('unsupported_element_type', 'WaitForClosing requires a form.')
        def closed():
            forms = adapter.action('find_objects', scope='application', cls='ManagedForm', timeout=0)['objects']
            return not any(f.get('key') == obj.data['key'] and f.get('handle') == obj.data.get('handle') for f in forms)
        return wait(adapter, closed, args[0] if args else Decimal(60))
    if action == 'wait_for_object_displayed':
        search = list(args) + [None]*(4-len(args))
        if len(args) < 4: search[3] = Decimal(60)
        timeout = search[3]
        search[3] = Decimal(0)
        return wait(adapter, lambda: adapter.search(obj, 'find_object', search) is not None, timeout)
    if action == 'wait_for_condition':
        from compatible_scenarios.shared.bsl.callbacks import CallbackDescription
        if not isinstance(args[0], CallbackDescription):
            raise Failure('scenario_failed', 'WaitForCondition requires CallbackDescription.')
        return wait(adapter, args[0].invoke, args[1] if len(args)>1 else Decimal(60), 1.)
    if action == 'set_file_dialog_result':
        accepted = boolean(args[0])
        filename, index = (args[1:] + [None, None])[:2]
        if accepted:
            if not (isinstance(filename, str) or isinstance(filename, list) and all(isinstance(s, str) for s in filename)):
                raise Failure('scenario_failed', 'A filename or array of filenames is required.')
            index = number(index, 'Filter index')
            if index != int(index): raise Failure('scenario_failed', 'Filter index must be an integer.')
        else: filename, index = None, 0
        native(adapter, action, dict(result=accepted, filename=filename, filter_index=int(index)),
               lambda R: R._set_file_dialog_result(accepted, filename, int(index), replace_pending=False))
        return None
    if action == 'record_start':
        native(adapter, action, {}, lambda R: R._record_start('native'))
        return None
    if action == 'set_max_action_time':
        kw['seconds'] = number(args[0], 'Max action execution time')
    if action in ('clear_performance', 'get_performance'):
        clear = True if action == 'clear_performance' else boolean(args[0]) if args and args[0] is not None else False
        result = adapter.action('get_performance', clear=clear)
        if action == 'clear_performance': return None
        from .context import english_script
        english = english_script(adapter.host)
        indicators = result['indicators']
        names = [('Calls', 'Вызовы'), ('Duration', 'Длительность'), ('Sent', 'Отправлено'), ('Received', 'Принято')]
        values = Structure()
        for en, ru in names:
            key = en if en in indicators else ru
            if key not in indicators: raise Failure('state_unavailable', 'Performance indicators are incomplete.')
            values[en if english else ru] = value_in(indicators[key])
        return values
    if action == 'get_choice_list':
        from .context import english_script
        result = adapter.action(action, obj)
        if result.get('status') == 'unavailable':
            raise Failure('state_unavailable', 'The choice list could not be read.')
        en = english_script(adapter.host)
        return FixedArray(Structure({('DataPresentation' if en else 'ПредставлениеДанных'): item['presentation'],
                                    ('DisplayedText' if en else 'ОтображаемыйТекст'): item['text']}) for item in result['items'])
    if action == 'get_state_presentation':
        kw = address(adapter, obj)
        result = native(adapter, action, kw, lambda R: R._get_state_presentation(**kw, structured=True))
        return result['presentation']
    result = adapter.action(action, **kw)
    if action == 'get_max_action_time' and result[slot] is None:
        import tc1c
        return Decimal(tc1c.TestClient.RECV_TIMEOUT)
    return value_in(result[slot]) if slot else None
