"""Tester files, libraries, suites and execution semantics."""
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import time

from compatible_scenarios.shared.bsl.language import Failure, Node, Parser, VM, Structure, Map, TypeValue, NULL, walk, value_in, value_out, string
from compatible_scenarios.shared.bsl.collections import CONSTRUCTORS, TYPE_NAMES, SortDirection
from compatible_scenarios.tester.ui import Adapter, Application, MainWindow, UIObject, FUNCTIONS, METHODS, ASSERTIONS, aliases


BUILTINS = aliases([
    'call Вызвать', 'run Позвать', 'pause Пауза', 'message Сообщить',
    'errordescription ОписаниеОшибки',
    'stop Стоп', 'logerror ЗаписатьОшибку',
    'environmentexists СозданоОкружение', 'environmentdata ДанныеОкружения', 'registerenvironment СохранитьОкружение',
    'progressshow ПрогрессПоказать', 'progresshide ПрогрессСкрыть',
    'testingid ИДОкружения', 'format Формат', 'nstr НСтр',
    'date Дата', 'currentdate ТекущаяДата', 'year Год', 'month Месяц', 'day День',
    'hour Час', 'minute Минута', 'second Секунда', 'begofday НачалоДня', 'endofday КонецДня',
    'begofmonth НачалоМесяца', 'endofmonth КонецМесяца', 'addmonth ДобавитьМесяц',
    'typeof ТипЗнч', 'type Тип',
    'string Строка', 'number Число', 'int Цел', 'strlen СтрДлина', 'trimall СокрЛП',
    'upper ВРег', 'lower НРег', 'left Лев', 'right Прав', 'mid Сред',
    'strreplace СтрЗаменить', 'isblankstring ПустаяСтрока',
    'strsplit СтрРазделить', 'strconcat СтрСоединить', 'find Найти', 'strfind СтрНайти', 'char Символ',
    'triml СокрЛ', 'trimr СокрП', 'strstartswith СтрНачинаетсяС', 'strendswith СтрЗаканчиваетсяНа',
    'strlinecount СтрЧислоСтрок', 'strgetline СтрПолучитьСтроку', 'charcode КодСимвола',
    'valueisfilled ЗначениеЗаполнено', 'round Окр', 'min Мин', 'max Макс',
    'begofyear НачалоГода', 'endofyear КонецГода', 'begofquarter НачалоКвартала', 'endofquarter КонецКвартала',
    'begofweek НачалоНедели', 'endofweek КонецНедели', 'weekday ДеньНедели', 'dayofyear ДеньГода',
    'vstudio ВСтудию', 'parametersspace ЗонаПараметров', 'disconnect Отключить',
    'errorinfo ИнформацияОбОшибке', 'detailederrordescription ПодробноеПредставлениеОшибки',
    'brieferrordescription КраткоеПредставлениеОшибки',
])
VALUE_METHODS = {'getbinarydata', 'получитьдвоичныеданные', 'getid', 'получитьидентификатор', 'findbyid', 'найтипоидентификатору',
                 'move', 'сдвинуть', 'sortbyvalue', 'сортироватьпозначению', 'sortbypresentation', 'сортироватьпопредставлению',
                 'findbyvalue', 'найтипозначению', 'indexof', 'индекс', 'copy', 'скопировать',
                 'loadvalues', 'загрузитьзначения', 'unloadvalues', 'выгрузитьзначения', 'fillchecks', 'заполнитьпометки',
                 'count', 'количество', 'add', 'добавить', 'ubound', 'вграница',
                 'get', 'получить', 'clear', 'очистить', 'insert', 'вставить', 'property', 'свойство', 'delete', 'удалить',
                 'write', 'записать', 'size', 'размер', 'find', 'найти', 'set', 'установить', 'jobrecord'}
ARITIES = {
    'environmentexists': (1, 1), 'environmentdata': (1, 1), 'registerenvironment': (1, 2),
    'progressshow': (0, 0), 'progresshide': (0, 0),
    'testingid': (0, 0), 'format': (1, 2), 'nstr': (1, 2),
    'get': (1, 3), 'fetch': (1, 3), 'set': (2, 4), 'click': (1, 3), 'activate': (1, 3),
    'clear': (1, 3), 'choose': (1, 3), 'pick': (2, 4), 'with': (0, 2), 'findform': (1, 1),
    'commando': (1, 2), 'close': (0, 1), 'closeall': (0, 0), 'check': (2, 4), 'assert': (1, 2),
    'checkerrors': (0, 0), 'getmessages': (0, 0), 'connect': (0, 3),
    'systemvariable': (1, 1),
    'put': (2, 5), 'entervalue': (2, 2), 'findmessages': (1, 1), 'gettablecontent': (1, 2),
    'openmenu': (1, 1), 'getwindow': (0, 1), 'getlinks': (0, 1), 'getmainmenu': (0, 0),
    'getspreadsheetcontent': (1, 2), 'screenshot': (0, 2), 'getscreenshot': (0, 0),
    'getactivewindowcontrols': (0, 0), 'getactivewindowchanges': (0, 0),
    'gotorow': (3, 5),
    'gotofirstrow': (1, 2), 'gotolastrow': (1, 2), 'gotonextrow': (1, 2), 'gotopreviousrow': (1, 2),
    'next': (0, 0), 'currenttab': (1, 3), 'waiting': (1, 3), 'openvalueininputfield': (1, 2),
    'checkstate': (2, 5), 'checktable': (1, 4),
    'expandtreerow': (1, 2), 'collapsetreerow': (1, 2), 'gooneleveldown': (1, 2), 'goonelevelup': (1, 2),
    'call': (1, 3), 'run': (1, 3), 'pause': (1, 1), 'message': (1, 2),
    'disconnect': (0, 1), 'parametersspace': (0, 0), 'errorinfo': (0, 0),
    'strstartswith': (2, 2), 'strendswith': (2, 2), 'strgetline': (2, 2), 'charcode': (1, 2),
    'round': (1, 3), 'min': (1, 256), 'max': (1, 256),
    'errordescription': (0, 0),
    'stop': (0, 1), 'logerror': (1, 1),
    'date': (1, 6), 'currentdate': (0, 0), 'addmonth': (2, 2),
    'left': (2, 2), 'right': (2, 2), 'mid': (2, 3), 'strreplace': (3, 3),
    'strsplit': (2, 3), 'strconcat': (1, 2), 'find': (2, 2), 'strfind': (2, 5),
}
# This adapter is selected by source content, never just by a library method's name.
# grumagargler/CommonTests, Общее/ИД.bsl; normal execution outside Tester's editor.
ID_SOURCE_SHA256 = 'aab456071e3e18fb0ca91a876320c059674e0af1d92670f2216f298c1ec53611'
TYPES = aliases(['string Строка', 'number Число', 'boolean Булево', 'undefined Неопределено', 'null',
                 'date Дата',
                 'array Массив', 'structure Структура', 'map Соответствие', 'type Тип',
                 'testedform ТестируемаяФорма', 'testedformtable ТестируемаяТаблицаФормы',
                 'testedformfield ТестируемоеПолеФормы', 'testedformbutton ТестируемаяКнопкаФормы',
                 'testedformgroup ТестируемаяГруппаФормы', 'testedformdecoration ТестируемаяДекорацияФормы'])
