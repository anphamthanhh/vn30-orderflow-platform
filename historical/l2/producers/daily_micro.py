"""Daily OHLCV liquidity features: Corwin-Schultz spread, Roll spread, Amihud illiquidity.

Produces ``ob_spread_cs_pts_v1``, ``ob_spread_roll_pts_v1`` and ``ob_illiq_amihud_v1`` from the
development (non-holdout) sessions of ``default.ohlcv_m1``. Pairs across a data gap or a contract
roll are excluded and all windows are trailing: the value at session D uses data up to D's close.
There is no Rust counterpart; ``historical/tests/test_l2_daily_micro.py`` checks golden vectors.

CLI::

    python -m historical.l2.producers.daily_micro --feature ob_spread_cs_pts_v1
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import date as Date
from pathlib import Path
from typing import Iterable, NamedTuple, Sequence

if __package__ in (None, ""):  # pragma: no cover - `python path/to/daily_micro.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

FEATURES: tuple[str, ...] = (
    "ob_spread_cs_pts_v1",
    "ob_spread_roll_pts_v1",
    "ob_illiq_amihud_v1",
)

#: ``3 - 2*sqrt(2) = (sqrt(2) - 1)**2``, the Corwin-Schultz alpha denominator. Module-level so
#: a mutation test in ``test_l2_daily_micro.py`` can perturb it.
CS_DENOM = 3.0 - 2.0 * math.sqrt(2.0)

#: Amihud turnover divisor (spec param ``turnover_scale``); fixes the units of ILLIQ.
AMIHUD_TURNOVER_SCALE = 1_000_000.0


# --- Corwin-Schultz (2012): pure functions, stdlib only ---
class CsEstimate(NamedTuple):
    """One two-day Corwin-Schultz estimate with all intermediates, so each can be tested."""

    beta: float
    gamma: float
    alpha: float
    spread_frac: float


def cs_beta(h1: float, l1: float, h2: float, l2: float) -> float:
    """``beta = [ln(H1/L1)]^2 + [ln(H2/L2)]^2`` from the two single-day ranges (C&S eq. 7)."""
    a = math.log(h1 / l1)
    b = math.log(h2 / l2)
    return a * a + b * b


def cs_gamma(h1: float, l1: float, h2: float, l2: float) -> float:
    """``gamma = [ln(max(H1, H2) / min(L1, L2))]^2``, the squared two-day range (C&S eq. 10)."""
    hi = h1 if h1 > h2 else h2
    lo = l1 if l1 < l2 else l2
    g = math.log(hi / lo)
    return g * g


def cs_alpha(beta: float, gamma: float) -> float:
    """``alpha = (sqrt(2*beta) - sqrt(beta)) / CS_DENOM - sqrt(gamma / CS_DENOM)`` (C&S eq. 18)."""
    if beta < 0.0 or gamma < 0.0:
        raise ValueError(f"beta and gamma are squares and cannot be negative: {beta}, {gamma}")
    return (math.sqrt(2.0 * beta) - math.sqrt(beta)) / CS_DENOM - math.sqrt(gamma / CS_DENOM)


def cs_spread_frac(alpha: float) -> float:
    """``S = 2*(e^alpha - 1)/(1 + e^alpha)``, the proportional spread (C&S eq. 14).

    Negative when ``alpha < 0``, a normal outcome; :func:`apply_negative_rule` handles it.
    """
    e = math.exp(alpha)
    return 2.0 * (e - 1.0) / (1.0 + e)


def corwin_schultz(h1: float, l1: float, h2: float, l2: float) -> CsEstimate:
    """Corwin-Schultz two-day spread estimate from two consecutive sessions' highs and lows.

    Exact at zero volatility: with ``H = P*(1 + S/2)`` and ``L = P*(1 - S/2)`` on both days and
    ``k = ln(H/L)``, ``beta = 2*k^2``, ``gamma = k^2``, ``alpha = k`` and ``S_hat = S``. The
    golden-vector test checks every intermediate against this identity.
    """
    for name, v in (("h1", h1), ("l1", l1), ("h2", h2), ("l2", l2)):
        if not math.isfinite(v) or v <= 0.0:
            raise ValueError(f"{name} must be finite and positive, got {v!r}")
    if h1 < l1 or h2 < l2:
        raise ValueError(f"high below low: ({h1}, {l1}), ({h2}, {l2})")
    beta = cs_beta(h1, l1, h2, l2)
    gamma = cs_gamma(h1, l1, h2, l2)
    alpha = cs_alpha(beta, gamma)
    return CsEstimate(beta=beta, gamma=gamma, alpha=alpha, spread_frac=cs_spread_frac(alpha))


def apply_negative_rule(spread: float, rule: str) -> float | None:
    """Apply the spec's ``negative_rule`` to a two-day estimate.

    ``zero`` floors at 0 (C&S's recommendation when averaging); ``keep`` returns it unchanged;
    ``drop`` returns ``None`` (no row) unless it is positive.
    """
    if rule == "keep":
        return spread
    if rule == "zero":
        return spread if spread > 0.0 else 0.0
    if rule == "drop":
        return spread if spread > 0.0 else None
    raise ValueError(f"unknown negative_rule {rule!r}; known: zero, keep, drop")


# --- Roll (1984): pure functions, stdlib only ---
def paired_covariance(xs: Sequence[float], ys: Sequence[float], ddof: int = 1) -> float:
    """Sample covariance of two equal-length sequences, dividing by ``n - ddof``."""
    n = len(xs)
    if n != len(ys):
        raise ValueError(f"samples differ in length: {n} vs {len(ys)}")
    if n <= ddof:
        raise ValueError(f"need more than ddof={ddof} pairs, got {n}")
    mx = sum(xs) / n
    my = sum(ys) / n
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - ddof)


def roll_spread_pts(
    segments: Iterable[Sequence[float]],
    *,
    ddof: int = 1,
    min_pairs: int = 10,
) -> float | None:
    """Roll (1984) spread in points: ``2*sqrt(-cov(dp_t, dp_{t-1}))``, ``None`` if ``cov >= 0``.

    ``segments`` are contiguous runs of price changes; lagged pairs are formed only within a run,
    so none spans a missing session or a contract roll. Roll's model implies ``cov = -S^2/4``;
    a non-negative covariance or fewer than ``min_pairs`` pairs yields no value, not zero.
    """
    xs: list[float] = []
    ys: list[float] = []
    for seg in segments:
        for i in range(1, len(seg)):
            xs.append(seg[i])
            ys.append(seg[i - 1])
    if len(xs) < max(min_pairs, ddof + 1):
        return None
    cov = paired_covariance(xs, ys, ddof)
    if cov >= 0.0:
        return None
    return 2.0 * math.sqrt(-cov)


# --- Amihud (2002): pure functions, stdlib only ---
def amihud_illiq(
    observations: Iterable[tuple[float, float]],
    *,
    scale: float = AMIHUD_TURNOVER_SCALE,
    min_obs: int = 10,
) -> float | None:
    """Amihud (2002) illiquidity: mean of ``|r_t| / (turnover_t / scale)`` over the window.

    ``observations`` are ``(return, turnover)`` pairs, turnover in contracts × points. Pairs with a
    non-finite value or non-positive turnover are skipped; under ``min_obs`` terms gives ``None``.
    """
    if scale <= 0.0:
        raise ValueError(f"scale must be positive, got {scale!r}")
    terms: list[float] = []
    for ret, turnover in observations:
        if not (math.isfinite(ret) and math.isfinite(turnover)) or turnover <= 0.0:
            continue
        terms.append(abs(ret) / (turnover / scale))
    if len(terms) < min_obs:
        return None
    return sum(terms) / len(terms)


# --- session chaining: which adjacent pairs are usable ---
#: Link classes. Only ``ok`` links carry a two-day estimate or a price change.
LINK_OK = "ok"
LINK_DATA_GAP = "data_gap"
LINK_ROLL = "roll_boundary"


def session_links(dates: Sequence[Date], expiry: Sequence[bool]) -> list[str]:
    """Classify each adjacent pair ``dates[i-1] -> dates[i]``; ``links[0]`` is ``""``.

    ``data_gap``: a session the calendar marks as trading is missing (``calendar_vn.gap_kind``).
    ``roll_boundary``: ``dates[i-1]`` is an expiry session, so the pair spans two contracts.
    Otherwise ``ok``; weekends and holidays do not break the chain.
    """
    from historical.research_lab import calendar_vn as cal

    if len(dates) != len(expiry):
        raise ValueError(f"dates and expiry differ in length: {len(dates)} vs {len(expiry)}")
    out = [""]
    for i in range(1, len(dates)):
        if cal.gap_kind(dates[i - 1], dates[i]) == "data_gap":
            out.append(LINK_DATA_GAP)
        elif bool(expiry[i - 1]):
            out.append(LINK_ROLL)
        else:
            out.append(LINK_OK)
    return out


# --- parameters, read from the feature registry ---
@dataclass(frozen=True)
class MicroParams:
    """The subset of a registry param set this module acts on."""

    window: int
    negative_rule: str = "zero"
    price_ref: str = "close_d2"
    cov_ddof: int = 1
    min_pairs: int = 10
    min_obs: int = 10
    turnover_scale: float = AMIHUD_TURNOVER_SCALE
    return_type: str = "simple"


def params_from_registry(feature: str, set_name: str = "default") -> MicroParams:
    """Load parameter set ``set_name`` of ``feature`` from ``specs/features/<feature>.yaml``."""
    from historical.research_lab.feature_boundary import FEATURES_DIR, load_feature

    if feature not in FEATURES:
        raise ValueError(f"unknown feature {feature!r}; known: {', '.join(FEATURES)}")
    feat = load_feature(FEATURES_DIR / f"{feature}.yaml")
    if set_name not in feat.param_sets:
        raise ValueError(f"{feature}: no param set {set_name!r} (have {sorted(feat.param_sets)})")
    p = feat.param_sets[set_name]
    win = p["window"]
    return MicroParams(
        window=int(win["value"]),
        negative_rule=str(p.get("negative_rule", "zero")),
        price_ref=str(p.get("price_ref", "close_d2")),
        cov_ddof=int(p.get("cov_ddof", 1)),
        min_pairs=int(p.get("min_pairs", 10)),
        min_obs=int(p.get("min_obs", 10)),
        turnover_scale=float(p.get("turnover_scale", AMIHUD_TURNOVER_SCALE)),
        return_type=str(p.get("return_type", "simple")),
    )


def default_params() -> dict[str, MicroParams]:
    """The ``default`` parameter set of all three features, from the registry."""
    return {f: params_from_registry(f) for f in FEATURES}


# --- daily frame (pandas is used only from here on) ---
def dev_daily_bars(path: str | Path | None = None):
    """Daily OHLCV bars for development sessions only.

    Removes the ranges reserved in ``holdout_lock.json`` (``lab.holdout_ranges()``); then
    ``daily.daily_bars`` asserts that no reserved session remains.
    """
    import datetime as _dt

    from historical.research_lab import daily, lab

    epoch = _dt.date(1970, 1, 1)
    ranges = lab.holdout_ranges()

    def _reserved(d: _dt.date) -> bool:
        n = (d - epoch).days
        return any(f <= n and (t is None or n <= t) for f, t in ranges)

    m1 = daily.load_m1(path) if path is not None else daily.load_m1()
    dev = m1[~m1["date"].map(_reserved)]
    return daily.daily_bars(dev)


def daily_micro_frame(bars, params: dict[str, MicroParams] | None = None):
    """Compute the three features and their diagnostics, one row per session.

    Adds ``link``, ``is_expiry``, the CS intermediates (``cs_beta``, ``cs_gamma``, ``cs_alpha``,
    ``cs_spread_frac``), the three feature columns and the inputs ``ret``, ``turnover`` and
    ``dp``. All windows are trailing: row D never reads row D+1.
    """
    import numpy as np
    import pandas as pd

    from historical.research_lab import daily as _daily

    if params is None:
        params = default_params()

    df = bars.copy()
    dates = [ts.date() for ts in pd.DatetimeIndex(df.index)]
    expiry = _daily.expiry_days(df).to_numpy(dtype=bool)
    links = session_links(dates, expiry)
    df["is_expiry"] = expiry
    df["link"] = links

    n = len(df)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    volume = df["volume"].to_numpy(dtype=float)
    turnover = volume * close

    nan = float("nan")
    cs_b = np.full(n, nan)
    cs_g = np.full(n, nan)
    cs_a = np.full(n, nan)
    cs_s = np.full(n, nan)
    cs_pts = np.full(n, nan)
    ret = np.full(n, nan)
    dp = np.full(n, nan)

    p_cs = params["ob_spread_cs_pts_v1"]
    for i in range(1, n):
        if links[i] != LINK_OK:
            continue
        est = corwin_schultz(high[i - 1], low[i - 1], high[i], low[i])
        cs_b[i], cs_g[i], cs_a[i], cs_s[i] = est
        ruled = apply_negative_rule(est.spread_frac, p_cs.negative_rule)
        if ruled is not None:
            ref = close[i] if p_cs.price_ref == "close_d2" else math.sqrt(high[i] * low[i])
            cs_pts[i] = ruled * ref
        # returns and price changes follow the same link rule as the CS pair
        dp[i] = close[i] - close[i - 1]
        ret[i] = close[i] / close[i - 1] - 1.0

    df["cs_beta"] = cs_b
    df["cs_gamma"] = cs_g
    df["cs_alpha"] = cs_a
    df["cs_spread_frac"] = cs_s
    df["ob_spread_cs_pts_v1"] = cs_pts
    df["ret"] = ret
    df["dp"] = dp
    df["turnover"] = turnover

    # --- Roll: trailing window of price changes, split into contiguous runs ----
    p_roll = params["ob_spread_roll_pts_v1"]
    roll_vals = np.full(n, nan)
    for i in range(n):
        lo = max(1, i - p_roll.window + 1)
        segments: list[list[float]] = []
        run: list[float] = []
        for j in range(lo, i + 1):
            if links[j] == LINK_OK and math.isfinite(dp[j]):
                run.append(float(dp[j]))
            else:
                if run:
                    segments.append(run)
                run = []
        if run:
            segments.append(run)
        v = roll_spread_pts(segments, ddof=p_roll.cov_ddof, min_pairs=p_roll.min_pairs)
        if v is not None:
            roll_vals[i] = v
    df["ob_spread_roll_pts_v1"] = roll_vals

    # --- Amihud: trailing window of |r| / turnover -----------------------------
    p_am = params["ob_illiq_amihud_v1"]
    am_vals = np.full(n, nan)
    for i in range(n):
        lo = max(0, i - p_am.window + 1)
        obs = [
            (float(ret[j]), float(turnover[j])) for j in range(lo, i + 1) if math.isfinite(ret[j])
        ]
        v = amihud_illiq(obs, scale=p_am.turnover_scale, min_obs=p_am.min_obs)
        if v is not None:
            am_vals[i] = v
    df["ob_illiq_amihud_v1"] = am_vals
    return df


class Obs(NamedTuple):
    """One output row: a session date and the value known at that session's close."""

    day: Date
    value: float


def produce(
    feature: str,
    bars=None,
    *,
    params: dict[str, MicroParams] | None = None,
    path: str | Path | None = None,
) -> list[Obs]:
    """Return the defined values of ``feature`` in date order (the L2 write path's entry point).

    An undefined estimate yields no row, never a zero or NaN.
    """
    if feature not in FEATURES:
        raise ValueError(f"unknown feature {feature!r}; known: {', '.join(FEATURES)}")
    if bars is None:
        bars = dev_daily_bars(path)
    frame = daily_micro_frame(bars, params)
    out: list[Obs] = []
    for ts, value in frame[feature].items():
        v = float(value)
        if math.isfinite(v):
            out.append(Obs(ts.date(), v))
    return out


def write_tsv(rows: Iterable[Obs], sink) -> None:
    """Write ``date<TAB>value`` rows with 12 significant digits; undefined sessions are absent."""
    sink.write("date\tvalue\n")
    for r in rows:
        sink.write(f"{r.day.isoformat()}\t{r.value:.12g}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--feature", default=FEATURES[0], choices=FEATURES)
    ap.add_argument("--csv", default=None, help="M1 CSV (default: the validated one)")
    ap.add_argument("--out", default=None, help="output path (default: stdout)")
    args = ap.parse_args(argv)

    rows = produce(args.feature, path=args.csv)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            write_tsv(rows, fh)
    else:
        write_tsv(rows, sys.stdout)
    print(f"{args.feature}: {len(rows)} defined session values", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
