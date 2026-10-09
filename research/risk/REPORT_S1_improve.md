# Making S1 earn more: 57 variants tested, and the S5 candidate

Same data, split and frictions as `REPORT.md` (479 days, 3 lots x 65 units, 0.10 points spread per fill, STT 0.15% from 2026-04-01; train to 2025-12-08, validation after). Scripts `run_8_s1_improve.py`, `run_9_indices.py`, `run_10_s5.py`; tables `results/17_*` to `19_*`. Nothing in the project was changed.

S1 baseline: net ₹659,121, worst day -₹89,614, max drawdown ₹116,076, t 2.40 (train 1.77, validation 1.67).

## What was tested (all on the actual 1-minute candles; triggers on bar closes, fills at the next bar's open)

| family | variants | result vs S1 (479 days) |
|---|--:|---|
| **Per-leg stop loss** (stop each short leg at +30% ... +200% of its entry price; leg only, or close both; on close or on intrabar touch) | 24 | **Destroys profit: -₹735k to +₹28k.** Best case (+200%) is about break-even. Every setting below +150% loses money in both periods. A leg stop closes a leg that is only whipping around a range and leaves the other leg exposed. |
| **Profit target** (exit at +20% ... +80% of premium) | 7 | **Loses -₹3.6k to -₹332k.** S1's profit is earned by holding to the close; cutting winners early removes it. |
| **Trailing exit** (arm at 0.3-0.7 x premium, give back 0.15-0.3) | 5 | Loses -₹12k to -₹126k. |
| **Combined stop** (exit both legs when the loss reaches k x premium, or a fixed points level) | 13 | **-₹62k (0.5 x premium, too tight) to +₹121k**; positive in both periods for 0.75x-1.5x premium and for 125-175 points per unit. |
| **Delayed hedge** (after the loss trigger, buy the S2/S3 wings and hold) | 8 | -₹17k to +₹95k; no better than the plain stop and costs 4 extra fills. |

## The S5 candidate

```text
S5 = S1 + "straddle doubles" stop
09:30  sell ATM CE + ATM PE (as S1)
any minute: IF the loss on the open position reaches 1.0 x the 09:30 straddle premium
            (the combined option price has doubled), buy both legs back at the next minute's open
15:29  otherwise exit as S1
```

| | S1 | S5 |
|---|--:|--:|
| Net, 479 days | 659,121 | **762,954** (+15.8%) |
| Worst day | -89,614 | **-48,218** |
| Max drawdown | 116,076 | **98,678** |
| t-value | 2.40 | 3.09 |
| Validation only (192 days): net / worst / max DD | 255,406 / -42,669 / 90,059 | 287,838 / -42,669 / 90,059 |
| Stops fired | | 16 of 479 days (3.3%), **all 16 on expiry days** |

**Read this before using it: the profit gain is not reliable.**
* Of the 16 stops, 10 helped and 6 hurt. Two days, **2025-04-17 (+₹71.7k) and 2025-05-15 (+₹38.9k)**, account for more than the whole gain (+₹103.8k); without them S5 is about -₹7k against S1.
* Out-of-sample from July 2025 (299 days, walk-forward), the 1.0x stop is **worse** than S1 (₹373.9k vs ₹408.3k), and the walk-forward multiple choice (0.75x every fold) gave ₹359.0k with a larger drawdown. In the validation block alone it is +₹32.4k (5 stops).
* Bootstrap 95% interval for the total gain: -₹68k to +₹321k (86% of resamples above zero). Not statistically significant.
* What *is* mechanical: it caps the days when the straddle doubles. Its worst day and drawdown are lower. It does not cap slower losses (validation worst day 2026-02-19, -₹42.7k, never doubled the premium).

So S5 is best described as a **tail-risk control that costs about nothing**, not a profit booster. If the lower drawdown lets you run more lots at the same risk, that is where extra profit would come from (S5 at 3 lots has a lower drawdown than S1 at 3 lots; S5 at 4 lots has ₹131k max drawdown, ₹1.03M net, worst day -₹64k, versus S1 at 3 lots ₹116k, ₹659k, -₹90k).

## What actually raises S1's profit

1. **Size.** Profit is proportional to lots and so is risk (S1: 3 lots ₹659k / drawdown ₹116k; 5 lots ₹1.13M / ₹192k).
2. **More indices** (`18_s1_other_indices.csv`, 1 lot each, the lab's cost model, no spread, screening only): MIDCPNIFTY ₹254k (t 3.4; validation t 1.4), BANKNIFTY ₹128k (t 1.9; validation t 0.27), FINNIFTY ₹119k (t 1.6; validation t 0.68), SENSEX ₹71k (t 0.83; **flat on non-expiry days**, +₹5/day). Correlation with NIFTY S1 is 0.47-0.74, so it diversifies only partly: NIFTY + BANKNIFTY (1 lot each) gives ₹345k against ₹217k but a max drawdown of ₹79k against ₹45k. Their recent-period results are much weaker, MIDCPNIFTY's options are thin, and BSE charges are not in the cost model.
3. **Lower costs.** Charges plus spread are roughly ₹240 a day at 3 lots against ₹1,376 average profit, mostly fixed brokerage and statutory charges; little scope to cut.

Not testable with stored data: entering at another time (the ATM strike would change; candles for it are not stored), and other wing structures (token expired).

## Conclusion

No rule tested reliably increases S1's profit. Per-leg stops, profit targets and trailing exits clearly reduce it. A wide combined stop (S5) is the only candidate that lowers the worst day and drawdown, at a profit effect that is positive on average but rests on two days.
