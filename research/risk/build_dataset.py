"""Step 1: locate the data, rebuild S1/S2/S3 independently from raw candles, and prove the result equals the project's stored 479-day backtest.

Writes cache/dataset.pkl (features + 1-minute leg paths for every day) and results/baseline_check.json.
If the reproduction does not match, the run says so and exits non-zero: nothing downstream should run on an unreproduced baseline.
"""
from __future__ import annotations

import pickle
import sys

import numpy as np

from common import *  # noqa: F403

spot_all = load_spot_csv()
conn = connect()


def option_key(expiry: str, strike: float, leg: str) -> str | None:
    rows = conn.execute(
        "SELECT instrument_key FROM instrument_contracts WHERE underlying_key = ? AND expiry = ? AND strike_price = ? AND instrument_type = ?",
        (INDEX_KEY, expiry, strike, leg),
    ).fetchall()
    keys = [r[0] for r in rows]
    return next((k for k in keys if k.count("|") == 2), keys[0] if keys else None)  # the expired-contract form first


def candles(key: str, day: str) -> dict[str, dict[str, float]]:
    """Downloaded 1-minute candles of one option for one day, keyed by HH:MM, entry bar to exit bar."""
    rows = conn.execute(
        "SELECT received_at, open, high, low, close, volume FROM market_observations WHERE instrument_key = ? AND trading_date = ? AND open IS NOT NULL AND instr(received_at, '.') = 0",
        (key, day),
    ).fetchall()
    out = {}
    for r in rows:
        t = r["received_at"][11:16]
        if "09:30" <= t <= "15:29":
            out[t] = {"open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"]}
    return out


def path(bars: dict[str, dict[str, float]]) -> dict[str, np.ndarray]:
    """Open/high/low/close for each of the 360 minutes; a minute with no trade repeats the previous close (the project's convention)."""
    o, h, l, c = (np.full(N, np.nan) for _ in range(4))
    real = np.zeros(N, dtype=bool)
    last = None
    for i, t in enumerate(MINUTES):
        bar = bars.get(t)
        if bar:
            o[i], h[i], l[i], c[i] = bar["open"], bar["high"], bar["low"], bar["close"]
            real[i] = True
            last = bar["close"]
        elif last is not None:
            o[i] = h[i] = l[i] = c[i] = last
    return {"open": o, "high": h, "low": l, "close": c, "real": real}


straddle_rows = [dict(r) for r in conn.execute("SELECT * FROM straddle_daily WHERE underlying = 'NIFTY' ORDER BY trading_date")]
days_all = [r["trading_date"] for r in straddle_rows]
spot_days = sorted(spot_all)  # only days with a full CSV series; a fallback day is added to spot_all after it is used

dataset: dict = {"days": [], "info": {}, "legs": {}, "spot": {}, "strategy_days": {s: [] for s in STRATEGIES}}
issues: list[str] = []
compare = {s: {"days": 0, "leg_price_mismatch": 0, "net_rs_mismatch": 0, "missing_in_repro": [], "extra_in_repro": [], "max_abs_net_diff_rs": 0.0} for s in STRATEGIES}

