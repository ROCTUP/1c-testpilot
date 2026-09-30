"""Tester window and command-interface navigation over existing UI operations."""
import re
import time
from compatible_scenarios.shared.bsl.language import Failure, Structure, TypeValue

WINDOWS = ('MainFrame', 'SecondaryFrame', 'HomePage')


def find_window(adapter, caption, timeout=0):
    from compatible_scenarios.tester.ui import UIObject
    pattern = re.compile(re.escape(caption).replace(r'\*', '.*').replace(r'\?', '.'), re.IGNORECASE)
    deadline = min(adapter.host.deadline, time.monotonic() + timeout)
    while True:
        windows = adapter.action('get_child_objects', scope='application')['children']
        matches = [w for w in windows if w.get('class') in WINDOWS and pattern.fullmatch(w.get('title') or '')]
        if matches: return UIObject(matches[0])
        if time.monotonic() >= deadline:
            adapter.host.tick(**adapter.host.location)
            return None
        adapter.host.pause(max(0, min(.1, deadline - time.monotonic())))


def object_type(data):
    cls = data.get('class')
    if cls in WINDOWS: return 'testedclientapplicationwindow'
    if cls == 'CI': return 'testedcommandinterface'
    if cls == 'CIButton': return 'testedcommandinterfacebutton'
    if '.CI.' in data.get('key', ''): return 'testedcommandinterfacegroup'
    return None


def live(adapter, obj):
    adapter.check_owner(obj)
    value = adapter.host.live(obj.data['key'])
    if not value or value.get('handle') != obj.data.get('handle'):
        raise Failure('element_unavailable', 'The scenario object is no longer available.')
    return value


def main_window(adapter):
    from compatible_scenarios.tester.ui import UIObject
    windows = adapter.action('get_child_objects', scope='application')['children']
    mains = [o for o in windows if o.get('class') == 'MainFrame']
    if len(mains) != 1: raise Failure('element_unavailable', 'The main application window was not found unambiguously.')
    return UIObject(mains[0])


def form_window(adapter, source):
    """Find the original form first, then the first window with the same FormName."""
    import _code_execution
    from compatible_scenarios.tester.ui import UIObject
    windows = adapter.action('get_child_objects', scope='application')['children']
    name = source.data.get('form_name')
    match = None
    for data in windows:
        if (data.get('class') not in WINDOWS or data.get('class') == 'MainFrame'
                or data.get('is_main') or _code_execution.protected(adapter.host.client, data.get('key'))):
            continue
        owner = UIObject(data)
        try:
            children = adapter.action('get_child_objects', owner)['children']
        except Failure as exc:
            if exc.result.get('code') == 'service_form_protected' and _code_execution.protected(adapter.host.client, data.get('key')):
                continue
            raise
        for form in children:
            if form.get('class') != 'ManagedForm': continue
            if form.get('key') == source.data['key'] and form.get('handle') == source.data.get('handle'):
                return owner
            if match is None and name and form.get('form_name') == name:
                match = owner
    return match


def window(adapter, source=None):
    adapter.check_owner(source)
    from compatible_scenarios.tester.ui import MainWindow, UIObject
    if isinstance(source, MainWindow): return main_window(adapter)
    if source is None:
        source = adapter.current
        if source is None:
            active = adapter.action('get_active_window')
            data = adapter.host.live(active['key'])
            if not data: raise Failure('element_unavailable', 'The active window is unavailable.')
            return UIObject(data)
    if isinstance(source, str):
        found = find_window(adapter, source, 3)
        if found is None: raise Failure('element_unavailable', f'Window {source!r} was not found.')
        return found
    if not isinstance(source, UIObject): raise Failure('scenario_failed', 'A window, form or window caption is required.')
    adapter.check_owner(source)
    if source.data.get('class') == 'ManagedForm': return form_window(adapter, source)
    data = live(adapter, source)
    if data.get('class') in WINDOWS: return source
    key = data['key'].split('.',1)[0]
    data = adapter.host.live(key)
    if not data or data.get('class') not in WINDOWS:
        raise Failure('element_unavailable', 'The owning window is unavailable.')
    return UIObject(data)


