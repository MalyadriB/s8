"""The 4-year test on the real-price table: S1, S2, S3 and S4 (daily-bar version), 2022-01-03 .. 2026-09-18.

Daily-bar = every leg entered at the day's OPEN (first trade, about 09:15) and exited at NSE's CLOSE, using the project's OWN rules: ATM = nearest 50 to the 09:15 spot,
iron-fly wings from lab.plan_legs() with W = premium (S2) or 2 x premium (S3) where the premium is the OPEN of the ATM call + put; S4 = S1 on ordinary days, S3 on expiry days.
Costs as in daily_bar_s1.py (3 lots x 65 units, 0.10 points spread per fill, statutory rates by period).  A day where any leg has no traded open or close is skipped for that
strategy and COUNTED - the count is reported so a missing wing cannot silently flatter a strategy.
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
from common import lab, load_spot_csv, max_drawdown, summarize  # noqa: E402

UNITS, SPREAD = 195, 0.10
BAR_T = 3.0  # the t-value S1-S4 are judged at

opts = pd.read_csv(HERE / "cache" / "nse_nifty_options_daily.csv")
for c in ("open", "close", "contracts", "strike"):
    opts[c] = pd.to_numeric(opts[c], errors="coerce")
by_day = {d: g for d, g in opts.groupby("date")}

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


def model_for(day: str):
    stt = 0.0005 if day < "2023-04-01" else 0.000625 if day < "2024-10-01" else 0.001 if day < "2026-04-01" else 0.0015
    exch = 0.0005 if day < "2024-10-01" else lab.DEFAULT_COST_MODEL.exchange_txn_rate
    return dataclasses.replace(lab.DEFAULT_COST_MODEL, stt_sell_rate=stt, exchange_txn_rate=exch)


STRATS = list(lab.STRATEGIES)
rows = []
skips = {s: [] for s in STRATS}
for day, g in by_day.items():
    if day not in spot:
        continue
    expiries = [e for e in sorted(g.loc[g["contracts"] > 0, "expiry"].unique()) if e >= day]
    if not expiries:
        continue
    exp = expiries[0]
    sub = g[g["expiry"] == exp]
    price = {(float(r.strike), r.type): (r.open, r.close, r.contracts) for r in sub.itertuples()}
    atm = float(lab.resolve_atm_strike(spot[day], 50.0))

    def usable(k):
        v = price.get(k)
        return v is not None and v[0] > 0 and v[1] > 0

    if not (usable((atm, "CE")) and usable((atm, "PE"))):
        for s in STRATS:
            skips[s].append((day, "ATM leg has no traded open/close"))
        continue
    premium = price[(atm, "CE")][0] + price[(atm, "PE")][0]
    rec = {"date": day, "expiry": exp, "dte": (pd.Timestamp(exp) - pd.Timestamp(day)).days, "atm": atm, "premium_open": premium}
    for s in STRATS:
        plan = lab.plan_legs(s, atm, premium)
        keys = [(sp["strike"], sp["type"]) for sp in plan]
        if not all(usable(k) for k in keys):
            missing = [k for k in keys if not usable(k)]
            skips[s].append((day, f"leg without traded open/close: {missing}"))
            continue
        legs = [{"side": sp["side"], "entry": price[k][0], "exit": price[k][1]} for sp, k in zip(plan, keys)]
        charges = lab.settle_units(legs, UNITS, model_for(day))["charges_rs"]
        gross = sum(lab.leg_pnl_pts(l["side"], l["entry"], l["exit"]) for l in legs)
        rec[f"{s}_net"] = gross * UNITS - SPREAD * 2 * len(legs) * UNITS - charges
        rec[f"{s}_gross_pts"] = gross
        if s != "straddle_sell":
            rec[f"{s}_width"] = abs(plan[0]["strike"] - atm)
            rec[f"{s}_min_wing_contracts"] = min(price[k][2] for sp, k in zip(plan, keys) if sp["role"] == "wing")
    rows.append(rec)
df = pd.DataFrame(rows).set_index("date").sort_index()
df["expiry_day"] = df["dte"] == 0
# S4: S1 on ordinary days, S3 on expiry days (a missing S3 on an expiry day drops that day and is counted)
df["S4_net"] = np.where(df["expiry_day"], df.get("iron_fly_w2_net"), df.get("straddle_sell_net"))
df.to_csv(HERE / "nse_daily_s1_s4.csv")
print(f"days with an ATM straddle: {len(df)}")
for s in STRATS:
    print(f"  {s:14s} skipped {len(skips[s])} days ({100 * len(skips[s]) / (len(df) + len(skips[s])):.1f}%)  usable {int(df[f'{s}_net'].notna().sum())}")

# ---------------------------------------------------------------- the 4-year table
names = {"straddle_sell": "S1", "iron_fly_w1": "S2", "iron_fly_w2": "S3"}
cols = {**{names[s]: f"{s}_net" for s in STRATS}, "S4": "S4_net"}
cut = df.index[len(df) // 2]
print(f"\nhalves split at {cut}\n")
result = []
for name, col in cols.items():
    s = df[col]
    for label, part in (("all 4.7 years", s), ("2022-01 to 2024-10-02", s[s.index < "2024-10-03"]), ("2024-10-03 on", s[s.index >= "2024-10-03"]), ("first half", s[s.index < cut]), ("second half", s[s.index >= cut])):
        m = summarize(part.dropna().tolist())
        result.append({"strategy": name, "period": label, **{k: m[k] for k in ("days", "net", "avg_day", "win_pct", "worst_day", "max_dd", "t")}})
res = pd.DataFrame(result)
res.to_csv(HERE / "nse_daily_s1_s4_summary.csv", index=False)
pd.set_option("display.width", 220)
print(res.to_string(index=False))

print("\nverdict against the t > 3.0 bar (S1-S4), with the extra facts:")
for name, col in cols.items():
    s = df[col].dropna()
    m = summarize(s.tolist())
    h1, h2 = s[s.index < cut], s[s.index >= cut]
    print(f"  {name}: t {m['t']:.2f} {'PASS' if m['t'] > BAR_T else 'below 3.0'} | profitable in both halves: {h1.sum() > 0 and h2.sum() > 0} | worst day {m['worst_day']:,.0f} | max DD {m['max_dd']:,.0f} | net {m['net']:,.0f}")

# ---------------------------------------------------------------- how well does the daily-bar version track the real minute-level strategies?
act = pd.read_csv(RISK / "results" / "day_dataset.csv").set_index("date")
print("\n=== daily-bar vs the project's minute-level strategy on the real days (479) ===")
for name, col, mcol in (("S1", "straddle_sell_net", "S1_net_rs"), ("S2", "iron_fly_w1_net", "S2_net_rs"), ("S3", "iron_fly_w2_net", "S3_net_rs")):
    common = df.index.intersection(act.index)
    a, b = act.loc[common, mcol], df.loc[common, col]
    ok = a.notna() & b.notna()
    print(f"  {name}: n={int(ok.sum())} corr of daily net {a[ok].corr(b[ok]):.2f} | net minute {a[ok].sum():>10,.0f} daily-bar {b[ok].sum():>10,.0f} ({100 * b[ok].sum() / a[ok].sum():.0f}%) | worst minute {a[ok].min():>9,.0f} daily-bar {b[ok].min():>9,.0f}")
a4 = np.where(act["is_expiry"] == 1, act["S3_net_rs"], act["S1_net_rs"])
a4 = pd.Series(a4, index=act.index)
common = df.index.intersection(act.index)
ok = a4.loc[common].notna() & df.loc[common, "S4_net"].notna()
print(f"  S4: n={int(ok.sum())} corr {a4.loc[common][ok].corr(df.loc[common, 'S4_net'][ok]):.2f} | net minute {a4.loc[common][ok].sum():,.0f} daily-bar {df.loc[common, 'S4_net'][ok].sum():,.0f}")

# ---------------------------------------------------------------- honesty checks: did skipped wings bias the picture?
same = df.dropna(subset=[cols["S1"], cols["S2"], cols["S3"]]).index
print(f"\nS1 on ALL {int(df[cols['S1']].notna().sum())} days: net {df[cols['S1']].sum():,.0f} | on the {len(same)} days where every strategy is usable: {df.loc[same, cols['S1']].sum():,.0f}")
late = df[df["iron_fly_w2_min_wing_contracts"] < 100]
print(f"days where an S3 wing traded fewer than 100 contracts all day (its 'open' may not be a 09:15 price): {len(late)}")
for name in ("S2", "S3"):
    col = {"S2": "iron_fly_w1", "S3": "iron_fly_w2"}[name]
    thin = df[df[f"{col}_min_wing_contracts"] < 100]
    print(f"   {name}: {len(thin)} thin-wing days; net on them {thin[f'{col}_net'].sum():,.0f}")
print("\nworst 5 days per strategy:")
for name, col in cols.items():
    w = df[col].dropna().sort_values().head(5)
    print(f"  {name}:", ", ".join(f"{d} {v:,.0f}" for d, v in w.items()))
