"""Test bootstrap.

`open_webui/__init__.py` imports the application (typer, config, the web app),
which these unit tests do not need. Registering a bare package with the same
``__path__`` lets ``open_webui.utils.<module>`` resolve to the source tree while
skipping the application import. Only pure-stdlib modules belong in this suite.
"""

import sys
import types
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / 'open_webui'

if 'open_webui' not in sys.modules:
    package = types.ModuleType('open_webui')
    package.__path__ = [str(_PACKAGE_ROOT)]
    sys.modules['open_webui'] = package
