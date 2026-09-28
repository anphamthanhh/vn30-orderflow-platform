"""Signal specs: hypothesis cards re-expressed as :class:`Forecast` objects.

Each module emits a forecast (a view in +/-20 units with a horizon), not trades or P&L, so the
signal can be scored on its own with ``historical.signal_eval.evaluate``. These are
re-expressions, not re-designs: a frozen card's own implementation remains its scoring authority.
"""

from __future__ import annotations

__all__: list[str] = []