def links(adapter, source=None):
    from compatible_scenarios.tester.ui import UIObject
    owner = window(adapter, source)
    if owner is None: raise Failure('element_unavailable', 'No window was found for the form.')
    # Resolving the derived CI object uses GET_COMMAND_INTERFACE on this exact
    # window, without switching the active form to address the ordinary getter.
    data = adapter.host.live(owner.data['key']+'.CI')
    if not data: raise Failure('element_unavailable', 'The window command interface is unavailable.')
    return UIObject(data, owner)


def find(adapter, obj, kind, title, required=False):
    return adapter.search(obj, 'get_object' if required else 'find_object', [TypeValue(kind), title])


def functions_menu(adapter, root):
    return next((obj for title in ('Functions menu','Меню функций')
                 if (obj := find(adapter, root, 'testedcommandinterfacegroup', title)) is not None), None)


def open_menu(adapter, path):
    if not isinstance(path,str) or not path.strip(): raise Failure('scenario_failed', 'Menu requires a nonempty path.')
    parts = [p.strip() for p in path.split('/')]
    if any(not p for p in parts): raise Failure('scenario_failed', 'A menu path contains an empty component.')
    root = links(adapter, main_window(adapter)); place = root
    for part in parts[:-1]:
        group = find(adapter, place, 'testedcommandinterfacegroup', part)
        if group is not None: place = group
        else:
            adapter.action('click', find(adapter, place, 'testedcommandinterfacebutton', part, True))
            root = links(adapter, main_window(adapter))
            # A window tab can have the same caption as a menu command. Once
            # a section is open, the remaining path belongs to its functions menu.
            place = functions_menu(adapter, root) or root
    adapter.action('click', find(adapter, place, 'testedcommandinterfacebutton', parts[-1], True))


def main_menu(adapter):
    from compatible_scenarios.tester.ui import UIObject
    root = links(adapter, main_window(adapter))
    sections = next((obj for title in ('Sections panel','Панель разделов')
                     if (obj := find(adapter, root, 'testedcommandinterfacegroup', title)) is not None), None)
    if sections is None: raise Failure('menu_unavailable', 'The sections panel was not found.')
    buttons = adapter.action('get_child_objects', sections)['children']
    result = []
    open_section = None
    for data in buttons:
        if data.get('class') != 'CIButton': continue
        section = UIObject(data, sections)
        adapter.action('click', section)
        root = links(adapter, main_window(adapter))
        menu = functions_menu(adapter, root)
        if menu is None:
            # Repeated clicks can close an already-open section's menu.
            adapter.action('click', section)
            root = links(adapter, main_window(adapter))
            menu = functions_menu(adapter, root)
        if menu is None:
            adapter.call('closeall', [])
            open_section = None
            continue
        def items(group, depth=0):
            if depth > 20: raise Failure('scenario_limit', 'The command menu is nested too deeply.')
            output = []
            for child in adapter.action('get_child_objects', group)['children']:
                if child.get('class') == 'CIButton':
                    output.append(Structure(Item=child.get('title'), URL=child.get('url')))
                elif object_type(child) == 'testedcommandinterfacegroup':
                    output.append(Structure(Group=child.get('title'), Items=items(UIObject(child,group), depth+1)))
            return output
        result.append(Structure(Subsystem=data.get('title'), Items=items(menu)))
        open_section = section
    if open_section is not None:
        # A skipped section opens a form; clicking it again would reopen that form.
        adapter.action('click', open_section)
    return result