TYPES.update({name: TYPE_NAMES[cls] for name, cls in CONSTRUCTORS.items()})
TYPES.update({'valuelistitem':'valuelistitem', 'элементсписказначений':'valuelistitem'})
TYPES.update({'sortdirection':'sortdirection', 'направлениесортировки':'sortdirection'})
TYPES.update(aliases(['testedclientapplicationwindow ТестируемоеОкноКлиентскогоПриложения',
                     'errorinfo ИнформацияОбОшибке', 'messagestatus СтатусСообщения', 'roundmode РежимОкругления',
                     'testedapplication ТестируемоеПриложение',
                     'binarydata ДвоичныеДанные', 'picture Картинка', 'callbackdescription ОписаниеОповещения',
                     'testedcommandinterface ТестируемыйКомандныйИнтерфейс',
                     'testedcommandinterfacebutton ТестируемаяКнопкаКомандногоИнтерфейса',
                     'testedcommandinterfacegroup ТестируемаяГруппаКомандногоИнтерфейса']))


def read_metadata(path):
    sidecar = path.with_suffix('.json')
    if not sidecar.is_file(): return {}
    if sidecar.stat().st_size > 1024*1024:
        raise Failure('scenario_limit', 'Scenario metadata is too large.', path=sidecar)
    try: metadata = json.loads(sidecar.read_text(encoding='utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise Failure('invalid_scenario', f'Invalid scenario metadata: {exc}', path=sidecar) from exc
    if not isinstance(metadata, dict):
        raise Failure('invalid_scenario', 'Scenario metadata must be an object.', path=sidecar)
    return metadata


@dataclass
class Program:
    path: Path
    root: Path
    tree: list
    functions: dict
    sha256: str
    adapter: str = ''
    application: str = ''


class Repository:
    def __init__(self, path, roots=None, applications=None, application_name='', metadata_path=None):
        self.entry = Path(path).expanduser().resolve()
        self.roots = [Path(r).expanduser().resolve() for r in (roots or [self.entry.parent])]
        if any(not r.is_dir() for r in self.roots):
            raise Failure('invalid_scenario_path', 'Every root must be an existing directory.')
        if not any(self.entry.is_relative_to(r) for r in self.roots):
            raise Failure('invalid_scenario_path', 'The scenario must be inside one of the supplied roots.')
        self.application_name = application_name.casefold()
        self.applications = {}
        if applications is not None and not isinstance(applications, dict):
            raise Failure('invalid_scenario', 'applications must map application names to lists of library directories.')
        for name, directories in (applications or {}).items():
            if not isinstance(name, str) or not name.strip() or not isinstance(directories, list) or not directories:
                raise Failure('invalid_scenario', 'Each application needs a nonempty name and list of library directories.')
            if any(not isinstance(p, (str, os.PathLike)) or not str(p) for p in directories):
                raise Failure('invalid_scenario_path', 'Application directories must be paths.')
            name = name.casefold()
            if name in self.applications:
                raise Failure('ambiguous_scenario', 'Application names must be unique, ignoring case.')
            paths = list(dict.fromkeys(Path(p).expanduser().resolve() for p in directories))
            if any(not p.is_dir() for p in paths):
                raise Failure('invalid_scenario_path', 'Every application directory must exist.')
            for other in self.applications.values():
                if any(a.is_relative_to(b) or b.is_relative_to(a) for a in paths for b in other):
                    raise Failure('ambiguous_scenario', 'Directories of different applications must not overlap.')
            self.applications[name] = paths
        self.all_roots = list(dict.fromkeys(self.roots + [p for paths in self.applications.values() for p in paths]))
        from compatible_scenarios.tester.metadata import read_file
        self.metadata = read_file(metadata_path) if metadata_path is not None else None
        self.programs, self.dependencies, self.total = {}, {}, 0
        self.dynamic_calls = []

    def resolve(self, name, caller, relative=False, application=None):
        if not isinstance(name, str) or not name or any(not s or s in ('.', '..') for s in name.split('.')) or any(c in name for c in '/\\:'):
            raise Failure('unsupported_scenario', 'Library names must be dot-separated scenario names.')
        selected = caller.application if application is None else application
        if not isinstance(selected, str):
            raise Failure('invalid_scenario', 'The application argument must be a name or Undefined.')
        selected = selected.casefold()
        if selected in self.applications:
            primary = self.applications[selected]
        elif selected == self.application_name:
            primary = self.roots
        else:
            raise Failure('scenario_application_missing', f'No library directories are configured for application {selected!r}.')
        # Tester prefixes Run's name with the caller's folder even for another application.
        folder = caller.path.parent.relative_to(caller.root) if relative else Path()
        groups = [primary]
        if application is None and primary != self.roots:
            groups.append(self.roots)
        candidates = []
        for bases in groups:
            for root in bases:
                stem = (root / folder).joinpath(*name.split('.'))
                files = [Path(str(stem) + suffix) for suffix in ('.bsl', '.1c.bsl', '.1cm.bsl')]
                files += [stem / (stem.name + suffix) for suffix in ('.dir.bsl', '.dir.1cm.bsl', '.dir.1c.bsl')]
                for candidate in files:
                    if candidate.is_file():
                        resolved = candidate.resolve()
                        if not any(resolved.is_relative_to(r) for r in bases):
                            raise Failure('invalid_scenario_path', 'A library path leaves the selected application directories.')
                        if resolved not in candidates: candidates.append(resolved)
            if candidates: break
        if len(candidates) != 1:
            raise Failure('scenario_dependency_missing' if not candidates else 'ambiguous_scenario',
                          f'Expected one library scenario {name!r}; found {len(candidates)}.', path=caller.path)
        return candidates[0]

    def load(self, path=None):
        path = self.entry if path is None else path
        if path in self.programs: return self.programs[path]
        if not path.is_file() or path.suffix.lower() != '.bsl':
            raise Failure('invalid_scenario_path', 'Provide an existing .bsl scenario file.', path=path)
        if path.stat().st_size > 1024*1024 or len(self.programs) >= 256:
            raise Failure('scenario_limit', 'Scenario sources exceed the file/count limit.', path=path)
        try: source = path.read_text(encoding='utf-8-sig')
        except UnicodeError as exc: raise Failure('invalid_scenario_path', 'Scenarios must be UTF-8.', path=path) from exc
        self.total += len(source)
        if self.total > 8*1024*1024: raise Failure('scenario_limit', 'Loaded sources exceed 8 MiB.')
        digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
        root = max((r for r in self.all_roots if path.is_relative_to(r)), key=lambda p: len(p.parts))
        program = Program(path, root, [], {}, digest)
        program.application = next((n for n, paths in self.applications.items() if root in paths), self.application_name)
        if digest == ID_SOURCE_SHA256:
            program.adapter = 'common_tests_id'
        else:
            metadata = read_metadata(path)
            if metadata.get('Type') not in (None, 'Method', 'Scenario', 'Test', 'Script'):
                raise Failure('unsupported_scenario', f'Unsupported Tester scenario type {metadata.get("Type")!r}.', path=path)
            program.tree, program.functions = Parser(source, path).parse()
            if not program.tree:
                raise Failure('unsupported_scenario', 'The file has no executable scenario body; select a scenario, not a library/group container.', path=path)
        self.programs[path] = program  # Permit recursive calls; execution depth is bounded.
        for node in walk([program.tree, list(program.functions.values())]):
            if node.kind == 'new' and node.args[0] not in (*CONSTRUCTORS, 'array', 'массив', 'structure', 'структура', 'map', 'соответствие', 'callbackdescription', 'описаниеоповещения'):
                raise Failure('unsupported_scenario', f'Unsupported BSL type {node.args[0]!r}.', path=path, line=node.line)
            if node.kind != 'call': continue
            target, args = node.args
            if target.kind == 'name':
                name = target.args[0]
                if name in program.functions: continue
                if name not in BUILTINS and name not in FUNCTIONS:
                    raise Failure('unsupported_scenario', f'Unsupported function {name!r}.', path=path, line=node.line)
                canonical = BUILTINS.get(name, FUNCTIONS.get(name))
                if canonical == 'date' and len(args) not in (1, 3, 6):
                    raise Failure('unsupported_scenario', 'Date takes 1, 3 or 6 arguments.', path=path, line=node.line)
                minimum, maximum = ARITIES.get(canonical, (1, 1))
                if not minimum <= len(args) <= maximum:
                    raise Failure('unsupported_scenario', f'{name} expects {minimum}..{maximum} arguments.', path=path, line=node.line)
                if BUILTINS.get(name) in ('call', 'run'):
                    if (not args or args[0].kind != 'value' or not isinstance(args[0].args[0], str)
                            or len(args) > 2 and args[2].kind != 'value'):
                        self.dynamic_calls.append(dict(path=str(path), line=node.line))
                        continue
                    dependency = self.resolve(args[0].args[0], program, BUILTINS[name] == 'run',
                                              args[2].args[0] if len(args) > 2 else None)
                    self.dependencies[(path, name, args[0].args[0])] = dependency
                    self.load(dependency)
            elif target.kind == 'attr':
                name = target.args[1]
                if name not in VALUE_METHODS and name not in METHODS and name not in ASSERTIONS:
                    raise Failure('unsupported_scenario', f'Unsupported method {name!r}.', path=path, line=node.line)
            else: raise Failure('unsupported_scenario', 'Indirect calls are not supported.', path=path, line=node.line)
        return program

    def load_dynamic(self, name, caller, relative, application=None):
        path = self.resolve(name, caller, relative, application)
        return self.load_checked(path)

    def load_checked(self, path):
        before, total, dynamic = set(self.programs), self.total, len(self.dynamic_calls)
        try: return self.load(path)
        except Exception:
            # A rejected program must not become runnable through the recursive-load cache.
            self.programs = {p: v for p, v in self.programs.items() if p in before}
            self.dependencies = {k: v for k, v in self.dependencies.items() if k[0] in before and v in before}
            self.total = total
            del self.dynamic_calls[dynamic:]
            raise


class Runner:
    def __init__(self, R, client, repository, timeout, application_name='', disconnected_root=None):
        self.R, self.client, self.repository = R, client, repository
        self.deadline = time.monotonic() + timeout
        self.depth, self.ticks, self.operations = 0, 0, 0
        self.location = {}
        self.messages, self.steps, self.message_details = [], [], []
        self.diagnostics = []
        self.previous_application_errors = {}
        self.window_close_revisions = {}
        self.metadata_cache = {}
        self.separators_cache = {}
        self.auto_separators = None
        self.auto_separator_values = None
        self.locale_cache = {}
        self.date_formats_cache = {}
        self.separators_explicit = False
        self.show_progress = False
        self.journal = R._state.get('_call_journal') if R.LOGGING else None
        self.globals = {'__': None, 'currentsource': None, 'текущийобъект': None,
                        'имяприложения': application_name, 'appname': application_name,
                        'latestseparatorsinfo': None, 'testerdynamiclistsearchwaittime': Decimal(2),
                        'testerdateformat': None,
                        'testerlanguage': None,
                        'testerscriptvariant': None,
                        'ignoreerrors': False, 'игнорироватьошибки': False}
        direction = Structure({'Down': 'down', 'Вниз': 'down', 'Up': 'up', 'Вверх': 'up'})
        self.globals.update(rowgotodirection=direction, направлениепереходакстроке=direction)
        from compatible_scenarios.shared.bsl.strings import SearchDirection, Constants
        search = Constants({'FromBegin': SearchDirection.BEGIN, 'СНачала': SearchDirection.BEGIN,
                            'FromEnd': SearchDirection.END, 'СКонца': SearchDirection.END})
        chars = Constants({'LF': '\n', 'ПС': '\n', 'CR': '\r', 'ВК': '\r', 'Tab': '\t', 'Таб': '\t'})
        self.globals.update(searchdirection=search, направлениепоиска=search, chars=chars, символы=chars)
        from compatible_scenarios.shared.bsl.builtins import constants
        self.globals.update(constants())
        self.globals['главноеокно'] = self.globals['mainwindow'] = MainWindow()
        self.globals['приложение'] = self.globals['app'] = Application()
        launch_parameters = Map()
        self.globals['launchparameters'] = self.globals['параметрызапуска'] = launch_parameters
        special_fields = self.default_special_fields = Structure(LineNo='N')
        self.special_fields_initialized = False
        self.globals['specialfields'] = self.globals['специальныеполя'] = special_fields
        self.ui = Adapter(self)
        from compatible_scenarios.tester.connections import Connections, AppData
        self.connections = Connections(self, disconnected_root)
        self.connections.appdata = AppData(self.connections)
        self.globals['appdata'] = self.globals['свойстваприложения'] = self.connections.appdata
        if self.connections.detached:
            self.globals['app'] = self.globals['приложение'] = None
            self.globals['mainwindow'] = self.globals['главноеокно'] = None


    def initial_locals(self, parameter):
        local = Structure()
        return {'_': parameter, 'this': local, 'тут': local,
                'стандартнаяобработка': True, 'standardprocessing': True}

    def inherit_locals(self, caller, callee):
        callee['this'] = callee['тут'] = caller['this']

    def resolve_global(self, name):
        if name in ('specialfields', 'специальныеполя'): return True, self.special_fields(name)
        if name in self.globals: return True, self.globals[name]
        if name in ('мета', 'meta'): return True, self.metadata()
        return False, None

    def assign_global(self, name, value):
        if name == 'latestseparatorsinfo': self.separators_explicit = value is not None
        if name not in self.globals and name not in ('мета', 'meta'): return False
        self.globals[name] = value
        return True

    def value_changed(self, target):
        if target is self.globals.get('latestseparatorsinfo'): self.separators_explicit = True

    def after_statement(self, path, line):
        self.check_application_error(path, line)

    def language_context(self):
        from .regional import language_context
        return language_context(self)

    def number_separators(self):
        from .regional import get
        return get(self)

    def scenario_language(self):
        from .regional import scenario_language
        return scenario_language(self)

    def system_language(self):
        fallback = self.language_context()[0]
        service = vars(self.client).get('_testpilot_service') or {}
        language = service.get('system_language') or fallback
        if not isinstance(language, str): raise Failure('helper_protocol_error', 'Invalid system language in ServiceInfo.')
        return language

    def special_fields(self, name='specialfields'):
        fields = self.globals[name]
        if fields is self.default_special_fields and not self.special_fields_initialized:
            from compatible_scenarios.tester.regional import scenario_language
            fields['LineNo'] = '#' if scenario_language(self).casefold() == 'en' else 'N'
            self.special_fields_initialized = True
        return fields

    @contextmanager
    def session(self):
        from compatible_scenarios.tester.ui import OBJECT_OWNER
        token = OBJECT_OWNER.set(self.client)
        if not self.connections.detached:
            self.globals['app'].owner = self.globals['mainwindow'].owner = self.client
        try: yield
        finally:
            try: self.connections.close()
            finally: OBJECT_OWNER.reset(token)

    def connect(self, port, computer):
        return self.operation('connect', dict(port=value_out(port), host=computer),
                              lambda: self.connections.connect(port, computer))

    def tick(self, path, line):
        self.location = dict(path=str(path), line=line)
        self.ticks += 1
        if self.ticks > 200000: raise Failure('scenario_limit', 'Scenario exceeded 200000 evaluation steps.', **self.location)
        if time.monotonic() >= self.deadline: raise Failure('scenario_timeout', 'Scenario deadline expired.', **self.location)
        if not self.connections.detached and (getattr(self.client, 'closed', False) or getattr(self.client, '_interrupted', False)):
            raise Failure('connection_closed', 'The scenario connection was closed.', **self.location)

    def operation(self, name, arguments, invoke, *, raise_on_error=True):
        self.tick(self.location.get('path', self.repository.entry), self.location.get('line', 1))
        self.operations += 1
        if self.operations > 10000: raise Failure('scenario_limit', 'Scenario exceeded 10000 UI operations.')
        R = self.R
        journal = self.journal
        call, token, result = None, None, None
        started = time.monotonic()
        try:
            if journal is not None and journal.active:
                try:
                    call = journal.begin('compatible_scenario', name, R._profiles.redact(dict(arguments, **self.location)), {})
                    token = R._call_logging.CURRENT.set(call)
                except Exception: journal.pause('log_write_failed')
            result = invoke()
            if raise_on_error and isinstance(result, dict) and result.get('ok') is False:
                raise Failure(result.get('code', 'action_failed'), result.get('error') or result.get('note') or f'{name} failed.', action_result=result)
            return result
        except Exception as exc:
            from _code_execution import Failure as ServiceFailure
            if isinstance(exc, (Failure, ServiceFailure)): result = exc.result
            elif isinstance(exc, R.tc1c.OperationError): result = exc.result()
            else: result = dict(ok=False, code='scenario_timeout' if isinstance(exc, TimeoutError) else 'operation_failed', error=str(exc))
            result = dict(result, **self.location)
            if isinstance(exc, Failure):
                exc.result.update(self.location); raise
            failure = Failure(result.get('code', 'operation_failed'), result.get('error', str(exc)), **self.location)
            raise failure from exc
        finally:
            success = not isinstance(result, dict) or result.get('ok') is not False
            step = dict(action=name, ok=success, **self.location, elapsed=round(time.monotonic()-started, 3))
            if self.connections.current is not None:
                step['connection_id'] = self.connections.current[1].id
            if not success: step.update({k: result[k] for k in ('code', 'error') if k in result})
            if len(self.steps) < 1000: self.steps.append(step)
            if call is not None:
                try:
                    call['result'] = R._profiles.redact(result if isinstance(result, dict) else dict(ok=True))
                    capture = None
                    if name == 'get_screenshot' and success and hasattr(result, 'png'):
                        capture = lambda: result
                    elif name != 'log_error' and time.monotonic() < self.deadline and (journal.mode == 'all' or
                            journal.mode == 'actions' and (name in R._LOG_CHANGING or not success)):
                        capture = lambda: R._screenshots.capture(self.client)
                    journal.finish(call, capture)
                except Exception: journal.pause('log_write_failed')
            if token is not None: R._call_logging.CURRENT.reset(token)

    def action(self, name, /, **arguments):
        arguments = value_out(arguments)
        with self.connections.bound():
            return self.operation(name, arguments, lambda: self.R._ACTIONS[self.R._ACTION_GROUP[name]][name](**arguments))

    def metadata(self):
        from compatible_scenarios.tester.metadata import get
        return get(self)

    def live(self, key):
        with self.connections.bound():
            return self.operation('resolve_element', {'key': key}, lambda: self.R._ref_live_object(self.client, key))

    def check_application_error(self, path, line):
        """Tester Compiler.addCheck / Debugger.ErrorCheck at statement boundaries."""
        from compatible_scenarios.shared.bsl.language import boolean
        if (self.connections.detached or boolean(self.globals['ignoreerrors'])
                or boolean(self.globals['игнорироватьошибки'])):
            return
        self.tick(path, line)
        def check():
            result = self.R._ACTIONS[self.R._ACTION_GROUP['get_current_error']]['get_current_error']()
            if result.get('ok') is False:
                raise Failure(result.get('code', 'action_failed'), result.get('error') or 'Could not read the application error.')
            description = result.get('error')
            client_id = id(self.client)
            if not description:
                self.previous_application_errors.pop(client_id, None)
            elif description != self.previous_application_errors.get(client_id):
                self.previous_application_errors[client_id] = description
                raise Failure('application_error', description, details=result.get('details') or [])
            return dict(ok=True)
        with self.connections.bound():
            try: self.operation('check_application_error', {}, check)
            except Failure as exc:
                if not self.ui.platform_refusal(exc): raise

    def pause(self, seconds):
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds < 0: raise Failure('scenario_failed', 'Pause must be finite and nonnegative.')
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.tick(self.location.get('path', self.repository.entry), self.location.get('line', 1))
            time.sleep(min(.1, max(0, min(end, self.deadline)-time.monotonic())))

    def execute(self, program, parameter):
        previous = self.ui.current
        previous_client = self.client
        previous_window = previous.data['key'].split('.', 1)[0] if previous is not None else None
        previous_close = self.ui.window_close_revision(previous_window)
        if program.adapter == 'common_tests_id':
            self.pause(1)
            return format(int(time.time()) % 1000000000, 'X')
        if isinstance(parameter, str) and parameter.startswith('{') and parameter.endswith('}'):
            parameter = value_in(json.loads(parameter))
        vm = VM(self, program, parameter)
        result = vm.run()
        if not self.connections.detached and vm.env['standardprocessing'] and vm.env['стандартнаяобработка']:
            self.ui.call('checkerrors', [])
            if previous_client is not self.client: previous = None
            if previous is not None and self.ui.window_close_revision(previous_window) != previous_close:
                previous = None
            if previous is not None and previous is not self.ui.current: self.ui.call('with', [previous])
            self.ui.current = previous
            self.globals['currentsource'] = self.globals['текущийобъект'] = previous
        return result

    def call(self, name, args, vm):
        if name in FUNCTIONS: return self.ui.call(name, args)
        builtin = BUILTINS[name]
        if builtin == 'testingid':
            from compatible_scenarios.tester.identifiers import next_id
            return self.operation('testing_id', {}, lambda: next_id(self.deadline))
        if builtin in ('format', 'nstr'):
            from compatible_scenarios.shared.bsl.formatting import call
            return self.text(call(self, builtin, args))
        if builtin in ('environmentexists', 'environmentdata', 'registerenvironment'):
            from compatible_scenarios.tester.environments import call
            return call(self, builtin, args)
        if builtin in ('progressshow', 'progresshide'):
            self.show_progress = builtin == 'progressshow'
            return None
        from compatible_scenarios.shared.bsl.builtins import ErrorInfo, filled, numeric, MessageStatus, RoundMode
        if builtin == 'disconnect':
            close = args[0] if args else False
            if type(close) is not bool: raise Failure('scenario_failed', 'Disconnect Close must be Boolean.')
            return self.operation('disconnect', {'close':close}, lambda: self.connections.disconnect(close))
        if builtin == 'parametersspace': return ParametersSpace()
        if builtin == 'valueisfilled': return filled(args[0])
        if builtin in ('round', 'min', 'max'): return numeric(builtin, args)
        if builtin == 'errorinfo':
            if vm.current_error is None: return None
            r = vm.current_error.result
            location = f"{r.get('path', vm.program.path)}:{r.get('line', vm.line)}"
            return ErrorInfo(r['error'], (location, *r.get('details', ())))
        if builtin in ('detailederrordescription', 'brieferrordescription'):
            if not isinstance(args[0], ErrorInfo): raise Failure('scenario_failed', 'ErrorInfo is required.')
            return args[0].detailed() if builtin == 'detailederrordescription' else args[0].description
        if builtin in ('strsplit', 'strconcat', 'strreplace', 'find', 'strfind', 'char', 'triml', 'trimr',
                       'strstartswith', 'strendswith', 'strlinecount', 'strgetline', 'charcode',
                       'strlen', 'left', 'right', 'mid'):
            from compatible_scenarios.shared.bsl.strings import call
            return call(builtin, args, self.text, lambda: self.tick(vm.program.path, vm.line))
        if builtin in ('date', 'currentdate', 'year', 'month', 'day', 'hour', 'minute', 'second',
                       'begofday', 'endofday', 'begofmonth', 'endofmonth', 'addmonth',
                       'begofyear', 'endofyear', 'begofquarter', 'endofquarter', 'begofweek', 'endofweek', 'weekday', 'dayofyear'):
            from compatible_scenarios.shared.bsl.dates import date_function
            return date_function(builtin, args)
        if builtin == 'string' and type(args[0]) is datetime:
            from compatible_scenarios.tester.regional import for_host
            return for_host(self, args[0])
        if builtin == 'stop':
            reason = self.text(args[0]) if args and args[0] is not None else 'Scenario stopped.'
            def stop(): raise Failure('scenario_stopped', reason)
            return self.operation('stop', {'reason': reason}, stop)
        if builtin == 'logerror':
            if len(self.diagnostics) >= 1000: raise Failure('scenario_limit', 'Scenario diagnostic limit exceeded.')
            entry = dict(message=self.text(args[0]), **self.location)
            self.diagnostics.append(entry)
            def record():
                from compatible_scenarios.tester.files import screenshot
                try: entry['screenshot'] = screenshot(self.ui, [], filename=True)
                except Failure as exc:
                    entry['screenshot_error'] = dict(exc.result)
                    if exc.result['code'] in ('scenario_timeout', 'scenario_limit', 'connection_closed'): raise
                return dict(ok=False, code='logged_error', error=entry['message'], **entry)
            self.operation('log_error', {'text': entry['message']}, record, raise_on_error=False)
            return None
        if builtin == 'errordescription':
            return vm.current_error.result['error'] if vm.current_error is not None else ''
        if builtin == 'type':
            if not isinstance(args[0], str) or args[0].casefold() not in TYPES:
                raise Failure('unsupported_scenario', f'Unsupported BSL type {args[0]!r}.')
            return TypeValue(TYPES[args[0].casefold()])
        if builtin == 'typeof':
            if isinstance(args[0], SortDirection): return TypeValue('sortdirection')
            from compatible_scenarios.tester.connections import AppData
            if isinstance(args[0], AppData): return TypeValue('structure')
            value = args[0]
            from compatible_scenarios.shared.bsl.pictures import Picture
            from compatible_scenarios.shared.bsl.callbacks import CallbackDescription
            if isinstance(value, Picture): return TypeValue('picture')
            if isinstance(value, CallbackDescription): return TypeValue('callbackdescription')
            if isinstance(value, ErrorInfo): return TypeValue('errorinfo')
            if isinstance(value, MessageStatus): return TypeValue('messagestatus')
            if isinstance(value, RoundMode): return TypeValue('roundmode')
            if value is NULL: return TypeValue('null')
            from compatible_scenarios.tester.files import BinaryData
            if isinstance(value, BinaryData): return TypeValue('binarydata')
            if isinstance(value, MainWindow): return TypeValue('testedclientapplicationwindow')
            if isinstance(value, Application): return TypeValue('testedapplication')
            if isinstance(value, UIObject):
                from compatible_scenarios.tester.windows import object_type
                kind = {'ManagedForm': 'testedform', 'Table': 'testedformtable', 'EditField': 'testedformfield',
                        'Button': 'testedformbutton', 'Group': 'testedformgroup', 'Decoration': 'testedformdecoration'}.get(value.data.get('class'))
                kind = object_type(value.data) or kind
            else:
                kind = {str:'string', Decimal:'number', bool:'boolean', type(None):'undefined',
                        list:'array', Structure:'structure', Map:'map', TypeValue:'type', datetime:'date'}.get(type(value))
            kind = TYPE_NAMES.get(type(value), kind)
            if kind is None: raise Failure('unsupported_scenario', 'TypeOf is not implemented for this value.')
            return TypeValue(kind)
        if builtin in ('call', 'run'):
            try:
                program = self.repository.load_dynamic(args[0], vm.program, builtin == 'run', args[2] if len(args) > 2 else None)
                return self.execute(program, args[1] if len(args) > 1 else None)
            except Failure as exc:
                exc.result.setdefault('call_stack', []).append(dict(path=str(vm.program.path), line=vm.line))
                raise
        if builtin == 'pause': return self.pause(*args)
        if builtin in ('message', 'vstudio'):
            status = args[1] if len(args) > 1 else MessageStatus.WITHOUT_STATUS
            if status is None: status = MessageStatus.WITHOUT_STATUS
            if not isinstance(status, MessageStatus): raise Failure('scenario_failed', 'A MessageStatus value is required.')
            text = self.text(args[0])
            def record():
                self.message(text)
                entry = dict(text=text, status=status.value, **self.location)
                self.message_details.append(entry)
                return dict(ok=True, **entry)
            self.operation('message', dict(text=text, status=status.value), record)
            return None
        funcs = {'string': string, 'number': lambda x: Decimal(x), 'int': lambda x: Decimal(int(x)),
                 'trimall': lambda x: x.strip(),
                 'upper': lambda x: x.upper(), 'lower': lambda x: x.lower(),
                 'isblankstring': lambda x: not x.strip()}
        return funcs[builtin](*args)

    def message(self, value):
        if len(self.messages) >= 1000: raise Failure('scenario_limit', 'Scenario message limit exceeded.')
        self.messages.append(self.text(value))

    def text(self, value):
        if type(value) is datetime:
            from compatible_scenarios.tester.regional import for_host
            return for_host(self, value)
        return string(value)

    def attribute(self, obj, name):
        from compatible_scenarios.shared.bsl.strings import Constants
        from compatible_scenarios.shared.bsl.builtins import ErrorInfo
        if isinstance(obj, ErrorInfo): return obj.get(name)
        if isinstance(obj, Constants): return obj.get(name)
        return self.ui.attribute(obj, name)
    def method(self, obj, name, args):
        if isinstance(obj, ParametersSpace): return obj.method(name, args)
        return self.ui.method(obj, name, args)


def scenario_name(path, directory):
    relative = path.relative_to(directory)
    name = relative.name
    for suffix in ('.1cm.bsl', '.1c.bsl', '.bsl'):
        if name.endswith(suffix):
            name = name[:-len(suffix)]; break
    parts = list(relative.parts[:-1])
    if name.endswith('.dir') and parts and name[:-4] == parts[-1]:
        return '.'.join(parts)
    return '.'.join([*parts, name])


def suite_inputs(path, roots, applications=None, application_name='', metadata_path=None):
    excluded, excluded_count = [], 0
    if isinstance(path, list):
        if not path or len(path) > 256:
            raise Failure('invalid_scenario_path', 'Provide a nonempty list of at most 256 scenario files.')
        if any(not isinstance(p, (str, os.PathLike)) or not str(p) for p in path):
            raise Failure('invalid_scenario_path', 'Every list entry must be a scenario file path.')
        files = [Path(p).expanduser().resolve() for p in path]
        if any(not p.is_file() or p.suffix.lower() != '.bsl' for p in files):
            raise Failure('invalid_scenario_path', 'Every list entry must be an existing .bsl file.')
        default_roots = list(dict.fromkeys(p.parent for p in files))
    else:
        directory = Path(path).expanduser().resolve()
        default_roots, files, names = [directory], [], set()
        for index, candidate in enumerate(directory.rglob('*.bsl')):
            if index >= 10000: raise Failure('scenario_limit', 'A suite directory contains more than 10000 BSL files.')
            resolved = candidate.resolve()
            if not resolved.is_relative_to(directory):
                raise Failure('invalid_scenario_path', 'A suite file resolves outside its directory.', path=candidate)
            metadata = read_metadata(candidate)
            kind = metadata.get('Type')
            reason = None
            if metadata.get('DeletionMark') is True: reason = 'deletion_mark'
            elif kind is not None and kind != 'Scenario': reason = 'not_a_scenario'
            elif kind is None:
                if not candidate.name.endswith('.1c.bsl'): reason = 'scenario_type_missing'
                elif '.dir.' in candidate.name and candidate.stat().st_size <= 1024*1024 and not candidate.read_text(encoding='utf-8-sig').strip():
                    reason = 'empty_container'
            if reason:
                excluded_count += 1
                if len(excluded) < 1000: excluded.append(dict(path=str(candidate), reason=reason))
                continue
            name = scenario_name(candidate, directory)
            if name in names: raise Failure('ambiguous_scenario', f'Multiple suite files describe {name!r}.')
            names.add(name); files.append(resolved)
            if len(files) > 256: raise Failure('scenario_limit', 'A suite contains more than 256 tests.')
        files.sort(key=lambda p: (scenario_name(p, directory).casefold(), scenario_name(p, directory)))
        excluded.sort(key=lambda item: item['path'].casefold())
    if not files:
        raise Failure('no_scenarios', 'No scenarios were found. Folder collection requires Type="Scenario" metadata or legacy .1c.bsl files.',
                      excluded=excluded, excluded_count=excluded_count)
    selected_roots = roots or [str(p) for p in default_roots]
    repository = Repository(files[0], selected_roots, applications, application_name, metadata_path)
    for file in files:
        if not any(file.is_relative_to(r) for r in repository.roots):
            raise Failure('invalid_scenario_path', 'A scenario is outside the supplied roots.', path=file)
    return repository, files, excluded, excluded_count


def sources(repository):
    return [dict(path=str(p.path), sha256=p.sha256, **({'adapter':p.adapter} if p.adapter else {})) for p in repository.programs.values()]


def execute_program(R, client, repository, program, parameters, timeout, application_name, disconnected_root=None):
    started = time.monotonic()
    runner = Runner(R, client, repository, timeout, application_name, disconnected_root)
    old_deadline = getattr(client, '_io_deadline', None)
    client._io_deadline = min(runner.deadline, old_deadline) if old_deadline is not None else runner.deadline
    try:
        with runner.session():
            result = value_out(runner.execute(program, value_in(parameters)))
        out = dict(ok=True, framework='tester', result=result)
    except Failure as exc: out = exc.result
    except (OSError, ValueError, RuntimeError, RecursionError) as exc:
        out = dict(ok=False, code='scenario_failed', error=str(exc))
    finally: client._io_deadline = old_deadline
    out.update(steps=runner.steps, steps_total=runner.operations, steps_truncated=runner.operations > len(runner.steps),
               messages=runner.messages, message_details=runner.message_details, diagnostics=runner.diagnostics, elapsed=round(time.monotonic()-started, 3))
    return out, runner.connections.continuation()


def run_suite(R, path, roots, parameters, timeout, check_only, application_name, stop_on_failure, applications=None, metadata_path=None, environments_path=None):
    repository, files, excluded, excluded_count = suite_inputs(path, roots, applications, application_name, metadata_path)
    repository.environments_path = environments_path
    tests = []
    for file in files:
        try:
            repository.load_checked(file)
            tests.append(dict(path=str(file), ok=True, status='checked'))
        except Failure as exc:
            tests.append(dict(exc.result, path=str(file), source_path=exc.result.get('path', str(file)), status='failed', phase='preflight'))
        except (OSError, ValueError, RecursionError) as exc:
            tests.append(dict(ok=False, code='invalid_scenario', error=str(exc), path=str(file), status='failed', phase='preflight'))
    preflight_failed = any(not test['ok'] for test in tests)
    if preflight_failed and not check_only:
        for test in tests:
            if test['ok']:
                test.pop('ok')
                test.update(status='skipped', reason='preflight_failed')
    if not preflight_failed and not check_only:
        client = R._need()
        disconnected_root = None
        deadline, stopped, returned_steps = time.monotonic() + timeout, None, 0
        for i, file in enumerate(files):
            if stopped:
                tests[i] = dict(path=str(file), status='skipped', reason=stopped)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                tests[i] = dict(path=str(file), status='skipped', reason='scenario_timeout')
                stopped = 'scenario_timeout'; continue
            journal = R._state.get('_call_journal') if R.LOGGING else None
            call, token = None, None
            if journal is not None and journal.active:
                try:
                    call = journal.begin('compatible_scenario', 'test', R._profiles.redact(dict(path=str(file), index=i+1)), {})
                    token = R._call_logging.CURRENT.set(call)
                except Exception: journal.pause('log_write_failed')
            try:
                client = disconnected_root[3] if disconnected_root is not None else R._need()
                result, disconnected_root = execute_program(
                    R, client, repository, repository.programs[file], parameters, remaining, application_name, disconnected_root)
                result['steps'] = result['steps'][:max(0, 1000-returned_steps)]
                returned_steps += len(result['steps'])
                result['steps_truncated'] = result['steps_total'] > len(result['steps'])
                tests[i] = dict(result, path=str(file), status='passed' if result['ok'] else 'failed')
                if result.get('path'): tests[i]['source_path'] = result['path']
                if not result['ok'] and (stop_on_failure or result.get('code') in ('scenario_timeout','scenario_limit','connection_closed')):
                    stopped = result.get('code', 'test_failed')
                if disconnected_root is None:
                    try: current_client = R._need()
                    except RuntimeError: stopped = 'connection_closed'
                    else:
                        if current_client is None or getattr(current_client, 'closed', False) or getattr(current_client, '_interrupted', False):
                            stopped = 'connection_closed'
                        if getattr(current_client, '_pending', 0): stopped = 'connection_pending'
            finally:
                if call is not None:
                    try:
                        call['result'] = R._profiles.redact(tests[i]); journal.finish(call)
                    except Exception: journal.pause('log_write_failed')
                if token is not None: R._call_logging.CURRENT.reset(token)
    summary = {status: sum(t['status'] == status for t in tests) for status in ('passed','failed','skipped','checked')}
    summary['total'] = len(tests)
    out = dict(ok=not summary['failed'] and not summary['skipped'], framework='tester', suite=True,
               tests=tests, summary=summary, sources=sources(repository), excluded=excluded,
               excluded_count=excluded_count, excluded_truncated=excluded_count > len(excluded))
    if check_only: out.update(checked=True, dynamic_calls=repository.dynamic_calls)
    if not out['ok']:
        out.update(code='suite_preflight_failed' if preflight_failed else 'suite_failed',
                   error='Suite preflight failed.' if preflight_failed else 'The suite contains failed or skipped tests.')
    return out


def run(R, *, path, framework='tester', parameters=None, roots=None, timeout=1800, check_only=False, application_name='', stop_on_failure=False,
        applications=None, metadata_path=None, environments_path=None):
    started, runner = time.monotonic(), None
    try:
        if framework != 'tester': raise Failure('unsupported_framework', 'Supported framework: tester.')
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 86400:
            raise Failure('invalid_timeout', 'timeout must be greater than 0 and at most 86400 seconds.')
        if type(stop_on_failure) is not bool: raise Failure('invalid_scenario', 'stop_on_failure must be Boolean.')
        if not isinstance(application_name, str): raise Failure('invalid_scenario', 'application_name must be a string.')
        from compatible_scenarios.tester.environments import path_argument
        environments_path = path_argument(environments_path)
        if not isinstance(path, (str, os.PathLike, list)) or not path:
            raise Failure('invalid_scenario_path', 'Provide a scenario file, directory or list of files.')
        if isinstance(path, list) or Path(path).expanduser().is_dir():
            out = run_suite(R, path, roots, parameters, timeout, check_only, application_name, stop_on_failure, applications, metadata_path, environments_path)
            out['elapsed'] = round(time.monotonic()-started, 3)
            return out
        repository = Repository(path, roots, applications, application_name, metadata_path)
        repository.environments_path = environments_path
        program = repository.load()
        sources = [dict(path=str(p.path), sha256=p.sha256, **({'adapter': p.adapter} if p.adapter else {})) for p in repository.programs.values()]
        if check_only: return dict(ok=True, framework=framework, checked=True, sources=sources, dynamic_calls=repository.dynamic_calls)
        client = R._need()
        runner = Runner(R, client, repository, timeout, application_name)
        old_deadline = getattr(client, '_io_deadline', None)
        client._io_deadline = min(runner.deadline, old_deadline) if old_deadline is not None else runner.deadline
        try:
            with runner.session():
                result = value_out(runner.execute(program, value_in(parameters)))
        finally: client._io_deadline = old_deadline
        sources = [dict(path=str(p.path), sha256=p.sha256, **({'adapter':p.adapter} if p.adapter else {})) for p in repository.programs.values()]
        out = dict(ok=True, framework=framework, result=result, sources=sources)
    except Failure as exc: out = exc.result
    except (OSError, ValueError, RecursionError) as exc:
        out = dict(ok=False, code='invalid_scenario' if runner is None else 'scenario_failed', error=str(exc))
    if runner is not None:
        out.update(steps=runner.steps, steps_total=runner.operations, steps_truncated=runner.operations > len(runner.steps), messages=runner.messages, message_details=runner.message_details, diagnostics=runner.diagnostics)
    out['elapsed'] = round(time.monotonic()-started, 3)
    return out


class ParametersSpace:
    def method(self, name, args):
        if name != 'jobrecord' or args:
            raise Failure('unsupported_scenario', 'ParametersSpace supports JobRecord() without arguments.')
        return Structure(Scenario=None, PinApplication=None, PinVersion=None, CloseAllAfter=True, Disconnect=True)
