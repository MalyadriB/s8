"""Sections 6, 7, 12, 13: SKIP rules and DYNAMIC SIZING rules, developed on the training days and then applied unchanged to the validation days."""
from __future__ import annotations

from rules import *  # noqa: F403

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
NET = {s: DF[f"{SHORT[s]}_net_rs"] for s in STRATEGIES}
MIN_SKIPPED, MAX_SKIP_PCT = 10, 25.0
PROMISING_P = 0.10

# ================================================================== 6. skip rules
cands = build_candidates()
print("skip candidates per strategy:", len(cands), "x 3 strategies")
all_rows, selected = [], {}
for s in STRATEGIES:
    tag, base = SHORT[s], NET[s]
    rows = []
    for c in cands:
        mask = rule_mask(c)
        mod = apply_skip(base, mask)
        tr, va = rule_metrics(base, mod, TRAIN_DAYS), rule_metrics(base, mod, VALID_DAYS)
        p_tr = permutation_p(base, TRAIN_DAYS, tr["skipped"], tr["score"]) if tr["skipped"] else float("nan")
        p_va = permutation_p(base, VALID_DAYS, va["skipped"], va["score"]) if va["skipped"] else float("nan")
        row = {"strategy": tag, "rule": c["name"], "family": c["family"], "kind": c["kind"], "thresholds": "; ".join(f"{a}{o}{v:.4g}" if isinstance(v, float) else f"{a}{o}{v}" for a, o, v in c["terms"])}
        row.update({f"train_{k}": v for k, v in tr.items()})
        row.update({f"valid_{k}": v for k, v in va.items()})
        row["train_perm_p"], row["valid_perm_p"] = p_tr, p_va
        rows.append(row)
        record("skip_rule", {"strategy": tag, "rule": c["name"], "terms": c["terms"]}, {"train": tr, "validation": va, "train_perm_p": p_tr, "valid_perm_p": p_va})
    t = pd.DataFrame(rows)
    eligible = (t["train_skipped"] >= MIN_SKIPPED) & (t["train_skipped_pct"] <= MAX_SKIP_PCT)
    t["eligible"] = eligible
    t["promising_in_train"] = eligible & (t["train_score"] > 0) & (t["train_perm_p"] <= PROMISING_P) & (t["train_worst_day"] >= t["train_base_worst"]) & (t["train_skipped_mean"] < 0)
    t["passes_validation"] = (t["valid_score"] > 0) & (t["valid_perm_p"] <= PROMISING_P) & (t["valid_skipped_mean"] < t["valid_traded_mean"]) & (t["valid_skipped"] >= 5)
    all_rows.append(t)
    promising = t[t["promising_in_train"]]
    chance = t[t["eligible"] & (t["valid_skipped"] >= 5)]
    best = promising.sort_values(["train_score", "train_skipped"], ascending=[False, True]).head(1)
    hit = float(promising["passes_validation"].mean()) if len(promising) else float("nan")
    chance_rate = float(((chance["valid_score"] > 0) & (chance["valid_perm_p"] <= PROMISING_P)).mean()) if len(chance) else float("nan")
    print(f"\n=== {tag}: {len(t)} rules, {int(eligible.sum())} eligible (>= {MIN_SKIPPED} train days skipped, <= {MAX_SKIP_PCT:.0f}%), {len(promising)} promising in TRAIN "
          f"-> {int(promising['passes_validation'].sum())} pass VALIDATION (hit rate {100 * hit:.0f}% ; share of ANY eligible rule passing validation by luck: {100 * chance_rate:.0f}%)")
    cols = ["rule", "train_skipped", "train_skipped_pct", "train_profit_retained_pct", "train_max_dd_reduction_pct", "train_worst_day_reduction_pct", "train_score", "train_perm_p",
            "valid_skipped", "valid_profit_retained_pct", "valid_max_dd_reduction_pct", "valid_worst_day_reduction_pct", "valid_score", "valid_perm_p", "passes_validation"]
    print(promising.sort_values("train_score", ascending=False)[cols].head(8).to_string(index=False))
    selected[tag] = best.iloc[0].to_dict() if len(best) else None
    record("skip_selection", {"strategy": tag, "criteria": f"eligible {MIN_SKIPPED}+ days <= {MAX_SKIP_PCT}%, train score>0, train perm p<={PROMISING_P}, worst day not worse, skipped-day mean<0; best train score"},
           {"selected": selected[tag]["rule"] if selected[tag] else None, "promising": len(promising), "pass_validation": int(promising['passes_validation'].sum()), "hit_rate": hit, "chance_rate": chance_rate})
