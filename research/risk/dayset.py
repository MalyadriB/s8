"""Step 2: the day-level research dataset - one row per trading day with everything known before 09:30 and every outcome.

Writes results/day_dataset.csv (wide), results/paths_<S>.csv.gz (the 1-minute mark-to-market path of each strategy) and cache/days.pkl.
Every feature uses only prices from bars that CLOSED before 09:30 (bar opens at 09:15/09:20/09:25/09:30 and the highs/lows of the 09:15-09:29 bars)
plus the previous session, and the 09:30 option prices the strategy itself trades on.  Nothing after 09:30 is a feature.
"""
from __future__ import annotations

import math
import pickle

import numpy as np
import pandas as pd

from engine import *  # noqa: F403

rows = []
premium_hist: list[float] = []
for day in DAYS:
    info, spot = INFO[day], DATA["spot"][day]
    bars, prev = spot["bars"], spot["prev"]
    pre = [bars[t] for t in sorted(bars) if "09:15" <= t < "09:30"]
    prev_close = prev[max(t for t in prev if t <= "15:29")]["close"]
    prev_high = max(b["high"] for t, b in prev.items() if "09:15" <= t <= "15:29")
    prev_low = min(b["low"] for t, b in prev.items() if "09:15" <= t <= "15:29")
    p0915, p0920, p0925, p0930 = (bars[t]["open"] for t in ("09:15", "09:20", "09:25", "09:30"))
    premium = info["premium"]
    premium_pct = 100 * premium / p0930
    r = {
        "date": day, "dow": pd.Timestamp(day).day_name()[:3], "is_expiry": info["is_expiry"], "dte": info["dte"], "expiry": info["expiry"],
        "prev_close": prev_close, "prev_high": prev_high, "prev_low": prev_low, "prev_range": prev_high - prev_low, "prev_range_pct": 100 * (prev_high - prev_low) / prev_close,
        "open_0915": p0915, "gap": p0915 - prev_close, "gap_pct": 100 * (p0915 - prev_close) / prev_close,
        "p0915": p0915, "p0920": p0920, "p0925": p0925, "p0930": p0930,
        "move_0915_0930": p0930 - p0915, "move_pct": 100 * (p0930 - p0915) / p0915,
        "range_0915_0930": max(b["high"] for b in pre) - min(b["low"] for b in pre),
        "atm": info["atm"], "ce_0930": info["ce_0930"], "pe_0930": info["pe_0930"], "premium": premium, "premium_pct": premium_pct,
        "ce_pe_ratio": info["ce_0930"] / info["pe_0930"], "ce_pe_diff": info["ce_0930"] - info["pe_0930"], "ce_pe_imb": (info["ce_0930"] - info["pe_0930"]) / premium,
        "n_strikes_listed": info["n_strikes_listed"], "spot_source": info["spot_source"],
    }
    r["gap_abs_pct"] = abs(r["gap_pct"])
    r["move_abs_pct"] = abs(r["move_pct"])
    r["range_pct"] = 100 * r["range_0915_0930"] / p0930
    r["ce_pe_imb_abs"] = abs(r["ce_pe_imb"])
    r["range_over_premium"] = r["range_0915_0930"] / premium
    r["move_over_premium"] = abs(r["move_0915_0930"]) / premium
    r["premium_pctile"] = (sum(1 for v in premium_hist if v <= premium_pct) / len(premium_hist)) if len(premium_hist) >= 60 else float("nan")  # past days only
    premium_hist.append(premium_pct)
    r["gap_dir"] = "flat" if abs(r["gap_pct"]) < 0.10 else ("strong_up" if r["gap_pct"] >= 0.5 else "mild_up" if r["gap_pct"] > 0 else "strong_down" if r["gap_pct"] <= -0.5 else "mild_down")
    r["open_move_dir"] = "up" if r["move_0915_0930"] > 0 else "down" if r["move_0915_0930"] < 0 else "flat"
    for s in STRATEGIES:
        tag = SHORT[s]
        ds = SIMS[day].get(s)
        if ds is None:
            continue
        if s != "straddle_sell":
            r[f"{tag}_width"] = abs(ds.strikes[0] - info["atm"])
            r[f"{tag}_protected_spread"] = r[f"{tag}_width"]
        legs_txt = " | ".join(f"{sd} {ty} {k:g} {e:g}" for sd, ty, k, e in zip(ds.sides, ds.types, ds.strikes, ds.entry))
        r[f"{tag}_legs_entry"] = legs_txt
        cost = settle(ds, UNITS, ds.exit_close(N - 1), "fric")
        proj = settle(ds, UNITS, ds.exit_close(N - 1), "proj")
        pnl_rs = ds.mtm * UNITS
        i_lo, i_hi = int(np.argmin(pnl_rs)), int(np.argmax(pnl_rs))
        r.update({
            f"{tag}_gross_rs": round(cost["gross_rs"], 2), f"{tag}_brokerage_rs": round(cost["brokerage_rs"], 2), f"{tag}_taxes_rs": round(cost["taxes_rs"], 2),
            f"{tag}_spread_rs": round(cost["spread_rs"], 2), f"{tag}_net_rs": round(cost["net_rs"], 2), f"{tag}_net_pts": round(cost["net_pts"], 4),
            f"{tag}_net_proj_rs": round(proj["net_rs"], 2),
            f"{tag}_net_1lot": round(net_close(day, s, UNIT, N - 1), 2), f"{tag}_net_2lot": round(net_close(day, s, 2 * UNIT, N - 1), 2),
            f"{tag}_max_loss_rs": round(float(pnl_rs[i_lo]), 2), f"{tag}_t_max_loss": MINUTES[i_lo],
            f"{tag}_max_profit_rs": round(float(pnl_rs[i_hi]), 2), f"{tag}_t_max_profit": MINUTES[i_hi],
            f"{tag}_max_loss_extreme_rs": round(float(ds.mtm_adverse.min() * UNITS), 2),
            f"{tag}_real_bar_ratio": round(ds.real_ratio, 3),
        })
    rows.append(r)

