"""MCP entry point; legacy Python imports share the common runtime module."""
import sys

if __name__ == '__main__':
    from _cli import configure
    configure()

import _runtime
from _mcp import install

install(_runtime)
if __name__ == '__main__':
    _runtime.main()
else:
    # Existing extensions/tests patch server's handlers. Keep the same module identity.
    sys.modules[__name__] = _runtime
