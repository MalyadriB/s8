"""Framework for testing skip / size / stop / exit rules on the day dataset with a chronological train/validation split.

Discipline built in:
  * thresholds are quantiles of the TRAINING days only and are frozen before the validation days are looked at;
  * a skipped day is a 0 in the daily series (it stays in the sample, so t-values and drawdowns are comparable with the baseline);
  * every rule is compared with random skipping of the same number of days (permutation test), so a rule that merely
    reduces exposure does not look like skill;
  * every tested rule is recorded (experiments.jsonl), not only the winners.
"""
from __future__ import annotations

import hashlib
import json
import time

import numpy as np
import pandas as pd

from engine import *  # noqa: F403

DF: pd.DataFrame = pd.read_pickle(CACHE / "days.pkl")
TRAIN_FRACTION = 0.60
CUT = int(TRAIN_FRACTION * len(DAYS))
TRAIN_DAYS, VALID_DAYS = DAYS[:CUT], DAYS[CUT:]
RNG_SEED = 20260920
N_PERM = 2000

_code_sha = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(ROOT.glob("*.py")))).hexdigest()[:12]
_data_sha = hashlib.sha256((CACHE / "dataset.pkl").read_bytes()).hexdigest()[:12]
RUN_ID = time.strftime("%Y%m%d-%H%M%S")
EXPERIMENTS = RESULTS / "experiments.jsonl"
_recorded = 0


def record(experiment: str, params: dict, result: dict) -> None:
    """Append one experiment (its parameters and everything it measured) so the run can be audited and reproduced."""
    global _recorded
    _recorded += 1
    line = {"run": RUN_ID, "code_sha": _code_sha, "data_sha": _data_sha, "experiment": experiment, "params": params, "result": result}
    with EXPERIMENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, default=str) + "\n")


def net_series(strategy: str, units: int = UNITS, j: int = N - 1, basis: str = "fric") -> pd.Series:
    """Baseline net rupees per day for a strategy (NaN where the project's backtest has no row for that day)."""
    have = set(DATA["strategy_days"][strategy])
    return pd.Series({d: (net_close(d, strategy, units, j, basis) if d in have else np.nan) for d in DAYS})


def maxdd_rows(matrix: np.ndarray) -> np.ndarray:
    cum = np.cumsum(matrix, axis=1)
    peak = np.maximum.accumulate(np.maximum(cum, 0), axis=1)
    return (peak - cum).max(axis=1)


def summary(daily: pd.Series) -> dict:
    return summarize(daily.dropna().tolist())


def rule_metrics(base: pd.Series, modified: pd.Series, days: list[str]) -> dict:
    """Comparison of a modified daily series with the baseline over `days` (NaN days = no baseline trade there, dropped)."""
    b = base.loc[days].dropna()
    m = modified.loc[b.index]
    sb, sm = summarize(b.tolist()), summarize(m.tolist(), traded=int((m != 0).sum()))
    skipped = (m == 0) & (b != 0)
    kept_changed = (m != b) & ~skipped
    out = {
        "days": sb["days"], "skipped": int(skipped.sum()), "skipped_pct": round(100 * skipped.sum() / max(1, len(b)), 1),
        "profitable_days_removed": int((skipped & (b > 0)).sum()), "losing_days_avoided": int((skipped & (b < 0)).sum()),
        "days_changed_not_skipped": int(kept_changed.sum()),
        "base_net": sb["net"], "net": sm["net"], "avg_day": sm["avg_day"], "median_day": sm["median_day"], "win_pct": sm["win_pct"],
        "worst_day": sm["worst_day"], "max_dd": sm["max_dd"], "t": sm["t"], "base_worst": sb["worst_day"], "base_max_dd": sb["max_dd"], "base_t": sb["t"],
        "profit_retained_pct": round(100 * sm["net"] / sb["net"], 1) if sb["net"] > 0 else float("nan"),
        "worst_day_reduction_pct": round(100 * (1 - sm["worst_day"] / sb["worst_day"]), 1) if sb["worst_day"] < 0 else 0.0,
        "max_dd_reduction_pct": round(100 * (1 - sm["max_dd"] / sb["max_dd"]), 1) if sb["max_dd"] > 0 else 0.0,
        "skipped_mean": round(float(b[skipped].mean()), 2) if skipped.any() else float("nan"),
        "traded_mean": round(float(b[~skipped].mean()), 2) if (~skipped).any() else float("nan"),
    }
    out["score"] = round(out["max_dd_reduction_pct"] - (100 - out["profit_retained_pct"] if out["profit_retained_pct"] == out["profit_retained_pct"] else 0), 1)
    return out


