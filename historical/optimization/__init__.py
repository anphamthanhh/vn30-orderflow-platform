"""Strategy-validation statistics and the staged go/no-go gate.

Submodules, imported on first access: ``dsr``, ``overfitting``, ``cpcv``, ``trials_ledger``,
``regime_labels`` and ``gate_report``.
"""

import importlib as _importlib

__all__ = ['dsr', 'overfitting', 'cpcv', 'trials_ledger', 'regime_labels', 'gate_report']


def __getattr__(name):  # PEP 562: import a submodule on first use
    if name in __all__:
        return _importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
