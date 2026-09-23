"""User-owned YAML connection profiles. No process or connection state."""
from contextvars import ContextVar
import os
from pathlib import Path
import re

import yaml


PASSWORD = ContextVar('testpilot_profile_password', default=None)
MAX_BYTES = 1024 * 1024
LAUNCH = {'base', 'port', 'server', 'user', 'password', 'password_env', 'version',
          'exe', 'extra_args', 'wait', 'connect', 'desktop', 'code_epf'}
CONNECT = {'host', 'port', 'version'}


class ProfileError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Loader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.constructor.ConstructorError(None, None, 'Aliases are not supported', event.start_mark)
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise yaml.constructor.ConstructorError(None, None,
                    'Mapping keys must be unique strings', key_node.start_mark)
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def invalid(name, message):
    raise ProfileError('invalid_profile', f'Profile {name!r}: {message}')


def validate(name, config):
    if not isinstance(name, str) or not name.strip():
        raise ProfileError('invalid_profiles_file', 'Profile names must be nonempty strings.')
    if not isinstance(config, dict):
        invalid(name, 'expected a mapping of settings.')
    unknown = set(config) - LAUNCH - CONNECT - {'description'}
    if unknown:
        invalid(name, 'unknown settings: ' + ', '.join(sorted(unknown)) + '.')
    action = 'launch_client' if 'base' in config else 'connect'
    allowed = LAUNCH if action == 'launch_client' else CONNECT
    if set(config) - allowed - {'description'}:
        invalid(name, 'do not mix launch settings (base, user, desktop, etc.) with connection settings (host, port).')
    if action == 'connect' and 'port' not in config:
        invalid(name, 'provide base for launch_client, or port and optional host for connect.')
    for key, value in config.items():
        if key in ('server', 'connect'):
            valid = type(value) is bool
        elif key == 'port':
            valid = type(value) is int and 1 <= value <= 65535
        elif key == 'wait':
            valid = type(value) is int and value > 0
        elif key == 'extra_args':
            valid = isinstance(value, list) and all(isinstance(v, str) and '\0' not in v for v in value)
        else:
            valid = isinstance(value, str) and '\0' not in value
            if key not in ('password', 'user', 'description'):
                valid = valid and bool(value.strip())
        if not valid:
            invalid(name, f'invalid value type or range for {key!r}; quote string values in YAML.')
    if 'password' in config and 'password_env' in config:
        invalid(name, 'use either password or password_env, not both.')
    if 'password_env' in config and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', config['password_env']):
        invalid(name, 'password_env must be an environment variable name.')
    if 'desktop' in config and config['desktop'] not in ('default', 'isolated'):
        invalid(name, 'desktop must be default or isolated.')
    return action


def load():
    filename = os.environ.get('TC1C_PROFILES_FILE', '').strip()
    if not filename:
        return {}, None
    path = Path(filename).expanduser().absolute()
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
    except OSError:
        raise ProfileError('profiles_file_unavailable', 'Cannot read TC1C_PROFILES_FILE.') from None
    if len(raw) > MAX_BYTES:
        raise ProfileError('invalid_profiles_file', 'The profiles file must not exceed 1 MiB.')
    try:
        data = yaml.load(raw.decode('utf-8-sig'), Loader=Loader)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        # YAML exceptions can include the source line, including a plaintext password.
        mark = getattr(exc, 'problem_mark', None)
        location = f' at line {mark.line + 1}, column {mark.column + 1}' if mark else ''
        raise ProfileError('invalid_profiles_file', 'Invalid YAML' + location +
                           '; use UTF-8, unique string keys and no aliases or custom tags.') from None
    if not isinstance(data, dict) or set(data) != {'profiles'} or not isinstance(data['profiles'], dict):
        raise ProfileError('invalid_profiles_file', 'Expected a profiles mapping at the YAML root.')
    for name, config in data['profiles'].items():
        validate(name, config)
    return data['profiles'], path


def paths(config, directory, explicit=()):
    result = dict(config)
    for key in ('base', 'exe', 'code_epf'):
        if key not in result or key in explicit or (key == 'base' and result.get('server', False)):
            continue
        path = Path(result[key]).expanduser()
        result[key] = str(path if path.is_absolute() else (directory / path).absolute())
    return result


def listing():
    profiles, path = load()
    result = []
    for name, config in profiles.items():
        config = paths(config, path.parent)
        action = 'launch_client' if 'base' in config else 'connect'
        result.append(dict(name=name, description=config.get('description', ''), action=action,
                           base=config.get('base'), user=config.get('user'),
                           host=config.get('host', '127.0.0.1'), port=config.get('port'),
                           version=config.get('version'),
                           desktop=config.get('desktop', 'default') if action == 'launch_client' else None))
    return {'ok': True, 'profiles': result, 'configured': path is not None}


def resolve(action, arguments):
    """Merge only explicitly supplied arguments, before handler defaults or pool reservation."""
    explicit = {k: v for k, v in arguments.items() if v is not None}
    name = explicit.pop('profile', None)
    if name is None:
        return explicit, None
    if not isinstance(name, str) or not name.strip():
        raise ProfileError('invalid_profile', 'profile must be a nonempty name from list_profiles.')
    profiles, path = load()
    if path is None:
        raise ProfileError('profiles_not_configured', 'Set TC1C_PROFILES_FILE to use named profiles.')
    if name not in profiles:
        raise ProfileError('profile_not_found', 'Unknown profile; use list_profiles.')
    config = profiles[name]
    expected = 'launch_client' if 'base' in config else 'connect'
    if action != expected:
        raise ProfileError('profile_action_mismatch', f'This profile is for {expected}; use that action.')
    merged = {k: v for k, v in config.items() if k not in ('description', 'password_env')}
    if 'password_env' in config and 'password' not in explicit:
        variable = config['password_env']
        if variable not in os.environ:
            raise ProfileError('profile_password_unavailable', f'Environment variable {variable!r} is not set.')
        merged['password'] = os.environ[variable]
    merged.update(explicit)
    return paths(merged, path.parent, explicit), name


def redact(value, *, diagnostic=True):
    """Mask credentials in diagnostic text, preserving IDs and structured values."""
    password = PASSWORD.get()
    if not password:
        return value
    if isinstance(value, str) and diagnostic:
        return value.replace(password, '***')
    if isinstance(value, dict):
        return {k: redact(v, diagnostic=k in {'error', 'message', 'connect_error', 'cleanup_error'})
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, diagnostic=diagnostic) for v in value]
    return value
