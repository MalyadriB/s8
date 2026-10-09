# The 4-year test: S1, S2, S3, S4 on the real-price table (NSE end-of-day, 2022-01-03 to 2026-09-18, 1,165 days)

Daily-bar version: every leg is entered at the day's OPEN (about 09:15) and exited at NSE's CLOSE, with the project's own rules (`lab.plan_legs`, ATM = nearest 50 to the 09:15 spot, wings W = 1x / 2x the open premium; S4 = S1 on ordinary days, S3 on expiry days). 3 lots x 65 units, 0.10 points spread per fill, statutory rates by period (pre-Oct-2024 rates approximate). Script `daily_bar_all.py`; tables `nse_daily_s1_s4.csv`, `nse_daily_s1_s4_summary.csv`. Bar: t above 3.0.

| | net | avg/day | win % | worst day | max DD | t | vs 3.0 | both halves profitable |
|---|--:|--:|--:|--:|--:|--:|---|---|
| S1 | 2,393,412 | 2,054 | 67.1 | -200,050 (2024-06-04) | 200,050 | 4.89 | pass | yes |
| S2 | -98,685 | -85 | 51.4 | -70,552 (2026-02-03) | 626,860 | -0.37 | fail | no |
| S3 | 1,000,868 | 861 | 60.6 | -106,656 (2024-06-04) | 142,206 | 2.62 | fail | yes |
| S4 | 1,972,963 | 1,694 | 65.8 | -200,050 (2024-06-04) | 200,050 | 4.22 | pass | yes |

By period: S1 2022-01..2024-10 +862,662 (t 2.35), 2024-10 on +1,530,751 (t 4.76). S2 -486,355 (t -2.50) then +387,670 (t 2.18). S3 +102,349 (t 0.38) then +898,519 (t 3.37). S4 +624,717 (t 1.76) then +1,348,246 (t 4.43). S4's worst day is S1's: 2024-06-04 was not an expiry day.
Skipped days: S1 0, S2 0, S3 3 (0.3%); no wing traded fewer than 100 contracts all day.

## How far to trust it (479 real days where the project's minute-level result is known)
The daily-bar version enters at the open, not 09:30, and earns 2.2x to 3x as much: S1 corr 0.72 (₹1.48M vs ₹0.66M), S2 corr **0.33** (₹0.36M vs ₹0.12M), S3 corr 0.65 (₹0.87M vs ₹0.37M), S4 corr 0.70. On the real days the project's own strategies have t = 2.40 (S1), 1.08 (S2), 1.69 (S3), 2.38 (S4) - none reaches 3.0. So a "pass" here is a pass for the open-to-close version, not for the strategies at their declared 09:30-15:29 times, and S2's daily-bar figures are the least reliable.
