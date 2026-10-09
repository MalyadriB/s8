"""Ways to make S1 (short ATM straddle, 09:30 -> 15:29) earn more, all simulated bar by bar on the actual 1-minute candles.

Families: combined stop, per-leg stop (leg only / both legs), profit target, trailing exit, delayed hedge (buy the S2/S3 wings after a loss trigger), and combinations.
Conventions match engine.py: entry 09:30 open, triggers on bar CLOSES, fill at the NEXT bar's open (intrabar per-leg stops are shown separately and marked optimistic),
frictions basis (0.10/fill spread, STT 0.15% from 2026-04-01), 3 lots x 65 units.  Rules are chosen on the TRAINING days only and reported on the validation days.
"""
from __future__ import annotations

import json

from run_3_helpers import *  # noqa: F403

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
S = "straddle_sell"
base = NET[S]
H = SIMS  # alias


def net_from_legs(day: str, legs: list[dict], units: int = UNITS) -> float:
    """Net rupees for explicit legs [{side, entry, exit}] on the frictions basis (spread off gross, charges on raw prices)."""
    model = MODEL_RAISED if day >= STT_FROM else COST
    cost = lab.settle_units(legs, units, model)
    gross = sum(lab.leg_pnl_pts(l["side"], l["entry"], l["exit"]) for l in legs) * units
    return gross - SPREAD_PER_FILL * 2 * len(legs) * units - cost["charges_rs"]


def next_open(arr: np.ndarray, j: int) -> float:
    return float(arr[j + 1]) if j + 1 < N else float(arr[N - 1])  # a signal on the last bar exits at that bar's close


def s1_legs(ds, exit_prices):
    return [{"side": "SELL", "entry": float(ds.entry[i]), "exit": float(exit_prices[i])} for i in range(2)]


# ------------------------------------------------------------------------------------------------ strategy variants: each returns net rupees for the day
def combined_stop(day, level_pts):
    ds = H[day][S]
    hits = np.nonzero(ds.mtm <= -level_pts)[0]
    if not hits.size:
        return net_close(day, S, UNITS)
    j = int(hits[0])
    return net_from_legs(day, s1_legs(ds, [next_open(ds.o[0], j), next_open(ds.o[1], j)]))


def leg_stop(day, k, both=False, intrabar=False):
    """Stop each short leg when ITS price reaches (1+k) x its entry price. `both`: the first stop closes the whole position."""
    ds = H[day][S]
    trig, fill = [None, None], [None, None]
    for i in range(2):
        level = ds.entry[i] * (1 + k)
        series = ds.h[i] if intrabar else ds.c[i]
        hits = np.nonzero(series >= level)[0]
        if hits.size:
            j = int(hits[0])
            trig[i] = j
            fill[i] = max(level, float(ds.o[i][j])) if intrabar else next_open(ds.o[i], j)
    if trig[0] is None and trig[1] is None:
        return net_close(day, S, UNITS)
    if both:
        j = min(t for t in trig if t is not None)
        ex = []
        for i in range(2):
            if trig[i] == j:
                ex.append(fill[i])
            else:
                ex.append(float(ds.c[i][j]) if intrabar else next_open(ds.o[i], j))
        return net_from_legs(day, s1_legs(ds, ex))
    ex = [fill[i] if trig[i] is not None else float(ds.c[i][N - 1]) for i in range(2)]
    return net_from_legs(day, s1_legs(ds, ex))


def profit_target(day, frac):
    ds = H[day][S]
    P = INFO[day]["premium"]
    hits = np.nonzero(ds.mtm >= frac * P)[0]
    if not hits.size:
        return net_close(day, S, UNITS)
    j = int(hits[0])
    return net_from_legs(day, s1_legs(ds, [next_open(ds.o[0], j), next_open(ds.o[1], j)]))


def trailing(day, arm, give):
    """After the mark-to-market has reached `arm` x premium, exit if it falls `give` x premium below its peak."""
    ds = H[day][S]
    P = INFO[day]["premium"]
    peak = np.maximum.accumulate(ds.mtm)
    hits = np.nonzero((peak >= arm * P) & (ds.mtm <= peak - give * P))[0]
    if not hits.size:
        return net_close(day, S, UNITS)
    j = int(hits[0])
    return net_from_legs(day, s1_legs(ds, [next_open(ds.o[0], j), next_open(ds.o[1], j)]))


def delayed_hedge(day, k, wing="iron_fly_w2"):
    """When the loss reaches k x premium, buy the S3 (or S2) wings at the next open and hold everything to 15:29."""
    ds = H[day][S]
    P = INFO[day]["premium"]
    hits = np.nonzero(ds.mtm <= -k * P)[0]
    if not hits.size or wing not in H[day]:
        return net_close(day, S, UNITS)
    j = int(hits[0])
    wds = H[day][wing]
    legs = s1_legs(ds, [float(ds.c[0][N - 1]), float(ds.c[1][N - 1])])
    for idx, role in enumerate(wds.roles):
        if role == "wing":
            legs.append({"side": "BUY", "entry": next_open(wds.o[idx], j), "exit": float(wds.c[idx][N - 1])})
    return net_from_legs(day, legs)


