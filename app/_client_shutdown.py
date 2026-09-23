"""Bounded application exit through the test-client protocol."""
import time

import guids as G
import tc1c


class _ReadRefused(RuntimeError):
    pass


def _exit_button(objects):
    forms = [o for o in objects if o.get('form_name') == 'MessageBox']
    if len(forms) != 1:
        return None, None
    root = forms[0]['key'] + '.'
    items = [o for o in objects if o.get('key', '').startswith(root)]
    messages = [o.get('title') for o in items
                if o.get('class') == 'Decoration' and o.get('name') == 'Message']
    if len(messages) != 1:
        return None, None
    question = messages[0]
    buttons = {o.get('name'): o for o in items if o.get('class') == 'Button'
               and '.Group[Buttons].Button[' in o.get('key', '')}
    yes, no = buttons.get('Button0'), buttons.get('Button1')
    def label(value):
        return ' '.join((value or '').replace('&', '').split()).casefold()
    known = {
        ('завершить работу с приложением?', 'да', 'нет'),
        ('работа в данном окне не завершена', 'завершить работу', 'продолжить работу'),
        ('exit the application?', 'yes', 'no'),
        ('do you want to exit the application?', 'yes', 'no'),
    }
    if yes and no and (label(question), label(yes.get('title')), label(no.get('title'))) in known:
        return yes, question
    return None, question


def graceful_exit(client, process, timeout):
    """Return a failure reason, or {} once the owned process has exited."""
    previous_deadline = client._io_deadline
    deadline = time.monotonic() + timeout
    if previous_deadline is not None:
        deadline = min(deadline, previous_deadline)
    client._io_deadline = deadline
    previous_track = client._track
    client._track = None
    requested = False
    confirmed = set()
    failure = {'shutdown_reason': 'graceful_shutdown_timeout'}

    def read(method, key, middle):
        result = client.send_cmd(method, key, kind='read', middle=middle)
        if not result.get('ok'):
            raise _ReadRefused('The client refused a shutdown read.')
        return tc1c.decode_collection(result['raw'], key)

    def action(method, target):
        for kind in ('action', 'commit'):
            result = client.send_cmd(method, target['key'], kind=kind, middle=b'',
                                     handle=target.get('handle') if method == G.CLICK else None)
            if not result.get('ok'):
                raise RuntimeError('The client refused the exit command.')

    try:
        # Launch readiness may precede the first application window.
        while True:
            if process.poll() is not None:
                return {}
            try:
                windows = read(G.GET_CHILD_OBJECTS, None, b'\xe1' + b'\x81' * 7)
            except _ReadRefused:
                windows = []
            main = [w for w in windows if w.get('is_main') is True]
            if len(main) == 1:
                break
            if len(main) > 1 or time.monotonic() >= deadline:
                return {'shutdown_reason': 'main_window_unavailable'}
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
        requested = True
        action(G.CLOSE, main[0])
        while process.poll() is None and time.monotonic() < deadline:
            try:
                windows = read(G.GET_ACTIVE_WINDOW, None, tc1c.RES_COLLECTION)
                objects = read(G.GET_CHILD_OBJECTS, windows[0]['key'], b'\xe1\x82' + b'\x81' * 6) if windows else []
            except (tc1c.OperationError, _ReadRefused):
                # The active window can disappear while the exit dialog is being shown.
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
                continue
            if objects:
                button, question = _exit_button(objects)
                if question is not None and button is None:
                    return {'shutdown_reason': 'shutdown_confirmation_required', 'shutdown_question': question}
                if button and button['key'] not in confirmed:
                    confirmed.add(button['key'])
                    action(G.CLICK, button)
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
    except (OSError, RuntimeError, ValueError) as exc:
        failure = {'shutdown_reason': 'graceful_shutdown_timeout' if time.monotonic() >= deadline
                   else 'close_request_failed', 'shutdown_error': str(exc)}
        # EOF can precede process exit. Give an already requested exit its remaining time.
        if requested:
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
    finally:
        client._io_deadline = previous_deadline
        client._track = previous_track
    return {} if process.poll() is not None else failure
