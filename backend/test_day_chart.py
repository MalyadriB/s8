"""The day chart: each strike's stored candles with the recorded entry and exit, and the whole position marked through the day."""
import ast
import unittest
from pathlib import Path

import day_chart
import storage
import strategy_lab as lab
import test_straddle as base

DAY = "2026-09-16"
BACKEND = Path(__file__).resolve().parent


def seed(entry_ce=100.0, exit_ce=110.0, entry_pe=90.0, exit_pe=60.0, ce_bars=None, pe_bars=None):
    legs = [{"side": "SELL", "entry": entry_ce, "exit": exit_ce, "type": "CE", "strike": 25000.0, "role": "body", "instrument_key": f"NSE_FO|CE{DAY}"},
            {"side": "SELL", "entry": entry_pe, "exit": exit_pe, "type": "PE", "strike": 25000.0, "role": "body", "instrument_key": f"NSE_FO|PE{DAY}"}]
    storage.upsert_backtest_row(lab.backtest_row("straddle_sell", DAY, "2026-09-22", 25000.0, entry_ce + entry_pe, legs, 65))
    ce = ce_bars if ce_bars is not None else [base.candle(DAY, "09:15", 99, 101, 98, 100), base.candle(DAY, "09:30", entry_ce, 105, 99, 104), base.candle(DAY, "11:00", 130, 140, 125, 135), base.candle(DAY, "15:29", 108, exit_ce + 1, 107, exit_ce)]
    pe = pe_bars if pe_bars is not None else [base.candle(DAY, "09:15", 91, 92, 89, 90), base.candle(DAY, "09:30", entry_pe, 92, 85, 86), base.candle(DAY, "11:00", 70, 72, 65, 68), base.candle(DAY, "15:29", 62, 63, 59, exit_pe)]
    storage.save_historical_candles(f"NSE_FO|CE{DAY}", ce)
    storage.save_historical_candles(f"NSE_FO|PE{DAY}", pe)


