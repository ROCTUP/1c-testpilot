"""Scenario connection selection, explicit disconnection and owned-socket cleanup."""
from contextlib import contextmanager
from decimal import Decimal
import time
import _connections
from compatible_scenarios.shared.bsl.language import Failure, Structure


class AppData(Structure):
    """The supported connection properties of Tester's application context."""
    def __init__(self, connections):
        self.connections = connections
        record = connections.root
        source = record[1] if record else connections.host.client
        computer, port = getattr(source, 'host', None), getattr(source, 'port', None)
        super().__init__(Computer=computer, Port=Decimal(port) if port is not None else None,
                         Connected=connections.connected(), ConnectedHost=computer,
                         ConnectedPort=Decimal(port) if port is not None else None)

    def __getitem__(self, key):
        key = self.actual(key)
        if key == 'Connected': return super().__getitem__(key) and self.connections.connected()
        return super().__getitem__(key)

    def get(self, key, default=None):
        return self[key] if self.actual(key) in self else default

    def items(self): return [(key, self[key]) for key in self]

    def __setitem__(self, key, value):
        key = self.actual(key)
        if key in ('Computer', 'Port'):
            validate_endpoint(value if key == 'Port' else None, value if key == 'Computer' else None)
        super().__setitem__(key, value)

    def attempt(self, computer, port):
        dict.__setitem__(self, 'ConnectedHost', computer)
        dict.__setitem__(self, 'ConnectedPort', Decimal(port))


def validate_endpoint(port, computer):
    if port is not None and (not isinstance(port, Decimal) or not port.is_finite() or port != int(port) or not 1 <= port <= 65535):
        raise Failure('invalid_port', 'Connect Port must be an integer from 1 to 65535.')
    if computer is not None and (not isinstance(computer, str) or not computer.strip() or '\x00' in computer):
        raise Failure('invalid_host', 'Connect Computer must be a nonempty host name.')


