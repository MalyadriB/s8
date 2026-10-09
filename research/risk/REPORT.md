# Risk-reduction research on S1 / S2 / S3 (NIFTY, 479 days, 2024-10-03 to 2026-09-18)

Question: can a repeatable pre-entry or intraday rule cut the large losing days without giving up too much of the edge?
Short answer: **no pre-entry filter, size rule, tight stop or earlier exit survived out-of-sample.** One narrow candidate (a wide "disaster" stop on S1) is positive but statistically weak. Details, numbers and caveats below. Nothing here changed S1-S3 or any project code; everything is in `D:\zero\research\risk`.

## 1. Data and baseline reproduction

| item | value |
|---|---|
| NIFTY 1-minute spot | `backend/data/exports/nifty_1min.csv`, 2023-09-06 to 2026-09-17 (750 days); 2026-09-18 comes from the 5-minute index candles in `market_observations` (the export is built after the close) |
| option candles | `market_observations`, 1-minute, expired-contract keys. ATM CE/PE for every day; S2/S3 wings for every day; a +-10-strike chain only on expiry days |
| trading days | 479 (S2 and S3 each miss one day, 2025-03-25 / 2025-03-24, because a wing has no candles - the project's backtest skips the same days) |
| expiry days | 102 (weekly expiry moved from Thursday to Tuesday in Sep 2025) |
| lot size | 25 / 75 / 65 over the history; this research uses a fixed 65 x 3 lots = 195 units |
| spread in the project's backtest | **none** (candle prices only); brokerage/STT/exchange/SEBI/stamp/GST are in |

**Reproduction:** rebuilt independently from raw candles (ATM from the 09:30 spot, contracts from `instrument_contracts`, 09:30 open and 15:29 close). 479 / 478 / 478 days, **0 mismatched leg prices, 0 mismatched nets**, 1-lot totals equal the stored backtest (S1 ₹208,702.51, S2 ₹9,493.83, S3 ₹91,155.19). File: `results/baseline_check.json`.

**Exit convention:** the baseline exit is the 15:29 bar's close (about 15:29:59); no 15:29:50 tick exists in candle data.

**Cost bases** (both use the project's own `settle_units` for statutory charges):
* `proj` - the project's backtest: no spread, STT 0.10%.
* `fric` - the frictions you declared on 20 Sep 2026: 0.10 points on every fill (0.20 per leg round trip) off gross, charges on unadjusted prices, STT 0.15% from 2026-04-01. **All research results below are on `fric`.**

Measured live spreads (`spreads.py`, sessions 15-16 Sep and part of 17 Sep 2026, so a small sample): ATM options 0.24-0.34 points wide (entry 0.34, mid-session 0.24, exit 0.28), wings 0.09-0.27. Half of that (0.12-0.17 at the money) is slightly above the 0.10 per fill assumed, so `fric` is if anything a little kind.

## 2. Baseline (3 lots = 195 units)

| | basis | net | avg/day | win % | worst day | max DD | t |
|---|---|--:|--:|--:|--:|--:|--:|
| S1 | proj | 699,363 | 1,460 | 66.0 | -89,536 | 114,516 | 2.55 |
| S1 | fric | 659,121 | 1,376 | 65.6 | -89,614 (2025-04-17, expiry) | 116,076 | 2.40 |
| S2 | proj | 199,495 | 417 | 59.2 | -16,064 | 84,444 | 1.78 |
| S2 | fric | 121,205 | 254 | 56.9 | -16,220 (2025-01-02) | 97,186 | 1.08 |
| S3 | proj | 427,346 | 894 | 62.3 | -43,464 | 98,405 | 2.06 |
| S3 | fric | 349,676 | 732 | 61.5 | -43,620 (2025-01-02) | 102,617 | 1.69 |
| S4 (derived) | fric | 567,193 | 1,184 | 64.7 | -48,218 (2025-05-12) | 106,763 | 2.38 |

Frictions remove 6% of S1's profit, **39% of S2's** and 18% of S3's (the iron flies pay the spread on 8 fills a day). S2's t-value falls to 1.08: after frictions its edge is not distinguishable from zero.

Chronological split used everywhere: **train 2024-10-03 to 2025-12-08 (287 days), validation 2025-12-09 to 2026-09-18 (192 days)**. Validation baselines: S1 ₹255,406 (t 1.67), S2 ₹35,906 (t 0.51), S3 ₹141,077 (t 1.17).

## 3. Day-level dataset

`results/day_dataset.csv` - 479 rows x 108 columns: date, weekday, expiry flag, days to expiry, previous close/high/low/range, gap, prices at 09:15/09:20/09:25/09:30, 09:15-09:30 move and range, ATM strike, CE/PE/straddle premium and ratios, premium % of spot, past-only premium percentile, listed strikes, wing widths, then for each strategy gross, brokerage, taxes, spread, net (frictions and project), 1- and 2-lot nets, max intraday loss/profit and their times, an adverse-extreme bound, equity, drawdown, drawdown increase, win flag, extreme-loss flag (worst 5%), and cross-strategy flags (all three lost: 159 days; only one lost: 28). Actual 1-minute mark-to-market paths: `results/paths_S1|S2|S3.csv.gz`. Every feature uses only bars that closed before 09:30.

## 4. The dangerous days

* S1's ten worst days (frictions, 3 lots): 2025-04-17 (-89.6k), 2025-05-15 (-65.1k), 2025-05-12 (-48.2k), 2024-11-28 (-46.6k), 2026-02-19 (-42.7k), 2026-09-15 (-42.7k), 2025-01-02 (-41.2k), 2025-01-06 (-38.8k), 2024-10-03 (-38.5k), 2026-06-23 (-38.4k). S2 and S3 worst-20 lists: `results/02_worst20_*.csv`.
* **Expiry days are over-represented among the worst 20**: S1 10/20, S2 14/20, S3 11/20 against a 21% base rate (binomial p 0.004 / <0.0001 / 0.001). Also a 09:15-09:30 range in the top decile relative to the straddle premium: 5/20, 6/20, 7/20 against 10% (p 0.04 / 0.01 / 0.002).
* But **a warning that fires on the worst days also fires on ordinary days**: "any one of seven features >= its 90th percentile" was true on 13 of S1's 20 worst days (65%) and on **43% of all days** - a lift of only 1.5x. Seven of the 20 worst days had no warning at all (for example 2026-02-19 and 2025-01-06).
* 7 days where S1 lost > 20k while S2 and S3 both stayed above -20k; 15 days where S2 or S3 also lost > 20k.

## 5. Pre-entry relationships (159 bucket comparisons, `results/03_buckets.csv`)

Only **5 of 159** buckets differ from the rest at p < 0.05 with n >= 30 (about 8 expected by chance), and none is a robust loss predictor:

* **Expiry vs non-expiry** (S1 mean 2,354 vs 1,111, p 0.55; S2 735 vs 123, p 0.50; S3 1,453 vs 536, p 0.56). Expiry days have fatter left tails (S1 5th percentile -38.5k vs -15.7k, 10 of S1's 21 losses over 20k) **but higher average profit**. Not different in the mean; do not skip.
* Gap size, opening move, opening range, premium level/percentile, CE/PE imbalance, gap direction, weekday: no consistent pattern. The "significant" ones point the other way (prior-day range in the top quartile is *better* for S1, mean +4,352, p 0.001).

## 6. Skip rules (section 6) - `results/04_skip_rules_all.csv`

120 rules per strategy (features x 6 quantile thresholds taken from the training days, both directions, weekday, expiry, 12 combinations). Eligible: >= 10 training days skipped and <= 25%. Promising in training: score > 0 (max-DD reduction % minus profit lost %), random-skip permutation p <= 0.10, worst day not worse, skipped days lost money.

| | eligible | promising in train | **pass validation** |
|---|--:|--:|--:|
| S1 | 84 | 17 | **0** |
| S2 | 88 | 7 | **0** |
| S3 | 87 | 7 | **0** |

The best training rule for S1, `|09:15 to 09:30 move| / straddle premium >= p90`, looked excellent in-sample (net ₹757,826 = 115% of baseline, worst day -46.6k, t 3.52) and **failed validation: 66% of profit kept, max drawdown 40% worse, t 1.39**. Every neighbouring threshold (p70-p95) also failed validation, which is what a chance finding looks like. "Skip Thursdays" also scored well in training only because Thursday *was* the expiry day until Sep 2025 (it is an ordinary day in the validation period). **Skipping expiry days** in validation kept 36% of S1's profit and raised the drawdown 44%.

## 7. Reduced size (section 7) - `results/06_sizing_rules_all.csv`

57 rules (8 features x two tier ladders incl. your 3/2/1/0 example, plus expiry-day sizing), tiers cut at training quantiles. 10 promising in training, **0 pass validation.** For S1 the best training ladder (3/2/1 at p80/p90 of the pre-entry move relative to premium) kept 73% of profit in validation with a 27% *larger* drawdown.

## 8. Intraday stops (section 8) - `results/07_stops.csv`, `09_stop_grid.csv`, `13_premium_relative_stops.csv`

Bar-by-bar on the 1-minute paths: breach is measured on the close-marked gross P&L; exit at the **next bar's open**. Levels: fixed 5,000-15,000 and training percentiles; a fine grid; premium-relative levels.

* **Tight stops hurt every strategy.** S1 at ₹7,500: 37.6% of days stopped, profit kept 46.7%, max DD 68% worse; holding would have been better in 64% of stopped days, and 56 stopped days were ones that would have finished profitable. Net cost of stopping ₹350k. S2 and S3 behave the same way (S3 ₹5,000: 42% of days stopped, profit kept 40.6%).
* **The training-selected stop failed validation for every strategy** (S1 train P90 = ₹20,944: worst day -25.5k but drawdown 64% worse in validation).
* **One candidate is different: a wide "disaster" stop on S1.** In the fine grid, levels **₹25,000 to ₹37,500 form a plateau** that helps on the whole sample, training and validation (validation score +18.5, +25.7, +23.1, +18.4, +11.3, +3.7); below ₹25,000 it hurts, above ₹40,000 it never fires. Training P95 of the daily max loss = **₹29,442 (151 points per unit)**: fires on 22 of 479 days (4.6%); whole sample net 107.8% of baseline, worst day -33.3k vs -89.6k (-63%), max DD 93.9k vs 116.1k (-19%), t 2.84 vs 2.40; validation: 111.6% net, worst -33.1k vs -42.7k, DD -14.1%. Walk-forward (level re-derived in each fold): pooled out-of-sample net 104.0% of baseline, worst -33.1k vs -42.7k, DD 76.4k vs 90.1k (-15.1%), t 2.61 vs 2.43 - but improved in only 2 of 5 folds, never fired in 2, and hurt in 1. Premium-relative stops at 1.0-2.0x the 09:30 straddle premium give a similar picture (net 105-116% of baseline, drawdown 9-19.5% lower, validation scores +2 to +13).
* **Caveat that keeps it a hypothesis:** only 7-22 stop events. Bootstrap 95% intervals of the total gain include zero for the P95 stop (all days: -140k to +240k; validation -4k to +73k). ₹35,000 is the only level whose whole-sample interval is above zero (+16k to +248k) but its validation gain is small (+8.6k). Also the stop assumes a fill at the next minute's open; a fast spike would slip.
* S2: no stop level helps. S3: nothing below ~₹29,500, above which it hardly ever fires.

## 9. Earlier exits (section 9) - `results/08_exits.csv`

Exits at 11:00 / 12:00 / 13:00 / 14:00 / 14:30 / 15:00 / 15:15 versus 15:29. **The baseline exit had the highest net in both train and validation for all three strategies.** Earlier exits gave up most of the profit (S1 at 12:00 kept 34% of baseline profit; S2 is negative for every exit before 15:00; S3 is near zero or negative). 12:00 shows the lowest validation drawdown for all three, but in the training period the same exit destroyed the profit (training score -91 for S1), so it is not a stable improvement.

## 10. Protection width (section 10) - `results/12_protection_widths.csv`, `15_widths_common_days.csv`

Only **W = 1x and 2x premium (S2, S3) exist for every day.** Alternative widths need candles that are not stored, and the Upstox token expired at 03:30 on 2026-09-20, so nothing could be downloaded. Coverage of the stored data: 0.5x on 102 days, 0.75x on 122, 1.25x on 116, 1.5x on 96 - **expiry days only** (the +-10 strike chain), and only 96 days have all six widths.
On those 96 expiry days: 0.5x loses money (-₹108/day, 33% wins), then average profit rises with width (0.75x +572, 1.0x +1,159, 1.25x +1,297, 1.5x +2,133, 2.0x +2,778, S1 +4,792 per day) and so does the worst day (-9.8k, -15.0k, -16.7k, -19.4k, -32.4k, S1 -46.6k). The ordering is broadly the same in the first and last part of the sample: **each width buys tail protection at a roughly proportional cost in profit; none is free.**
**Do not rely on this.** The six expiry days that lack 1.5x wing candles include four of S1's ten worst days (2024-10-03, 2025-01-02, 2025-04-17, 2025-05-15; S1 -₹219,885 on those six days), so the comparable sample is missing exactly the days that decide the question. Re-running with a fresh token (about 3,000 candle series) would fix this.

## 11. Worst-day diagnostics

Tables for the 20 worst days of each strategy with expiry flag, gap, 09:15-09:30 move, straddle price, S1/S2/S3 results and the pre-09:30 warnings: `results/02_worst20_S1.csv`, `_S2.csv`, `_S3.csv`. Common characteristics supported by data: expiry day (about half or more of the worst days), a large 09:15-09:30 range relative to the premium (5-7 of 20), and, for S1's single worst day, a very low premium (92.9 points, in the bottom decile) selling into a quiet open. None separates bad days from ordinary days well enough to act on (section 6).

## 12. Out-of-sample validation

* Single chronological split: train 287 days / validation 192 days; every threshold from training days only.
* **Hit rate: 0 of 41 rules that looked good in training passed validation** (31 skip rules, 10 sizing ladders). For comparison, arbitrary eligible rules pass the same validation test 0-10% of the time, so the pass rate of the training winners is no better than chance: what no signal looks like.
* Walk-forward, 5 expanding folds (test blocks 2025-07-03 to 2026-09-18, 299 days). The skip rule chosen in each fold changed from fold to fold (`move_over_premium >= p85`, then `day is Thu`, then `premium <= p20`). Pooled out-of-sample: S1 kept 64% of profit with a 40% larger drawdown; S2 turned negative (-82% of baseline); S3 shows +4.8% profit and -44.7% drawdown, but **entirely from one fold** in which the rule "skip Thursdays" (no longer expiry days) happened to work - not a finding. Files: `results/10_walk_forward_folds.csv`, `11_walk_forward_pooled.csv`.

## 13. Rules rejected (and why)

* All skip rules and size ladders: fail validation; every neighbouring threshold fails too.
* Skipping expiry days: they are the most profitable days on average.
* Any tight stop (below ~₹25k on S1, any level on S2, any level below ~₹29k on S3): raises drawdown by turning temporary losses into realised ones.
* Earlier exits: profit falls faster than risk.
* "Day is Thu" and other weekday rules: proxy for the old expiry weekday.
* Everything using information after 09:30 was excluded from entry decisions by construction.

## 14. Final comparison (validation days only, 192 days; rules selected on the training days; frictions basis; 3 lots)

| strategy / rule | trades | skipped | net | avg/day | win % | worst day | max DD | t | profit kept % | worst-day cut % | DD cut % |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| S1 baseline | 192 | 0 | 255,406 | 1,330 | 69.3 | -42,669 | 90,059 | 1.67 | 100 | 0 | 0 |
| S1 best skip (`move/premium >= p90`) | 174 | 18 | 169,272 | 882 | 63.5 | -42,669 | 125,983 | 1.39 | 66.3 | 0 | **-39.9** |
| S1 dynamic sizing (3/2/1 at p80/p90) | 192 | 0 | 187,032 | 974 | 69.3 | -42,669 | 113,965 | 1.52 | 73.2 | 0 | **-26.5** |
| S1 loss control, train-best (stop Rs 20,944) | 192 | 0 | 238,490 | 1,242 | 68.8 | -25,459 | 147,477 | 1.70 | 93.4 | 40.3 | **-63.8** |
| S1 disaster stop (train P95, Rs 29,442) - see caveat | 192 | 0 | 285,069 | 1,485 | 69.3 | -33,114 | 77,320 | 1.96 | 111.6 | 22.4 | +14.1 |
| S2 baseline | 192 | 0 | 35,906 | 187 | 59.9 | -14,980 | 97,186 | 0.51 | 100 | 0 | 0 |
| S2 best skip (`move/premium >= p85`) | 165 | 27 | -27,089 | -141 | 52.1 | -14,980 | 87,972 | -0.55 | -75.4 | 0 | +9.5 |
| S2 dynamic sizing | 192 | 0 | -8,575 | -45 | 59.9 | -14,980 | 85,564 | -0.16 | -23.9 | 0 | +12.0 |
| S2 loss control, train-best (Rs 10,000) | 192 | 0 | 5,934 | 31 | 59.4 | -12,627 | 129,007 | 0.09 | 16.5 | 15.7 | -32.7 |
| S3 baseline | 192 | 0 | 141,077 | 735 | 65.1 | -28,693 | 101,427 | 1.17 | 100 | 0 | 0 |
| S3 best skip (`move/premium >= p85`) | 165 | 27 | 51,496 | 268 | 55.7 | -23,458 | 118,228 | 0.58 | 36.5 | 18.2 | -16.6 |
| S3 dynamic sizing (3/2/1/0) | 182 | 10 | 62,315 | 325 | 61.5 | -23,458 | 107,414 | 0.73 | 44.2 | 18.2 | -5.9 |
| S3 loss control, train-best (train P70, Rs 8,245) | 192 | 0 | 53,998 | 281 | 60.4 | -12,381 | 204,508 | 0.54 | 38.3 | 56.9 | **-101.6** |

Same table on the full 479 days (in-sample, for contrast) and on the training days: `results/14_final_all.csv`, `14_final_train.csv`. In-sample, the S1 skip rule shows 115% profit and t 3.52 - the size of the gap between the in-sample and validation results is the overfitting.

**S4 (S1 with S3 on expiry days) as a rule** - `results/`: on validation it kept 80.8% of S1's profit, its worst day did not change (the worst validation day, 2026-02-19, is not an expiry day) and **max drawdown rose 18.5% (90,059 to 106,763)**, t 1.47 vs 1.67; a random swap of 41 days to S3 scored as well in 99% of trials. On training days: profit 89.3%, drawdown -14.2%, worst day -48.2k vs -89.6k (random-swap p = 0.33). Whole sample: 86.1% of profit, worst day -46%, drawdown -8%. The in-sample worst-day gain comes mostly from 2025-04-17.

## 15. The seven questions

1. **Can we identify high-risk days before 09:30?** Only weakly. Expiry days and a large 09:15-09:30 range relative to the premium are over-represented among the worst days, but the same alarms fire on 43% of ordinary days and 7 of S1's 20 worst days gave no warning.
2. **Which observable conditions go with extreme losses?** Expiry day (10/20, 14/20, 11/20 of the worst days), a large pre-entry range relative to premium (5-7/20), and, for the single worst S1 day, a bottom-decile premium. None predicts the *mean* outcome (expiry days are more profitable on average).
3. **Can we skip those days without giving up too much profit?** No. 0 of 31 skip rules that worked in training worked in validation; skipping expiry keeps 36% of profit and raises drawdown.
4. **Would reducing size work better than skipping?** No. 0 of 10 promising ladders passed; they cut profit and raised drawdown in validation.
5. **Can intraday loss management cut the worst losses without destroying recoveries?** Tight stops: no (holding would have been better in 52-67% of stopped days for S1 and S3). A wide disaster stop on S1 (about 151 points per unit, roughly 1.0-1.5x the premium): possibly, and it survived one chronological split and a walk-forward, but on only 7-22 events with confidence intervals that include zero.
6. **Which protection width gives the best risk/return?** Not answerable from the stored data (alternative widths only exist on expiry days and miss four of the worst S1 days). On the data that exists there is a smooth, roughly proportional trade of profit for tail protection and no free lunch; 0.5x loses money.
7. **Do the findings survive out-of-sample testing?** The negative ones do (skip, size, tight stops, earlier exits, expiry skipping all fail). The single positive one (S1 disaster stop) partly does.

## 16. Recommendation

**Baseline:** keep S1/S2/S3 as they are. After frictions S2 is not distinguishable from zero (t 1.08), S1 (t 2.40) and S3 (t 1.69) carry the profit.

**Do not add:** pre-entry skip filters, reduced size on "risky" days, expiry skipping, tight stops, or earlier exits.

**One research variant worth paper-tracking, clearly labelled a hypothesis:**

```text
S1 only.
IF the S1 position's close-marked gross loss reaches 151 points per unit
   (Rs 29,442 at 3 x 65 units; equivalently about 1.0-1.5 x the day's 09:30 straddle premium)
THEN exit both legs at the next minute's open
ELSE hold to 15:29 as now.
```

Expected on the historical data: worst day cut by about 20-60% (63% whole sample, 22% validation and walk-forward), max drawdown 14-19% lower, net profit about the same or slightly higher (104-112%), firing on about 5% of days. Expected cost: on days it fires, holding would have been better about 40% of the time. It needs the lab's paper marks to test live; I have not implemented anything.

**What not to change:** anything that looked good in-sample only (S1 move/premium skip: +15% profit in-sample, -34% out-of-sample), Thursday skipping, and any stop inside the S1 losing range below ~₹25k.

**Still to do (needs a fresh Upstox token, click Reconnect Upstox):** download 0.5x-1.5x wing candles for all 479 days and the six missing expiry days, then re-run `run_5_widths_relative.py`; protection width is the one section that is open.

## Reproducing

```text
cd D:\zero\research\risk
python build_dataset.py      # rebuilds cache/dataset.pkl and re-proves the baseline
python spreads.py            # measured spreads from the recorded stream
python dayset.py             # day dataset + paths
python run_1_diagnose.py     # baseline, dangerous days, buckets
python run_2_skip_size.py    # skip and size rules (train / validation)
python run_3_stops_exits.py  # stops and earlier exits
python run_4_walkforward.py  # stop grid + walk-forward
python run_5_widths_relative.py
python run_6_final.py        # final comparison
python run_7_extras.py
```

Every rule tested is in `results/experiments.jsonl` (564 distinct experiments: 360 skip rules, 57 size rules, 27 stop rules + 37 grid + 17 premium-relative, 24 exit times, 16 width cells, 15 walk-forward fold selections, plus summaries). Each line carries the run id, a hash of the code and of the data, its parameters and everything it measured. Seeds are fixed (20260920). Expect about 5-9% of eligible rules to pass a p <= 0.10 test by luck; that is the yardstick for every "promising" count above.
