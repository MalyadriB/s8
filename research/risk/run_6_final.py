"""Section 14/15: the final comparison, plus the checks that decide how much weight each finding deserves.

Rules compared are the ones SELECTED ON THE TRAINING DAYS by the procedures in run_2/run_3 (not the best-looking ones on validation).
"""
from __future__ import annotations

import json
import math

from run_3_helpers import *  # noqa: F403

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
skip_sel = json.load(open(RESULTS / "05_skip_selected.json"))
sizing = pd.read_csv(RESULTS / "06_sizing_rules_all.csv")
stops = pd.read_csv(RESULTS / "07_stops.csv")
out_md = []


def md(df: pd.DataFrame, floatfmt: str = "{:,.1f}") -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            cells.append(floatfmt.format(v) if isinstance(v, (float, np.floating)) and v == v else ("" if (isinstance(v, float) and v != v) else str(v)))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------- sizing series (same construction as run_2)
LOTS_NET = {s: {k: pd.Series({d: (net_close(d, s, k * UNIT) if d in set(DATA["strategy_days"][s]) else np.nan) for d in DAYS}) for k in (1, 2, 3)} for s in STRATEGIES}
for s in STRATEGIES:
    LOTS_NET[s][0] = pd.Series(0.0, index=DF.index).where(LOTS_NET[s][3].notna())


def sized(s: str, feat: str, tier: str) -> pd.Series:
    x = DF[feat]
    if tier.startswith("3/2/1/0"):
        c = [train_quantile(feat, q) for q in (0.70, 0.85, 0.95)]
        lots = np.select([x < c[0], x < c[1], x < c[2]], [3, 2, 1], default=0)
    else:
        c = [train_quantile(feat, q) for q in (0.80, 0.90)]
        lots = np.select([x < c[0], x < c[1]], [3, 2], default=1)
    lots = pd.Series(lots, index=DF.index).where(x.notna(), 3)
    out = pd.Series(np.nan, index=DF.index)
    for k in (0, 1, 2, 3):
        out[lots == k] = LOTS_NET[s][k][lots == k]
    return out.where(LOTS_NET[s][3].notna()), lots


def row_for(label: str, tag: str, base: pd.Series, mod: pd.Series, days: list[str], lots: pd.Series | None = None) -> dict:
    m = rule_metrics(base, mod, days)
    r = {"strategy": tag, "variant": label, "trades": m["days"] - m["skipped"], "skipped": m["skipped"], "net": m["net"], "avg/day": m["avg_day"], "win%": m["win_pct"], "worst_day": m["worst_day"],
         "max_dd": m["max_dd"], "t": m["t"], "profit_retained%": m["profit_retained_pct"], "worst_day_reduction%": m["worst_day_reduction_pct"], "max_dd_reduction%": m["max_dd_reduction_pct"]}
    if lots is not None:
        r["avg_lots"] = round(float(lots.loc[days][base.loc[days].notna()].mean()), 2)
    return r


