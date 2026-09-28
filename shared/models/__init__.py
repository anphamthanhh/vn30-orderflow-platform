"""Shared domain models: the ±20 forecast contract (:mod:`shared.models.forecast`).

Names are re-exported lazily, so ``from shared.models import Forecast`` imports only that module.
"""
import importlib as _importlib

_EXPORTS = {
    # Signal layer (+/-20 forecast contract)
    "Forecast": "forecast",
    "scale_and_cap": "forecast",
    "combine": "forecast",
    "to_position_target": "forecast",
}
__all__ = ["forecast", *_EXPORTS]


def __getattr__(name):  # PEP 562
    if name == "forecast":
        return _importlib.import_module(f"{__name__}.forecast")
    if name in _EXPORTS:
        return getattr(_importlib.import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
