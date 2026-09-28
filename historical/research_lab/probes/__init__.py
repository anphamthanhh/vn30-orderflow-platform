"""Live microstructure probes that measure resting-limit fills on a DNSE demo account.

``resting_limit_probe`` places qty-1 limits behind the touch (dry run by default) and logs
each outcome; ``fakes`` provides a seeded offline broker; ``analyze_probe`` turns the log into
fill rates, time to fill and adverse selection. Probes import nothing from the trading stack.
"""

from __future__ import annotations

__all__ = ["resting_limit_probe", "analyze_probe", "fakes"]
