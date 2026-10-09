# Can S1 be backtested from 2022? What Upstox has, and a modelled extension

## 1. What Upstox can give (tested live on 2026-09-20 with your valid token)

| data | earliest available |
|---|---|
| Expired option/future **contracts** and their candles (NIFTY) | **2024-10-03**. The expiry list starts there, and asking for the contracts of expiries on 2024-09-26, 2024-06-27, 2023-12-28, 2022-09-29 and 2022-01-27 returns 0. BANKNIFTY starts 2024-10-01, SENSEX 2024-10-04. Upstox's documentation states no limit, so this is the observed behaviour. |
| NIFTY 1-minute **spot** | 2022-01 (January 2022 returns data, November 2021 returns none); daily from 2015 |
| India VIX 1-minute | 2022-01; daily from at least 2019 |

The "2022" is spot and VIX history. No option prices exist on Upstox before Oct 2024, so a **real** S1 backtest for 2022-2024 is not possible from Upstox.

Where real 2022-2024 option prices exist: Dhan's *Expired Options Data* API ("upto last 5 years", minute-level, weekly and monthly expiries, index options ATM +-10 strikes, 1/5/15/25/60-minute, up to 30 days per call, returns OHLC, volume, OI and IV; needs a Dhan `access-token`, plan requirements not stated on the page). Sources: https://dhanhq.co/docs/v2/expired-options-data/ .

## 2. Modelled S1 (Black-Scholes from actual spot and India VIX), validated on the real days

Script `model_backtest.py`; data cached in `cache/` (project database untouched).

* Plain model vs the 478 real days: daily gross P&L correlation **0.86-0.89**; entry premium correlation 0.93, mean error 12%. But it is **biased**: model total -₹6.6L against real +₹6.5L. Real options lose value faster than a constant-vol model says, by about 14 points a day.
* Bias-corrected (additive points per days-to-expiry bucket, fitted on the real training days only) on the **unseen validation days**: model ₹285.7k against real ₹248.1k (115%), correlation 0.86, worst day -45.1k vs -42.7k, win rate 74% vs 69%, max drawdown 84k vs 90k.

## 3. The 2022-2024 extension (678 trading days, 2022-01-03 to 2024-10-01), 3 lots, frictions basis

| premium richness assumed (1.0 = same as 2024-26) | net | avg/day | win % | worst | max DD | t |
|--:|--:|--:|--:|--:|--:|--:|
| 0 (plain model) | -1,019,793 | -1,504 | 55.8 | -76,453 | 1,061,056 | -3.97 |
| 0.5 | -86,680 | -128 | 67.1 | -76,038 | 291,633 | -0.34 |
| 0.75 | 379,877 | 560 | 70.1 | -75,831 | 210,170 | 1.48 |
| **1.0** | **846,433** | 1,248 | 73.6 | -75,624 | 148,724 | 3.30 |

By year at 1.0: 2022 +₹329k, 2023 +₹398k, 2024 (to Sep) +₹120k. Break-even needs about **0.55x** of the 2024-26 richness. Worst modelled days: 2022-06-16 (expiry, -₹75.6k), 2024-06-05 (election-result week, -₹60.2k), 2023-12-20, 2022-02-24 (war day, -₹51.2k).

Independent spot-only check: mean |15:29 - 09:30 move| divided by the move the India VIX implies is 1.49 (2022), 1.43 (2023), 1.57 (2024), 1.43 (2025), 1.49 (2026) - no visible shift between 2022-24 and 2024-26, which supports (does not prove) similar premium richness.

## 4. What this does and does not prove

It shows S1 would have been profitable in 2022-2024 **if** option premiums were as rich relative to realised moves as in 2024-26 (and at least about 55% as rich), on the real spot paths of those years including the 2022 war and 2024 election days. It cannot show that they were, because the richness is exactly what the missing option prices would tell us. Only real option prices (Dhan or another vendor) prove it.

Next step if you want proof: get a Dhan API token, download NIFTY ATM/ATM+-k expired weekly options for 2022-01 to 2026-09, check that Dhan's data matches our Upstox candles on the overlapping Oct 2024+ days, then run S1 (and the stop and S4 tests) on the real 2022-2024 prices.
