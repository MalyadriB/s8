"""Sections 2, 4, 5 and 11: the baseline, the dangerous days, and the descriptive relationships between pre-09:30 conditions and outcomes."""
from __future__ import annotations

import math

from rules import *  # noqa: F403

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
tags = {s: SHORT[s] for s in STRATEGIES}
NET = {s: DF[f"{SHORT[s]}_net_rs"] for s in STRATEGIES}

# ---------------------------------------------------------------- 2. baseline (3 lots x 65 units), both cost bases, overall / train / validation
base_rows = []
for s in STRATEGIES:
    for basis, col in (("fric (spread 0.10/fill, STT 0.15% from 2026-04-01)", f"{SHORT[s]}_net_rs"), ("project (no spread, STT 0.10%)", f"{SHORT[s]}_net_proj_rs")):
        series = DF[col]
        for label, days in (("all", DAYS), ("train", TRAIN_DAYS), ("validation", VALID_DAYS)):
            base_rows.append({"strategy": SHORT[s], "basis": basis, "period": label, "from": days[0], "to": days[-1], **summary(series.loc[days])})
# S4 (derived, reference only): S1 on normal days, S3 on expiry days
s4 = NET["straddle_sell"].where(DF["is_expiry"] == 0, NET["iron_fly_w2"])
for label, days in (("all", DAYS), ("train", TRAIN_DAYS), ("validation", VALID_DAYS)):
    base_rows.append({"strategy": "S4 (derived)", "basis": "fric", "period": label, "from": days[0], "to": days[-1], **summary(s4.loc[days])})
baseline = pd.DataFrame(base_rows)
baseline.to_csv(RESULTS / "01_baseline.csv", index=False)
record("baseline", {"units": UNITS, "train_days": len(TRAIN_DAYS), "validation_days": len(VALID_DAYS), "train_end": TRAIN_DAYS[-1]}, baseline.to_dict("records"))
print("TRAIN", TRAIN_DAYS[0], "->", TRAIN_DAYS[-1], len(TRAIN_DAYS), "days | VALIDATION", VALID_DAYS[0], "->", VALID_DAYS[-1], len(VALID_DAYS), "days")
print(baseline[baseline["basis"].str.startswith("fric")].drop(columns=["basis", "median_day", "traded"]).to_string(index=False))

# ---------------------------------------------------------------- 4 / 11. dangerous days
FEATURES_FOR_WARNING = ["gap_abs_pct", "move_abs_pct", "range_pct", "range_over_premium", "premium_pct", "ce_pe_imb_abs", "prev_range_pct"]
rank = {c: DF[c].rank(pct=True) for c in FEATURES_FOR_WARNING}
worst_tables = {}
for s in STRATEGIES:
    tag = SHORT[s]
    worst = NET[s].dropna().sort_values().head(20).index
    rows = []
    for d in worst:
        flags = [f"{c}>=p90" for c in FEATURES_FOR_WARNING if rank[c][d] >= 0.90]
        if DF.loc[d, "is_expiry"]:
            flags.append("expiry")
        if DF.loc[d, "premium_pct"] <= DF["premium_pct"].quantile(0.10):
            flags.append("premium<=p10")
        r = DF.loc[d]
        rows.append({
            "date": d, "expiry_day": int(r["is_expiry"]), "dow": r["dow"], "gap_pct": round(r["gap_pct"], 2), "move_0915_0930_pct": round(r["move_pct"], 2), "range_0915_0930": round(r["range_0915_0930"], 1),
            "straddle": round(r["premium"], 1), "S1": r["S1_net_rs"], "S2": r["S2_net_rs"], "S3": r["S3_net_rs"], "worst_intraday_this_strategy": r[f"{tag}_max_loss_rs"],
            "warning_before_0930": ", ".join(flags) if flags else "none",
        })
    t = pd.DataFrame(rows)
    worst_tables[tag] = t
    t.to_csv(RESULTS / f"02_worst20_{tag}.csv", index=False)
    n_exp = int(t["expiry_day"].sum())
    n_warn = int((t["warning_before_0930"] != "none").sum())
    n_no_expiry_flag = int(sum(1 for w in t["warning_before_0930"] if w != "none" and w != "expiry"))
    # how often would a generic "any feature >= p90" alarm fire on an ordinary day? (false-alarm base rate)
    any_flag_all = pd.concat([(rank[c] >= 0.90) for c in FEATURES_FOR_WARNING], axis=1).any(axis=1)
    print(f"\n{tag}: worst-20 days -> expiry days {n_exp}/20 (base rate {100 * DF['is_expiry'].mean():.0f}%), any pre-09:30 flag {n_warn}/20, non-expiry flags {n_no_expiry_flag}/20; "
          f"the same 'any feature >= p90' alarm fires on {100 * any_flag_all.mean():.0f}% of ALL days")
    print(t[["date", "expiry_day", "gap_pct", "move_0915_0930_pct", "straddle", "S1", "S2", "S3", "warning_before_0930"]].head(10).to_string(index=False))
    record("worst20", {"strategy": tag}, {"expiry_days_in_worst20": n_exp, "any_flag": n_warn, "false_alarm_rate_all_days": float(any_flag_all.mean())})

# clusters: all-three-lost / only-one-lost days
both = DF.dropna(subset=["strategies_lost"])
print("\ndays all three strategies lose:", int(both["all_three_lost"].sum()), "| only one loses:", int(both["only_one_lost"].sum()), "| of", len(both))
for name, mask in (("S1 loses > 20,000 while S2 and S3 survive (> -20,000)", (NET["straddle_sell"] < -20000) & (NET["iron_fly_w1"] > -20000) & (NET["iron_fly_w2"] > -20000)),
                   ("S2 or S3 also lose > 20,000", (NET["iron_fly_w1"] < -20000) | (NET["iron_fly_w2"] < -20000))):
    print(f"  {name}: {int(mask.sum())} days", list(DF.index[mask][:8]))