def close_all(adapter):
    """Close working windows, discarding edits, while retaining protected windows."""
    import _code_execution
    from compatible_scenarios.tester.ui import UIObject
    active_key = None
    observed = set()

    def working_windows():
        windows = [w for w in adapter.action('get_child_objects', scope='application')['children']
                if w.get('class') == 'SecondaryFrame' and not w.get('home_page')
                and not w.get('is_main') and not _code_execution.protected(adapter.host.client, w.get('key'))]
        current = {w['key'] for w in windows}
        for key in observed - current: adapter.remember_closed_window(key)
        observed.clear()
        observed.update(current)
        return windows

    def dismissal_dialog():
        nonlocal active_key
        try: active = adapter.action('get_active_window')
        except Failure as exc:
            if exc.result.get('code') == 'service_form_protected': return False
            raise
        key = active.get('key')
        active_key = key
        if not key or _code_execution.protected(adapter.host.client, key): return False
        objects = adapter.action('find_objects', root_key=key)['objects']
        if not any(o.get('form_name') == 'MessageBox' for o in objects): return False
        cancel = [o for o in objects if o.get('class') == 'Button'
                  and (o.get('title') or '').strip().casefold() in ('отменить ввод', 'cancel entry')]
        if len(cancel) > 1:
            raise Failure('close_dialog_unrecognized', 'Several cancel-entry buttons were found.')
        if len(cancel) == 1:
            adapter.action('click', UIObject(cancel[0]))
            return True
        import re
        messages = [o.get('title', '') for o in objects if o.get('name') == 'Message']
        question = ' '.join(messages).strip()
        recognized = re.fullmatch(
            r'(?:(?:Данные были изменены\.\s*)?Сохранить изменения(?:\s+в\s+.+)?|'
            r'(?:(?:The )?Data (?:has|have) been (?:changed|modified)\.\s*)?'
            r'(?:Do you want to )?Save changes(?:\s+(?:to|in)\s+.+)?)\s*\?',
            question, re.IGNORECASE | re.DOTALL)
        buttons = [o for o in objects if o.get('class') == 'Button'
                   and (o.get('title') or '').strip().casefold() in ('нет', 'no')]
        if not buttons:
            buttons = [o for o in objects if o.get('name') == 'Button1'
                       and o.get('title') in (None, '', 'Button1')
                       and '.Group[Buttons].Button[' in o.get('key','')]
        if not recognized or len(buttons) != 1:
            raise Failure('close_dialog_unrecognized', 'CloseAll cannot answer this dialog.', question=question)
        adapter.action('click', UIObject(buttons[0]))
        return True

    for _ in range(100):
        windows = working_windows()
        if not windows:
            adapter.current = None
            adapter.host.globals['currentsource'] = adapter.host.globals['текущийобъект'] = None
            return
        # Dismiss a save question or invalid-input dialog before addressing its owner.
        if dismissal_dialog(): continue
        target = next((w for w in windows if w['key'] == active_key), windows[-1])
        for attempt in range(3):
            if _code_execution.protected(adapter.host.client, target['key']): break
            try:
                adapter.action('close_window', UIObject(target))
                break
            except Failure as exc:
                # After reconnecting, identifying the target can discover the
                # helper for the first time. Its window must remain open.
                if exc.result.get('code') == 'service_form_protected' and _code_execution.protected(adapter.host.client, target['key']):
                    break
                if exc.result.get('code') not in ('window_not_closed', 'target_not_interactive', 'invalid_element_state'):
                    raise
                if dismissal_dialog():
                    if not any(w['key'] == target['key'] for w in working_windows()): break
                    if attempt < 2: continue
                if attempt == 2: raise
                forms = adapter.action('find_objects', root_key=target['key'], cls='ManagedForm')['objects']
                if len(forms) != 1: raise
                items = adapter.action('get_current_element', UIObject(forms[0]))['item']
                if not items: raise
                data = adapter.host.live(items[0]['key'])
                if not data: raise
                obj = UIObject(data)
                # A table can report its edited column as the form's current item.
                table = adapter.table(obj)
                if data.get('class') == 'Table': table = obj
                if table is not None:
                    if adapter.action('current_mode_is_edit', table).get('edit_mode') is not True: raise
                    adapter.action('end_edit_row', table, cancel=True)
                elif data.get('type') == 'InputField':
                    try:
                        adapter.action('cancel_edit', obj)
                    except Failure as cancel_error:
                        if cancel_error.result.get('code') not in ('target_not_interactive', 'invalid_element_state', 'client_busy'):
                            raise
                        if not dismissal_dialog(): raise
                else: raise
    raise Failure('scenario_limit', 'CloseAll exceeded 100 window-close cycles.')
