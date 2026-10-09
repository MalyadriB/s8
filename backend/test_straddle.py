"""Offline tests for the straddle dataset and the shared HTTP retry helper.

    python -m unittest test_straddle -v

Everything runs against a throwaway DATA_DIR and a local HTTP server - no Upstox, no real database.
"""
import contextlib
import inspect
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.error import HTTPError

import http_util
import storage
import straddle

NIFTY = straddle.UNDERLYINGS["NIFTY"]
BANKNIFTY = straddle.UNDERLYINGS["BANKNIFTY"]
SPOT_KEY = NIFTY.index_key
DAY = "2026-09-15"  # an expiry day: the option keys below are lapsed-style (three-part) keys
CE_KEY, PE_KEY = "NSE_FO|111|15-09-2026", "NSE_FO|112|15-09-2026"

SPEC_COLUMNS = [
    ("trading_date", "TEXT"), ("expiry", "TEXT"), ("dte", "INTEGER"), ("is_expiry_day", "INTEGER"),
    ("atm_strike", "REAL"), ("ce_instrument_key", "TEXT"), ("pe_instrument_key", "TEXT"),
    ("spot_0915", "REAL"), ("spot_0930", "REAL"), ("spot_close", "REAL"),
    ("open_high", "REAL"), ("open_low", "REAL"), ("rest_high", "REAL"), ("rest_low", "REAL"),
    ("ce_0930", "REAL"), ("pe_0930", "REAL"), ("straddle_0930", "REAL"),
    ("ce_close", "REAL"), ("pe_close", "REAL"), ("straddle_close", "REAL"),
    ("realized_move_abs", "REAL"), ("straddle_pnl", "REAL"),
    ("ce_oi_0930", "INTEGER"), ("pe_oi_0930", "INTEGER"), ("ce_vol_0930", "INTEGER"), ("pe_vol_0930", "INTEGER"),
    ("built_at", "TEXT"), ("build_version", "TEXT"),
]
# Added after the first version shipped; appended so the original columns keep their spec order.
ADDED_COLUMNS = [("intrinsic_close", "REAL"), ("strike_offset", "REAL"), ("underlying", "TEXT")]


NIFTY_BASE = {**dict.fromkeys(storage.STRADDLE_COLUMNS), "underlying": "NIFTY"}


def minutes(first: str, last: str):
    h, m = map(int, first.split(":"))
    end_h, end_m = map(int, last.split(":"))
    while (h, m) <= (end_h, end_m):
        yield f"{h:02d}:{m:02d}"
        m += 1
        if m == 60:
            h, m = h + 1, 0


def candle(day: str, hhmm: str, o, h, low, c, vol=0, oi=0):
    return [f"{day}T{hhmm}:00+05:30", o, h, low, c, vol, oi]


def spot_candles(day: str, after_entry_shift: float = 0.0):
    """375 one-minute bars. Everything up to and including the 09:30 open is identical for any
    `after_entry_shift`; only bars after 09:30 move."""
    out = []
    for i, hhmm in enumerate(minutes("09:15", "15:29")):
        base = 25013.0 + (i - 15) * 0.2 if i <= 15 else 25013.0 + after_entry_shift + (i - 15) * 0.3
        out.append(candle(day, hhmm, base, base + 5, base - 5, base + 1))
    return out


def leg_candles(day: str, entry_open: float, exit_close: float, drop_entry: bool = False):
    out = []
    for hhmm in minutes("09:15", "15:39"):  # options trade to 15:39; the exit must still be the 15:29 bar
        if hhmm == "09:30" and drop_entry:
            continue
        if hhmm < "09:30":
            out.append(candle(day, hhmm, 1.0, 1.0, 1.0, 1.0, vol=10, oi=1000 + int(hhmm[3:])))
        elif hhmm == "09:30":
            out.append(candle(day, hhmm, entry_open, entry_open + 1, entry_open - 1, entry_open, vol=999, oi=9999))
        elif hhmm == "15:29":
            out.append(candle(day, hhmm, exit_close, exit_close, exit_close, exit_close))
        elif hhmm > "15:29":
            out.append(candle(day, hhmm, 0.05, 0.05, 0.05, 0.05))
        else:
            out.append(candle(day, hhmm, 50.0, 60.0, 40.0, 55.0))
    return out


def contract(key: str, side: str, strike: float, expiry: str = DAY):
    return {"instrument_key": key, "expiry": expiry, "instrument_type": side, "strike_price": strike,
            "underlying_key": SPOT_KEY, "trading_symbol": f"NIFTY {strike:g} {side}"}