def combo(day, stop_pts=None, target=None, trail=None):
    """First of: disaster stop (points/unit), profit target (x premium), trailing exit - whichever fires first on the same minute path."""
    ds = H[day][S]
    P = INFO[day]["premium"]
    idxs = []
    if stop_pts is not None:
        h = np.nonzero(ds.mtm <= -stop_pts)[0]
        idxs += [int(h[0])] if h.size else []
    if target is not None:
        h = np.nonzero(ds.mtm >= target * P)[0]
        idxs += [int(h[0])] if h.size else []
    if trail is not None:
        peak = np.maximum.accumulate(ds.mtm)
        h = np.nonzero((peak >= trail[0] * P) & (ds.mtm <= peak - trail[1] * P))[0]
        idxs += [int(h[0])] if h.size else []
    if not idxs:
        return net_close(day, S, UNITS)
    j = min(idxs)
    return net_from_legs(day, s1_legs(ds, [next_open(ds.o[0], j), next_open(ds.o[1], j)]))


def series_of(fn, **kw):
    return pd.Series({d: fn(d, **kw) for d in DAYS})


variants: list[tuple[str, str, pd.Series]] = []
for k in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5):
    variants.append(("combined stop", f"loss >= {k} x premium", pd.Series({d: combined_stop(d, k * INFO[d]["premium"]) for d in DAYS})))
for pts in (100, 125, 150, 175, 200, 250):
    variants.append(("combined stop", f"loss >= {pts} pts/unit (Rs {pts * UNITS:,})", series_of(combined_stop, level_pts=pts)))
for k in (0.3, 0.5, 0.7, 1.0, 1.5, 2.0):
    variants.append(("per-leg stop (that leg only), on close", f"leg +{int(k * 100)}%", series_of(leg_stop, k=k)))
    variants.append(("per-leg stop (closes both), on close", f"leg +{int(k * 100)}%", series_of(leg_stop, k=k, both=True)))
    variants.append(("per-leg stop (that leg only), INTRABAR touch (optimistic)", f"leg +{int(k * 100)}%", series_of(leg_stop, k=k, intrabar=True)))
    variants.append(("per-leg stop (closes both), INTRABAR touch (optimistic)", f"leg +{int(k * 100)}%", series_of(leg_stop, k=k, both=True, intrabar=True)))
for f in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
    variants.append(("profit target", f"exit at +{int(f * 100)}% of premium", series_of(profit_target, frac=f)))
for arm, give in ((0.3, 0.15), (0.3, 0.25), (0.5, 0.2), (0.5, 0.3), (0.7, 0.2)):
    variants.append(("trailing exit", f"arm {arm} x premium, give back {give}", series_of(trailing, arm=arm, give=give)))
for k in (0.5, 0.75, 1.0, 1.25):
    for wing in ("iron_fly_w2", "iron_fly_w1"):
        variants.append(("delayed hedge (buy wings after loss)", f"loss >= {k} x premium, buy {'S3' if wing.endswith('w2') else 'S2'} wings", series_of(delayed_hedge, k=k, wing=wing)))

rows = []
for family, label, ser in variants:
    rec = {"family": family, "variant": label}
    for period, days in (("all", DAYS), ("train", TRAIN_DAYS), ("valid", VALID_DAYS)):
        m = rule_metrics(base, ser, days)
        rec.update({f"{period}_net": m["net"], f"{period}_gain": round(m["net"] - m["base_net"], 0), f"{period}_worst": m["worst_day"], f"{period}_max_dd": m["max_dd"], f"{period}_t": m["t"], f"{period}_win%": m["win_pct"]})
    diff = ser - base
    rng = np.random.default_rng(RNG_SEED)
    boots = [diff.sample(len(diff), replace=True, random_state=int(rng.integers(1e9))).sum() for _ in range(1000)]
    rec["gain_ci95_low"], rec["gain_ci95_high"] = round(float(np.percentile(boots, 2.5)), 0), round(float(np.percentile(boots, 97.5)), 0)
    rows.append(rec)
    record("s1_improvement", {"family": family, "variant": label, "units": UNITS}, rec)
res = pd.DataFrame(rows)
res.to_csv(RESULTS / "17_s1_improvements.csv", index=False)
b = {p: summarize(base.loc[d].tolist()) for p, d in (("all", DAYS), ("train", TRAIN_DAYS), ("valid", VALID_DAYS))}
print("BASELINE S1 (3 lots, frictions):", {p: (b[p]["net"], b[p]["worst_day"], b[p]["max_dd"], b[p]["t"]) for p in b})
cols = ["family", "variant", "all_gain", "all_worst", "all_max_dd", "all_t", "train_gain", "train_max_dd", "valid_gain", "valid_worst", "valid_max_dd", "valid_t", "gain_ci95_low", "gain_ci95_high"]
for family in res["family"].unique():
    print(f"\n--- {family}")
    print(res[res["family"] == family][cols].drop(columns=["family"]).to_string(index=False))

# choose on TRAIN: maximise train net subject to train max drawdown not above baseline's, then look at validation
ok = res[(res["train_max_dd"] <= b["train"]["max_dd"] + 1) & (res["train_gain"] > 0)].sort_values("train_gain", ascending=False)
print("\n=== chosen on TRAINING days (max train gain with train max DD <= baseline), then applied to validation ===")
print(ok[["family", "variant", "train_gain", "train_max_dd", "valid_gain", "valid_worst", "valid_max_dd", "valid_t"]].head(12).to_string(index=False))
save_json("17_s1_train_choice.json", ok.head(12).to_dict("records"))
print("\nvariants tested:", len(res), "| gains > 0 on train:", int((res["train_gain"] > 0).sum()), "| of those gains > 0 on validation too:", int(((res["train_gain"] > 0) & (res["valid_gain"] > 0)).sum()))
