"""Persistent, process-safe identifiers compatible with Tester's TestingID."""
import os
from pathlib import Path
import re
import sqlite3
import time

from compatible_scenarios.shared.bsl.language import Failure


def state_path():
    configured = os.environ.get('TC1C_TESTER_STATE_DIR')
    if configured:
        root = Path(configured).expanduser()
    elif os.name == 'nt':
        root = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData'/'Local')/'1c-testpilot'
    else:
        root = Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local'/'state')/'1c-testpilot'
    return root/'testing-id.sqlite3'


def successor(previous):
    if not isinstance(previous, str) or not re.fullmatch('[0-9A-Z]{4}', previous):
        raise Failure('testing_id_store_invalid', 'The stored TestingID is invalid; the counter was not reset.')
    alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    digits = list(previous)
    for index in range(len(digits)-1, -1, -1):
        value = alphabet.index(digits[index])+1
        digits[index] = alphabet[value % 36]
        if value < 36: return ''.join(digits)
    raise Failure('testing_id_exhausted', 'The four-character TestingID counter is exhausted.')


def next_id(deadline):
    remaining = deadline-time.monotonic()
    if remaining <= 0: raise Failure('scenario_timeout', 'Scenario deadline expired.')
    connection = None
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=min(remaining, 5), isolation_level=None)
        connection.execute('BEGIN IMMEDIATE')
        initialized = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='counter'").fetchone()
        connection.execute('CREATE TABLE IF NOT EXISTS counter (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
        row = connection.execute("SELECT value FROM counter WHERE name='testing_id'").fetchone()
        if initialized and row is None:
            raise Failure('testing_id_store_invalid', 'The TestingID counter record is missing; the counter was not reset.')
        value = successor(row[0] if row else 'A000')
        if time.monotonic() >= deadline: raise Failure('scenario_timeout', 'Scenario deadline expired.')
        connection.execute("INSERT OR REPLACE INTO counter(name,value) VALUES ('testing_id',?)", (value,))
        connection.execute('COMMIT')
        return value
    except (OSError, sqlite3.Error) as exc:
        code = 'scenario_timeout' if time.monotonic() >= deadline else 'testing_id_store_unavailable'
        raise Failure(code, 'Cannot update the persistent TestingID counter: '+str(exc)) from exc
    finally:
        if connection is not None: connection.close()
