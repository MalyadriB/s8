# S1 on a real-price 4.7-year table (NSE end-of-day, 2022-01-03 to 2026-09-18)

Table: NSE derivatives bhavcopy, NIFTY index options, 1,167 trading days, 2.0 million contract-days (open, high, low, close, settle, volume, open interest), `cache/nse_nifty_options_daily.csv`. Downloaded with `fetch_nse_bhavcopy.py`; test in `daily_bar_s1.py`; output `nse_daily_s1.csv`.

Daily-bar S1: sell the ATM straddle (ATM from the 09:15 spot) at the day's OPEN (about 09:15), buy it back at NSE's CLOSE. 3 lots x 65 units, 0.10 points spread per fill, statutory rates by period (historical STT/exchange rates from memory, approximate).

## How much to trust it (validated on the 479 real minute-level days)
* NSE OPEN equals the first Upstox 1-minute candle open exactly (320 legs, mean absolute difference 0.00): the two sources agree.
* NSE CLOSE is noisier than the 15:29 candle close (mean +0.19, mean absolute difference 6.56 points).
* Daily-bar S1 is **not** a proxy for the project's S1 (09:30 entry). Daily gross points correlate 0.72 (train 0.80, validation 0.59) and it earns **2.2x** as much on the same days (₹1.48M against ₹0.66M): entering at the open sells richer prices than at 09:30.

## Result
| period | days | net | avg/day | win % | worst day | max DD | t |
|---|--:|--:|--:|--:|--:|--:|--:|
| 2022-01 to 2024-10-02 | 680 | 862,662 | 1,269 | 67.2 | -200,050 (2024-06-04) | 200,050 | 2.35 |
| 2024-10-03 to 2026-09-18 | 485 | 1,530,751 | 3,156 | 67.0 | -65,344 | 84,545 | 4.76 |
| all 4.7 years | 1,165 | 2,393,412 | 2,054 | 67.1 | -200,050 | 200,050 | 4.89 |
| first half (to 2024-05-13) | 582 | 682,417 | 1,173 | 66.2 | -65,003 | 107,130 | 2.51 |
| second half | 583 | 1,710,995 | 2,935 | 68.1 | -200,050 | 200,050 | 4.21 |

Profitable in every year (2022 +316k, 2023 +275k, 2024 +432k, 2025 +698k, 2026 to Sep +673k) and in both halves. Per-day profit in 2022-24 was **0.40x** that of 2024-26 by the same method. Scaling by the 2.2x open-versus-09:30 gap, the project's S1 would have earned roughly ₹0.4M over 2022-24 - a rough estimate, not a result.
Worst day: **2024-06-04** (election result): straddle 450 at the open, 1,474 at the close, -₹200,050 at 3 lots, more than twice the worst day in the 479-day sample. Without that day the 2022-24 t-value is 3.46. Expiry days earned an average ₹3,280 against ₹733 on other days before Oct 2024.

## Limits
Open-to-close only: no 09:30 entry, no stops, no wings at 09:30 prices. It cannot test any queue idea that needs a 09:30 or intraday price.