class TheDayChart(base.TempDatabase):
    def test_each_leg_carries_its_candles_and_the_recorded_entry_and_exit(self):
        seed()
        data = day_chart.day_chart(DAY, "straddle_sell")
        self.assertTrue(data["available"])
        self.assertEqual([(l["type"], l["strike"], l["side"], l["entry"], l["exit"], l["bars_source"]) for l in data["legs"]],
                         [("CE", 25000.0, "SELL", 100.0, 110.0, "candles"), ("PE", 25000.0, "SELL", 90.0, 60.0, "candles")])
        self.assertEqual([b["t"] for b in data["legs"][0]["bars"]], ["09:15", "09:30", "11:00", "15:29"])
        self.assertEqual(data["legs"][0]["bars"][2], {"t": "11:00", "o": 130.0, "h": 140.0, "l": 125.0, "c": 135.0, "v": 0})

    def test_it_says_whether_the_recorded_trade_matches_the_candles(self):
        seed()
        self.assertEqual(day_chart.day_chart(DAY, "straddle_sell")["checks"], {"entry_matches_0930_open": True, "exit_matches_1529_close": True})
        # a stored exit that is not the 15:29 close of its candle is reported, not hidden
        seed(exit_ce=110.0, ce_bars=[base.candle(DAY, "09:30", 100.0, 105, 99, 104), base.candle(DAY, "15:29", 108, 111, 107, 108.5)])
        checks = day_chart.day_chart(DAY, "straddle_sell")["checks"]
        self.assertEqual((checks["entry_matches_0930_open"], checks["exit_matches_1529_close"]), (True, False))

    def test_the_position_is_marked_at_each_close_and_ends_at_the_recorded_profit(self):
        seed()
        data = day_chart.day_chart(DAY, "straddle_sell")
        self.assertTrue(data["complete"])
        self.assertEqual(len(data["position"]), 360, "one point per minute, 09:30 to 15:29")
        by = {p["t"]: p for p in data["position"]}
        pnl = {p["t"]: p["pts"] for p in data["pnl"]}
        self.assertEqual(by["11:00"]["price"], 135.0 + 68.0, "both legs' closes at 11:00")
        self.assertEqual(pnl["11:00"], round((100.0 + 90.0) - (135.0 + 68.0), 2))
        self.assertEqual(by["11:01"]["price"], by["11:00"]["price"], "a minute with no trade repeats the previous close")
        self.assertEqual(pnl["15:29"], data["trade"]["gross_pts"], "the last point of the chart is the trade's recorded gross points")
        self.assertEqual(data["stats"]["worst"], min(data["pnl"], key=lambda p: p["pts"]))

    def test_a_leg_without_candles_is_flagged_and_no_position_is_invented(self):
        seed(pe_bars=[])
        data = day_chart.day_chart(DAY, "straddle_sell")
        self.assertTrue(data["available"])
        self.assertFalse(data["complete"])
        self.assertEqual((data["position"], data["pnl"], data["legs"][1]["bars_source"]), ([], [], "none"))

    def test_a_day_without_a_row_and_the_nse_basis_explain_themselves(self):
        seed()
        self.assertIn("no backtest row", day_chart.day_chart("2026-09-17", "straddle_sell")["reason"].lower())
        self.assertIn("no minute candles", day_chart.day_chart(DAY, "straddle_sell", "backtest", "nse_daily")["reason"].lower())
        with self.assertRaises(ValueError):
            day_chart.day_chart(DAY, "not_a_strategy")
        with self.assertRaises(ValueError):
            day_chart.day_chart(DAY, "straddle_sell", "somewhere")

    def test_s4_shows_the_legs_of_the_strategy_it_is_taken_from(self):
        seed()
        data = day_chart.day_chart(DAY, lab.S4)
        self.assertEqual((data["derived_from"], len(data["legs"])), ("straddle_sell", 2))

    def test_candles_are_built_from_ticks_when_a_paper_day_has_none_downloaded(self):
        key = "NSE_FO|TICKKEY"
        storage.initialize_database()
        with storage.get_connection() as connection:
            for stamp, ltp in ((f"{DAY}T10:00:05.100000+05:30", 100.0), (f"{DAY}T10:00:40.200000+05:30", 104.0), (f"{DAY}T10:01:10.300000+05:30", 99.0)):
                connection.execute("INSERT INTO market_observations (received_at, trading_date, instrument_key, ltp, payload) VALUES (?, ?, ?, ?, '{}')", (stamp, DAY, key, ltp))
        bars, where = day_chart.contract_bars(key, DAY)
        self.assertEqual(where, "ticks")
        self.assertEqual([(b["t"], b["o"], b["h"], b["l"], b["c"]) for b in bars], [("10:00", 100.0, 104.0, 100.0, 104.0), ("10:01", 99.0, 99.0, 99.0, 99.0)])
        self.assertEqual(day_chart.contract_bars(None, DAY), ([], "none"))


class TheChartsLastMinute(base.TempDatabase):
    def test_a_paper_day_is_drawn_to_its_own_exit_minute_and_a_backtest_day_to_1529(self):
        key = f"NSE_FO|CE{DAY}"
        storage.save_historical_candles(key, [base.candle(DAY, "15:29", 60, 60, 60, 60), base.candle(DAY, "15:39", 50, 50, 50, 50)])
        self.assertEqual([b["t"] for b in day_chart.contract_bars(key, DAY)[0]], ["15:29"], "the backtest window ends at 15:29")
        self.assertEqual([b["t"] for b in day_chart.contract_bars(key, DAY, lab.exit_minute(DAY))[0]], ["15:29", "15:39"])


class ReadOnly(unittest.TestCase):
    def test_the_module_only_reads(self):
        tree = ast.parse((BACKEND / "day_chart.py").read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(imported, {"__future__", "os", "pathlib", "typing", "strategy_lab", "storage"}, "no paper engine, stream, login or network code")
        source = (BACKEND / "day_chart.py").read_text(encoding="utf-8")
        self.assertNotIn("INSERT", source.upper())
        self.assertNotIn("UPDATE ", source.upper())


if __name__ == "__main__":
    unittest.main()