final_rows = {"validation": [], "all (in-sample)": [], "train": []}
picked = {}
for s in STRATEGIES:
    tag, base = SHORT[s], NET[s]
    # skip rule chosen on train
    sel = skip_sel.get(tag)
    rule = next(c for c in build_candidates() if c["name"] == sel["rule"])
    skip_ser = apply_skip(base, rule_mask(rule))
    # sizing chosen on train
    sz = sizing[(sizing["strategy"] == tag) & (sizing["promising_in_train"])].sort_values("train_score", ascending=False)
    size_pick = sz.iloc[0] if len(sz) else None
    size_ser, lots = sized(s, size_pick["feature"], size_pick["tiers"]) if size_pick is not None else (base, None)
    # loss control chosen on train: best train score across all stop levels evaluated
    st = stops[stops["strategy"] == tag].sort_values("train_score", ascending=False).iloc[0]
    stop_ser, trig = stop_series(s, float(st["level_rs"]))
    mae = (-DF.loc[TRAIN_DAYS, f"{tag}_max_loss_rs"]).dropna().tolist()
    p95_ser, _ = stop_series(s, quantile(mae, 0.95))
    picked[tag] = {"skip": sel["rule"], "sizing": f"{size_pick['feature']} {size_pick['tiers']}" if size_pick is not None else None, "stop": st["stop"], "stop_p95_level": round(quantile(mae, 0.95))}
    for period, days in (("validation", VALID_DAYS), ("all (in-sample)", DAYS), ("train", TRAIN_DAYS)):
        final_rows[period].append(row_for("baseline (3 lots)", tag, base, base, days))
        final_rows[period].append(row_for(f"skip: {sel['rule']}", tag, base, skip_ser, days))
        if size_pick is not None:
            final_rows[period].append(row_for(f"size: {size_pick['feature']} {size_pick['tiers']}", tag, base, size_ser, days, lots))
        final_rows[period].append(row_for(f"loss control (train-best): {st['stop']}", tag, base, stop_ser, days))
        final_rows[period].append(row_for(f"stop at train P95 = Rs {quantile(mae, 0.95):,.0f}", tag, base, p95_ser, days))
print(json.dumps(picked, indent=1))
for period in final_rows:
    tbl = pd.DataFrame(final_rows[period])
    tbl.to_csv(RESULTS / f"14_final_{period.split()[0]}.csv", index=False)
    print(f"\n===== FINAL COMPARISON - {period.upper()} =====")
    print(tbl.to_string(index=False))
    out_md.append(f"### Final comparison - {period}\n\n" + md(tbl))
record("final_comparison", picked, {p: final_rows[p] for p in final_rows})

# ---------------------------------------------------------------- S4 as an expiry-swap rule (S1 normally, S3 on expiry days)
s1, s3 = NET["straddle_sell"], NET["iron_fly_w2"]
s4 = s1.where(DF["is_expiry"] == 0, s3)
print("\n===== S4 (S1, but S3 on expiry days) vs S1 =====")
s4_rows = []
for period, days in (("train", TRAIN_DAYS), ("validation", VALID_DAYS), ("all", DAYS)):
    m = rule_metrics(s1, s4, days)
    n_swap = int(DF.loc[days, "is_expiry"].sum())
    # chance benchmark: swap the same number of RANDOM days to S3 instead
    b = s1.loc[days].to_numpy()
    a = s3.loc[days].fillna(s1.loc[days]).to_numpy()
    rng = np.random.default_rng(RNG_SEED)
    scores = []
    base_dd = max_drawdown(b.tolist())
    for _ in range(2000):
        idx = rng.choice(len(b), n_swap, replace=False)
        mod = b.copy()
        mod[idx] = a[idx]
        scores.append(100 * (1 - max_drawdown(mod.tolist()) / base_dd) - (100 - 100 * mod.sum() / b.sum()))
    s4_rows.append({"period": period, "expiry_days_swapped": n_swap, "S1_net": m["base_net"], "S4_net": m["net"], "profit_retained%": m["profit_retained_pct"], "S1_worst": m["base_worst"], "S4_worst": m["worst_day"],
                    "S1_max_dd": m["base_max_dd"], "S4_max_dd": m["max_dd"], "max_dd_reduction%": m["max_dd_reduction_pct"], "score": m["score"], "p_random_swap_scores_as_well": round(float((np.array(scores) >= m["score"]).mean()), 3),
                    "S1_t": m["base_t"], "S4_t": m["t"]})
s4t = pd.DataFrame(s4_rows)
print(s4t.to_string(index=False))
out_md.append("### S4 vs S1 (frictions basis)\n\n" + md(s4t))
record("s4_vs_s1", {}, s4_rows)

# ---------------------------------------------------------------- paired statistics for the S1 stop candidates
print("\n===== paired daily difference (stopped - baseline), S1, all / train / validation =====")
pair_rows = []
base = NET["straddle_sell"]
cands_stops = [("Rs 25,000", lambda d: 25000.0), ("Rs 30,000", lambda d: 30000.0), ("Rs 35,000", lambda d: 35000.0), ("train P95 (Rs %s)" % f"{picked['S1']['stop_p95_level']:,}", lambda d: float(picked["S1"]["stop_p95_level"])),
               ("1.0 x premium", lambda d: INFO[d]["premium"] * UNITS), ("1.5 x premium", lambda d: 1.5 * INFO[d]["premium"] * UNITS)]
