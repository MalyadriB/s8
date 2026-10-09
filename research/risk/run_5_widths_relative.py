"""Section 10 (protection width) and a premium-relative stop.

Alternative wing widths need option candles that only exist for some days (the S2/S3 strikes on every day; a +-10-strike chain on expiry days).
The Upstox token expired at 03:30 on 2026-09-20, so nothing new could be downloaded: this measures exactly what the stored data supports and says how much that is.
"""
from __future__ import annotations

from run_3_helpers import *  # noqa: F403

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
conn = connect()
MULTS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]


def key_of(expiry, strike, leg):
    ks = [r[0] for r in conn.execute("SELECT instrument_key FROM instrument_contracts WHERE underlying_key=? AND expiry=? AND strike_price=? AND instrument_type=?", (INDEX_KEY, expiry, strike, leg))]
    return next((k for k in ks if k.count("|") == 2), ks[0] if ks else None)


def bars_of(key, day):
    rows = conn.execute("SELECT received_at, open, high, low, close FROM market_observations WHERE instrument_key=? AND trading_date=? AND open IS NOT NULL AND instr(received_at,'.')=0", (key, day)).fetchall()
    return {r["received_at"][11:16]: {"open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]} for r in rows if "09:30" <= r["received_at"][11:16] <= "15:29"}


def path_of(bars):
    o, h, l, c = (np.full(N, np.nan) for _ in range(4))
    real = np.zeros(N, dtype=bool)
    last = None
    for i, t in enumerate(MINUTES):
        b = bars.get(t)
        if b:
            o[i], h[i], l[i], c[i] = b["open"], b["high"], b["low"], b["close"]
            real[i] = True
            last = b["close"]
        elif last is not None:
            o[i] = h[i] = l[i] = c[i] = last
    return {"open": o, "high": h, "low": l, "close": c, "real": real}


avail: dict[float, dict[str, DayStrategy]] = {m: {} for m in MULTS}
cover = {m: 0 for m in MULTS}
for d in DAYS:
    info = INFO[d]
    body = [sp for sp in DATA["legs"][d]["straddle_sell"]]
    for m in MULTS:
        w = max(50.0, lab.round_to_step(m * info["premium"], 50.0))
        specs = []
        ok = True
        for typ, strike in (("CE", info["atm"] + w), ("PE", info["atm"] - w)):
            key = key_of(info["expiry"], strike, typ)
            bars = bars_of(key, d) if key else {}
            if "09:30" not in bars or "15:20" > max(bars):
                ok = False
                break
            specs.append({"role": "wing", "type": typ, "strike": strike, "side": "BUY", "path": path_of(bars)})
        if ok:
            cover[m] += 1
            avail[m][d] = DayStrategy(d, f"iron_fly_x{m}", specs + body)
print("coverage of protection widths (days of", len(DAYS), "):", {m: cover[m] for m in MULTS})
all_six = [d for d in DAYS if all(d in avail[m] for m in MULTS)]
print("days on which every width is available:", len(all_six))
rows = []
for m in MULTS:
    days = [d for d in DAYS if d in avail[m]]
    if len(days) < 5:
        continue
    for label, sub in (("all covered days", days), ("expiry days only", [d for d in days if INFO[d]["is_expiry"]]), ("non-expiry days only", [d for d in days if not INFO[d]["is_expiry"]])):
        if len(sub) < 5:
            continue
        nets = [settle(avail[m][d], UNITS, avail[m][d].exit_close(N - 1), "fric")["net_rs"] for d in sub]
        credit = [INFO[d]["premium"] - (avail[m][d].entry[0] + avail[m][d].entry[1]) for d in sub]  # straddle premium collected minus the two wings paid
        wing_cost = [avail[m][d].entry[0] + avail[m][d].entry[1] for d in sub]
        width = [abs(avail[m][d].strikes[0] - INFO[d]["atm"]) for d in sub]
        s1 = [net_close(d, "straddle_sell", UNITS) for d in sub]
        rows.append({"width_x_premium": m, "sample": label, "days": len(sub), "net_iron_fly": round(sum(nets), 0), "avg_day": round(mean(nets), 0), "win_pct": round(100 * sum(1 for v in nets if v > 0) / len(nets), 1),
                     "worst_day": round(min(nets), 0), "max_dd": round(max_drawdown(nets), 0), "t": round(t_value(nets), 2), "avg_net_credit_pts": round(mean(credit), 1), "avg_wing_cost_pts": round(mean(wing_cost), 1), "avg_width_pts": round(mean(width), 0),
                     "avg_max_loss_pts(width-credit)": round(mean([w - c for w, c in zip(width, credit)]), 0), "same_days_S1_net": round(sum(s1), 0), "same_days_S1_worst": round(min(s1), 0), "same_days_S1_max_dd": round(max_drawdown(s1), 0)})
        record("protection_width", {"multiple": m, "sample": label}, rows[-1])
wid = pd.DataFrame(rows)
wid.to_csv(RESULTS / "12_protection_widths.csv", index=False)
print(wid.to_string(index=False))
save_json("12_width_coverage.json", {"days_total": len(DAYS), "coverage_days": {str(m): cover[m] for m in MULTS}, "days_with_all_widths": len(all_six), "note": "alternative widths need candles that are not stored; token expired 2026-09-20 03:30"})

# ---------------------------------------------------------------- a premium-relative stop: exit when the loss reaches m x the 09:30 straddle premium (in rupees at 195 units)
print("\n=== premium-relative stops: loss >= m x (09:30 straddle premium x 195 units)")
rel_rows = []
for s in STRATEGIES:
    tag, base = SHORT[s], NET[s]
    for m in ([0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5] if tag == "S1" else [0.25, 0.35, 0.5, 0.75, 1.0]):
        out, trig = {}, {}
        for d in DAYS:
            if d not in set(DATA["strategy_days"][s]):
                out[d] = np.nan
                continue
            j = first_breach(SIMS[d][s], UNITS, m * INFO[d]["premium"] * UNITS)
            out[d] = net_close(d, s, UNITS) if j is None else net_stop(d, s, UNITS, j)
            if j is not None:
                trig[d] = j
        ser = pd.Series(out)
        rec = {"strategy": tag, "stop_at_x_premium": m}
        for period, days in (("all", DAYS), ("train", TRAIN_DAYS), ("valid", VALID_DAYS)):
            mm = rule_metrics(base, ser, days)
            rec.update({f"{period}_triggered": sum(1 for d in days if d in trig), f"{period}_profit_retained_pct": mm["profit_retained_pct"], f"{period}_worst_day": mm["worst_day"],
                        f"{period}_max_dd_reduction_pct": mm["max_dd_reduction_pct"], f"{period}_score": mm["score"], f"{period}_t": mm["t"]})
        rel_rows.append(rec)
        record("premium_relative_stop", {"strategy": tag, "multiple": m}, rec)
rel = pd.DataFrame(rel_rows)
rel.to_csv(RESULTS / "13_premium_relative_stops.csv", index=False)
print(rel[["strategy", "stop_at_x_premium", "all_triggered", "all_profit_retained_pct", "all_worst_day", "all_max_dd_reduction_pct", "all_score", "train_score", "valid_score", "valid_worst_day", "all_t"]].to_string(index=False))
