"""Real-price S1 from NSE end-of-day data, 2022-01 .. today, and how well it matches the minute-level S1 on the real days.

Daily-bar S1: sell the ATM straddle at the day's OPEN (first trade, about 09:15) and buy it back at the day's CLOSE (NSE's closing price), ATM = nearest 50 to the 09:15 spot.
This is NOT the project's S1 (09:30 entry, 15:29 exit) - it is the closest thing real 2022-2024 prices allow - so it is validated against the project's S1 on the 479 real days first.
Costs: 3 lots x 65 units, 0.10 points spread per fill, and statutory rates by period (STT 0.05% to Mar 2023, 0.0625% to Sep 2024, 0.10% to Mar 2026, 0.15% after;
NSE transaction charge 0.05% before Oct 2024 - these historical rates are from memory, so treat 2022-24 costs as approximate; they are about Rs 150-250 a day).
"""
from __future__ import annotations

import csv
import dataclasses
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RISK = HERE.parent / "risk"
sys.path.insert(0, str(RISK))
from common import connect, lab, load_spot_csv, max_drawdown, summarize  # noqa: E402

UNITS, SPREAD = 195, 0.10
opts = pd.read_csv(HERE / "cache" / "nse_nifty_options_daily.csv")
for c in ("open", "high", "low", "close", "last", "settle", "contracts", "oi", "strike"):
    opts[c] = pd.to_numeric(opts[c], errors="coerce")
days_have = sorted(opts["date"].unique())
print("NSE days loaded:", len(days_have), days_have[0], "->", days_have[-1])

# spot 09:15 open
spot = {}
with (HERE / "cache" / "nifty_1min_2022_to_2023-09-05.csv").open(encoding="utf-8") as f:
    r = csv.reader(f)
    next(r)
    for stamp, o, *_ in r:
        if stamp[11:16] == "09:15":
            spot[stamp[:10]] = float(o)
for d, bars in load_spot_csv().items():
    if "09:15" in bars:
        spot.setdefault(d, bars["09:15"]["open"])
with open(RISK / "cache" / "dataset.pkl", "rb") as f:
    D = pickle.load(f)
for d in D["days"]:
    spot.setdefault(d, D["spot"][d]["bars"]["09:15"]["open"])
real_days = set(D["days"])


def model_for(day: str) -> "lab.CostModel":
    stt = 0.0005 if day < "2023-04-01" else 0.000625 if day < "2024-10-01" else 0.001 if day < "2026-04-01" else 0.0015
    exch = 0.0005 if day < "2024-10-01" else lab.DEFAULT_COST_MODEL.exchange_txn_rate
    return dataclasses.replace(lab.DEFAULT_COST_MODEL, stt_sell_rate=stt, exchange_txn_rate=exch)


rows, skipped = [], []
by_day = {d: g for d, g in opts.groupby("date")}
for day in days_have:
    g = by_day[day]
    if day not in spot:
        skipped.append((day, "no spot"))
        continue
    expiries = sorted(g.loc[g["contracts"] > 0, "expiry"].unique())
    expiries = [e for e in expiries if e >= day]
    if not expiries:
        skipped.append((day, "no expiry with trades"))
        continue
    exp = expiries[0]
    atm = float(lab.resolve_atm_strike(spot[day], 50.0))
    ce = g[(g["expiry"] == exp) & (g["strike"] == atm) & (g["type"] == "CE")]
    pe = g[(g["expiry"] == exp) & (g["strike"] == atm) & (g["type"] == "PE")]
    if ce.empty or pe.empty or not (ce.iloc[0]["open"] > 0 and pe.iloc[0]["open"] > 0 and ce.iloc[0]["close"] > 0 and pe.iloc[0]["close"] > 0):
        skipped.append((day, f"no usable open/close for {atm:g} {exp}"))
        continue
    c, p = ce.iloc[0], pe.iloc[0]
    legs = [{"side": "SELL", "entry": c["open"], "exit": c["close"]}, {"side": "SELL", "entry": p["open"], "exit": p["close"]}]
    charges = lab.settle_units(legs, UNITS, model_for(day))["charges_rs"]
    gross_pts = (c["open"] + p["open"]) - (c["close"] + p["close"])
    net = gross_pts * UNITS - SPREAD * 4 * UNITS - charges
    last_alt = None
    if c["last"] == c["last"] and p["last"] == p["last"]:
        last_alt = (c["open"] + p["open"]) - (c["last"] + p["last"])
    rows.append({"date": day, "expiry": exp, "dte": (pd.Timestamp(exp) - pd.Timestamp(day)).days, "atm": atm, "spot0915": spot[day], "entry": c["open"] + p["open"], "exit_close": c["close"] + p["close"],
                 "gross_pts": gross_pts, "gross_pts_last": last_alt, "net": net, "real": day in real_days,
                 "ce_oi": c["oi"], "pe_oi": p["oi"], "ce_vol": c["contracts"], "pe_vol": p["contracts"]})