def permutation_p(base: pd.Series, days: list[str], k: int, observed_score: float, seed: int = RNG_SEED) -> float:
    """Probability that skipping k RANDOM days scores at least `observed_score` (score = maxDD reduction % - profit lost %)."""
    b = base.loc[days].dropna().to_numpy()
    n = len(b)
    if k <= 0 or k >= n:
        return float("nan")
    rng = np.random.default_rng(seed)
    idx = np.argsort(rng.random((N_PERM, n)), axis=1)[:, :k]
    mask = np.zeros((N_PERM, n), dtype=bool)
    np.put_along_axis(mask, idx, True, axis=1)
    matrix = np.where(mask, 0.0, b[None, :])
    base_dd, base_net = maxdd_rows(b[None, :])[0], b.sum()
    dd_red = 100 * (1 - maxdd_rows(matrix) / base_dd) if base_dd > 0 else np.zeros(N_PERM)
    lost = 100 - 100 * matrix.sum(axis=1) / base_net if base_net > 0 else np.zeros(N_PERM)
    scores = dd_red - lost
    return round(float((scores >= observed_score).mean()), 4)


# ------------------------------------------------------------------ candidate skip rules (every one uses only pre-09:30 information)

UPPER = ["gap_abs_pct", "move_abs_pct", "range_pct", "range_over_premium", "move_over_premium", "ce_pe_imb_abs", "prev_range_pct", "premium_pct", "premium_pctile"]
LOWER = ["premium_pct", "premium_pctile"]
SIGNED = ["gap_pct", "move_pct", "ce_pe_imb"]
QUANTILES = [0.70, 0.75, 0.80, 0.85, 0.90, 0.95]


def train_quantile(col: str, q: float) -> float:
    return float(DF.loc[TRAIN_DAYS, col].dropna().quantile(q))


def build_candidates() -> list[dict]:
    """Every skip rule tested: a feature, a direction and a threshold taken from the training days' quantiles."""
    rules = []
    for col in UPPER:
        for q in QUANTILES:
            rules.append({"name": f"{col} >= p{int(q * 100)}", "kind": "single", "terms": [(col, ">=", train_quantile(col, q))], "family": col})
    for col in LOWER:
        for q in QUANTILES:
            qq = round(1 - q, 2)
            rules.append({"name": f"{col} <= p{int(qq * 100)}", "kind": "single", "terms": [(col, "<=", train_quantile(col, qq))], "family": col + "_low"})
    for col in SIGNED:
        for q in QUANTILES:
            rules.append({"name": f"{col} >= p{int(q * 100)}", "kind": "single", "terms": [(col, ">=", train_quantile(col, q))], "family": col + "_up"})
            qq = round(1 - q, 2)
            rules.append({"name": f"{col} <= p{int(qq * 100)}", "kind": "single", "terms": [(col, "<=", train_quantile(col, qq))], "family": col + "_down"})
    rules.append({"name": "expiry day", "kind": "single", "terms": [("is_expiry", ">=", 1)], "family": "is_expiry"})
    for dow in ("Mon", "Tue", "Wed", "Thu", "Fri"):
        rules.append({"name": f"day is {dow}", "kind": "single", "terms": [("dow", "==", dow)], "family": "dow"})
    # combinations (section 5H), each at two strictness levels
    for q in (0.75, 0.85):
        t = lambda c, qq=q: (c, ">=", train_quantile(c, qq))
        rules += [
            {"name": f"expiry & move_abs >= p{int(q * 100)}", "kind": "combo", "terms": [("is_expiry", ">=", 1), t("move_abs_pct")], "family": "combo"},
            {"name": f"expiry & gap_abs >= p{int(q * 100)}", "kind": "combo", "terms": [("is_expiry", ">=", 1), t("gap_abs_pct")], "family": "combo"},
            {"name": f"gap_abs >= p{int(q * 100)} & premium_pct >= p{int(q * 100)}", "kind": "combo", "terms": [t("gap_abs_pct"), t("premium_pct")], "family": "combo"},
            {"name": f"range >= p{int(q * 100)} & move_abs >= p{int(q * 100)}", "kind": "combo", "terms": [t("range_pct"), t("move_abs_pct")], "family": "combo"},
            {"name": f"premium extreme(>=p{int(q * 100)}) & ce_pe_imb_abs >= p{int(q * 100)}", "kind": "combo", "terms": [t("premium_pct"), t("ce_pe_imb_abs")], "family": "combo"},
            {"name": f"gap_abs >= p{int(q * 100)} & move_abs >= p{int(q * 100)}", "kind": "combo", "terms": [t("gap_abs_pct"), t("move_abs_pct")], "family": "combo"},
        ]
    return rules


def rule_mask(rule: dict) -> pd.Series:
    """True on days the rule says to skip / treat as risky. Days where a feature is unknown (NaN) are never flagged."""
    mask = pd.Series(True, index=DF.index)
    for col, op, value in rule["terms"]:
        x = DF[col]
        if op == ">=":
            mask &= x >= value
        elif op == "<=":
            mask &= x <= value
        else:
            mask &= x == value
    return mask.fillna(False)


def apply_skip(base: pd.Series, mask: pd.Series) -> pd.Series:
    return base.where(~mask.reindex(base.index).fillna(False), 0.0)