rng = np.random.default_rng(RNG_SEED)
for label, fn in cands_stops:
    out = {}
    for d in DAYS:
        j = first_breach(SIMS[d]["straddle_sell"], UNITS, fn(d))
        out[d] = net_close(d, "straddle_sell", UNITS) if j is None else net_stop(d, "straddle_sell", UNITS, j)
    diff = pd.Series(out) - base
    for period, days in (("all", DAYS), ("train", TRAIN_DAYS), ("validation", VALID_DAYS)):
        dd = diff.loc[days]
        trig = dd[dd.abs() > 0.5]  # the baseline series is stored rounded to the paisa; a real stop changes a day by far more than that
        boots = [dd.sample(len(dd), replace=True, random_state=int(rng.integers(1e9))).sum() for _ in range(2000)]
        pair_rows.append({"stop": label, "period": period, "days_triggered": len(trig), "triggered_better": int((trig > 0).sum()), "triggered_worse": int((trig < 0).sum()), "total_gain_rs": round(dd.sum(), 0),
                          "bootstrap_95%_low": round(float(np.percentile(boots, 2.5)), 0), "bootstrap_95%_high": round(float(np.percentile(boots, 97.5)), 0), "paired_t": round(t_value(dd.tolist()), 2)})
pairs = pd.DataFrame(pair_rows)
print(pairs.to_string(index=False))
out_md.append("### S1 stop candidates: paired difference vs baseline\n\n" + md(pairs, "{:,.2f}"))
record("s1_stop_paired", {}, pair_rows)

# ---------------------------------------------------------------- protection widths on the days where every width exists
print("\n===== protection widths on the SAME days (all six widths available) =====")
import run_5_widths_relative as W5  # rebuilds the width coverage (and re-prints its tables)
avail, MULTS = W5.avail, W5.MULTS
common = [d for d in DAYS if all(d in avail[m] for m in MULTS)]
cut = int(0.6 * len(common))
w_rows = []
for label, sub in (("all common days", common), ("first 60%", common[:cut]), ("last 40%", common[cut:])):
    for m in MULTS:
        nets = [settle(avail[m][d], UNITS, avail[m][d].exit_close(N - 1), "fric")["net_rs"] for d in sub]
        w_rows.append({"sample": label, "days": len(sub), "structure": f"iron fly W = {m} x premium", "net": round(sum(nets), 0), "avg/day": round(mean(nets), 0), "win%": round(100 * sum(1 for v in nets if v > 0) / len(nets), 1),
                       "worst_day": round(min(nets), 0), "max_dd": round(max_drawdown(nets), 0), "t": round(t_value(nets), 2)})
    s1n = [net_close(d, "straddle_sell", UNITS) for d in sub]
    w_rows.append({"sample": label, "days": len(sub), "structure": "S1 (no wings)", "net": round(sum(s1n), 0), "avg/day": round(mean(s1n), 0), "win%": round(100 * sum(1 for v in s1n if v > 0) / len(s1n), 1), "worst_day": round(min(s1n), 0),
                   "max_dd": round(max_drawdown(s1n), 0), "t": round(t_value(s1n), 2)})
wt = pd.DataFrame(w_rows)
wt.to_csv(RESULTS / "15_widths_common_days.csv", index=False)
print(wt.to_string(index=False))
out_md.append("### Protection widths on the days where every width exists (expiry days only)\n\n" + md(wt, "{:,.1f}"))
record("widths_common_days", {"days": len(common)}, w_rows)
(RESULTS / "final_tables.md").write_text("\n\n".join(out_md), encoding="utf-8")
print("\nexperiments recorded (all runs in this session):", sum(1 for _ in open(EXPERIMENTS)))
