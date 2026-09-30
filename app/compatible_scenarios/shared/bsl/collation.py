"""Case-insensitive Unicode sorting without changing the process locale."""
import ctypes as C
import ctypes.util
from functools import lru_cache
import re
import sys

from compatible_scenarios.shared.bsl.language import Failure


@lru_cache(maxsize=1)
def api():
    try:
        name = ('icu.dll' if sys.platform == 'win32' else
                '/usr/lib/libicucore.A.dylib' if sys.platform == 'darwin' else C.util.find_library('icui18n'))
        if not name: raise OSError('ICU library was not found')
        library = C.CDLL(name)
        match = re.search(r'\.so\.(\d+)', name)
        suffix = '_' + match[1] if match else ''
        def function(name, args, result):
            f = getattr(library, name + suffix)
            f.argtypes, f.restype = args, result
            return f
        return (library,
                function('ucol_open', [C.c_char_p, C.POINTER(C.c_int32)], C.c_void_p),
                function('ucol_close', [C.c_void_p], None),
                function('ucol_setStrength', [C.c_void_p, C.c_int32], None),
                function('ucol_getSortKey', [C.c_void_p, C.c_void_p, C.c_int32, C.c_void_p, C.c_int32], C.c_int32))
    except (OSError, AttributeError) as exc:
        raise Failure('collation_unavailable', 'String sorting requires the system ICU library (libicu on Linux).') from exc


class Collation:
    def __enter__(self):
        _, open_, self.close, strength, self.sort_key = api()
        status = C.c_int32()
        self.handle = open_(b'root', C.byref(status))
        if not self.handle or status.value > 0:
            if self.handle: self.close(self.handle)
            raise Failure('collation_unavailable', 'ICU could not initialize string sorting.')
        strength(self.handle, 1)  # Secondary: accents matter, letter case does not.
        return self

    def __exit__(self, *exc): self.close(self.handle)

    def key(self, text):
        raw = text.encode('utf-16-le' if sys.byteorder == 'little' else 'utf-16-be', errors='surrogatepass')
        source = C.create_string_buffer(raw)
        length = self.sort_key(self.handle, source, len(raw)//2, None, 0)
        if not 0 < length <= 8000000:
            raise Failure('scenario_limit', 'String collation key exceeds its size limit.')
        result = C.create_string_buffer(length)
        self.sort_key(self.handle, source, len(raw)//2, result, length)
        return result.raw
