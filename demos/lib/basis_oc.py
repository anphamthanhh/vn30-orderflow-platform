"""Operating characteristics of the Stage-0 basis screen (``historical/research_lab/basis_screen.py``).

The frozen screen tests whether fading extreme end-of-day basis z-scores clears the cost bar.
Running it on many synthetic markets with a planted effect of known size measures its null pass
rate (1 - specificity) and its power (sensitivity). A proposed variant also requires the fade's
per-trade t-statistic to reach 1.645 (one-sided 5 %) and is scored on the same draws. The frozen
rule itself stays unchanged (``historical/research_lab/PRE_REGISTRATION.md``).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import common  # noqa: F401  - puts the repo root on sys.path
from historical.research_lab.basis_screen import EXTREME_Q, Z_LOOKBACK, _flag_roll_boundary, screen
from samples.synthetic.index_basis import MarketParams, simulate

T_CRIT = 1.645
PASS_VERDICTS = {"PULSE_REGISTER_FORWARD", "MARGINAL_REGISTER_FORWARD"}


def fade_trades(daily: pd.DataFrame) -> np.ndarray:
    """Per-trade gross points of the screen's frozen fade rule (mirror of ``screen()`` internals)."""
    d = daily.sort_values("d").reset_index(drop=True).copy()
    roll = d["basis_eod"].rolling(Z_LOOKBACK)
    d["basis_z"] = (d["basis_eod"] - roll.mean()) / roll.std()
    d["fwd_ret"] = d["f_close"].shift(-1) - d["f_open"].shift(-1)
    d["is_roll"] = _flag_roll_boundary(d["d"])
    v = d[d["basis_z"].notna() & d["fwd_ret"].notna() & ~d["is_roll"]].copy()
    thr = float(v["basis_z"].abs().quantile(EXTREME_Q))
    ex = v[v["basis_z"].abs() >= thr].copy()
    return -np.sign(ex["basis_z"].to_numpy()) * ex["fwd_ret"].to_numpy()


@dataclass(frozen=True)
class Draw:
    effect: float
    seed: int
    frozen_verdict: str
    t_stat: float
    n_fade: int
    sd_trade: float

    @property
    def frozen_pass(self) -> bool:
        return self.frozen_verdict in PASS_VERDICTS

    @property
    def proposed_pass(self) -> bool:
        return self.frozen_pass and self.t_stat >= T_CRIT


def draw(effect: float, seed: int) -> Draw:
    """One synthetic market -> the frozen verdict plus the fade t-statistic."""
    _, daily = simulate(MarketParams(seed=seed, planted_reversion_pts=effect), with_m1=False)
    res = screen(daily)
    g = fade_trades(daily)
    # the mirror must reproduce the frozen screen's own aggregate
    if len(g) != res["fade_n"] or round(float(np.mean(g)), 3) != res["fade_gross_per_trade_pts"]:
        raise AssertionError("fade_trades() drifted from basis_screen.screen()")
    sd = float(np.std(g, ddof=1))
    return Draw(effect, seed, res["verdict"], float(np.mean(g) / (sd / np.sqrt(len(g)))), len(g), sd)


def operating_characteristics(effects: tuple[float, ...], n_seeds: int) -> pd.DataFrame:
    """Pass-to-forward rates of the frozen and the proposed screen for each planted effect."""
    rows = []
    for k in effects:
        draws = [draw(k, 10_000 + s) for s in range(n_seeds)]
        rows.append({
            "planted_effect_pts_per_z": k,
            "frozen_pass_rate": np.mean([d.frozen_pass for d in draws]),
            "proposed_pass_rate": np.mean([d.proposed_pass for d in draws]),
            "median_n_fade": int(np.median([d.n_fade for d in draws])),
            "median_sd_trade_pts": float(np.median([d.sd_trade for d in draws])),
        })
    return pd.DataFrame(rows)


def minimum_detectable_effect(sd_trade: float, n_fade: int, z_alpha: float = 1.645, z_power: float = 0.8416) -> float:
    """Per-trade gross (points) detectable at 5% one-sided with 80% power: (z_a + z_b) * sd / sqrt(n)."""
    return (z_alpha + z_power) * sd_trade / np.sqrt(n_fade)