skip_all = pd.concat(all_rows)
skip_all.to_csv(RESULTS / "04_skip_rules_all.csv", index=False)
save_json("05_skip_selected.json", {k: v for k, v in selected.items()})

# threshold robustness of each selected rule: the same feature at its neighbouring thresholds
print("\n--- robustness: selected rule vs its neighbouring thresholds (same feature) ---")
for tag, sel in selected.items():
    if not sel:
        print(tag, "no rule was promising in the training period")
        continue
    fam = sel["family"]
    nb = skip_all[(skip_all["strategy"] == tag) & (skip_all["family"] == fam)]
    print(f"{tag} selected: {sel['rule']}")
    print(nb[["rule", "train_skipped", "train_profit_retained_pct", "train_max_dd_reduction_pct", "train_score", "valid_skipped", "valid_profit_retained_pct", "valid_max_dd_reduction_pct", "valid_score"]].to_string(index=False))

# ================================================================== 7. dynamic sizing
LOTS_NET = {s: {k: pd.Series({d: (net_close(d, s, k * UNIT) if d in set(DATA["strategy_days"][s]) else np.nan) for d in DAYS}) for k in (0, 1, 2, 3)} for s in STRATEGIES}
for s in STRATEGIES:
    LOTS_NET[s][0] = pd.Series(0.0, index=DF.index).where(LOTS_NET[s][3].notna())  # zero lots = no trade, zero P&L (and no charges)


def sized_series(s: str, lots: pd.Series) -> pd.Series:
    out = pd.Series(np.nan, index=DF.index)
    for k in (0, 1, 2, 3):
        out[lots == k] = LOTS_NET[s][k][lots == k]
    return out.where(LOTS_NET[s][3].notna())


def sizing_perm_p(s: str, lots: pd.Series, days: list[str], observed_score: float) -> float:
    ok = LOTS_NET[s][3].loc[days].notna()
    L = lots.loc[days][ok].to_numpy().astype(int)
    n = len(L)
    table = np.stack([LOTS_NET[s][k].loc[days][ok].to_numpy() for k in (0, 1, 2, 3)])  # (4, n)
    base = table[3]
    rng = np.random.default_rng(RNG_SEED)
    scores = np.empty(N_PERM)
    base_dd, base_net = maxdd_rows(base[None, :])[0], base.sum()
    mats = np.empty((N_PERM, n))
    for i in range(N_PERM):
        perm = rng.permutation(L)
        mats[i] = table[perm, np.arange(n)]
    dd_red = 100 * (1 - maxdd_rows(mats) / base_dd) if base_dd > 0 else np.zeros(N_PERM)
    lost = 100 - 100 * mats.sum(axis=1) / base_net if base_net > 0 else np.zeros(N_PERM)
    return round(float(((dd_red - lost) >= observed_score).mean()), 4)


