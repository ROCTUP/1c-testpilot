"""Built-in scenario formats, imported only when selected."""
from importlib import import_module

from .errors import Failure


FORMATS = {'tester': 'compatible_scenarios.tester.runner'}


def executor(framework):
    if not isinstance(framework, str) or framework not in FORMATS:
        raise Failure('unsupported_framework', 'Supported framework: ' + ', '.join(FORMATS) + '.')
    return import_module(FORMATS[framework]).run
