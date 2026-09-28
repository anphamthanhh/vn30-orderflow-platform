"""Pure quantitative functions; ``profile`` mirrors the Rust market-profile engine's value area.

Submodules are imported on first attribute access.
"""

import importlib as _importlib

__all__ = ['profile']


def __getattr__(name):  # PEP 562: import a subpackage/submodule on first use
    if name in __all__:
        return _importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