df = pd.DataFrame(rows).set_index("date")
df.to_csv(HERE / "nse_daily_s1.csv")
print("days built:", len(df), "| skipped:", len(skipped))
for s in skipped[:10]:
    print("   skipped", s)
if len(skipped) > 10:
    print("   ...", len(skipped) - 10, "more")

# ------------------------------------------------------------ validation against the real (minute) S1 on the overlapping days
act = pd.read_csv(RISK / "results" / "day_dataset.csv").set_index("date")
common = df.index.intersection(act.index)
a_g = act.loc[common, "S1_gross_rs"] / UNITS
a_n = act.loc[common, "S1_net_rs"]
d_g = df.loc[common, "gross_pts"]
d_n = df.loc[common, "net"]
print(f"\n=== daily-bar S1 (open->NSE close) vs the project's minute S1 (09:30->15:29) on {len(common)} real days ===")
print(f"corr of daily gross points {a_g.corr(d_g):.2f} | mean pts/day minute {a_g.mean():.2f} daily-bar {d_g.mean():.2f} | net minute {a_n.sum():,.0f} daily-bar {d_n.sum():,.0f} ({100 * d_n.sum() / a_n.sum():.0f}%) "
      f"| win% {100 * (a_n > 0).mean():.1f} vs {100 * (d_n > 0).mean():.1f} | worst {a_n.min():,.0f} vs {d_n.min():,.0f}")
tr = [d for d in common if d <= "2025-12-08"]
va = [d for d in common if d > "2025-12-08"]
for name, idx in (("train", tr), ("validation", va)):
    print(f"   {name:10s} n={len(idx)} corr {a_g.loc[idx].corr(d_g.loc[idx]):.2f} | net minute {a_n.loc[idx].sum():>10,.0f} daily-bar {d_n.loc[idx].sum():>10,.0f}")
if df.loc[common, "gross_pts_last"].notna().any():
    lp = df.loc[common, "gross_pts_last"]
    print(f"   using NSE 'last price' instead of 'close' as the exit: corr {a_g.corr(lp):.2f}, mean {lp.mean():.2f} pts (close-based {d_g.mean():.2f})")

# contract-level: NSE close vs the project's 15:29 candle close; NSE open vs the 09:15 candle open, for the project's own S1 legs
conn = connect()
diffs_close, diffs_open = [], []
for day in common[::3]:
    info = D["info"][day]
    for typ in ("CE", "PE"):
        leg = next(sp for sp in D["legs"][day]["straddle_sell"] if sp["type"] == typ)
        g = by_day.get(day)
        m = g[(g["expiry"] == info["expiry"]) & (g["strike"] == leg["strike"]) & (g["type"] == typ)]
        if m.empty:
            continue
        diffs_close.append(float(m.iloc[0]["close"] - leg["path"]["close"][-1]))
        row = conn.execute("SELECT open FROM market_observations WHERE instrument_key=? AND trading_date=? AND open IS NOT NULL AND instr(received_at,'.')=0 ORDER BY received_at LIMIT 1", (leg["key"], day)).fetchone()
        if row:
            diffs_open.append(float(m.iloc[0]["open"] - row[0]))
print(f"   contract level (sample of {len(diffs_close)} legs): NSE close minus 15:29 candle close: mean {np.mean(diffs_close):+.2f}, mean abs {np.mean(np.abs(diffs_close)):.2f} pts; "
      f"NSE open minus first candle open: mean {np.mean(diffs_open):+.2f}, mean abs {np.mean(np.abs(diffs_open)):.2f} pts")

# ------------------------------------------------------------ the real-price 2022-2024 result
pre = df[df.index < "2024-10-03"]
print(f"\n=== REAL-PRICE daily-bar S1, {pre.index[0]} .. {pre.index[-1]} ({len(pre)} days) ===")
print(summarize(pre["net"].tolist()))
yr = pre.groupby(pre.index.str[:4])["net"].agg(["sum", "count", "min", lambda s: 100 * (s > 0).mean()]).round(0)
yr.columns = ["net", "days", "worst_day", "win%"]
print(yr.to_string())
print("\nworst 8 days:")
for d, v in pre["net"].sort_values().head(8).items():
    print("  ", d, round(v), "expiry-day" if pre.loc[d, "dte"] == 0 else f"dte {int(pre.loc[d, 'dte'])}", "entry", round(pre.loc[d, "entry"], 1), "exit", round(pre.loc[d, "exit_close"], 1))
post = df[df.index >= "2024-10-03"]
print(f"\nfor comparison, the same daily-bar method on {post.index[0]} .. {post.index[-1]} ({len(post)} days):", summarize(post["net"].tolist()))
print("\nby year, all years:")
print(df.groupby(df.index.str[:4])["net"].agg(["sum", "count", lambda s: 100 * (s > 0).mean()]).round(0).to_string())
print("expiry days vs other days (pre-Oct-2024): avg net", round(pre[pre["dte"] == 0]["net"].mean()), "vs", round(pre[pre["dte"] > 0]["net"].mean()))
