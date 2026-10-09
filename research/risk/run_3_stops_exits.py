"""Sections 8 and 9: intraday loss-management (stops) and earlier exits, simulated bar by bar on the 1-minute paths.

A stop is checked on every bar's close-marked gross P&L; when breached at the close of bar j, the exit is filled at the OPEN of bar j+1.
Nothing uses the day's final result to decide anything.  Fixed rupee levels need no fitting; percentile levels are taken from the TRAINING days only.
"""
from __future__ import annotations

from rules import *  # noqa: F403

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
NET = {s: DF[f"{SHORT[s]}_net_rs"] for s in STRATEGIES}
FIXED = [5000, 7500, 10000, 12500, 15000]


def stop_series(s: str, level: float, extreme: bool = False):
    have = set(DATA["strategy_days"][s])
    out, trig = {}, {}
    for d in DAYS:
        if d not in have:
            out[d] = np.nan
            continue
        ds = SIMS[d][s]
        j = first_breach(ds, UNITS, level, extreme)
        if j is None:
            out[d] = net_close(d, s, UNITS)
        else:
            out[d] = net_stop(d, s, UNITS, j)
            trig[d] = j
    return pd.Series(out), trig


rows = []
for s in STRATEGIES:
    tag, base = SHORT[s], NET[s]
    mae_train = (-DF.loc[TRAIN_DAYS, f"{tag}_max_loss_rs"]).dropna()
    levels = [(f"Rs {x:,}", float(x)) for x in FIXED] + [(f"train P{q} of daily max loss (Rs {quantile(mae_train.tolist(), q / 100):,.0f})", quantile(mae_train.tolist(), q / 100)) for q in (70, 80, 90, 95)]
    for label, level in levels:
        mod, trig = stop_series(s, level)
        rec = {"strategy": tag, "stop": label, "level_rs": round(level, 0)}
        res = {}
        for period, days in (("all", DAYS), ("train", TRAIN_DAYS), ("valid", VALID_DAYS)):
            m = rule_metrics(base, mod, days)
            sel = [d for d in days if d in trig]
            b_t = base.loc[sel]
            s_t = mod.loc[sel]
            m.update({
                "triggered": len(sel), "trigger_pct": round(100 * len(sel) / max(1, base.loc[days].notna().sum()), 1),
                "avg_stopped_result": round(float(s_t.mean()), 0) if sel else float("nan"), "avg_if_held": round(float(b_t.mean()), 0) if sel else float("nan"),
                "held_would_have_been_better_pct": round(100 * float((b_t > s_t).mean()), 1) if sel else float("nan"),
                "held_would_have_finished_profitable_pct": round(100 * float((b_t > 0).mean()), 1) if sel else float("nan"),
                "prematurely_stopped_winners": int((b_t > 0).sum()) if sel else 0,
                "avg_recovery_if_held": round(float((b_t - s_t).mean()), 0) if sel else float("nan"),
                "net_gain_from_stopping": round(float((s_t - b_t).sum()), 0) if sel else 0.0,
            })
            res[period] = m
            rec.update({f"{period}_{k}": v for k, v in m.items() if k in ("net", "profit_retained_pct", "worst_day", "worst_day_reduction_pct", "max_dd", "max_dd_reduction_pct", "t", "score", "triggered", "trigger_pct",
                                                                        "avg_stopped_result", "avg_if_held", "held_would_have_been_better_pct", "held_would_have_finished_profitable_pct", "prematurely_stopped_winners",
                                                                        "avg_recovery_if_held", "net_gain_from_stopping")})
        rows.append(rec)
        record("stop_rule", {"strategy": tag, "level_rs": level, "label": label, "units": UNITS, "fill": "next bar open after close-marked breach"}, res)
stops = pd.DataFrame(rows)
stops.to_csv(RESULTS / "07_stops.csv", index=False)
for tag in ("S1", "S2", "S3"):
    t = stops[stops["strategy"] == tag]
    print(f"\n=== STOPS {tag}: baseline all-days worst {t.iloc[0]['all_worst_day'] if False else ''}")
    print(t[["stop", "all_triggered", "all_trigger_pct", "all_avg_stopped_result", "all_avg_if_held", "all_held_would_have_been_better_pct", "all_held_would_have_finished_profitable_pct", "all_prematurely_stopped_winners",
             "all_net_gain_from_stopping", "all_profit_retained_pct", "all_worst_day", "all_max_dd_reduction_pct", "all_t"]].to_string(index=False))
    print("-- train vs validation consistency (score = maxDD reduction% - profit lost%)")
    print(t[["stop", "train_profit_retained_pct", "train_max_dd_reduction_pct", "train_score", "valid_profit_retained_pct", "valid_worst_day_reduction_pct", "valid_max_dd_reduction_pct", "valid_score"]].to_string(index=False))

# ============================================================ 9. earlier exits
TIMES = ["11:00", "12:00", "13:00", "14:00", "14:30", "15:00", "15:15", "15:29"]
erows = []
for s in STRATEGIES:
    tag, base = SHORT[s], NET[s]
    for t in TIMES:
        j = M_INDEX[t]
        ser = net_series(s, UNITS, j)
        res = {}
        rec = {"strategy": tag, "exit_bar_close": t}
        for period, days in (("all", DAYS), ("train", TRAIN_DAYS), ("valid", VALID_DAYS)):
            m = rule_metrics(base, ser, days)
            vals = ser.loc[days].dropna()
            m["p5_day"] = round(float(np.percentile(vals, 5)), 0)
            m["avg_expiry_days"] = round(float(ser.loc[days][DF.loc[days, "is_expiry"] == 1].mean()), 0)
            m["avg_other_days"] = round(float(ser.loc[days][DF.loc[days, "is_expiry"] == 0].mean()), 0)
            res[period] = m
            rec.update({f"{period}_{k}": v for k, v in m.items() if k in ("net", "avg_day", "win_pct", "worst_day", "p5_day", "max_dd", "t", "profit_retained_pct", "max_dd_reduction_pct", "score", "avg_expiry_days", "avg_other_days")})
        erows.append(rec)
        record("earlier_exit", {"strategy": tag, "exit_bar_close": t, "units": UNITS}, res)
exits = pd.DataFrame(erows)
exits.to_csv(RESULTS / "08_exits.csv", index=False)
for tag in ("S1", "S2", "S3"):
    t = exits[exits["strategy"] == tag]
    print(f"\n=== EARLIER EXITS {tag} (exit filled at the close of the bar labelled ...; 15:29 = baseline)")
    print(t[["exit_bar_close", "all_net", "all_avg_day", "all_win_pct", "all_worst_day", "all_p5_day", "all_max_dd", "all_t", "train_score", "valid_score", "valid_avg_day", "valid_worst_day", "valid_max_dd"]].to_string(index=False))
    print(f"   best train net: {t.loc[t['train_net'].idxmax(), 'exit_bar_close']} | best validation net: {t.loc[t['valid_net'].idxmax(), 'exit_bar_close']} | lowest validation max DD: {t.loc[t['valid_max_dd'].idxmin(), 'exit_bar_close']}")
