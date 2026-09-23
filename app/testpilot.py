"""Synchronous Python API for 1C Testpilot. No MCP server is created or started."""
from functools import partial
from contextlib import contextmanager
import inspect
import math
import sys
import threading
import time
from typing import Mapping

import _runtime as R
from _screenshots import Screenshot

__all__ = ['Client', 'Element', 'ActionError', 'Screenshot']


class ActionError(RuntimeError):
    """An action failed; result preserves partial results and 1C diagnostics."""
    def __init__(self, action, result):
        self.action, self.result = action, result
        self.code = result.get('code', 'action_failed')
        detail = result.get('error') or result.get('message') or result.get('connect_error') or self.code
        super().__init__(f'{action}: {detail}')


class Client:
    """One test-client connection. Use a context manager to release owned resources.

    Client(profile='demo') starts that profile on entering the context. connect()
    attaches to an existing client; launch_client() starts an owned process.
    launch_client(code_epf='.../Testpilot.epf') prepares code, queries, metadata and custom BSL functions.
    The path can come from the profile or TC1C_CODE_EPF; code_epf='' skips the processing.
    close() stops owned processes and only disconnects from externally started ones.
    All actions are also available as call('action', **parameters), returning Python
    dictionaries (or Screenshot for get_screenshot). Failed actions raise ActionError.
    """
    def __init__(self, profile=None):
        self.profile = profile
        self._runtime = R.Runtime()
        self._lock = threading.RLock()
        self._generation = 0
        self._closed = False

    @property
    def connection_id(self):
        entries = self._runtime.pool.list()
        return entries[0]['connection_id'] if entries else None

    def __enter__(self):
        if self.profile is not None and self.connection_id is None:
            try:
                self.start()
            except BaseException:
                self.__exit__(*sys.exc_info())
                raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            # Preserve the original test failure, but do not hide cleanup failures.
            if hasattr(exc, 'add_note'):
                exc.add_note(f'Testpilot cleanup failed: {cleanup}')
            else:
                import warnings
                warnings.warn(f'Testpilot cleanup failed: {cleanup}', RuntimeWarning)

    def start(self, **overrides):
        """Launch/connect using the named YAML profile, with explicit overrides."""
        profiles = self.call('list_profiles')['profiles']
        profile = next((p for p in profiles if p['name'] == self.profile), None)
        if profile is None:
            raise ActionError('start', {'code': 'profile_not_found', 'error': 'Choose a name from list_profiles.'})
        return self.call(profile['action'], profile=self.profile, **overrides)

    def call(self, action, *, check=True, **arguments):
        """Call an existing Testpilot action. check=False returns expected refusals."""
        control = action == 'list_connections' or (action == 'disconnect' and arguments.get('force') is True)
        acquired = False if control else self._lock.acquire(timeout=self._runtime.pool.wait_timeout)
        if not control and not acquired:
            result = self._busy_result()
            if check: raise ActionError(action, result)
            return result
        try:
            if self._closed:
                raise ActionError(action, {'code': 'client_closed', 'error': 'Create a new Client after close().'})
            if action not in R._ACTION_GROUP:
                raise ValueError(f'Unknown Testpilot action: {action}')
            if 'connection_id' in arguments:
                raise ValueError('This Client selects its own connection; omit connection_id.')
            if action == 'get_screenshot' and not R.SCREENSHOTS:
                result = {'ok': False, 'code': 'screenshots_disabled', 'error': 'Screenshots are disabled.'}
            else:
                self._validate(action, arguments)
                previous = self._connected_object()
                try:
                    result = self._runtime.call(action, arguments)
                except (OSError, RuntimeError) as exc:
                    result = {'ok': False, 'code': 'operation_failed', 'error': str(exc)}
                finally:
                    if self._connected_object() is not previous:
                        self._generation += 1
            if check and isinstance(result, dict) and result.get('ok') is False:
                raise ActionError(action, result)
            return result
        finally:
            if acquired: self._lock.release()

    def _busy_result(self):
        entries = self._runtime.pool.list()
        result = {'ok': False, 'code': 'connection_busy', 'error': 'The previous action is still running.',
                  'queue_timeout': self._runtime.pool.wait_timeout}
        if entries:
            result.update({k: entries[0][k] for k in ('connection_id', 'busy', 'active_action', 'active_seconds')})
        return result

    @contextmanager
    def _access(self, action):
        if not self._lock.acquire(timeout=self._runtime.pool.wait_timeout):
            raise ActionError(action, self._busy_result())
        previous, generation = self._connected_object(), self._generation
        try: yield
        except R._connections.ConnectionError as exc:
            raise ActionError(action, dict(ok=False, code=exc.code, error=str(exc), **exc.details)) from exc
        finally:
            # Element verification can finish deferred disconnect cleanup before
            # reaching Client.call. Invalidate its elements on that path as well.
            if self._generation == generation and self._connected_object() is not previous:
                self._generation += 1
            self._lock.release()

    def _connected_object(self):
        with self._runtime.pool.lock:
            return next((entry.state.get('client') for entry in self._runtime.pool.entries.values()), None)

    def wait_until(self, description, probe, *, condition, timeout=120, interval=1):
        """Poll a read until condition(result) is true; return that result.

        Explicitly groups probes in the journal. Exceptions abort the wait; only
        a false condition is retried. timeout bounds polling, not an in-flight call.
        """
        if not isinstance(description, str) or not description.strip():
            raise ValueError('description must be a nonempty string')
        if not callable(probe) or not callable(condition):
            raise TypeError('probe and condition must be callable')
        for name, value in (('timeout', timeout), ('interval', interval)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0 or (name == 'interval' and value == 0)):
                raise ValueError(f'{name} must be finite and ' + ('positive' if name == 'interval' else 'nonnegative'))
        with self._access('wait_until'):
            if self._closed:
                raise ActionError('wait_until', dict(ok=False, code='client_closed'))
            if R._call_logging.WAIT.get() is not None:
                raise ValueError('Nested waits are not supported')
            journal = next((e.state.get('_call_journal') for e in self._runtime.pool.entries.values()), None)
            identity = None
            if R.LOGGING and journal is not None and journal.active:
                try:
                    identity = journal.begin_wait(R._profiles.redact(description), timeout, interval)
                except Exception:
                    journal.pause('log_write_failed')
            token = R._call_logging.WAIT.set((journal, identity))
            started = time.monotonic()
            deadline = started + timeout
            attempts, last, outcome = 0, None, None
            try:
                while True:
                    if attempts and time.monotonic() >= deadline:
                        raise ActionError('wait_until', dict(ok=False, code='wait_timeout',
                            error='The condition was not met before the polling deadline.', last_result=last))
                    attempts += 1
                    last = probe()
                    if condition(last):
                        outcome = dict(ok=True, value=last)
                        return last
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ActionError('wait_until', dict(ok=False, code='wait_timeout',
                            error='The condition was not met before the polling deadline.', last_result=last))
                    time.sleep(min(interval, remaining))
            except BaseException as exc:
                outcome = (exc.result if isinstance(exc, ActionError) else
                           dict(ok=False, exception=type(exc).__name__, error=str(exc)))
                raise
            finally:
                R._call_logging.WAIT.reset(token)
                if identity is not None:
                    try:
                        journal.finish_wait(identity, R._profiles.redact(outcome), attempts, time.monotonic() - started)
                    except Exception:
                        journal.pause('log_write_failed')

    @staticmethod
    def _validate(action, arguments):
        """Validate before side effects, without depending on MCP's merged schema."""
        from pydantic import TypeAdapter, ValidationError
        fn = R._ACTIONS[R._ACTION_GROUP[action]][action]
        params = inspect.signature(fn).parameters
        for name, value in arguments.items():
            if name == 'profile' and action in ('connect', 'launch_client'):
                annotation = str
            elif name == 'code_epf' and action == 'launch_client':
                annotation = str
            elif name in params:
                annotation = params[name].annotation
            else:
                raise TypeError(f'{action}: unexpected parameter {name!r}')
            if value is not None and annotation is not inspect.Parameter.empty:
                try:
                    TypeAdapter(annotation).validate_python(value, strict=True)
                except ValidationError:
                    # ValidationError's text includes the input, possibly a password.
                    raise TypeError(f'{action}: invalid value for parameter {name!r}') from None

    def __getattr__(self, name):
        if name in R._ACTION_GROUP:
            return partial(self.call, name)
        raise AttributeError(name)

    def find_objects(self, **criteria):
        with self._access('find_objects'):
            return [Element(self, obj) for obj in self.call('find_objects', **criteria)['objects']]

    def find_object(self, **criteria):
        """Find exactly one object; fail rather than choosing between ambiguous matches."""
        objects = self.find_objects(**criteria)
        if len(objects) != 1:
            raise ActionError('find_object', dict(ok=False,
                code='object_not_found' if not objects else 'ambiguous_object',
                error=f'Expected one element, found {len(objects)}.',
                objects=[o.info for o in objects]))
        return objects[0]

    def read_fields(self, targets, properties=None):
        with self._access('read_fields'):
            return self.call('read_fields', targets=[self._address(o) for o in targets], properties=properties)

    def set_fields(self, values: Mapping):
        """Fill {Element: text_or_boolean}; the existing batch handles completion."""
        if not isinstance(values, Mapping):
            raise TypeError('set_fields expects a mapping of Element to text or boolean.')
        with self._access('set_fields'):
            entries = [{**self._address(element), 'checked' if type(value) is bool else 'text': value}
                       for element, value in values.items()]
            return self.call('set_fields', entries=entries)

    def _address(self, element):
        if not isinstance(element, Element) or element.client is not self:
            raise ValueError('The element must belong to this Client.')
        return element.address

    def close(self):
        """Release this session; a failed stop retains ownership so close can be retried."""
        with self._access('close'):
            if self._closed:
                return
            entries = self._runtime.pool.list()
            if entries:
                self.call('stop_client' if entries[0]['launched'] else 'disconnect')
            self._runtime.pool.close()
            self._runtime.snapshots.entries.clear()
            self._runtime.snapshots.size = 0
            self._closed = True
            self._generation += 1


