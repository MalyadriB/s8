"""Modelled S1 backtest for 2022-01 .. 2024-09 (no option candles exist for that period on Upstox), validated on the 479 real days.

Model: at 09:30 sell the ATM straddle priced by Black-Scholes from the actual NIFTY spot (09:30 open) and an implied vol = m(dte) x India VIX (09:29 close); at 15:29 buy it back priced from the
actual spot (15:29 close) and either the same vol ("frozen IV") or m(dte) x the 15:29 India VIX ("tracking IV").  m(dte) is the median of (implied vol from the REAL premium / VIX) over the
TRAINING part of the real period only.  Nothing about the real option candles is used for 2022-2024.
Costs: the project's statutory charges on the modelled premiums + 0.10 points spread per fill (the 20 Sep 2026 frictions basis), 3 lots x 65 units.
"""
from __future__ import annotations

import csv
import math
import pickle
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RISK = HERE.parent / "risk"
sys.path.insert(0, str(RISK))
from common import lab, load_spot_csv, summarize, t_value, max_drawdown  # noqa: E402

UNITS = 195
R = 0.065
SPREAD = 0.10
STT_FROM, STT_RATE = "2026-04-01", 0.0015
import dataclasses  # noqa: E402

MODEL_RAISED = dataclasses.replace(lab.DEFAULT_COST_MODEL, stt_sell_rate=STT_RATE)
TRAIN_END = "2025-12-08"


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S: float, K: float, T: float, sigma: float) -> tuple[float, float]:
    """(call, put) with forward carry r."""
    if T <= 1e-9 or sigma <= 0:
        return max(S - K, 0.0), max(K - S, 0.0)
    d1 = (math.log(S / K) + (R + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    disc = math.exp(-R * T)
    return S * ncdf(d1) - K * disc * ncdf(d2), K * disc * ncdf(-d2) - S * ncdf(-d1)


def implied_sigma(S: float, K: float, T: float, straddle: float) -> float:
    lo, hi = 0.01, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        c, p = bs(S, K, T, mid)
        if c + p < straddle:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------- data
def load_csv_days(path: Path) -> dict:
    days: dict = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader)
        for stamp, o, h, l, c, *_ in reader:
            t = stamp[11:16]
            if "09:15" <= t <= "15:29":
                days.setdefault(stamp[:10], {})[t] = {"open": float(o), "high": float(h), "low": float(l), "close": float(c)}
    return days


spot = load_csv_days(HERE / "cache" / "nifty_1min_2022_to_2023-09-05.csv")
proj = load_spot_csv()
for d, bars in proj.items():
    spot.setdefault(d, bars)
vix = load_csv_days(HERE / "cache" / "india_vix_1min.csv")
with open(RISK / "cache" / "dataset.pkl", "rb") as handle:
    D = pickle.load(handle)
real_days = D["days"]
real_info = D["info"]
spot.update({d: D["spot"][d]["bars"] for d in real_days if d not in spot or "09:30" not in spot.get(d, {})})  # 2026-09-18 comes from the 5-minute series
trading_days = sorted(d for d, b in spot.items() if "09:30" in b and "15:29" in b and d >= "2022-01-03")
print("trading days with spot:", len(trading_days), trading_days[0], "->", trading_days[-1])


def expiry_for(day: str) -> str:
    """Weekly NIFTY expiry rule: Thursday (Tuesday from Sep 2025); a holiday moves it to the previous trading day. Real expiries are used on the real days."""
    if day in real_info:
        return real_info[day]["expiry"]
    d = date.fromisoformat(day)
    target_wd = 3 if day < "2025-09-01" else 1
    cand = d + timedelta(days=(target_wd - d.weekday()) % 7)
    for _ in range(12):
        # the expiry date is the last TRADING day at or before the nominal date (holiday -> earlier)
        nominal = cand
        while nominal.isoformat() not in spot and nominal > d:
            nominal -= timedelta(days=1)
        if nominal >= d:
            return nominal.isoformat()
        cand += timedelta(days=7)
    return cand.isoformat()


# how good is the calendar rule where the real expiry is known?
rule_hits = 0
for d in real_days:
    dd = date.fromisoformat(d)
    target_wd = 3 if d < "2025-09-01" else 1
    cand = dd + timedelta(days=(target_wd - dd.weekday()) % 7)
    nominal = cand
    while nominal.isoformat() not in spot and nominal > dd:
        nominal -= timedelta(days=1)
    rule_hits += (nominal.isoformat() == real_info[d]["expiry"])
print(f"calendar rule reproduces the real nearest expiry on {rule_hits} of {len(real_days)} real days")


def T_years(now: datetime, expiry: str) -> float:
    end = datetime.fromisoformat(f"{expiry}T15:30:00")
    return max((end - now).total_seconds(), 60.0) / (365 * 24 * 3600)


rows = []
for d in trading_days:
    b, v = spot[d], vix.get(d)
    if not v or "09:29" not in v or "15:29" not in v:
        rows.append({"date": d, "skip": "no VIX bars"})
        continue
    S0, S1 = b["09:30"]["open"], b["15:29"]["close"]
    K = float(lab.resolve_atm_strike(S0, 50.0))
    exp = expiry_for(d)
    dte = (date.fromisoformat(exp) - date.fromisoformat(d)).days
    T0 = T_years(datetime.fromisoformat(f"{d}T09:30:00"), exp)
    T1 = T_years(datetime.fromisoformat(f"{d}T15:29:59"), exp)
    rows.append({"date": d, "S0": S0, "S1": S1, "K": K, "expiry": exp, "dte": dte, "T0": T0, "T1": T1, "vix0": v["09:29"]["close"], "vix1": v["15:29"]["close"], "real": d in real_info})
df = pd.DataFrame(rows).set_index("date")
print("days skipped (no VIX):", int(df["skip"].notna().sum()) if "skip" in df else 0)
df = df[df["S0"].notna()].copy()

# real premium (only on real days) -> implied vol ratio to VIX
ratio = {}
for d in df.index[df["real"]]:
    r = df.loc[d]
    real_prem = real_info[d]["premium"]
    ratio[d] = implied_sigma(r["S0"], r["K"], r["T0"], real_prem) / (r["vix0"] / 100)
df["ratio"] = pd.Series(ratio)
train_idx = [d for d in df.index if df.loc[d, "real"] and d <= TRAIN_END]
bucket = lambda dte: min(int(dte), 4)  # 0,1,2,3,4+
m_by = {b_: float(np.nanmedian([df.loc[d, "ratio"] for d in train_idx if bucket(df.loc[d, "dte"]) == b_])) for b_ in range(5)}
print("implied vol / India VIX by days-to-expiry (median over the real TRAINING days):", {k: round(v, 2) for k, v in m_by.items()})


def cost(day: str, legs, units: int = UNITS) -> float:
    model = MODEL_RAISED if day >= STT_FROM else lab.DEFAULT_COST_MODEL
    return lab.settle_units(legs, units, model)["charges_rs"]


def model_day(d: str, tracking: bool) -> dict:
    r = df.loc[d]
    m = m_by[bucket(r["dte"])]
    s0 = m * r["vix0"] / 100
    s1 = m * r["vix1"] / 100 if tracking else s0
    c0, p0 = bs(r["S0"], r["K"], r["T0"], s0)
    c1, p1 = bs(r["S1"], r["K"], r["T1"], s1)
    legs = [{"side": "SELL", "entry": c0, "exit": c1}, {"side": "SELL", "entry": p0, "exit": p1}]
    gross_pts = (c0 + p0) - (c1 + p1)
    net = gross_pts * UNITS - SPREAD * 4 * UNITS - cost(d, legs)
    return {"premium": c0 + p0, "close_value": c1 + p1, "gross_pts": gross_pts, "net": net}


for label, tracking in (("frozen", False), ("tracking", True)):
    res = {d: model_day(d, tracking) for d in df.index}
    df[f"model_{label}_net"] = pd.Series({d: v["net"] for d, v in res.items()})
    df[f"model_{label}_gross_pts"] = pd.Series({d: v["gross_pts"] for d, v in res.items()})
    df[f"model_premium"] = pd.Series({d: v["premium"] for d, v in res.items()})
    df[f"model_{label}_close"] = pd.Series({d: v["close_value"] for d, v in res.items()})

# actual S1 (frictions basis) on the real days
act = pd.read_csv(RISK / "results" / "day_dataset.csv").set_index("date")
df["actual_net"] = act["S1_net_rs"]
df["actual_gross_pts"] = act["S1_gross_rs"] / UNITS
df["actual_premium"] = pd.Series({d: real_info[d]["premium"] for d in real_days})
df.to_csv(HERE / "model_days.csv")

# ---------------------------------------------------------------- how well does the model reproduce the REAL days?
print("\n=== MODEL vs REAL on the real days (2024-10 .. 2026-09) ===")
real = df[df["real"] & df["actual_net"].notna()]
for label in ("frozen", "tracking"):
    for name, sub in (("train (to 2025-12-08)", real[real.index <= TRAIN_END]), ("validation", real[real.index > TRAIN_END]), ("all real days", real)):
        a, m = sub["actual_gross_pts"], sub[f"model_{label}_gross_pts"]
        print(f"{label:9s} {name:22s} n={len(sub):3d} corr(daily gross pts) {a.corr(m):5.2f} | mean actual {a.mean():7.2f} model {m.mean():7.2f} pts | "
              f"net actual {sub['actual_net'].sum():>10,.0f} model {sub[f'model_{label}_net'].sum():>10,.0f} | win% actual {100*(sub['actual_net']>0).mean():4.1f} model {100*(sub[f'model_{label}_net']>0).mean():4.1f}")
sub = real
print("premium: model vs real  corr", round(sub["model_premium"].corr(sub["actual_premium"]), 3), "| mean abs error %", round(float((abs(sub["model_premium"] - sub["actual_premium"]) / sub["actual_premium"]).mean() * 100), 1))
exp_days = real[real["dte"] == 0]
oth = real[real["dte"] > 0]
for label in ("frozen", "tracking"):
    print(f"{label:9s} expiry days n={len(exp_days)}: corr {exp_days['actual_gross_pts'].corr(exp_days[f'model_{label}_gross_pts']):.2f}, mean actual {exp_days['actual_gross_pts'].mean():.1f} model {exp_days[f'model_{label}_gross_pts'].mean():.1f} | "
          f"other days n={len(oth)}: corr {oth['actual_gross_pts'].corr(oth[f'model_{label}_gross_pts']):.2f}, mean actual {oth['actual_gross_pts'].mean():.1f} model {oth[f'model_{label}_gross_pts'].mean():.1f}")

# ---------------------------------------------------------------- the extension: 2022-01 .. 2024-10-02
pre = df[(~df["real"]) & (df.index < "2024-10-03")]
print(f"\n=== MODELLED S1, {pre.index[0]} .. {pre.index[-1]}  ({len(pre)} trading days; no real option prices exist) ===")
for label in ("frozen", "tracking"):
    s = pre[f"model_{label}_net"]
    print(f"{label:9s}", summarize(s.tolist()))
by_year = pre.groupby(pre.index.str[:4])[[f"model_frozen_net", f"model_tracking_net"]].agg(["sum", "count"])
print(by_year.round(0).to_string())
print("\nfor reference, the model on the real days ->", {lab_: summarize(real[f'model_{lab_}_net'].tolist())['net'] for lab_ in ('frozen', 'tracking')}, "actual", round(real['actual_net'].sum()))


# ================================================================================================
# Bias-corrected model. The plain model misses the real "richness" of option premiums (points a day). Calibrate an additive bias per days-to-expiry bucket on the
# real TRAINING days, test it on the real VALIDATION days, then extend to 2022-2024 with the SAME bias and with weaker biases (the unknowable part).
# ================================================================================================
print("\n\n############ bias-corrected model ############")
real = df[df["real"] & df["actual_net"].notna()].copy()
tr = real[real.index <= TRAIN_END]
bias = {b_: float((tr[tr["dte"].map(bucket) == b_]["actual_gross_pts"] - tr[tr["dte"].map(bucket) == b_]["model_tracking_gross_pts"]).mean()) for b_ in range(5)}
print("additive bias (real minus model gross points/day) by days-to-expiry, from TRAINING days:", {k: round(v, 1) for k, v in bias.items()})


def corrected_net(row, lam=1.0):
    b_ = bias[bucket(row["dte"])] * lam
    gross = row["model_tracking_gross_pts"] + b_
    # costs from the same modelled legs: re-derive charges (independent of the correction) via the net identity
    charges_and_spread = row["model_tracking_gross_pts"] * UNITS - row["model_tracking_net"]
    return (gross * UNITS) - charges_and_spread


for lam in (1.0,):
    df[f"corr{lam}_net"] = df.apply(lambda r: corrected_net(r, lam), axis=1)
real = df[df["real"] & df["actual_net"].notna()]
print("\nvalidation of the corrected model on REAL days (bias fitted on the training days only):")
for name, sub in (("train (fitted)", real[real.index <= TRAIN_END]), ("VALIDATION (unseen)", real[real.index > TRAIN_END])):
    a, m = sub["actual_net"], sub["corr1.0_net"]
    print(f"  {name:20s} n={len(sub)} actual net {a.sum():>10,.0f} | model net {m.sum():>10,.0f} ({100*m.sum()/a.sum():5.1f}%) | corr daily net {a.corr(m):.2f} | worst day actual {a.min():>9,.0f} model {m.min():>9,.0f} | "
          f"win% {100*(a>0).mean():.1f} vs {100*(m>0).mean():.1f} | max DD actual {max_drawdown(a.tolist()):>9,.0f} model {max_drawdown(m.tolist()):>9,.0f}")

pre = df[(~df["real"]) & (df.index < "2024-10-03")]
print(f"\nEXTENSION {pre.index[0]} .. {pre.index[-1]} ({len(pre)} days), bias-corrected model, by strength of the bias assumed (1.0 = same premium richness as 2024-26):")
ext = {}
for lam in (0.0, 0.25, 0.5, 0.75, 1.0):
    s = pre.apply(lambda r: corrected_net(r, lam), axis=1)
    ext[lam] = s
    m = summarize(s.tolist())
    print(f"  bias x{lam:4.2f}: net {m['net']:>11,.0f}  avg/day {m['avg_day']:>7,.0f}  win% {m['win_pct']:>5}  worst {m['worst_day']:>9,.0f}  max DD {m['max_dd']:>10,.0f}  t {m['t']}")
# break-even richness
lo, hi = 0.0, 1.5
for _ in range(40):
    mid = (lo + hi) / 2
    if pre.apply(lambda r: corrected_net(r, mid), axis=1).sum() < 0:
        lo = mid
    else:
        hi = mid
print(f"  break-even: 2022-2024 needed {hi:.2f} x the 2024-26 premium richness for S1 to have broken even")
s1x = ext[1.0]
print("\nby calendar year (bias x1.0):")
print(s1x.groupby(s1x.index.str[:4]).agg(["sum", "count", "min"]).round(0).to_string())
print("\nworst 8 modelled days (bias x1.0):")
w = s1x.sort_values().head(8)
for d, v in w.items():
    print(" ", d, round(v), "expiry" if df.loc[d, "dte"] == 0 else f"dte {int(df.loc[d, 'dte'])}", f"open->close move {df.loc[d, 'S1'] - df.loc[d, 'S0']:+.0f}", f"VIX {df.loc[d, 'vix0']:.1f}")

# independent lens (no option model): realised move from 09:30 to 15:29 against what the India VIX implied for that window
print("\nrealised vs VIX-implied intraday move (spot only): mean |S15:29 - S09:30| / (S x VIX x sqrt(dt) x 0.798), by year; below 1.0 = options over-priced for sellers")
dt = (5 * 3600 + 59 * 60) / (365 * 24 * 3600)
df["implied_move"] = df["S0"] * (df["vix0"] / 100) * math.sqrt(dt) * math.sqrt(2 / math.pi)
df["real_move"] = (df["S1"] - df["S0"]).abs()
g = df.groupby(df.index.str[:4]).apply(lambda x: pd.Series({"days": len(x), "realised/implied": x["real_move"].mean() / x["implied_move"].mean(), "expiry days only": x[x["dte"] == 0]["real_move"].mean() / x[x["dte"] == 0]["implied_move"].mean(), "avg VIX": x["vix0"].mean()}))
print(g.round(2).to_string())
df.to_csv(HERE / "model_days.csv")