# ---------------------------------------------------------------- 5. descriptive buckets
def welch_p(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    if se == 0:
        return float("nan")
    z = (a.mean() - b.mean()) / se
    return math.erfc(abs(z) / math.sqrt(2))  # normal approximation


bucket_rows = []


def add_bucket(family: str, label: str, mask: pd.Series):
    for s in STRATEGIES:
        y = NET[s]
        ok = y.notna()
        a, b = y[mask & ok].to_numpy(), y[~mask & ok].to_numpy()
        if len(a) == 0:
            continue
        grp = y[mask & ok]
        bucket_rows.append({
            "family": family, "bucket": label, "strategy": SHORT[s], "n": len(a), "mean": round(a.mean(), 1), "median": round(float(np.median(a)), 1), "worst": round(a.min(), 1),
            "p5": round(float(np.percentile(a, 5)), 1), "loss_days_pct": round(100 * (a < 0).mean(), 1), "big_loss_days(<-20k)": int((a < -20000).sum()),
            "sum": round(a.sum(), 0), "max_dd_in_bucket": round(max_drawdown(grp.tolist()), 0), "mean_vs_rest_p": round(welch_p(a, b), 3), "weak(n<30)": len(a) < 30,
        })


add_bucket("A expiry", "all days", pd.Series(True, index=DF.index))
add_bucket("A expiry", "expiry days", DF["is_expiry"] == 1)
add_bucket("A expiry", "non-expiry days", DF["is_expiry"] == 0)
for d in (0, 1, 2, 3, 4, 5, 6):
    add_bucket("A days-to-expiry", f"dte={d}", DF["dte"] == d)
edges = [(0, 0.10), (0.10, 0.25), (0.25, 0.50), (0.50, 0.75), (0.75, 99)]
for lo, hi in edges:
    add_bucket("B |gap| %", f"{lo:.2f}-{hi:.2f}" if hi < 99 else f">{lo:.2f}", (DF["gap_abs_pct"] >= lo) & (DF["gap_abs_pct"] < hi))


def quantile_buckets(family: str, col: str):
    q25, q75, q95 = DF[col].quantile([0.25, 0.75, 0.95])
    add_bucket(family, f"very quiet (<p25 = {q25:.3g})", DF[col] < q25)
    add_bucket(family, f"normal (p25-p75, to {q75:.3g})", (DF[col] >= q25) & (DF[col] < q75))
    add_bucket(family, f"elevated (p75-p95, to {q95:.3g})", (DF[col] >= q75) & (DF[col] < q95))
    add_bucket(family, f"extreme (>=p95 = {q95:.3g})", DF[col] >= q95)


quantile_buckets("C |09:15-09:30 move| %", "move_abs_pct")
add_bucket("C move direction", "up (09:30 above 09:15)", DF["move_0915_0930"] > 0)
add_bucket("C move direction", "down", DF["move_0915_0930"] < 0)
add_bucket("C signed move: strong up (>=p90)", "", DF["move_pct"] >= DF["move_pct"].quantile(0.90))
add_bucket("C signed move: strong down (<=p10)", "", DF["move_pct"] <= DF["move_pct"].quantile(0.10))
quantile_buckets("D range 09:15-09:30 % of spot", "range_pct")
quantile_buckets("E straddle premium % of spot", "premium_pct")
for lo, hi in ((0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0001)):
    add_bucket("E premium percentile (past days only)", f"{lo:.1f}-{min(hi, 1):.1f}", (DF["premium_pctile"] >= lo) & (DF["premium_pctile"] < hi))
quantile_buckets("F |CE-PE|/premium", "ce_pe_imb_abs")
add_bucket("F CE richer than PE (>p75 imbalance)", "", DF["ce_pe_imb"] >= DF["ce_pe_imb"].quantile(0.75))
add_bucket("F PE richer than CE (<p25 imbalance)", "", DF["ce_pe_imb"] <= DF["ce_pe_imb"].quantile(0.25))
for g in ("strong_down", "mild_down", "flat", "mild_up", "strong_up"):
    add_bucket("G opening direction (gap)", g, DF["gap_dir"] == g)
for dow in ("Mon", "Tue", "Wed", "Thu", "Fri"):
    add_bucket("day of week", dow, DF["dow"] == dow)
add_bucket("prior-day range", "top quartile", DF["prev_range_pct"] >= DF["prev_range_pct"].quantile(0.75))
buckets = pd.DataFrame(bucket_rows)
buckets.to_csv(RESULTS / "03_buckets.csv", index=False)
record("descriptive_buckets", {"n_buckets": int(buckets["bucket"].nunique())}, buckets.to_dict("records"))
print("\nbucket tests run:", len(buckets), "(expect ~5% to show p<0.05 by chance)")
sig = buckets[(buckets["mean_vs_rest_p"] < 0.05) & (~buckets["weak(n<30)"])].sort_values("mean_vs_rest_p")
print("buckets with p<0.05 vs the rest and n>=30:", len(sig))
print(sig[["family", "bucket", "strategy", "n", "mean", "median", "worst", "loss_days_pct", "big_loss_days(<-20k)", "mean_vs_rest_p"]].head(25).to_string(index=False))
print("\nexpiry effect:")
print(buckets[buckets["family"] == "A expiry"][["bucket", "strategy", "n", "mean", "median", "worst", "p5", "loss_days_pct", "big_loss_days(<-20k)", "max_dd_in_bucket", "mean_vs_rest_p"]].to_string(index=False))
