"""Trade simulation on the cached 1-minute leg paths.

Cost bases (both use the project's own strategy_lab.settle_units for the statutory charges):
  proj  the project's backtest basis: candle prices, project cost model (STT 0.10%), no spread.
  fric  the frictions declared on 20 Sep 2026: 0.10 points spread on every fill (0.20 per leg over entry and exit) taken off the gross
        result, statutory charges computed on the unadjusted candle prices, STT 0.15% on trades from 2026-04-01.
Research results are quoted on `fric` (it includes a spread; the backtest has none) and the baseline is also shown on `proj`.

Execution conventions (no look-ahead):
  * entry at the 09:30 bar's open (the project's rule);
  * a fixed-time exit at time T is filled at the close of the bar labelled T (the baseline exit is the 15:29 bar's close, ~15:29:59);
  * a stop is evaluated on the close-marked gross P&L of each bar; when it is breached at the close of bar j the exit is filled at the
    OPEN of bar j+1 (the first price after the signal), or at the close of 15:29 if j is the last bar;
  * a minute with no trade repeats the previous close (the project's convention).
"""
from __future__ import annotations

import dataclasses
import pickle

import numpy as np

from common import *  # noqa: F403

SPREAD_PER_FILL = 0.10
STT_FROM, STT_RATE = "2026-04-01", 0.0015
MODEL_RAISED = dataclasses.replace(COST, stt_sell_rate=STT_RATE)

with open(CACHE / "dataset.pkl", "rb") as handle:
    DATA = pickle.load(handle)
DAYS: list[str] = DATA["days"]
INFO = DATA["info"]


class DayStrategy:
    """One strategy on one day: leg price matrices (legs x 360 minutes) and the marks derived from them."""

    def __init__(self, day: str, strategy: str, specs: list[dict]):
        self.day, self.strategy = day, strategy
        self.sides = [sp["side"] for sp in specs]
        self.roles = [sp["role"] for sp in specs]
        self.strikes = [sp["strike"] for sp in specs]
        self.types = [sp["type"] for sp in specs]
        self.sign = np.array([-1.0 if sp["side"] == "SELL" else 1.0 for sp in specs])  # pnl of a leg = sign * (price - entry)
        self.o = np.stack([sp["path"]["open"] for sp in specs])
        self.h = np.stack([sp["path"]["high"] for sp in specs])
        self.l = np.stack([sp["path"]["low"] for sp in specs])
        self.c = np.stack([sp["path"]["close"] for sp in specs])
        self.real_ratio = float(np.mean([sp["path"]["real"].mean() for sp in specs]))
        self.entry = self.o[:, 0].copy()
        self.mtm = (self.sign[:, None] * (self.c - self.entry[:, None])).sum(axis=0)  # gross points per unit, marked at each bar's close
        # a bound on the worst mark inside each bar: shorts at the bar's high, longs at its low (legs need not peak together)
        adverse = np.where(self.sign[:, None] < 0, self.h, self.l)
        self.mtm_adverse = (self.sign[:, None] * (adverse - self.entry[:, None])).sum(axis=0)

    def exit_close(self, j: int) -> np.ndarray:
        return self.c[:, j]

    def exit_next_open(self, j: int) -> np.ndarray:
        return self.o[:, j + 1] if j + 1 < N else self.c[:, N - 1]


SIMS: dict[str, dict[str, DayStrategy]] = {d: {s: DayStrategy(d, s, specs) for s, specs in DATA["legs"][d].items()} for d in DAYS}


def settle(ds: DayStrategy, units: int, exit_prices: np.ndarray, basis: str = "fric") -> dict[str, float]:
    """Gross, costs and net in rupees for `units` units exiting at `exit_prices` (one per leg)."""
    if units <= 0:
        return {"gross_rs": 0.0, "brokerage_rs": 0.0, "taxes_rs": 0.0, "charges_rs": 0.0, "spread_rs": 0.0, "net_rs": 0.0, "net_pts": 0.0}
    legs = [{"side": sd, "entry": float(e), "exit": float(x)} for sd, e, x in zip(ds.sides, ds.entry, exit_prices)]
    model = MODEL_RAISED if (basis == "fric" and ds.day >= STT_FROM) else COST
    cost = lab.settle_units(legs, units, model)
    gross_pts = float(np.sum(ds.sign * (np.asarray(exit_prices) - ds.entry)))
    spread = SPREAD_PER_FILL * 2 * len(legs) * units if basis == "fric" else 0.0
    brokerage = model.brokerage_per_order * 2 * len(legs) * (1 + model.gst_rate)  # brokerage plus the GST on it
    gross_rs = gross_pts * units
    net = gross_rs - spread - cost["charges_rs"]
    return {
        "gross_rs": gross_rs, "brokerage_rs": brokerage, "taxes_rs": cost["charges_rs"] - brokerage, "charges_rs": cost["charges_rs"],
        "spread_rs": spread, "net_rs": net, "net_pts": net / units,
    }


_cache: dict = {}


def net_close(day: str, strategy: str, units: int, j: int = N - 1, basis: str = "fric") -> float:
    """Net rupees of holding to the close of bar j (default the baseline 15:29 exit)."""
    key = (day, strategy, units, j, basis)
    if key not in _cache:
        ds = SIMS[day][strategy]
        _cache[key] = settle(ds, units, ds.exit_close(j), basis)["net_rs"]
    return _cache[key]


def net_stop(day: str, strategy: str, units: int, j: int, basis: str = "fric") -> float:
    """Net rupees when a stop signalled at the close of bar j is filled at the next bar's open."""
    key = (day, strategy, units, "stop", j, basis)
    if key not in _cache:
        ds = SIMS[day][strategy]
        _cache[key] = settle(ds, units, ds.exit_next_open(j), basis)["net_rs"]
    return _cache[key]


def first_breach(ds: DayStrategy, units: int, loss_rs: float, extreme: bool = False) -> int | None:
    """First bar index whose mark is at or below -loss_rs (None if it never is). Close-marked unless `extreme`."""
    series = ds.mtm_adverse if extreme else ds.mtm
    hits = np.nonzero(series * units <= -loss_rs)[0]
    return int(hits[0]) if hits.size else None
