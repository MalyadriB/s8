"""Verify S4 (S1's result each day, S3's on expiry days) against the declared check figures.

Declared check (20 Sep 2026): 3 lots x 65 units, spread 0.20 per leg (round trip: 0.10 on each fill), STT 0.15% from 1 Apr 2026
-> total +Rs 5,67,193; worst day -Rs 48,218 on 12 May 2025; 102 expiry days.

Read-only: it reads the stored S1/S3 backtest rows through the project's own strategy_lab, and does not change the lab's cost model.
How the spread is charged: it is taken off the gross result (points x units); the statutory charges are computed on the unadjusted candle
prices - that is the basis on which the declared numbers reproduce to the rupee.
"""
import dataclasses
import json
import os
import sys

sys.path.insert(0, r"D:\zero\backend")
os.environ.setdefault("DATA_DIR", r"D:\zero\backend\data")
import strategy_lab as lab  # noqa: E402

UNITS = 65 * 3
SPREAD_PER_FILL = 0.10  # 0.20 per leg over its entry and exit fills
STT_FROM, STT_RATE = "2026-04-01", 0.0015

base = lab.DEFAULT_COST_MODEL
raised = dataclasses.replace(base, stt_sell_rate=STT_RATE)
s4_rows, skips = lab.derived_backtest_rows(lab.S4)


def net_with_frictions(row):
    raw = [{"side": l["side"], "entry": l["entry"], "exit": l["exit"]} for l in row["legs"]]
    fills = len(raw) * 2
    charges = lab.settle_units(raw, UNITS, raised if row["trading_date"] >= STT_FROM else base)["charges_rs"]
    gross = sum(lab.leg_pnl_pts(l["side"], l["entry"], l["exit"]) for l in raw) * UNITS - SPREAD_PER_FILL * fills * UNITS
    return gross - charges


daily = {r["trading_date"]: net_with_frictions(r) for r in s4_rows}
worst_day = min(daily, key=daily.get)
result = {
    "s4_days": len(s4_rows), "skipped_days": skips, "expiry_days": sum(1 for r in s4_rows if r["expiry"] == r["trading_date"]),
    "days_taken_from_S3": sum(1 for r in s4_rows if r["derived_from"] == "iron_fly_w2"),
    "declared_check": {"total_rs": round(sum(daily.values()), 2), "worst_day": worst_day, "worst_day_rs": round(daily[worst_day], 2)},
    "lab_basis_3_lots_65_units": {  # what the lab itself shows: shared cost model (STT 0.10%), no spread
        "total_rs": round(sum(r["net_rs"] for r in lab.backtest_daily(lab.S4, None, None, lab.Size(qty=UNITS))), 2),
    },
}
for name in ("straddle_sell", "iron_fly_w1", "iron_fly_w2"):
    result["lab_basis_3_lots_65_units"][name] = round(sum(r["net_rs"] for r in lab.backtest_daily(name, None, None, lab.Size(qty=UNITS))), 2)
print(json.dumps(result, indent=2))
open(r"D:\zero\research\s4\s4_check_result.json", "w").write(json.dumps(result, indent=2))