class Element:
    """An element discovered in this session; rediscover after reconnecting."""
    def __init__(self, client, info):
        self.client, self.info = client, dict(info)
        self._generation = client._generation

    @property
    def address(self):
        if self.client._closed or self._generation != self.client._generation:
            raise ActionError('element', {'code': 'stale_element', 'error': 'Find the element again after reconnecting.'})
        return {'key': self.info['key'], 'handle': self.info.get('handle')}

    def _live_object(self, action, address):
        # The caller holds the client lock through verification and the operation.
        pool = self.client._runtime.pool
        with pool.lock:
            connection = next(iter(pool.entries.values()), None)
        if connection is None:
            raise ActionError(action, dict(ok=False, code='not_connected', error='No connected test client.'))
        with pool.use(connection, action=action):
            c = R._state.get('client')
            if c is None:
                raise ActionError(action, dict(ok=False, code='not_connected', error='No connected test client.'))
            track = getattr(c, '_track', None)
            try:
                c._track = None
                found = R._ref_live_object(c, address['key'])
            except R.tc1c.OperationError as exc:
                result = exc.result()
                if exc.code == 'target_unavailable':
                    result.update(code='element_unavailable',
                                  error='The element is no longer available. Find it again.')
                raise ActionError(action, result) from exc
            except R._code_execution.Failure as exc:
                raise ActionError(action, exc.result) from exc
            except (OSError, RuntimeError) as exc:
                raise ActionError(action, {'ok': False, 'code': 'operation_failed',
                                          'error': str(exc)}) from exc
            finally:
                c._track = track
        if found is None or (address['handle'] is not None and R._collection_parent(address['key']) is not None
                              and found.get('handle') != address['handle']):
            raise ActionError(action, {'code': 'element_unavailable', 'error': 'The element is no longer available. Find it again.'})
        return found

    def call(self, action, **arguments):
        with self.client._access(action):
            address = self.address
            if any(k in arguments for k in ('key', 'handle', 'root_key', 'ref', 'root_ref')):
                raise TypeError('The Element selects its own target.')
            if action not in R._ACTION_GROUP:
                raise ValueError(f'Unknown Testpilot action: {action}')
            fn = R._ACTIONS[R._ACTION_GROUP[action]][action]
            params = inspect.signature(fn).parameters
            if 'key' not in params:
                raise TypeError(f'{action} is not an element action; call it on Client.')
            # Confirm membership/handle just as the MCP ref path does. Never redirect
            # an old element to a newly created control with the same address.
            found = self._live_object(action, address)
            kw = {'key': address['key']}
            if 'handle' in params:
                kw['handle'] = found.get('handle') or address['handle']
                if kw['handle'] is None:
                    raise ActionError(action, {'code': 'element_unavailable', 'error': 'The element has no action handle.'})
            return self.client.call(action, **kw, **arguments)

    def __getattr__(self, name):
        if name in R._ACTION_GROUP:
            return partial(self.call, name)
        raise AttributeError(name)

    def find_objects(self, **criteria):
        with self.client._access('find_objects'):
            address = self.address
            self._live_object('find_objects', address)
            return self.client.find_objects(root_key=address['key'], **criteria)

    def find_object(self, **criteria):
        with self.client._access('find_object'):
            address = self.address
            self._live_object('find_object', address)
            return self.client.find_object(root_key=address['key'], **criteria)

    def get_text(self):
        return self._value('get_text', 'text')

    def get_data_presentation(self):
        return self._value('get_data_presentation', 'presentation')

    def _value(self, action, name):
        result = self.call(action)
        if result.get(name) is None:
            raise ActionError(action, {**result, 'ok': False, 'code': 'value_unavailable',
                                      'error': 'The value could not be read; this does not mean it is empty.'})
        return result[name]

    def __repr__(self):
        return f"Element({self.info.get('name') or self.info.get('title') or self.info['key']!r})"