df = pd.DataFrame(rows).set_index("date")
for s in STRATEGIES:
    tag = SHORT[s]
    net = df[f"{tag}_net_rs"]
    eq = net.fillna(0).cumsum()
    peak = eq.cummax().clip(lower=0)
    dd = peak - eq
    df[f"{tag}_equity"] = eq.round(2)
    df[f"{tag}_drawdown"] = dd.round(2)
    df[f"{tag}_dd_increase"] = (dd - dd.shift(1).fillna(0)).clip(lower=0).round(2)
    df[f"{tag}_win"] = (net > 0).astype("float").where(net.notna())
    thresh = net.quantile(0.05)
    df[f"{tag}_extreme_loss"] = (net <= thresh).astype("float").where(net.notna())  # worst 5% of that strategy's days (descriptive flag, full sample)
lost = pd.concat([(df[f"{SHORT[s]}_net_rs"] < 0).astype(float).where(df[f"{SHORT[s]}_net_rs"].notna()) for s in STRATEGIES], axis=1)
df["strategies_lost"] = lost.sum(axis=1, min_count=3)
df["all_three_lost"] = (df["strategies_lost"] == 3).astype(float).where(df["strategies_lost"].notna())
df["only_one_lost"] = (df["strategies_lost"] == 1).astype(float).where(df["strategies_lost"].notna())
df.to_csv(RESULTS / "day_dataset.csv")
df.to_pickle(CACHE / "days.pkl")

for s in STRATEGIES:  # the actual 1-minute path, so intraday rules can be re-simulated outside this code
    recs = []
    for day in DAYS:
        ds = SIMS[day].get(s)
        if ds is None:
            continue
        recs.append(pd.DataFrame({"date": day, "time": MINUTES, "mtm_pts": np.round(ds.mtm, 4), "mtm_rs_195": np.round(ds.mtm * UNITS, 2), "mtm_adverse_rs_195": np.round(ds.mtm_adverse * UNITS, 2)}))
    pd.concat(recs).to_csv(RESULTS / f"paths_{SHORT[s]}.csv.gz", index=False)

print(df.shape, "columns")
for s in STRATEGIES:
    tag = SHORT[s]
    print(tag, summarize(df[f"{tag}_net_rs"].dropna().tolist()), "| project basis net:", round(df[f"{tag}_net_proj_rs"].sum(), 2))
print("features with NaN:", {c: int(df[c].isna().sum()) for c in df.columns if df[c].isna().any() and not c.startswith(("S2", "S3"))})
