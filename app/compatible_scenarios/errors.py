"""Errors shared by compatible scenario executors."""

class Failure(Exception):
    def __init__(self, code, message, *, path=None, line=None, **details):
        super().__init__(message)
        self.result = dict(ok=False, code=code, error=message, **details)
        if path is not None: self.result['path'] = str(path)
        if line is not None: self.result['line'] = line
