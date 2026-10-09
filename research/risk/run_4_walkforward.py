"""Sections 12 and 13: walk-forward validation and threshold-robustness of the loss-control candidates.

1. A fine grid of stop levels (not used to select anything): is the one stop that passed validation part of a smooth region, or an isolated spike?
2. Expanding-window walk-forward: for each fold, choose the rule using ONLY the days before the fold (same procedure as run_2/run_3), apply it to the next block, pool the
   out-of-sample blocks and compare them with the baseline on the same days.
"""
from __future__ import annotations

import rules  # noqa: F401
from run_3_helpers import *  # noqa: F403

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)

# ---------------------------------------------------------------- 1. fine grid of stop levels
grids = {"S1": [15000 + 2500 * i for i in range(0, 15)], "S2": [7000 + 1000 * i for i in range(0, 11)], "S3": [12000 + 2500 * i for i in range(0, 11)]}
grid_rows = []
for s in STRATEGIES:
    tag, base = SHORT[s], NET[s]
    for level in grids[tag]:
        mod, trig = stop_series(s, float(level))
        rec = {"strategy": tag, "level_rs": level}
        for period, days in (("all", DAYS), ("train", TRAIN_DAYS), ("valid", VALID_DAYS)):
            m = rule_metrics(base, mod, days)
            sel = [d for d in days if d in trig]
            rec.update({f"{period}_triggered": len(sel), f"{period}_profit_retained_pct": m["profit_retained_pct"], f"{period}_worst_day": m["worst_day"], f"{period}_worst_day_reduction_pct": m["worst_day_reduction_pct"],
                        f"{period}_max_dd_reduction_pct": m["max_dd_reduction_pct"], f"{period}_score": m["score"], f"{period}_t": m["t"]})
        grid_rows.append(rec)
        record("stop_grid", {"strategy": tag, "level_rs": level}, rec)
grid = pd.DataFrame(grid_rows)
grid.to_csv(RESULTS / "09_stop_grid.csv", index=False)
for tag in ("S1", "S2", "S3"):
    print(f"\n=== fine grid of stop levels {tag} (score = maxDD reduction% - profit lost%; positive = better than baseline on that period)")
    print(grid[grid["strategy"] == tag][["level_rs", "all_triggered", "all_profit_retained_pct", "all_worst_day", "all_max_dd_reduction_pct", "all_score", "train_score", "valid_score", "valid_worst_day_reduction_pct", "valid_max_dd_reduction_pct", "all_t"]].to_string(index=False))

# ---------------------------------------------------------------- 2. walk-forward
FOLDS = [(180, 240), (240, 300), (300, 360), (360, 420), (420, len(DAYS))]
MIN_SKIPPED, MAX_SKIP_PCT, PROMISING_P = 10, 25.0, 0.10
wf_rows = []
pooled = {s: {"base": [], "skip": [], "stop95": [], "stop90": []} for s in STRATEGIES}
for a, b in FOLDS:
    train, test = DAYS[:a], DAYS[a:b]
    rules.TRAIN_DAYS = train  # thresholds are quantiles of the days before the fold only
    cands = rules.build_candidates()
    for s in STRATEGIES:
        tag, base = SHORT[s], NET[s]
        # --- skip rule chosen on the fold's training days
        best, best_score = None, -1e9
        for c in cands:
            mask = rule_mask(c)
            mod = apply_skip(base, mask)
            tr = rule_metrics(base, mod, train)
            if not (tr["skipped"] >= MIN_SKIPPED and tr["skipped_pct"] <= MAX_SKIP_PCT and tr["score"] > 0 and tr["worst_day"] >= tr["base_worst"] and tr["skipped_mean"] < 0):
                continue
            p = permutation_p(base, train, tr["skipped"], tr["score"])
            if p <= PROMISING_P and tr["score"] > best_score:
                best, best_score = (c, mask), tr["score"]
        if best:
            skip_series = apply_skip(base, best[1])
            skip_name = best[0]["name"]
        else:
            skip_series, skip_name = base, "(no rule promising - trade as baseline)"
        # --- stop levels from the fold's training days
        mae = (-DF.loc[train, f"{tag}_max_loss_rs"]).dropna().tolist()
        st95, _ = stop_series(s, quantile(mae, 0.95))
        st90, _ = stop_series(s, quantile(mae, 0.90))
        for name, ser in (("skip", skip_series), ("stop95", st95), ("stop90", st90)):
            m = rule_metrics(base, ser, test)
            wf_rows.append({"fold_test": f"{test[0]}..{test[-1]}", "train_days": len(train), "strategy": tag, "method": name, "rule": skip_name if name == "skip" else f"stop at train P{name[-2:]} = Rs {quantile(mae, int(name[-2:]) / 100):,.0f}", **{k: m[k] for k in ("days", "skipped", "net", "base_net", "worst_day", "base_worst", "max_dd", "base_max_dd", "profit_retained_pct", "max_dd_reduction_pct", "worst_day_reduction_pct", "score")}})
            pooled[s][name].extend(ser.loc[test].dropna().tolist())
        pooled[s]["base"].extend(base.loc[test].dropna().tolist())
        record("walk_forward_fold", {"strategy": tag, "train_days": len(train), "test": [test[0], test[-1]], "skip_rule": skip_name}, {})
rules.TRAIN_DAYS = TRAIN_DAYS
wf = pd.DataFrame(wf_rows)
wf.to_csv(RESULTS / "10_walk_forward_folds.csv", index=False)
print("\n=== WALK-FORWARD (5 expanding folds, out-of-sample blocks pooled)")
pool_rows = []
for s in STRATEGIES:
    tag = SHORT[s]
    b = summarize(pooled[s]["base"])
    for name in ("skip", "stop95", "stop90"):
        m = summarize(pooled[s][name])
        r = {"strategy": tag, "method": name, "oos_days": b["days"], "base_net": b["net"], "net": m["net"], "profit_retained_pct": round(100 * m["net"] / b["net"], 1), "base_worst": b["worst_day"], "worst_day": m["worst_day"],
             "base_max_dd": b["max_dd"], "max_dd": m["max_dd"], "max_dd_reduction_pct": round(100 * (1 - m["max_dd"] / b["max_dd"]), 1), "base_t": b["t"], "t": m["t"]}
        f = wf[(wf["strategy"] == tag) & (wf["method"] == name)]
        r["folds_improved(score>0)"] = f"{int((f['score'] > 0).sum())}/{len(f)}"
        pool_rows.append(r)
pool = pd.DataFrame(pool_rows)
pool.to_csv(RESULTS / "11_walk_forward_pooled.csv", index=False)
print(pool.to_string(index=False))
save_json("11_walk_forward_pooled.json", pool.to_dict("records"))
