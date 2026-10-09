"""Last checks: how surprising is the expiry share of the worst days, and a 1.5x-premium-wing version of S4 on the expiry days where its candles exist."""
from __future__ import annotations

import math

import run_5_widths_relative as W5  # noqa: F401  (rebuilds W5.avail; its tables print again)
from run_3_helpers import *  # noqa: F403

print("\n\n########## run_7 extras ##########")


def binom_tail(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


base_rate = DF["is_expiry"].mean()
rank = DF["range_over_premium"].rank(pct=True)
for s in STRATEGIES:
    tag = SHORT[s]
    worst = NET[s].dropna().sort_values().head(20).index
    k = int(DF.loc[worst, "is_expiry"].sum())
    k2 = int((rank.loc[worst] >= 0.9).sum())
    print(f"{tag}: expiry days among the 20 worst = {k}/20 (base rate {100 * base_rate:.1f}%) binomial p = {binom_tail(k, 20, base_rate):.4f} | range_over_premium >= p90 among the 20 worst = {k2}/20 (base 10%) p = {binom_tail(k2, 20, 0.10):.4f}")
    record("worst20_binomial", {"strategy": tag}, {"expiry": k, "range_over_premium_p90": k2})

# S4 with 1.5x-premium wings on expiry days
have = set(W5.avail[1.5])
days = [d for d in DAYS if (INFO[d]["is_expiry"] == 0) or d in have]
s1 = {d: net_close(d, "straddle_sell", UNITS) for d in days}
s3 = {d: (net_close(d, "iron_fly_w2", UNITS) if d in set(DATA["strategy_days"]["iron_fly_w2"]) else s1[d]) for d in days}
w15 = {d: (settle(W5.avail[1.5][d], UNITS, W5.avail[1.5][d].exit_close(N - 1), "fric")["net_rs"] if INFO[d]["is_expiry"] else s1[d]) for d in days}
s4 = {d: (s3[d] if INFO[d]["is_expiry"] else s1[d]) for d in days}
rows = []
cut = int(0.6 * len(days))
for label, sub in (("all", days), ("first 60%", days[:cut]), ("last 40%", days[cut:])):
    for name, series in (("S1", s1), ("S4 (S3 on expiry)", s4), ("S1 + 1.5x wings on expiry", w15)):
        v = [series[d] for d in sub]
        rows.append({"sample": label, "days": len(sub), "variant": name, **summarize(v)})
tbl = pd.DataFrame(rows)[["sample", "days", "variant", "net", "avg_day", "win_pct", "worst_day", "max_dd", "t"]]
print(f"(uses the {len(have)} expiry days where 1.5x-premium wing candles exist; other {int(DF['is_expiry'].sum()) - len(have)} expiry days are left out of all three lines)")
print(tbl.to_string(index=False))
tbl.to_csv(RESULTS / "16_s4_variant_1p5x.csv", index=False)
record("s4_variant_1p5x", {"expiry_days_covered": len(have)}, rows)
