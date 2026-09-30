"""Tester's Meta.epf JSON schema, loaded only when a scenario reads Meta/Мета."""
import json
from pathlib import Path
import time

from compatible_scenarios.shared.bsl.language import Failure, value_in


def read_file(path):
    if not isinstance(path, (str, Path)) or not str(path):
        raise Failure('invalid_metadata_file', 'metadata_path must be a JSON file path.')
    source = Path(path).expanduser().resolve()
    try:
        if source.stat().st_size > 32 * 1024 * 1024:
            raise Failure('invalid_metadata_file', 'Metadata JSON exceeds 32 MiB.', path=source)
        value = json.loads(source.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise Failure('invalid_metadata_file', f'Cannot read metadata JSON: {exc}', path=source) from exc
    if not isinstance(value, dict):
        raise Failure('invalid_metadata_file', 'Metadata JSON must contain an object.', path=source)
    return value


def get(host):
    if host.repository.metadata is not None:
        key = None
        data = host.repository.metadata
    else:
        key = id(host.client)
        data = None
    if key not in host.metadata_cache:
        if data is None:
            import _code_execution
            def read():
                remaining = host.deadline - time.monotonic()
                if remaining <= 0:
                    raise Failure('scenario_timeout', 'Scenario deadline expired.')
                return _code_execution.execute(
                    host.R, host.client, mode='metadata', context='server', options={'format': 'tester'},
                    timeout=min(remaining, 180), check_permissions=False)
            with host.connections.bound():
                response = host.operation('get_tester_metadata', {}, read)
            data = response.get('metadata')
            if not isinstance(data, dict):
                raise Failure('helper_protocol_error', 'The helper did not return Tester metadata.')
        # Retain the client as well: a reconnected client must never inherit a reused id.
        host.metadata_cache[key] = (host.client if key is not None else None, value_in(data))
    return host.metadata_cache[key][1]
