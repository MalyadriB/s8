"""Can NSE end-of-day data be made to look like the project's 09:30 -> 15:29 S1?

Two steps, both tested against the 479 real minute days with a chronological train/validation split:
  A. the ATM strike from the 09:30 spot (real data, exact) instead of the 09:15 spot;
  B. an ESTIMATE of the 09:30 entry price: the day's open premium x a ratio (09:30 premium / open premium) learned on the real TRAINING days by days-to-expiry.
The exit stays NSE's close.  Nothing is fitted on the validation days.
"""
from __future__ import annotations

import csv
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RISK = HERE.parent / "risk"
sys.path.insert(0, str(RISK))
from common import lab, load_spot_csv, summarize  # noqa: E402

UNITS, TRAIN_END = 195, "2025-12-08"
opts = pd.read_csv(HERE / "cache" / "nse_nifty_options_daily.csv")
for c in ("open", "close", "contracts", "strike"):
    opts[c] = pd.to_numeric(opts[c], errors="coerce")
by_day = {d: g for d, g in opts.groupby("date")}

spot915, spot930 = {}, {}
with (HERE / "cache" / "nifty_1min_2022_to_2023-09-05.csv").open(encoding="utf-8") as f:
    r = csv.reader(f)
    next(r)
    for stamp, o, *_ in r:
        t = stamp[11:16]
        if t == "09:15":
            spot915[stamp[:10]] = float(o)
        elif t == "09:30":
            spot930[stamp[:10]] = float(o)
for d, bars in load_spot_csv().items():
    if "09:15" in bars and "09:30" in bars:
        spot915.setdefault(d, bars["09:15"]["open"])
        spot930.setdefault(d, bars["09:30"]["open"])
with open(RISK / "cache" / "dataset.pkl", "rb") as f:
    D = pickle.load(f)
for d in D["days"]:
    b = D["spot"][d]["bars"]
    spot915.setdefault(d, b["09:15"]["open"])
    spot930.setdefault(d, b["09:30"]["open"])
real = set(D["days"])


def straddle(g, expiry, atm):
    ce = g[(g["expiry"] == expiry) & (g["strike"] == atm) & (g["type"] == "CE")]
    pe = g[(g["expiry"] == expiry) & (g["strike"] == atm) & (g["type"] == "PE")]
    if ce.empty or pe.empty:
        return None
    c, p = ce.iloc[0], pe.iloc[0]
    if min(c["open"], p["open"], c["close"], p["close"]) <= 0:
        return None
    return c["open"] + p["open"], c["close"] + p["close"]


rows = []
for day, g in by_day.items():
    if day not in spot915 or day not in spot930:
        continue
    exps = [e for e in sorted(g.loc[g["contracts"] > 0, "expiry"].unique()) if e >= day]
    if not exps:
        continue
    exp = exps[0]
    a915 = float(lab.resolve_atm_strike(spot915[day], 50.0))
    a930 = float(lab.resolve_atm_strike(spot930[day], 50.0))
    s915, s930 = straddle(g, exp, a915), straddle(g, exp, a930)
    if s915 is None or s930 is None:
        continue
    rows.append({"date": day, "dte": (pd.Timestamp(exp) - pd.Timestamp(day)).days, "same_strike": a915 == a930, "open915": s915[0], "close915": s915[1], "open930strike": s930[0], "close930strike": s930[1],
                 "dspot": spot930[day] - spot915[day], "real": day in real})
df = pd.DataFrame(rows).set_index("date").sort_index()
act = pd.read_csv(RISK / "results" / "day_dataset.csv").set_index("date")
df["actual_gross_pts"] = act["S1_gross_rs"] / UNITS
df["actual_premium"] = pd.Series({d: D["info"][d]["premium"] for d in D["days"]})
bucket = lambda d: min(int(d), 4)
df["b"] = df["dte"].map(bucket)
train = df[df["real"] & (df.index <= TRAIN_END) & df["actual_premium"].notna()]
ratio = {b: float((train[train["b"] == b]["actual_premium"] / train[train["b"] == b]["open930strike"]).median()) for b in range(5)}
print("entry ratio (real 09:30 premium / NSE open premium at the 09:30 strike), median on the TRAINING days by days-to-expiry:", {k: round(v, 3) for k, v in ratio.items()})
df["entry_est"] = df["open930strike"] * df["b"].map(ratio)

variants = {
    "1 as loaded: 09:15 strike, open entry, NSE close": (df["open915"] - df["close915"]),
    "2 + strike from the 09:30 spot": (df["open930strike"] - df["close930strike"]),
    "3 + entry estimated to 09:30": (df["entry_est"] - df["close930strike"]),
}
print(f"\nvs the project's real minute S1 (gross points per unit per day); train = real days to {TRAIN_END}, validation = after")
print(f"{'variant':52s} {'set':11s} {'n':>4s} {'corr':>5s} {'mean pts':>9s} {'minute mean':>11s} {'sum/minute sum':>14s}")
for name, series in variants.items():
    for label, sub in (("train", df[df["real"] & (df.index <= TRAIN_END) & df["actual_gross_pts"].notna()]), ("validation", df[df["real"] & (df.index > TRAIN_END) & df["actual_gross_pts"].notna()])):
        a, m = sub["actual_gross_pts"], series.loc[sub.index]
        print(f"{name:52s} {label:11s} {len(sub):4d} {a.corr(m):5.2f} {m.mean():9.2f} {a.mean():11.2f} {m.sum() / a.sum():14.2f}")
best = variants["3 + entry estimated to 09:30"]
val = df[df["real"] & (df.index > TRAIN_END) & df["actual_gross_pts"].notna()]
err = (best.loc[val.index] - val["actual_gross_pts"])
print(f"\nvariant 3 on validation: mean error {err.mean():+.2f} pts/day, typical daily error (mean absolute) {err.abs().mean():.1f} pts vs typical daily move of the real result {val['actual_gross_pts'].abs().mean():.1f} pts")
print("same-strike days:", int(df[df['real']]['same_strike'].sum()), "of", int(df['real'].sum()), "real days (the strike differs on the rest)")
df.to_csv(HERE / "adjusted_0930_days.csv")

# ---- what the adjusted series says about 2022 .. 2024-10-02
pre = df[~df["real"] & (df.index < "2024-10-03")]
adj = (pre["entry_est"] - pre["close930strike"]) * UNITS
orig = (pre["open915"] - pre["close915"]) * UNITS
print(f"\n2022-01..2024-10-02 ({len(pre)} days), gross rupees at 195 units (before costs):  as loaded {orig.sum():,.0f}  ->  estimated 09:30 {adj.sum():,.0f}")
E = lambda s: summarize(s.tolist())
print("  estimated-09:30 daily gross:", E(adj))
