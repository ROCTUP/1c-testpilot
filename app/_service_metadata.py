"""Metadata options and the public custom BSL function registry contract."""
from copy import deepcopy
import json
import math
import re

from _code_execution import Failure

PARAMETER_TYPES = {'any', 'string', 'number', 'boolean', 'array', 'object', 'date', 'ref', 'enum'}


def metadata_options(name=None, meta_type=None, search=None, field_search=None,
                     sections=None, limit=100, offset=0):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise Failure('invalid_limit', 'limit must be an integer from 1 to 500.')
    if type(offset) is not int or offset < 0:
        raise Failure('invalid_offset', 'offset must be a nonnegative integer.')
    for key, value in dict(name=name, meta_type=meta_type, search=search, field_search=field_search).items():
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise Failure('invalid_metadata_filter', key + ' must be a nonempty string.')
    if name and any((meta_type, search, field_search)):
        raise Failure('invalid_metadata_filter', 'Use name for exact details or filters for a search.')
    allowed = {'fields', 'table_parts', 'values', 'properties'}
    if sections is not None and (not isinstance(sections, list) or any(not isinstance(s, str) or s not in allowed for s in sections)):
        raise Failure('invalid_metadata_sections', 'sections: fields, table_parts, values, properties.')
    if sections is not None and not name:
        raise Failure('invalid_metadata_filter', 'sections selects details of the object specified in name.')
    return dict(name=name or '', meta_type=meta_type or '', search=search or '',
                field_search=field_search or '', sections=sections if sections is not None else sorted(allowed),
                limit=limit, offset=offset)


def _matches(value, kind):
    if kind == 'any': return True
    if kind == 'string': return isinstance(value, str)
    if kind == 'number': return type(value) is int or (type(value) is float and math.isfinite(value))
    if kind == 'boolean': return type(value) is bool
    if kind == 'array': return isinstance(value, list)
    if kind == 'object': return isinstance(value, dict) and '$type' not in value
    return isinstance(value, dict) and value.get('$type') == kind


def validate_registry(value):
    """Keep only documented public fields; never expose internal handler names."""
    def invalid(message):
        raise Failure('invalid_function_registry', message)
    if not isinstance(value, list): invalid('The custom function registry must be an array.')
    result, names = [], set()
    for function in value:
        if not isinstance(function, dict): invalid('Each registered function must be an object.')
        name = function.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[^\W\d]\w*', name):
            invalid('Function names must be identifiers.')
        if name.casefold() in names: invalid('Duplicate custom function: ' + name)
        names.add(name.casefold())
        if function.get('context') not in ('client', 'server'): invalid('Invalid context for ' + name)
        if not isinstance(function.get('description'), str) or not function['description'].strip():
            invalid('A description is required for ' + name)
        params = function.get('parameters', [])
        if not isinstance(params, list): invalid('parameters must be an array for ' + name)
        public_params, param_names = [], set()
        for p in params:
            if not isinstance(p, dict) or not isinstance(p.get('name'), str) or not p['name']:
                invalid('Each parameter needs a name for ' + name)
            if p['name'] in param_names: invalid('Duplicate parameter for ' + name)
            param_names.add(p['name'])
            kind = p.get('type', 'any')
            if not isinstance(kind, str) or kind not in PARAMETER_TYPES or type(p.get('required', False)) is not bool:
                invalid('Invalid parameter type or required flag for ' + name)
            if not isinstance(p.get('description', ''), str): invalid('Invalid parameter description for ' + name)
            if 'default' in p and not _matches(p['default'], p.get('type', 'any')):
                invalid('Invalid parameter default for ' + name)
            try:
                json.dumps(p.get('default'), allow_nan=False)
            except (TypeError, ValueError):
                invalid('Parameter defaults must contain JSON values for ' + name)
            item = {k: deepcopy(p[k]) for k in ('name', 'type', 'description', 'required', 'default') if k in p}
            item.setdefault('type', 'any'); item.setdefault('required', False); item.setdefault('description', '')
            public_params.append(item)
        if not isinstance(function.get('result_description', ''), str): invalid('Invalid result description for ' + name)
        result.append(dict(name=name, description=function['description'], context=function['context'],
                           parameters=public_params, result_description=function.get('result_description', '')))
    return result


def function_arguments(registry, name, parameters):
    if not isinstance(name, str):
        raise Failure('unknown_custom_function', 'Choose a name from list_custom_bsl_functions.')
    function = next((f for f in registry if f['name'].casefold() == name.casefold()), None)
    if function is None:
        raise Failure('unknown_custom_function', 'Choose a name from list_custom_bsl_functions.')
    if parameters is not None and not isinstance(parameters, dict):
        raise Failure('invalid_parameters', 'parameters must be an object.')
    arguments = deepcopy(parameters or {})
    specs = {p['name']: p for p in function['parameters']}
    if arguments.keys() - specs.keys():
        raise Failure('invalid_parameters', 'Unknown custom function parameters.', parameters=sorted(arguments.keys() - specs.keys()))
    for key, spec in specs.items():
        if key not in arguments:
            if 'default' in spec: arguments[key] = deepcopy(spec['default'])
            elif spec['required']:
                raise Failure('invalid_parameters', 'Required parameter is missing.', parameter=key)
            else: continue
        if not _matches(arguments[key], spec['type']):
            raise Failure('invalid_parameters', 'Parameter does not match its registered type.', parameter=key, expected_type=spec['type'])
    return function, arguments


def list_functions(R, client, *, check_permissions=True):
    import _code_execution as E
    if check_permissions and not E.SETTINGS.functions:
        raise Failure('execution_disabled', 'Custom BSL functions are disabled in the server settings.')
    service = vars(client).get('_testpilot_service')
    if service is None:
        service, _ = E.discover(R, client)
    if service is None:
        raise Failure('helper_not_ready', 'Launch the client with the Testpilot processing.')
    if 'custom_functions' not in service.get('capabilities', []):
        raise Failure('helper_version_mismatch', 'Rebuild or update Testpilot.epf to use custom functions.')
    return dict(ok=True, functions=deepcopy(service['functions']))