for row in straddle_rows:
    day = row["trading_date"]
    spot = spot_all.get(day, {})
    spot_source = "nifty_1min.csv"
    if "09:30" not in spot:
        # the export is built once, hours after each close; a day it does not hold yet is the index series cached in market_observations (5-minute, as the project's straddle build used)
        rows = conn.execute(
            "SELECT received_at, open, high, low, close FROM market_observations WHERE instrument_key = ? AND trading_date = ? AND open IS NOT NULL AND instr(received_at, '.') = 0",
            (INDEX_KEY, day),
        ).fetchall()
        spot = {r["received_at"][11:16]: {"open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]} for r in rows}
        spot_source = "market_observations (5-minute index candles)"
        spot_all[day] = spot
    if "09:30" not in spot or "09:15" not in spot:
        issues.append(f"{day}: NIFTY spot series lacks the 09:15/09:30 bars")
        continue
    p0930 = spot["09:30"]["open"]
    atm = lab.resolve_atm_strike(p0930, lab.STRIKE_STEP)
    if atm != row["atm_strike"]:
        issues.append(f"{day}: ATM from spot {atm} != stored {row['atm_strike']}")
    if abs(p0930 - row["spot_0930"]) > 0.005:
        issues.append(f"{day}: 09:30 spot {p0930} != stored {row['spot_0930']}")
    expiry = row["expiry"]
    body = {}
    for leg in ("CE", "PE"):
        key = option_key(expiry, atm, leg)
        if key is None:
            issues.append(f"{day}: no {leg} {atm} contract for {expiry}")
            break
        bars = candles(key, day)
        if "09:30" not in bars:
            issues.append(f"{day}: no 09:30 bar for {leg} {atm}")
            break
        body[leg] = (key, bars)
    if len(body) < 2:
        continue
    premium = body["CE"][1]["09:30"]["open"] + body["PE"][1]["09:30"]["open"]
    dataset["days"].append(day)
    n_strikes = conn.execute(
        "SELECT count(DISTINCT strike_price) FROM instrument_contracts WHERE underlying_key = ? AND expiry = ? AND instrument_type = 'CE'", (INDEX_KEY, expiry)
    ).fetchone()[0]
    prev_days = [d for d in spot_days if d < day]
    prev = spot_all[prev_days[-1]] if prev_days else {}
    dataset["info"][day] = {
        "expiry": expiry, "dte": row["dte"], "is_expiry": int(row["is_expiry_day"]), "atm": atm, "premium": premium,
        "ce_0930": body["CE"][1]["09:30"]["open"], "pe_0930": body["PE"][1]["09:30"]["open"], "n_strikes_listed": n_strikes,
        "prev_day": prev_days[-1] if prev_days else None, "spot_source": spot_source,
    }
    dataset["spot"][day] = {"bars": spot, "prev": prev}
    legs_for_day = {}
    for s in STRATEGIES:
        plan = lab.plan_legs(s, atm, premium)
        specs = []
        ok = True
        for spec in plan:
            if spec["role"] == "body":
                key, bars = body[spec["type"]]
            else:
                key = option_key(expiry, spec["strike"], spec["type"])
                bars = candles(key, day) if key else {}
                if key is None or "09:30" not in bars:
                    ok = False
                    issues.append(f"{day} {s}: wing {spec['type']} {spec['strike']:g} unavailable ({'no contract' if key is None else 'no 09:30 bar'}) - the project's backtest skips this day too")
                    break
            specs.append({**spec, "key": key, "path": path(bars), "n_real_bars": len(bars)})
        if ok:
            legs_for_day[s] = specs
            dataset["strategy_days"][s].append(day)
    dataset["legs"][day] = legs_for_day

# ------------------------------------------------------------ compare with the project's stored backtest (at the project's own lots and at 3 fixed lots)
report: dict = {"date_range": [days_all[0], days_all[-1]], "straddle_daily_days": len(days_all), "reproduced_days": len(dataset["days"]), "issues": issues, "strategies": {}}
for s in STRATEGIES:
    stored_rows = {r["trading_date"]: r for r in lab.backtest_daily(s, None, None, lab.Size(qty=UNITS))}
    stored_lots1 = {r["trading_date"]: r for r in lab.backtest_daily(s, None, None, lab.Size(lots=1))}
    mine_days = set(dataset["strategy_days"][s])
    cmp = compare[s]
    cmp["days"] = len(mine_days)
    cmp["missing_in_repro"] = sorted(set(stored_rows) - mine_days)
    cmp["extra_in_repro"] = sorted(mine_days - set(stored_rows))
    total_mine_qty195 = total_stored_qty195 = total_mine_lots1 = total_stored_lots1 = 0.0
    for day in sorted(mine_days & set(stored_rows)):
        specs = dataset["legs"][day][s]
        legs_mine = [{"side": sp["side"], "entry": float(sp["path"]["open"][0]), "exit": float(sp["path"]["close"][-1])} for sp in specs]
        theirs = stored_rows[day]
        for a, b in zip(legs_mine, theirs["legs"]):
            if abs(a["entry"] - b["entry"]) > 1e-9 or abs(a["exit"] - b["exit"]) > 1e-9 or a["side"] != b["side"]:
                cmp["leg_price_mismatch"] += 1
        mine = lab.settle_units(legs_mine, UNITS, COST)
        diff = abs(mine["net_rs"] - theirs["net_rs"])
        cmp["max_abs_net_diff_rs"] = max(cmp["max_abs_net_diff_rs"], diff)
        if diff > 0.011:
            cmp["net_rs_mismatch"] += 1
        mine1 = lab.settle_units(legs_mine, theirs["lot_size"], COST)
        total_mine_qty195 += mine["net_rs"]
        total_stored_qty195 += theirs["net_rs"]
        total_mine_lots1 += mine1["net_rs"]
        total_stored_lots1 += stored_lots1[day]["net_rs"]
    report["strategies"][s] = {
        **cmp, "reproduced_net_rs_195_units": round(total_mine_qty195, 2), "stored_net_rs_195_units": round(total_stored_qty195, 2),
        "reproduced_net_rs_1_lot": round(total_mine_lots1, 2), "stored_net_rs_1_lot": round(total_stored_lots1, 2), "stored_days": len(stored_rows),
    }

ok = all(r["leg_price_mismatch"] == 0 and r["net_rs_mismatch"] == 0 and not r["missing_in_repro"] and not r["extra_in_repro"] for r in report["strategies"].values())
report["baseline_reproduced"] = ok
report["data_facts"] = {
    "nifty_1min_csv": {"path": str(NIFTY_CSV), "first_day": spot_days[0], "last_day": spot_days[-1], "days": len(spot_days)},
    "option_candles": "market_observations (1-minute, per option contract, expired-contract keys); wings only exist for the S2/S3 strikes (plus ATM +-10 strikes on expiry days)",
    "trading_days_in_dataset": len(dataset["days"]),
    "expiry_days": sum(i["is_expiry"] for i in dataset["info"].values()),
    "lot_sizes_in_project_history": sorted({r["lot_size"] for r in lab.backtest_daily("straddle_sell", None, None, 1)}),
    "spread_assumption_in_backtest": "none - candle prices (09:30 open, 15:29 close) with brokerage/STT/exchange/SEBI/stamp/GST only",
}
save_json("baseline_check.json", report)
with open(CACHE / "dataset.pkl", "wb") as handle:
    pickle.dump(dataset, handle)
print(json.dumps({k: v for k, v in report.items() if k not in ("issues",)}, indent=1, default=str)[:2500])
print("issues:", issues)
sys.exit(0 if ok else 2)
