"""Would running the same S1 (sell the 09:30 ATM straddle, hold to the close) on other indices add profit?

Uses the project's `straddle_daily` (09:30 open and 15:29 close of the ATM call and put per index and day - no minute paths, so no stops here) and the project's cost model.
Caveats that make this a screening test only: BSE (SENSEX) exchange charges differ slightly from the NSE rates in the cost model, no spread is applied (the option premium and
tick size differ per index), and monthly-expiry indices (BANKNIFTY/FINNIFTY/MIDCPNIFTY) have far lower theta on most days.
"""
from __future__ import annotations

from run_3_helpers import *  # noqa: F403

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
conn = connect()
frames = {}
for name in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
    rows = [dict(r) for r in conn.execute("SELECT trading_date, dte, is_expiry_day, ce_0930, pe_0930, ce_close, pe_close, ce_instrument_key FROM straddle_daily WHERE underlying = ? ORDER BY trading_date", (name,))]
    out = []
    for r in rows:
        lot = lab.lot_size_of(r["ce_instrument_key"])
        if lot is None:
            continue
        legs = [{"side": "SELL", "entry": r["ce_0930"], "exit": r["ce_close"]}, {"side": "SELL", "entry": r["pe_0930"], "exit": r["pe_close"]}]
        net1 = lab.settle_units(legs, lot, COST)["net_rs"]  # 1 lot at that day's lot size, project cost model
        out.append({"date": r["trading_date"], "dte": r["dte"], "expiry_day": r["is_expiry_day"], "lot": lot, "premium": r["ce_0930"] + r["pe_0930"], "net_1lot": net1})
    frames[name] = pd.DataFrame(out).set_index("date")

rows = []
nifty = frames["NIFTY"]["net_1lot"]
for name, f in frames.items():
    s = f["net_1lot"]
    common = s.index.intersection(nifty.index)
    rec = {"index": name, "days": len(s), "first": s.index[0], "lot_sizes": sorted(f["lot"].unique().tolist()), "avg_dte": round(f["dte"].mean(), 1), "share_expiry_days_%": round(100 * f["expiry_day"].mean(), 1),
           **{k: v for k, v in summarize(s.tolist()).items() if k in ("net", "avg_day", "win_pct", "worst_day", "max_dd", "t")}, "corr_with_NIFTY_S1": round(float(s.loc[common].corr(nifty.loc[common])), 2)}
    tr = s.loc[[d for d in s.index if d <= TRAIN_DAYS[-1]]]
    va = s.loc[[d for d in s.index if d > TRAIN_DAYS[-1]]]
    rec.update({"train_net": round(tr.sum(), 0), "train_t": round(t_value(tr.tolist()), 2), "valid_net": round(va.sum(), 0), "valid_t": round(t_value(va.tolist()), 2)})
    rows.append(rec)
    record("index_s1", {"index": name}, rec)
tbl = pd.DataFrame(rows)
tbl.to_csv(RESULTS / "18_s1_other_indices.csv", index=False)
print("S1 (hold 09:30 -> 15:29, 1 lot each, project cost model, no spread)")
print(tbl.to_string(index=False))

# weekly vs monthly structure: profit on expiry days vs other days for each index
print("\nprofit split by expiry day (1 lot):")
for name, f in frames.items():
    e, o = f[f["expiry_day"] == 1]["net_1lot"], f[f["expiry_day"] == 0]["net_1lot"]
    print(f"{name:11s} expiry days {len(e):3d} avg {e.mean():9.0f} | other days {len(o):3d} avg {o.mean():9.0f}")

# portfolio: NIFTY S1 + SENSEX S1 (same lots each) on the days both traded
for other in ("SENSEX", "BANKNIFTY"):
    common = frames["NIFTY"].index.intersection(frames[other].index)
    a, b = frames["NIFTY"].loc[common, "net_1lot"], frames[other].loc[common, "net_1lot"]
    combo = a + b
    print(f"\nNIFTY + {other} (1 lot each, {len(common)} common days):")
    for label, s in (("NIFTY alone", a), (f"{other} alone", b), ("both", combo)):
        m = summarize(s.tolist())
        print(f"  {label:14s} net {m['net']:>10,.0f}  avg/day {m['avg_day']:>7,.0f}  win% {m['win_pct']:>5}  worst {m['worst_day']:>10,.0f}  max DD {m['max_dd']:>10,.0f}  t {m['t']}")
    record("portfolio", {"pair": ["NIFTY", other]}, {"corr": float(a.corr(b))})
