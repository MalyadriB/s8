"""NSE end-of-day rows: the older P&L (Jan 2022 on) as its own basis, never mixed with the minute backtest."""
import unittest

import nse_daily
import storage
import strategy_lab as lab
import test_straddle as base


def option(day, expiry, strike, kind, open_, close, contracts=5000):
    return {"date": day, "expiry": expiry, "strike": float(strike), "type": kind, "open": open_, "close": close, "contracts": contracts}


def table(day="2022-01-05", expiry="2022-01-06", drop=()):
    rows = [option(day, expiry, 17800, "CE", 100.0, 40.0), option(day, expiry, 17800, "PE", 90.0, 95.0)]
    rows += [option(day, expiry, 18000, "CE", 30.0, 5.0), option(day, expiry, 17600, "PE", 28.0, 60.0)]  # S2 wings (W = 200)
    rows += [option(day, expiry, 18200, "CE", 12.0, 1.0), option(day, expiry, 17400, "PE", 11.0, 30.0)]  # S3 wings (W = 400)
    return [r for r in rows if (r["strike"], r["type"]) not in drop]


class BuildingTheRows(unittest.TestCase):
    def test_the_projects_rules_on_open_and_close_prices(self):
        rows, skipped = nse_daily.build_rows(table(), {"2022-01-05": 17810.0}, lambda day: 50)
        self.assertEqual(skipped, [])
        by = {r["strategy"]: r for r in rows}
        self.assertEqual(sorted(by), sorted(lab.STRATEGIES))
        s1 = by["straddle_sell"]
        self.assertEqual((s1["atm_strike"], s1["lot_size"], s1["lots"], s1["build_version"], s1["expiry"]), (17800.0, 50, 1, "nse-daily-v1", "2022-01-06"))
        self.assertEqual([(l["side"], l["type"], l["entry"], l["exit"]) for l in s1["legs"]], [("SELL", "CE", 100.0, 40.0), ("SELL", "PE", 90.0, 95.0)])
        self.assertEqual(s1["gross_pts"], 55.0)
        s2, s3 = by["iron_fly_w1"], by["iron_fly_w2"]
        self.assertEqual((s2["wing_width"], s3["wing_width"]), (200.0, 400.0), "W = 1x and 2x the ATM open premium (190), rounded to 50")
        plan = lab.plan_legs("iron_fly_w1", 17800.0, 190.0)
        self.assertEqual([(l["side"], l["type"], l["strike"]) for l in s2["legs"]], [(p["side"], p["type"], p["strike"]) for p in plan], "the same legs, in the same fill order, as the paper engine")

    def test_a_missing_wing_leaves_out_that_strategy_only_and_says_so(self):
        rows, skipped = nse_daily.build_rows(table(drop={(18200.0, "CE")}), {"2022-01-05": 17810.0}, lambda day: 50)
        self.assertEqual({r["strategy"] for r in rows}, {"straddle_sell", "iron_fly_w1"})
        self.assertEqual([(s["strategy"], s["trading_date"]) for s in skipped], [("iron_fly_w2", "2022-01-05")])
        self.assertIn("18200", skipped[0]["reason"])

    def test_a_day_without_a_traded_atm_or_a_spot_is_reported_not_dropped_silently(self):
        _, skipped = nse_daily.build_rows(table(drop={(17800.0, "PE")}), {"2022-01-05": 17810.0}, lambda day: 50)
        self.assertEqual([(s["strategy"], "ATM" in s["reason"]) for s in skipped], [("all", True)])
        _, skipped = nse_daily.build_rows(table(), {}, lambda day: 50)
        self.assertEqual(skipped[0]["reason"], "no 09:15 spot")

    def test_the_nearest_expiry_with_trades_is_used(self):
        rows = table(expiry="2022-01-06") + [option("2022-01-05", "2022-01-13", 17800, "CE", 150.0, 140.0), option("2022-01-05", "2022-01-13", 17800, "PE", 140.0, 150.0)]
        rows += [option("2022-01-05", "2022-01-04", 17800, "CE", 1.0, 1.0)]  # already expired: ignored
        built, _ = nse_daily.build_rows(rows, {"2022-01-05": 17810.0}, lambda day: 50)
        self.assertEqual({r["expiry"] for r in built}, {"2022-01-06"})

    def test_lot_size_schedule(self):
        recorded = {"2024-10-03": 25, "2024-11-21": 75, "2025-12-31": 65}
        self.assertEqual([nse_daily.lot_size_schedule(d, recorded) for d in ("2022-01-03", "2024-04-25", "2024-04-26", "2024-10-02")], [50, 50, 25, 25])
        self.assertEqual([nse_daily.lot_size_schedule(d, recorded) for d in ("2024-10-03", "2024-11-20", "2024-11-21", "2026-01-02")], [25, 25, 75, 65])


