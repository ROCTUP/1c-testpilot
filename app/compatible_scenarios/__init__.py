"""Common entry point for compatible scenario formats."""
from .errors import Failure
from .registry import executor


def run(runtime, *, path, framework='tester', **options):
    """Select an executor; return its check or execution result unchanged."""
    try:
        execute = executor(framework)
    except Failure as exc:
        return dict(exc.result, elapsed=0.0)
    return execute(runtime, path=path, framework=framework, **options)