class Connections:
    def __init__(self, host, disconnected_root=None):
        self.host, self.R = host, host.R
        self.current = None
        self.owned = []
        self.records = []
        self.root = None
        self.detached = False
        self.disconnected_root = disconnected_root
        if disconnected_root is not None:
            self.current = self.root = disconnected_root
            self.records.append(disconnected_root)
            self.detached = True
            return
        if hasattr(self.R, '_connection_pool'):
            pool = self.R._connection_pool()
            with pool.lock:
                entry = next((e for e in pool.entries.values() if e.state.get('client') is host.client), None)
            if entry is not None:
                self.current = (pool, entry, self.R._execution.get(), host.client)
                self.root = self.current
                self.records.append(self.current)

    @contextmanager
    def bound(self, record=None, *, connecting=False):
        if record is None and self.detached:
            raise Failure('not_connected', 'The scenario is disconnected. Call Connect before using the application.')
        record = record or self.current
        if record is None:
            yield
            return
        pool, entry, execution, client = record
        token = self.R._execution.set(execution)
        try:
            with pool.use(entry, action='run_compatible_scenario', timeout=self.host.deadline-time.monotonic()):
                if not connecting and entry.state.get('client') is not client:
                    raise Failure('connection_changed', 'The scenario connection changed. Call Connect again.')
                old = getattr(client, '_io_deadline', None) if client else None
                if client is not None:
                    client._io_deadline = min(old, self.host.deadline) if old is not None else self.host.deadline
                try: yield
                finally:
                    if client is not None: client._io_deadline = old
        except _connections.ConnectionError as exc:
            raise Failure(exc.code, str(exc), **exc.details) from exc
        finally:
            self.R._execution.reset(token)

    def connect(self, port, computer):
        port = self.appdata['Port'] if port is None else port
        computer = self.appdata['Computer'] if computer is None else computer
        validate_endpoint(port, computer)
        if self.current is None and self.connected() and port is None and computer is None: return
        if self.current is None:
            raise Failure('not_connected', 'Connect requires a registered Testpilot connection.')
        if port is None or computer is None:
            raise Failure('not_connected', 'Connect requires Computer and Port in AppData or explicit arguments.')
        self.appdata.attempt(computer, port)
        endpoint = _connections.endpoint(computer, int(port))
        # Previously visited connections, then other connections of the calling MCP session.
        record = next((r for r in self.records if (r[1].host, r[1].port) == endpoint
                       and r[0].entries.get(r[1].id) is r[1]), None)
        if record is None:
            root_pool, _, root_execution, _ = self.root
            with root_pool.lock:
                entry = next((e for e in root_pool.entries.values() if (e.host, e.port) == endpoint), None)
            if entry is not None:
                if entry.starting: raise Failure('connection_in_use', 'The requested client is still connecting.')
                record = (root_pool, entry, root_execution, entry.state.get('client'))
            else:
                if len(self.records) >= self.R._pool.limit:
                    raise Failure('connection_limit_exceeded', 'The scenario connection limit was reached.')
                # A Python Client owns one connection. Extra scenario sockets get their own Runtime.
                target_execution = self.R.Runtime() if root_execution is not None else None
                pool = target_execution.pool if target_execution is not None else root_pool
                try: entry = pool.reserve(*endpoint)
                except _connections.ConnectionError as exc:
                    raise Failure(exc.code, str(exc), **exc.details) from exc
                record = (pool, entry, target_execution, entry.state.get('client'))
                if entry.starting: self.owned.append(record)
        pool, entry, execution, client = record
        with self.bound(record, connecting=True):
            client = entry.state.get('client')
            if not _connections.client_connected(client):
                entry.state['_launch_deadline'] = self.host.deadline
                try:
                    result = self.R.tc_connect(host=endpoint[0], port=endpoint[1],
                                               version=getattr(self.host.client, 'platform_version', None))
                    if isinstance(result, dict) and result.get('ok') is False:
                        raise Failure(result.get('code', 'connection_failed'), result.get('error', 'Connect failed.'))
                    client = entry.state['client']
                except self.R.tc1c.ConnectionFailure as exc:
                    result = exc.result()
                    raise Failure(result['code'], result['error']) from exc
                finally:
                    entry.state.pop('_launch_deadline', None)
                    if entry is not self.root[1]: pool.finish(entry)
            record = (pool, entry, execution, client)
        self.records = [r for r in self.records if r[1] is not entry] + [record]
        self.current = record
        self.detached = False
        dict.__setitem__(self.appdata, 'Connected', True)
        if entry is self.root[1]:
            self.root = record
            self.disconnected_root = None
        if self.host.client is not client:
            from compatible_scenarios.tester.ui import Adapter, Application, MainWindow, OBJECT_OWNER
            self.host.client = client
            OBJECT_OWNER.set(client)
            self.host.ui = Adapter(self.host)
            self.host.globals['app'] = self.host.globals['приложение'] = Application()
            self.host.globals['mainwindow'] = self.host.globals['главноеокно'] = MainWindow()
            self.host.globals['currentsource'] = self.host.globals['текущийобъект'] = None

    def connected(self):
        if self.detached: return False
        client = self.host.client
        if not _connections.client_connected(client): return False
        if self.current is None: return client is not None
        pool, entry, _, _ = self.current
        return pool.entries.get(entry.id) is entry and entry.state.get('client') is client

    def application_method(self, obj, name):
        """Direct application methods retain the application object and its endpoint."""
        if name in ('connect', 'установитьсоединение'):
            owner = obj.owner
            port, computer = getattr(owner, 'port', None), getattr(owner, 'host', None)
            if port is None or computer is None:
                raise Failure('not_connected', 'The application has no connection address.')
            self.connect(Decimal(port), computer)
            obj.owner = self.host.client
            self.host.globals['app'] = self.host.globals['приложение'] = obj
        else:
            self.host.ui.check_owner(obj)
            self.disconnect(clear_globals=False)

    def disconnect(self, close=False, *, clear_globals=True):
        if self.detached: return
        with self.bound():
            if close:
                # Match Tester: request normal closure of the main window before
                # disconnecting; never kill a process connected by host/port.
                from compatible_scenarios.tester.windows import main_window
                import _code_execution
                import guids as G
                key = main_window(self.host.ui).data['key']
                _code_execution.guard(self.host.client, G.CLOSE, key)
                for kind in ('action', 'commit'):
                    result = self.host.client.send_cmd(G.CLOSE, key, kind=kind, middle=b'')
                    if not result.get('ok'):
                        raise Failure(result.get('code', 'close_refused'), result.get('error', 'Closing the application was refused.'))
            self.R.tc_disconnect()
            if self.current is not None and self.current[1] is self.root[1]:
                self.disconnected_root = self.current
            # Keep the caller's entry until its dispatch exits: a subsequent
            # Connect() must restore that same Python/MCP connection and state.
            if self.current is not None and self.current[1] is not self.root[1]:
                self.current[0].finish(self.current[1])
        self.detached = True
        dict.__setitem__(self.appdata, 'Connected', False)
        self.host.ui.current = None
        if clear_globals:
            for name in ('app', 'приложение', 'mainwindow', 'главноеокно', 'currentsource', 'текущийобъект'):
                self.host.globals[name] = None

    def continuation(self):
        """Retain only an explicitly detached caller connection between suite tests."""
        record = self.disconnected_root
        if record is None: return None
        pool, entry, _, client = record
        with pool.lock:
            if (pool.entries.get(entry.id) is not entry or entry.disconnect_cleanup is not None
                    or getattr(client, '_interrupted', False) or entry.state.get('client') is not None):
                return None
        return record

    def close(self):
        errors = []
        for pool, entry, execution, _ in reversed(self.owned):
            # Only sockets created by this scenario; never terminate external processes.
            with pool.lock:
                if pool.entries.get(entry.id) is not entry: continue
            try:
                with self.bound((pool, entry, execution, entry.state.get('client')), connecting=True):
                    self.R.tc_disconnect()
                    pool.finish(entry)
            except (Failure, OSError, RuntimeError) as exc:
                errors.append(dict(connection_id=entry.id, error=str(exc)))
        if errors: self.host.diagnostics.append(dict(code='connection_cleanup_failed', connections=errors))