class TheTwoBasesStayApart(base.TempDatabase):
    def seed(self):
        rows, _ = nse_daily.build_rows(table(), {"2022-01-05": 17810.0}, lambda day: 50)
        for row in rows:
            storage.upsert_nse_daily_row(row)
        # a minute-backtest row on another date, and one on the SAME date with a different result
        legs = [{"side": "SELL", "entry": 100.0, "exit": 90.0, "type": "CE", "strike": 25000.0, "role": "body"}]
        for day, exit_ in (("2024-10-07", 90.0), ("2022-01-05", 10.0)):
            storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, "2024-10-10", 25000.0, 190.0, [{**legs[0], "exit": exit_}], 25))

    def test_each_basis_reads_only_its_own_table(self):
        self.seed()
        nse = lab.backtest_daily("straddle_sell", None, None, 1, basis="nse_daily")
        minute = lab.backtest_daily("straddle_sell", None, None, 1)
        self.assertEqual([r["trading_date"] for r in nse], ["2022-01-05"])
        self.assertEqual([r["trading_date"] for r in minute], ["2022-01-05", "2024-10-07"])
        self.assertEqual((nse[0]["basis"], minute[0]["basis"]), ("nse_daily", "minute"))
        self.assertNotEqual(nse[0]["gross_pts"], minute[0]["gross_pts"])
        self.assertEqual(nse[0]["running_net_rs"], nse[0]["net_rs"], "running totals are per basis, never carried over from the other table")
        self.assertEqual(lab.backtest_daily("straddle_sell", None, None, 1), lab.backtest_daily("straddle_sell", None, None, 1, basis="minute"), "the default is the minute backtest")

    def test_s4_is_built_within_the_basis(self):
        self.seed()
        s4 = lab.backtest_daily(lab.S4, None, None, 1, basis="nse_daily")
        self.assertEqual([(r["trading_date"], r["derived_from"]) for r in s4], [("2022-01-05", "straddle_sell")], "2022-01-05 is not its own expiry (2022-01-06), so S1's result")
        expiry_rows, _ = nse_daily.build_rows(table("2022-01-06", "2022-01-06"), {"2022-01-06": 17810.0}, lambda day: 50)
        for row in expiry_rows:
            storage.upsert_nse_daily_row(row)
        s4 = {r["trading_date"]: r for r in lab.backtest_daily(lab.S4, None, None, 1, basis="nse_daily")}
        self.assertEqual(s4["2022-01-06"]["derived_from"], "iron_fly_w2")
        self.assertEqual({r["derived_from"] for r in lab.derived_backtest_rows(lab.S4, basis="minute")[0]}, {"straddle_sell"}, "the minute S4 never sees the NSE iron-fly rows")

    def test_a_size_can_be_applied_to_the_older_rows(self):
        self.seed()
        one = lab.backtest_daily("straddle_sell", None, None, lab.Size(lots=1), basis="nse_daily")[0]
        qty = lab.backtest_daily("straddle_sell", None, None, lab.Size(qty=100), basis="nse_daily")[0]
        self.assertEqual((one["qty"], qty["qty"], qty["lots"]), (50, 100, 2))
        self.assertEqual(qty["gross_rs"], 100 * 55.0)

    def test_an_unknown_basis_is_refused_and_paper_is_never_listed_next_to_older_rows(self):
        with self.assertRaises(ValueError):
            lab.check_basis("weekly")
        self.seed()
        csv_text = lab.pnl_csv(lab.Size(lots=1), "all", None, "nse_daily")
        rows = [line.split(",") for line in csv_text.strip().split("\n")]
        self.assertEqual({r[0] for r in rows[1:]}, {"backtest"})
        self.assertEqual(rows[0][-1], "basis")
        self.assertEqual({r[-1] for r in rows[1:]}, {"nse_daily"})


if __name__ == "__main__":
    unittest.main()