class TempDatabase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows keeps the SQLite file open
        self.addCleanup(self._tmp.cleanup)
        # MARKET_HOLIDAYS_FETCH off: no test may ask Upstox for the holiday list (see reliability.market_holidays())
        patcher = mock.patch.dict(os.environ, {"DATA_DIR": self._tmp.name, "MARKET_HOLIDAYS_FETCH": "false"})
        patcher.start()
        self.addCleanup(patcher.stop)
        import reliability
        import strategy_lab  # local import: keeps this shared helper free of a hard top-level dependency

        reliability._holidays.update(fetched_on=None, days=None, retry_at=0.0)

        # S5-S7's caches are keyed on (dates, row count, last date), not on which temp database it came from, so a
        # signature from one test's fixtures can otherwise collide with another test's completely different data -
        # this bit PnlExport once already (S8 used to have its own cache too, before it became a pure filter over
        # s5_overnight_rows() with no cache of its own - clearing S5's here already covers it). Clearing all three
        # here, not just in the test classes that exercise them directly, is what actually closes the bug for good.
        strategy_lab._s5_cache.clear()
        strategy_lab._s6_cache.clear()
        strategy_lab._s7_cache.clear()

    def seed_day(self, day=DAY, after_entry_shift=0.0, drop_ce_entry=False):
        storage.save_historical_candles(SPOT_KEY, spot_candles(day, after_entry_shift))
        storage.save_instrument_contracts([contract(CE_KEY, "CE", 25000.0), contract(PE_KEY, "PE", 25000.0)])
        storage.save_historical_candles(CE_KEY, leg_candles(day, 120.0, 40.0, drop_entry=drop_ce_entry))
        storage.save_historical_candles(PE_KEY, leg_candles(day, 95.0, 210.0))


def offline_builder():
    return straddle.DayBuilder(NIFTY, lambda: None, fetch=False, today=date(2026, 9, 19), log=lambda line: None)


