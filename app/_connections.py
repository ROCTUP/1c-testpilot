"""Connection ownership and request-local state. No 1C protocol or credentials here."""
from collections.abc import MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
import os
import socket
import threading
import uuid


class ConnectionError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def empty_state():
    return {'client': None, 'window_key': None, 'launched_pid': None}


class State(MutableMapping):
    """Existing handlers use this mapping within the selected request's context."""
    def __init__(self):
        self.legacy = empty_state()
        self.current = ContextVar('tc1c_connection_state', default=None)

    def data(self):
        value = self.current.get()
        return self.legacy if value is None else value

    def __getitem__(self, key): return self.data()[key]
    def __setitem__(self, key, value): self.data()[key] = value
    def __delitem__(self, key): del self.data()[key]
    def __iter__(self): return iter(self.data())
    def __len__(self): return len(self.data())
    def copy(self): return self.data().copy()

    @contextmanager
    def bind(self, value):
        token = self.current.set(value)
        try: yield
        finally: self.current.reset(token)


def endpoint(host, port):
    host = host.strip().lower()
    if host in ('localhost', '::1'): host = '127.0.0.1'
    return host, port


def free_port(port=None):
    """Check an explicit local port or let the OS allocate one."""
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
        raise ConnectionError('invalid_port', 'port must be between 1 and 65535.')
    with socket.socket() as s:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try: s.bind(('127.0.0.1', port or 0))
        except OSError as exc:
            raise ConnectionError('port_in_use', 'The requested test-client port is already in use.') from exc
        return s.getsockname()[1]


class Connection:
    def __init__(self, host, port, base=None, user=None):
        self.id = 'c' + uuid.uuid4().hex[:16]
        self.host, self.port = endpoint(host, port)
        self.base, self.user = base, user
        self.state = empty_state()
        self.state['connection_id'] = self.id
        self.lock = threading.RLock()
        self.starting = True

    def info(self):
        c = self.state.get('client')
        return {'connection_id': self.id, 'profile': self.state.get('profile'), 'host': self.host, 'port': self.port,
                'base': self.base, 'user': self.user,
                'version': getattr(c, 'platform_version', None),
                'connected': c is not None, 'starting': self.starting,
                'launched': bool(self.state.get('launched_pid')),
                'desktop': self.state.get('desktop'),
                'recording': bool(self.state.get('rec_active'))}


class Pool:
    def __init__(self, state, limit=None, legacy=None):
        self.state = state
        self.legacy = legacy
        self.limit = int(os.environ.get('TC1C_CONNECTION_LIMIT', '16')) if limit is None else limit
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise ValueError('TC1C_CONNECTION_LIMIT must be a positive integer')
        self.lock = threading.RLock()
        self.entries = {}
        self.legacy_lock = threading.RLock()

    def legacy_state(self):
        return self.state.legacy if self.legacy is None else self.legacy

    def list(self):
        with self.lock: return [c.info() for c in self.entries.values()]

    def reserve(self, host, port, *, launch=False, connection_id=None, base=None, user=None):
        """Reserve before network I/O; concurrent launches cannot take the same endpoint."""
        with self.lock:
            if launch:
                if connection_id is not None:
                    raise ConnectionError('invalid_connection_id', 'launch_client creates a new connection; omit connection_id.')
                if port is None:
                    for _ in range(20):
                        port = free_port()
                        if not any((c.host, c.port) == ('127.0.0.1', port) for c in self.entries.values()): break
                    else: raise ConnectionError('port_in_use', 'No free test-client port could be allocated.')
            host, port = endpoint(host, port)
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ConnectionError('invalid_port', 'port must be between 1 and 65535.')
            existing = next((c for c in self.entries.values() if (c.host, c.port) == (host, port)), None)
            if connection_id is not None:
                selected = self.entries.get(connection_id)
                if selected is None:
                    raise ConnectionError('connection_not_found', 'Unknown connection_id. Use list_connections.')
                if selected is not existing:
                    raise ConnectionError('connection_mismatch', 'The host and port do not belong to this connection_id.')
            if existing:
                if launch or existing.starting:
                    raise ConnectionError('connection_in_use', 'This test-client endpoint is already in use.')
                return existing
            if len(self.entries) >= self.limit:
                raise ConnectionError('connection_limit_exceeded', 'Close an unused connection or increase TC1C_CONNECTION_LIMIT.')
            if launch: free_port(port)
            c = Connection(host, port, base, user)
            self.entries[c.id] = c
            return c

    def select(self, connection_id=None, refs=(), *, stop=False):
        with self.lock:
            selected = None
            if connection_id is not None:
                selected = self.entries.get(connection_id)
                if selected is None:
                    raise ConnectionError('connection_not_found', 'Unknown connection_id. Use list_connections.')
            owners = set()
            for ref in refs:
                owner = None
                for c in self.entries.values():
                    registry = getattr(c.state.get('client'), '_object_handles', None)
                    namespace = getattr(registry, '_namespace', None)
                    if namespace and isinstance(ref, str) and ref.startswith('e' + namespace + '-'):
                        owner = c
                        break
                if owner is None:
                    # Preserve standalone Python-handler use with its legacy client.
                    if not self.entries and connection_id is None and self.legacy_state().get('client') is not None:
                        return None
                    raise ConnectionError('stale_ref', 'Unknown or expired element reference. Find the element again.')
                owners.add(owner)
            if len(owners) > 1 or (selected is not None and owners and selected not in owners):
                raise ConnectionError('connection_mismatch', 'The supplied references and connection_id must belong to one connection.')
            if owners: selected = next(iter(owners))
            if selected is not None: return selected
            choices = [c for c in self.entries.values() if not c.starting and
                       (c.state.get('client') is not None or (stop and c.state.get('launched_pid')))]
            if len(choices) == 1: return choices[0]
            if len(choices) > 1:
                raise ConnectionError('connection_required', 'Several connections are available. Supply connection_id or an element ref.')
            if self.entries:
                raise ConnectionError('not_connected', 'No connected test client. Use connect or list_connections.')
            return None

    @contextmanager
    def use(self, connection):
        if connection is None:
            with self.legacy_lock, self.state.bind(self.legacy_state()): yield
            return
        with connection.lock:
            with self.lock:
                if self.entries.get(connection.id) is not connection:
                    raise ConnectionError('connection_not_found', 'This connection has been closed. Use list_connections.')
            with self.state.bind(connection.state): yield

    def finish(self, connection):
        if connection is None: return
        with self.lock:
            connection.starting = False
            if connection.state.get('client') is None and not connection.state.get('launched_pid'):
                self.entries.pop(connection.id, None)

    def close(self):
        with self.lock: entries = list(self.entries.values())
        failures = []
        for connection in entries:
            with connection.lock:
                c = connection.state.get('client')
                if c is not None:
                    try: c.close()
                    except Exception: pass
                isolated = connection.state.get('_isolated_process')
                if isolated is not None:
                    try: isolated.close()
                    except Exception as exc:
                        failures.append(exc)
                        continue
                connection.state.clear()
                with self.lock: self.entries.pop(connection.id, None)
        legacy = self.legacy_state()
        isolated = legacy.get('_isolated_process')
        if isolated is not None:
            try:
                isolated.close()
                c = legacy.get('client')
                if c is not None: c.close()
                legacy.clear()
                legacy.update(empty_state())
            except Exception as exc: failures.append(exc)
        if failures:
            raise OSError('Could not release an isolated client: %s' % failures[0])