TIERS = {
    "3/2/1/0 at p70/p85/p95": [(0.70, 3), (0.85, 2), (0.95, 1), (1.01, 0)],
    "3/2/1 at p80/p90": [(0.80, 3), (0.90, 2), (1.01, 1)],
}
size_rows = []
FEATS = ["gap_abs_pct", "move_abs_pct", "range_pct", "range_over_premium", "move_over_premium", "ce_pe_imb_abs", "prev_range_pct", "premium_pct"]
for s in STRATEGIES:
    tag, base = SHORT[s], NET[s]
    for feat in FEATS:
        for tier_name, tiers in TIERS.items():
            cut = [(train_quantile(feat, q) if q <= 1 else float("inf"), lots) for q, lots in tiers]
            x = DF[feat]
            if len(cut) == 4:
                lots = pd.Series(np.select([x < cut[0][0], x < cut[1][0], x < cut[2][0]], [3, 2, 1], default=0), index=DF.index)
            else:
                lots = pd.Series(np.select([x < cut[0][0], x < cut[1][0]], [3, 2], default=1), index=DF.index)
            lots = lots.where(x.notna(), 3)
            mod = sized_series(s, lots)
            res = {}
            for label, days in (("train", TRAIN_DAYS), ("valid", VALID_DAYS)):
                m = rule_metrics(base, mod, days)
                m["avg_lots"] = round(float(lots.loc[days][base.loc[days].notna()].mean()), 2)
                m["exposure_reduction_pct"] = round(100 * (1 - m["avg_lots"] / 3), 1)
                m["perm_p"] = sizing_perm_p(s, lots, days, m["score"])
                res[label] = m
            row = {"strategy": tag, "feature": feat, "tiers": tier_name}
            for label in res:
                row.update({f"{label}_{k}": v for k, v in res[label].items()})
            size_rows.append(row)
            record("size_rule", {"strategy": tag, "feature": feat, "tiers": tier_name, "cuts": cut}, res)
# explicit expiry-day sizing
for s in STRATEGIES:
    tag, base = SHORT[s], NET[s]
    for k in (2, 1, 0):
        lots = pd.Series(np.where(DF["is_expiry"] == 1, k, 3), index=DF.index)
        mod = sized_series(s, lots)
        res = {}
        for label, days in (("train", TRAIN_DAYS), ("valid", VALID_DAYS)):
            m = rule_metrics(base, mod, days)
            m["avg_lots"] = round(float(lots.loc[days][base.loc[days].notna()].mean()), 2)
            m["exposure_reduction_pct"] = round(100 * (1 - m["avg_lots"] / 3), 1)
            m["perm_p"] = sizing_perm_p(s, lots, days, m["score"])
            res[label] = m
        row = {"strategy": tag, "feature": "is_expiry", "tiers": f"expiry -> {k} lots, else 3"}
        for label in res:
            row.update({f"{label}_{kk}": v for kk, v in res[label].items()})
        size_rows.append(row)
        record("size_rule", {"strategy": tag, "feature": "is_expiry", "expiry_lots": k}, res)
sizing = pd.DataFrame(size_rows)
sizing["promising_in_train"] = (sizing["train_score"] > 0) & (sizing["train_perm_p"] <= PROMISING_P) & (sizing["train_worst_day"] >= sizing["train_base_worst"])
sizing["passes_validation"] = (sizing["valid_score"] > 0) & (sizing["valid_perm_p"] <= PROMISING_P)
sizing.to_csv(RESULTS / "06_sizing_rules_all.csv", index=False)
print("\n=== dynamic sizing: rules tested", len(sizing))
for tag in ("S1", "S2", "S3"):
    t = sizing[sizing["strategy"] == tag]
    pr = t[t["promising_in_train"]]
    print(f"{tag}: {len(pr)} promising in train -> {int(pr['passes_validation'].sum())} pass validation")
    cols = ["feature", "tiers", "train_avg_lots", "train_profit_retained_pct", "train_max_dd_reduction_pct", "train_score", "train_perm_p", "valid_profit_retained_pct", "valid_max_dd_reduction_pct", "valid_worst_day_reduction_pct", "valid_score", "valid_perm_p", "passes_validation"]
    print(pr.sort_values("train_score", ascending=False)[cols].head(6).to_string(index=False))
print("\nrecorded experiments this run:", len(open(EXPERIMENTS).readlines()))