class AtmStrikeRule(unittest.TestCase):
    def test_takes_exactly_one_argument(self):
        self.assertEqual(list(inspect.signature(straddle.resolve_atm_strike).parameters), ["spot_0930", "strike_step"])

    def test_rounds_to_nearest_50_halves_up(self):
        for spot, strike in [(25013.0, 25000.0), (24974.9, 24950.0), (24975.0, 25000.0), (25025.0, 25050.0), (25024.95, 25000.0), (100, 100.0)]:
            self.assertEqual(straddle.resolve_atm_strike(spot, 50), strike, spot)

    def test_refuses_anything_but_a_positive_scalar(self):
        for bad in ([25000.0, 25100.0], (25000.0,), {"open": 25000.0}, None, "25000", True, float("nan"), float("inf"), 0, -5.0):
            with self.assertRaises(AssertionError, msg=repr(bad)):
                straddle.resolve_atm_strike(bad, 50)

    def test_strike_step_is_a_positive_constant_and_rounds_per_underlying(self):
        for bad in (0, -50, None, True, "50", float("nan"), [50]):
            with self.assertRaises(AssertionError, msg=repr(bad)):
                straddle.resolve_atm_strike(25000.0, bad)
        u = straddle.UNDERLYINGS
        self.assertEqual({name: x.strike_step for name, x in u.items()},
                         {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100})
        for spot, step, strike in [(51349.9, 100, 51300.0), (51350.0, 100, 51400.0), (12512.4, 25, 12500.0), (12512.5, 25, 12525.0),
                                   (12537.4, 25, 12525.0), (82449.0, 100, 82400.0), (23425.0, 50, 23450.0)]:
            self.assertEqual(straddle.resolve_atm_strike(spot, step), strike, (spot, step))

    def test_later_information_cannot_move_the_strike(self):
        """Two days identical up to the 09:30 open but wildly different afterwards must pick the same strike."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, mock.patch.dict(os.environ, {"DATA_DIR": tmp}):
            storage.save_historical_candles(SPOT_KEY, spot_candles("2026-09-14"))
            storage.save_historical_candles(SPOT_KEY, spot_candles("2026-09-15", after_entry_shift=300.0))
            chosen = {}
            for day in ("2026-09-14", "2026-09-15"):
                spot = straddle.SpotBars(NIFTY, lambda: None, False).get(day)
                chosen[day] = straddle.resolve_atm_strike(float(spot["09:30"]["open"]), 50)
            self.assertEqual(chosen["2026-09-14"], chosen["2026-09-15"])
            self.assertEqual(chosen["2026-09-14"], 25000.0)


class Schema(TempDatabase):
    def test_straddle_daily_matches_the_spec_exactly(self):
        storage.initialize_database()
        with sqlite3.connect(storage.database_path()) as connection:
            info = connection.execute("PRAGMA table_info(straddle_daily)").fetchall()
        self.assertEqual([(name, kind) for _, name, kind, *_ in info], SPEC_COLUMNS + ADDED_COLUMNS)
        self.assertEqual([name for _, name, _, _, _, pk in sorted(info, key=lambda r: r[5]) if pk], ["underlying", "trading_date"])
        self.assertEqual(storage.STRADDLE_COLUMNS, [name for name, _ in SPEC_COLUMNS + ADDED_COLUMNS])

    def _legacy_tables(self, columns, with_skips=True):
        """A database from before the study covered more than NIFTY: trading_date alone was the primary key."""
        with contextlib.closing(sqlite3.connect(storage.database_path())) as connection, connection:
            definition = ", ".join(f"{name} {kind}" + (" PRIMARY KEY" if name == "trading_date" else "") for name, kind in columns)
            connection.execute(f"CREATE TABLE straddle_daily ({definition})")
            connection.execute("INSERT INTO straddle_daily (trading_date, straddle_0930) VALUES ('2025-01-02', 200.0)")
            if with_skips:
                connection.execute("CREATE TABLE straddle_build_skips (trading_date TEXT PRIMARY KEY, reason TEXT NOT NULL, detail TEXT, logged_at TEXT NOT NULL, build_version TEXT)")
                connection.execute("INSERT INTO straddle_build_skips VALUES ('2025-01-01', 'no_spot_data', '', 't', 'v1')")

    def _assert_migrated(self):
        with contextlib.closing(sqlite3.connect(storage.database_path())) as connection:
            info = connection.execute("PRAGMA table_info(straddle_daily)").fetchall()
            tables = [r[0] for r in connection.execute("select name from sqlite_master where name like 'straddle%'")]
        self.assertEqual([(name, kind) for _, name, kind, *_ in info], SPEC_COLUMNS + ADDED_COLUMNS)
        self.assertEqual(sorted(tables), ["straddle_build_skips", "straddle_daily"], "no leftover legacy table")
        row = storage.query_straddle_rows()[0]
        self.assertEqual((row["underlying"], row["trading_date"], row["straddle_0930"], row["intrinsic_close"]), ("NIFTY", "2025-01-02", 200.0, None))
        # and the new key really is (underlying, trading_date): the same day can exist for two underlyings
        storage.upsert_straddle_row({**NIFTY_BASE, "underlying": "BANKNIFTY", "trading_date": "2025-01-02", "straddle_0930": 500.0})
        self.assertEqual({r["underlying"]: r["straddle_0930"] for r in storage.query_straddle_rows()}, {"BANKNIFTY": 500.0, "NIFTY": 200.0})

    def test_a_table_from_the_first_version_is_migrated_in_place(self):
        self._legacy_tables(SPEC_COLUMNS[:28] if False else [c for c in SPEC_COLUMNS])
        storage.initialize_database()
        self._assert_migrated()

    def test_a_table_from_the_second_version_is_migrated_in_place(self):
        self._legacy_tables(SPEC_COLUMNS + [("intrinsic_close", "REAL"), ("strike_offset", "REAL")])
        storage.initialize_database()
        self._assert_migrated()

    def test_schema_creation_is_idempotent_with_existing_tables(self):
        storage.initialize_database()
        storage._initialized_databases.discard(storage.database_path())
        storage.initialize_database()  # second startup against an existing database must not fail


class BuildDay(TempDatabase):

    def test_active_and_lapsed_key_shapes_are_kept_apart(self):
        storage.save_instrument_contracts([contract("NSE_FO|56984", "CE", 25000.0), contract(CE_KEY, "CE", 25000.0)])
        find = lambda expired: storage.find_option_contract(SPOT_KEY, DAY, 25000.0, "CE", expired=expired)
        self.assertEqual(find(True), CE_KEY)
        self.assertEqual(find(False), "NSE_FO|56984")


def bank_contract(key: str, side: str, strike: float, expiry: str = DAY):
    return {"instrument_key": key, "expiry": expiry, "instrument_type": side, "strike_price": strike,
            "underlying_key": BANKNIFTY.index_key, "trading_symbol": f"BANKNIFTY {strike:g} {side}"}


class FlakyServer(BaseHTTPRequestHandler):
    script: list = []
    hits = 0
    agents: list = []

    def do_GET(self):
        type(self).hits += 1
        type(self).agents.append(self.headers.get("User-Agent"))
        status, headers = type(self).script[min(type(self).hits, len(type(self).script)) - 1]
        body = json.dumps({"status": status}).encode()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RetryHelper(unittest.TestCase):
    def setUp(self):
        FlakyServer.hits = 0
        self.server = HTTPServer(("127.0.0.1", 0), FlakyServer)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/x"
        sleeps = []
        patcher = mock.patch.object(http_util.time, "sleep", sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleeps = sleeps

    def test_retries_429_and_5xx_and_honours_retry_after(self):
        FlakyServer.script = [(429, {"Retry-After": "7"}), (503, {}), (200, {})]
        self.assertEqual(http_util.request_json(self.url, {}), {"status": 200})
        self.assertEqual(FlakyServer.hits, 3)
        self.assertEqual(self.sleeps[0], 7.0)  # server-named wait wins
        self.assertTrue(2.0 <= self.sleeps[1] < 3.0)  # else exponential 2**(attempt-1) + jitter

    def test_backoff_doubles(self):
        FlakyServer.script = [(500, {})] * 4 + [(200, {})]
        http_util.request_json(self.url, {})
        self.assertEqual([int(s) for s in self.sleeps], [1, 2, 4, 8])

    def test_gives_up_after_five_attempts_with_a_readable_error(self):
        FlakyServer.script = [(500, {})]
        with self.assertRaises(HTTPError) as caught:
            http_util.request_json(self.url, {})
        self.assertEqual((FlakyServer.hits, caught.exception.code), (5, 500))
        self.assertIn("500", caught.exception.read().decode())

    def test_client_errors_fail_fast(self):
        for status in (400, 401, 404):
            FlakyServer.hits = 0
            FlakyServer.script = [(status, {})]
            with self.assertRaises(HTTPError) as caught:
                http_util.request_json(self.url, {})
            self.assertEqual((FlakyServer.hits, caught.exception.code), (1, status))

    def test_a_user_agent_is_always_sent_because_cloudflare_blocks_urllibs_default(self):
        """Regression: the NIFTY exporter had no User-Agent after moving to this helper, and every export got HTTP 403 (error 1010)."""
        FlakyServer.script = [(200, {})]
        FlakyServer.agents.clear()
        http_util.request_json(self.url, {"Authorization": "Bearer x"})
        http_util.request_json(self.url, {"user-agent": "mine/2"})  # a caller's own, in any case, is kept
        self.assertEqual(FlakyServer.agents, [http_util.DEFAULT_USER_AGENT, "mine/2"])
        self.assertNotIn("Python-urllib", FlakyServer.agents[0])

    def test_pause_after_success_only_when_asked(self):
        FlakyServer.script = [(200, {})]
        http_util.request_json(self.url, {})
        self.assertEqual(self.sleeps, [])
        http_util.request_json(self.url, {}, pause=0.35)
        self.assertEqual(self.sleeps, [0.35])


class ExpiryAfterTheNearestLiveOne(unittest.TestCase):
    """On an expiry day the unfiltered contract search only shows that day's own expiry, so the NEXT one (what S5's
    paper roll asks for) has to be found by asking Upstox for a specific date."""

    TODAY, NEXT = "2026-09-29", "2026-10-06"  # Tuesday expiry, and the following Tuesday

    def fake_search(self, token, *, expiry=None, **params):
        self.searched.append(expiry)
        listed = {None: self.TODAY, self.TODAY: self.TODAY, self.NEXT: self.NEXT}
        if expiry not in listed:
            return {"data": []}
        return {"data": [{"expiry": listed[expiry], "underlying_key": NIFTY.index_key}]}

    def resolver(self):
        return straddle.ContractResolver(NIFTY, lambda: "tok", True, date.fromisoformat(self.TODAY), lambda line: None)

    def setUp(self):
        self.searched = []
        patches = [
            mock.patch.object(straddle.instruments, "search_instruments", self.fake_search),
            mock.patch.object(straddle.expired_api, "get_expiries", return_value=["2026-09-15", "2026-09-22"]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_today_still_resolves_to_todays_own_expiry_without_probing(self):
        self.assertEqual(self.resolver().nearest_expiry(self.TODAY), self.TODAY)
        self.assertEqual(self.searched, [None], "only the one unfiltered search, as before")

    def test_the_day_after_an_expiry_resolves_to_next_weeks(self):
        resolver = self.resolver()
        self.assertEqual(resolver.nearest_expiry("2026-09-30"), self.NEXT)
        self.assertEqual(self.searched, [None, "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", self.NEXT], "weekdays only, stopping at the first listed")
        resolver.nearest_expiry("2026-09-30")
        self.assertEqual(len(self.searched), 6, "cached: asked once per day")

    def test_nothing_listed_in_the_next_ten_days_is_still_a_skip(self):
        self.NEXT = "2026-12-01"
        with self.assertRaises(straddle.SkipDay):
            self.resolver().nearest_expiry("2026-09-30")


if __name__ == "__main__":
    unittest.main()
