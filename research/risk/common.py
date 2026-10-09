"""Shared helpers for the S1/S2/S3 risk-reduction research.

Read-only against the project's data: the SQLite market database is opened with mode=ro and the NIFTY 1-minute CSV is only read.
Nothing here places, stages or simulates a broker order; strategy rules and the cost model are imported unchanged from backend/strategy_lab.py.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT.parents[1] / "backend"
CACHE = ROOT / "cache"
RESULTS = ROOT / "results"
CACHE.mkdir(exist_ok=True)
RESULTS.mkdir(exist_ok=True)
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("DATA_DIR", str(BACKEND / "data"))  # the project's database, not a fresh empty one relative to this folder

import strategy_lab as lab  # noqa: E402  (the project's own rules, plan_legs, wing_width, cost model)

DB_PATH = BACKEND / "data" / "market.db"
NIFTY_CSV = BACKEND / "data" / "exports" / "nifty_1min.csv"
INDEX_KEY = lab.UNDERLYING.index_key
STRATEGIES = list(lab.STRATEGIES)  # straddle_sell, iron_fly_w1, iron_fly_w2
SHORT = {"straddle_sell": "S1", "iron_fly_w1": "S2", "iron_fly_w2": "S3"}
UNIT = 65  # units per lot in this research (the project's current NIFTY lot size)
LOTS = 3  # primary comparison size
UNITS = UNIT * LOTS
COST = lab.DEFAULT_COST_MODEL


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# the 360 one-minute bars of the trade: entry bar 09:30 (entered at its open) through the exit bar 15:29 (exited at its close)
MINUTES = [hhmm(m) for m in range(9 * 60 + 30, 15 * 60 + 30)]
M_INDEX = {t: i for i, t in enumerate(MINUTES)}
N = len(MINUTES)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_spot_csv() -> dict[str, dict[str, dict[str, float]]]:
    """{day: {'HH:MM': {open, high, low, close}}} from the project's NIFTY 1-minute export."""
    days: dict[str, dict[str, dict[str, float]]] = {}
    with NIFTY_CSV.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for stamp, open_, high, low, close, *_ in reader:
            days.setdefault(stamp[:10], {})[stamp[11:16]] = {"open": float(open_), "high": float(high), "low": float(low), "close": float(close)}
    return days


def save_json(name: str, payload: Any) -> Path:
    path = RESULTS / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


# ------------------------------------------------------------------ statistics


def mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else float("nan")


def stdev(values) -> float:
    values = list(values)
    if len(values) < 2:
        return float("nan")
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def t_value(values) -> float:
    """Mean over its standard error; every day of the sample counts (a skipped day is a 0)."""
    values = list(values)
    n = len(values)
    s = stdev(values)
    return mean(values) / (s / math.sqrt(n)) if n > 1 and s and s == s else float("nan")


def median(values) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    k = len(s) // 2
    return s[k] if len(s) % 2 else (s[k - 1] + s[k]) / 2


def quantile(values, q: float) -> float:
    """Linear-interpolated quantile (q in 0..1)."""
    s = sorted(values)
    if not s:
        return float("nan")
    pos = q * (len(s) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def max_drawdown(daily) -> float:
    """Largest fall of the cumulative net below its running peak (peak starts at 0), as a positive rupee number."""
    total = peak = worst = 0.0
    for value in daily:
        total += value
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def summarize(daily, traded: int | None = None) -> dict[str, float]:
    """The comparison-table row for a list of per-day net results (0 for skipped days). `traded` = days a position was opened."""
    daily = list(daily)
    n = len(daily)
    return {
        "days": n,
        "traded": traded if traded is not None else sum(1 for v in daily if v != 0),
        "net": round(sum(daily), 2),
        "avg_day": round(mean(daily), 2) if n else float("nan"),
        "median_day": round(median(daily), 2) if n else float("nan"),
        "win_pct": round(100 * sum(1 for v in daily if v > 0) / n, 1) if n else float("nan"),
        "worst_day": round(min(daily), 2) if n else float("nan"),
        "max_dd": round(max_drawdown(daily), 2),
        "t": round(t_value(daily), 2) if n > 1 else float("nan"),
    }
