"""S5 = S1 with a scale-free "straddle doubles" stop: sell the ATM straddle at 09:30; if the mark-to-market loss reaches 1.0 x the 09:30 straddle premium
(the combined price has doubled), buy both legs back at the next minute's open; otherwise hold to 15:29.

Everything is on the same 1-minute candles, frictions basis (and the project's basis for comparison with the lab), 3 lots x 65 units unless stated.
"""
from __future__ import annotations

import json

from run_8_s1_improve import S, combined_stop, next_open, net_from_legs, s1_legs  # noqa: F401  (importing re-runs run_8; its tables print again)
from run_3_helpers import *  # noqa: F403

print("\n\n########## run_10 S5 ##########")
MULT = 1.0


def s5_day(day: str, mult: float = MULT, units: int = UNITS, basis: str = "fric"):
    """(net rupees, stop bar index or None, exit prices) for S5 on one day."""
    ds = SIMS[day][S]
    P = INFO[day]["premium"]
    hits = np.nonzero(ds.mtm <= -mult * P)[0]
    if not hits.size:
        return (net_close(day, S, units, N - 1, basis), None, (float(ds.c[0][N - 1]), float(ds.c[1][N - 1])))
    j = int(hits[0])
    ex = [next_open(ds.o[0], j), next_open(ds.o[1], j)]
    legs = s1_legs(ds, ex)
    if basis == "fric":
        return (net_from_legs(day, legs, units), j, tuple(ex))
    return (lab.settle_units(legs, units, COST)["net_rs"], j, tuple(ex))


rows = {}
for basis in ("fric", "proj"):
    s5 = pd.Series({d: s5_day(d, basis=basis)[0] for d in DAYS})
    s1 = pd.Series({d: net_close(d, S, UNITS, N - 1, basis) for d in DAYS})
    rows[basis] = (s1, s5)

# ------------------------------------------------------------------ headline table
out = []
for basis, (s1, s5) in rows.items():
    for period, days in (("all", DAYS), ("train (to 2025-12-08)", TRAIN_DAYS), ("validation", VALID_DAYS)):
        for name, ser in (("S1", s1), ("S5", s5)):
            out.append({"basis": basis, "period": period, "strategy": name, **summarize(ser.loc[days].tolist())})
head = pd.DataFrame(out)
head.to_csv(RESULTS / "19_s5_headline.csv", index=False)
print(head[["basis", "period", "strategy", "days", "net", "avg_day", "win_pct", "worst_day", "max_dd", "t"]].to_string(index=False))

s1, s5 = rows["fric"]
diff = s5 - s1
stop_days = [d for d in DAYS if s5_day(d)[1] is not None]
print("\nstop fired on", len(stop_days), "of", len(DAYS), "days (", round(100 * len(stop_days) / len(DAYS), 1), "%); by period: train", sum(1 for d in stop_days if d in set(TRAIN_DAYS)), "validation", sum(1 for d in stop_days if d in set(VALID_DAYS)))
sd = pd.DataFrame([{"date": d, "expiry_day": INFO[d]["is_expiry"], "premium": round(INFO[d]["premium"], 1), "stop_time": MINUTES[s5_day(d)[1]], "S1_result": round(s1[d]), "S5_result": round(s5[d]), "stop_saved(+)/cost(-)": round(diff[d])} for d in stop_days])
sd.to_csv(RESULTS / "19_s5_stop_days.csv", index=False)
print(sd.to_string(index=False))
print("days where holding would have been better:", int((diff[stop_days] < 0).sum()), "| where stopping was better:", int((diff[stop_days] > 0).sum()), "| net effect Rs", round(diff.sum()))
print("stops on expiry days:", int(sd["expiry_day"].sum()), "of", len(sd))

# ------------------------------------------------------------------ bootstrap and consistency
rng = np.random.default_rng(RNG_SEED)
boots = [diff.sample(len(diff), replace=True, random_state=int(rng.integers(1e9))).sum() for _ in range(3000)]
print("total gain vs S1: Rs", round(diff.sum()), "| bootstrap 95% interval", round(float(np.percentile(boots, 2.5))), "to", round(float(np.percentile(boots, 97.5))), "| share of bootstrap samples > 0:", round(float((np.array(boots) > 0).mean()), 3))

# year / month view
by = pd.DataFrame({"S1": s1, "S5": s5})
by["year"] = by.index.str[:4]
by["month"] = by.index.str[:7]
yr = by.groupby("year")[["S1", "S5"]].sum().round(0)
mo = by.groupby("month")[["S1", "S5"]].sum().round(0)
print("\nyear:\n", yr.to_string())
better, worse, same = int((mo["S5"] > mo["S1"] + 1).sum()), int((mo["S5"] < mo["S1"] - 1).sum()), int((mo["S5"].sub(mo["S1"]).abs() <= 1).sum())
print(f"months: S5 better {better}, S5 worse {worse}, identical {same} (of {len(mo)}); worst month S1 {mo['S1'].min():,.0f} S5 {mo['S5'].min():,.0f}")
mo.to_csv(RESULTS / "19_s5_monthly.csv")

# ------------------------------------------------------------------ walk-forward on the multiple (chosen in each fold on prior days only)
FOLDS = [(180, 240), (240, 300), (300, 360), (360, 420), (420, len(DAYS))]
grid = [0.75, 1.0, 1.25, 1.5, 2.0]
cache = {m: pd.Series({d: s5_day(d, mult=m)[0] for d in DAYS}) for m in grid}
pooled_s1, pooled_s5, picks = [], [], []
for a, b in FOLDS:
    train, test = DAYS[:a], DAYS[a:b]
    best = max(grid, key=lambda m: (cache[m].loc[train] - s1.loc[train]).sum())
    picks.append(best)
    pooled_s1.extend(s1.loc[test].tolist())
    pooled_s5.extend(cache[best].loc[test].tolist())
print("\nwalk-forward (multiple chosen on prior days each fold):", picks)
print("  pooled out-of-sample S1:", summarize(pooled_s1))
print("  pooled out-of-sample S5:", summarize(pooled_s5))
fixed = summarize([cache[1.0].loc[d] for d in DAYS[FOLDS[0][0]:]])
print("  S5 at a fixed 1.0x on the same out-of-sample days:", fixed)

# ------------------------------------------------------------------ sizes
print("\nscale: S1 vs S5 at different lots (frictions basis)")
srows = []
for lots in (1, 2, 3, 4, 5):
    u = lots * UNIT
    a = pd.Series({d: net_close(d, S, u) for d in DAYS})
    b5 = pd.Series({d: s5_day(d, units=u)[0] for d in DAYS})
    for name, ser in (("S1", a), ("S5", b5)):
        srows.append({"lots": lots, "strategy": name, **{k: v for k, v in summarize(ser.tolist()).items() if k in ("net", "avg_day", "worst_day", "max_dd", "t")}})
sz = pd.DataFrame(srows)
sz.to_csv(RESULTS / "19_s5_sizes.csv", index=False)
print(sz.to_string(index=False))
record("s5_summary", {"multiple": MULT}, {"stop_days": len(stop_days), "gain": float(diff.sum()), "bootstrap_ci": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))], "walk_forward_picks": picks})
