"""Tests for the PAPER trading engine and the strategy lab.

    python -m unittest test_paper -v

The safety tests come first: paper trading must never be able to place a real order.
"""
import ast
import base64
import csv
import io
import json
import os
import re
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

import day_chart
import paper
import storage
import straddle
import strategy_lab as lab
import test_straddle as base
from paper import PaperEngine, Quote

BACKEND = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")
INDEX = lab.UNDERLYING.index_key
EXPIRY = "2026-09-22"
MONDAY = "2026-09-21"  # a market day
LOT = 65

# Anything that could place, change, cancel or convert a real order or position, or a GTT, or reach the broker SDK.
ORDER_API = re.compile(
    r"(?i)(place|modify|cancel|convert|square|exit)[_ ]?(multi[_ ]?)?(order|position)s?\b"
    r"|order[_ ]?api|order[_ ]?(book|management)|/v[0-9]+/order|order/(place|modify|cancel)|\bgtt\b|upstox_client|ordercontroller|multi[_ ]?order"
)
HTTP_WRITE = re.compile(r"""(?i)method\s*=\s*['"](POST|PUT|DELETE|PATCH)['"]|requests\.(post|put|delete|patch)|\.post\(""")


def local_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return {n for n in names if (BACKEND / f"{n}.py").exists()}


def reachable_modules(start: str = "paper") -> dict[str, Path]:
    seen: dict[str, Path] = {}
    queue = [start]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen[name] = BACKEND / f"{name}.py"
        queue.extend(local_imports(seen[name]))
    return seen


class PaperCanNeverPlaceAnOrder(unittest.TestCase):
    def test_paper_py_references_no_order_api(self):
        source = (BACKEND / "paper.py").read_text(encoding="utf-8")
        self.assertEqual(ORDER_API.findall(source), [], "paper.py must not reference any order/position/GTT endpoint or the broker SDK")
        self.assertEqual(HTTP_WRITE.findall(source), [])

    def test_every_module_paper_can_reach_is_free_of_order_code(self):
        """paper.py -> strategy_lab -> straddle -> historical/expired/instruments/http_util/storage/reliability: none may place an order."""
        modules = reachable_modules()
        self.assertIn("strategy_lab", modules)
        for name, path in modules.items():
            source = path.read_text(encoding="utf-8")
            self.assertEqual(ORDER_API.findall(source), [], f"{name}.py references an order API")
            self.assertEqual(HTTP_WRITE.findall(source), [], f"{name}.py makes a non-GET request")

    def test_paper_never_imports_the_stream_the_login_or_the_app(self):
        imported = set()
        for node in ast.walk(ast.parse((BACKEND / "paper.py").read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(imported.isdisjoint({"market_stream", "auto_login", "main", "upstox_client", "collector", "requests", "urllib", "http", "socket"}), imported)
        self.assertNotIn("upstox_client", reachable_modules())
        # and importing it does not drag the broker SDK (with its order classes) into the process
        import subprocess
        import sys

        code = "import sys, paper; print(any(m.startswith('upstox_client') for m in sys.modules))"
        out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_the_order_pattern_really_does_catch_order_code(self):
        for bad in ("client.place_order(x)", "OrderApi()", "api.cancel_order(1)", "/v3/order/place", "modify_order", "exit_positions()", "place_multi_order", "GTT"):
            self.assertTrue(ORDER_API.search(bad), bad)
        for fine in ("ORDER BY trading_date", "leg order: buys first", "border", "orders of magnitude"):
            self.assertFalse(ORDER_API.search(fine), fine)


class Fills(unittest.TestCase):
    NOW = datetime(2026, 9, 21, 9, 30, tzinfo=IST)

    def quote(self, bid=None, ask=None, ltp=None):
        return Quote("k", bid, ask, ltp, self.NOW, self.NOW)

    def test_sell_fills_at_the_bid_and_buy_at_the_ask_never_at_ltp(self):
        q = self.quote(bid=99.5, ask=100.5, ltp=100.0)
        sell, buy = paper.fill_for("SELL", q, self.NOW), paper.fill_for("BUY", q, self.NOW)
        self.assertEqual((sell["fill"], sell["source"]), (99.5, "bid"))
        self.assertEqual((buy["fill"], buy["source"]), (100.5, "ask"))
        self.assertEqual((sell["bid"], sell["ask"], sell["ltp"]), (99.5, 100.5, 100.0))  # all three are recorded
        self.assertNotEqual(sell["fill"], 100.0)

    def test_no_bid_or_ask_falls_back_to_ltp_minus_or_plus_20_paise_and_is_flagged(self):
        q = self.quote(ltp=100.0)
        self.assertEqual(paper.fill_for("SELL", q, self.NOW)["fill"], 99.8)
        self.assertEqual(paper.fill_for("BUY", q, self.NOW)["fill"], 100.2)
        self.assertEqual({paper.fill_for(s, q, self.NOW)["source"] for s in ("SELL", "BUY")}, {"estimated"})
        # only the side that is needed matters: a SELL with no bid is estimated even though an ask exists
        self.assertEqual(paper.fill_for("SELL", self.quote(ask=100.5, ltp=100.0), self.NOW)["source"], "estimated")
        self.assertEqual(paper.fill_for("SELL", self.quote(ltp=0.1), self.NOW)["fill"], 0.05)  # never below one tick

    def test_no_price_at_all_is_no_fill(self):
        self.assertIsNone(paper.fill_for("SELL", self.quote(), self.NOW))
        self.assertIsNone(paper.fill_for("BUY", None, self.NOW))

    def test_a_stored_full_mode_stream_message_parses_to_best_bid_and_ask(self):
        payload = {"fullFeed": {"marketFF": {"ltpc": {"ltp": 267.65, "ltt": "1789629298401"}, "marketLevel": {"bidAskQuote": [
            {"bidQ": "390", "bidP": 266.95, "askQ": "195", "askP": 267.65}, {"bidQ": "390", "bidP": 266.9, "askQ": "65", "askP": 267.7}]}}}}
        q = paper.parse_quote("NSE_FO|1", "2026-09-16T09:20:17.863797+05:30", payload)
        self.assertEqual((q.bid, q.ask, q.ltp), (266.95, 267.65, 267.65))
        index = paper.parse_quote("I", "2026-09-16T09:20:17+05:30", {"fullFeed": {"indexFF": {"ltpc": {"ltp": 23346.5, "ltt": "1789629301000"}}}})
        self.assertEqual((index.ltp, index.bid, index.ask), (23346.5, None, None))
        empty = paper.parse_quote("k", "2026-09-16T09:20:17+05:30", {"fullFeed": {"marketFF": {"ltpc": {"ltp": 10.0}, "marketLevel": {"bidAskQuote": [{"bidP": 0, "askP": 0}]}}}})
        self.assertEqual((empty.bid, empty.ask), (None, None))  # a zero price is no price


class SharedRules(unittest.TestCase):
    def test_wing_widths_and_legs_in_fill_order(self):
        self.assertEqual((lab.wing_width("iron_fly_w1", 231.4), lab.wing_width("iron_fly_w2", 231.4)), (250.0, 450.0))
        self.assertEqual(lab.wing_width("iron_fly_w1", 225.0), 250.0)  # halves round up
        self.assertEqual(lab.wing_width("iron_fly_w2", 210.0), 400.0)  # 2 x premium, then rounded (not 2 x the rounded premium)
        legs = lab.plan_legs("iron_fly_w1", 25000.0, 210.0)
        self.assertEqual([(l["side"], l["type"], l["strike"]) for l in legs],
                         [("BUY", "CE", 25200.0), ("BUY", "PE", 24800.0), ("SELL", "CE", 25000.0), ("SELL", "PE", 25000.0)])
        self.assertEqual([(l["side"], l["type"]) for l in lab.plan_legs("straddle_sell", 25000.0, 210.0)], [("SELL", "CE"), ("SELL", "PE")])

    def test_cost_model_worked_example(self):
        result = lab.settle([{"side": "SELL", "entry": 100.0, "exit": 60.0}, {"side": "SELL", "entry": 100.0, "exit": 90.0}], 65, 1)
        self.assertEqual((result["gross_pts"], result["charges_rs"], result["net_rs"]), (50.0, 117.12, 3132.88))
        iron = lab.settle([{"side": "SELL", "entry": 100.0, "exit": 60.0}, {"side": "BUY", "entry": 10.0, "exit": 20.0}], 65, 2)
        self.assertEqual(iron["gross_pts"], 50.0)
        self.assertAlmostEqual(iron["net_pts"] * 130, iron["net_rs"], 1)


def fake_jwt(expires_at: float) -> str:
    part = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none'})}.{part({'exp': expires_at})}.sig"


class FakeClock:
    def __init__(self, start: str):
        self.now = datetime.fromisoformat(start).replace(tzinfo=IST)

    def __call__(self):
        return self.now

    def to(self, hhmm: str, seconds: int = 0):
        h, m = map(int, hhmm.split(":"))
        self.now = self.now.replace(hour=h, minute=m, second=seconds, microsecond=0)


class FakeQuotes:
    """A live feed: quotes are stamped `now` unless frozen (feed stopped) or a key was never given a price."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.prices: dict[str, dict] = {}
        self.frozen_at: datetime | None = None
        self.calls: list[str] = []

    def set(self, key, bid=None, ask=None, ltp=None):
        self.prices[key] = {"bid": bid, "ask": ask, "ltp": ltp}

    def freeze(self):
        self.frozen_at = self.clock.now

    def thaw(self):
        self.frozen_at = None

    def quote(self, key):
        self.calls.append(key)
        p = self.prices.get(key)
        if p is None:
            return None
        stamp = self.frozen_at or self.clock.now
        return Quote(key, p["bid"], p["ask"], p["ltp"], stamp, stamp)

    def spot(self):
        return self.quote(INDEX)


def ck(strike, leg):
    return f"NSE_FO|{leg[0]}{int(strike)}"


class EngineCase(base.TempDatabase):
    def setUp(self):
        super().setUp()
        paper._wanted.clear()
        contracts = []
        for strike in range(24000, 26050, 50):
            for leg in ("CE", "PE"):
                item = {"instrument_key": ck(strike, leg), "expiry": EXPIRY, "instrument_type": leg, "strike_price": float(strike),
                        "underlying_key": INDEX, "trading_symbol": f"NIFTY {strike} {leg}", "lot_size": LOT}
                contracts.append(item)
        storage.save_instrument_contracts(contracts)
        self.clock = FakeClock(f"{MONDAY}T09:19:55")
        self.quotes = FakeQuotes(self.clock)
        self.reconciled: list[str] = []
        holiday_patch = mock.patch("paper.is_market_day", lambda d: d.weekday() < 5)
        holiday_patch.start()
        self.addCleanup(holiday_patch.stop)

    def engine(self, **kw):
        defaults = dict(clock=self.clock, token_provider=lambda: "opaque-token", expiry_for=lambda day: EXPIRY, ensure_contracts=lambda e: None,
                        reconcile=self.fake_reconcile, lots=1)
        return PaperEngine(self.quotes, **{**defaults, **kw})

    def fake_reconcile(self, trade):
        self.reconciled.append(f"{trade['strategy']}|{trade['trading_date']}")
        return {"bt_gross_pts": 100.0, "bt_charges_pts": 2.0, "bt_net_pts": 98.0, "bt_net_rs": 6370.0, "bt_legs": trade["legs"], "bt_note": None}

    def market(self, spot=25012.0, atm=25000, half_spread=0.5):
        """A quiet book: every strike ATM +/- 700 quoted with a bid below and an ask above its LTP."""
        self.quotes.set(INDEX, ltp=spot)
        for strike in range(24300, 25750, 50):
            for leg in ("CE", "PE"):
                intrinsic = max(0.0, (atm - strike) if leg == "CE" else (strike - atm))
                ltp = round(intrinsic + max(3.0, 105.0 - abs(strike - atm) * 0.3), 2)
                self.quotes.set(ck(strike, leg), bid=round(ltp - half_spread, 2), ask=round(ltp + half_spread, 2), ltp=ltp)

    def advance(self, engine, until: str, step=None):
        """Tick the engine until `until`: every 5 s across short spans (the entry window), every minute across long ones."""
        h, m = map(int, until.split(":"))
        end = self.clock.now.replace(hour=h, minute=m, second=0)
        step = step or (5 if end - self.clock.now <= timedelta(minutes=15) else 60)
        while self.clock.now < end:
            engine.tick()
            self.clock.now += timedelta(seconds=step)

    def trades(self):
        return {t["strategy"]: t for t in storage.query_paper_trades(None, MONDAY, MONDAY)}


class TheTradingDay(EngineCase):
    def test_open_positions_are_subscribed_again_after_a_restart(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:31")
        held = {leg["instrument_key"] for t in self.trades().values() if t["status"] == "open" for leg in t["legs"]}
        self.assertTrue(held)
        paper._wanted.clear()  # a backend restart empties the in-memory wanted list
        self.engine().tick()
        self.assertLessEqual(held, set(paper.wanted_stream_instruments()))

    def test_a_full_day_health_presubscribe_entry_fills_marks_exit_and_reconcile(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:25")
        self.assertEqual(storage.paper_get_health(MONDAY)["ok"], 1)
        self.assertEqual(self.trades(), {}, "nothing is entered before 09:30")
        self.assertEqual(paper.wanted_stream_instruments(), [], "not before 09:25")

        self.advance(engine, "09:29")
        wanted = set(paper.wanted_stream_instruments())
        self.assertEqual(wanted, {ck(k, leg) for k in range(24500, 25550, 50) for leg in ("CE", "PE")}, "ATM 25000 +/- 10 strikes, both sides")
        self.assertEqual(self.trades(), {})

        self.clock.to("09:30")
        engine.tick()
        trades = self.trades()
        self.assertEqual(set(trades), set(lab.STRATEGIES) | {lab.S5})
        s1, w1, w2 = trades["straddle_sell"], trades["iron_fly_w1"], trades["iron_fly_w2"]
        self.assertTrue(all(t["status"] == "open" for t in trades.values()))
        self.assertEqual((s1["atm_strike"], s1["expiry"], s1["lots"], s1["lot_size"], s1["spot_at_entry"]), (25000.0, EXPIRY, 1, LOT, 25012.0))
        premium = s1["premium_ref"]
        self.assertEqual(premium, 210.0)  # ATM CE 105 + PE 105 at LTP
        self.assertEqual((w1["wing_width"], w2["wing_width"]), (200.0, 400.0))  # 210 -> 200 ; 2 x 210 = 420 -> 400

        # legs, in fill order, from the shared plan
        for trade in (s1, w1, w2):
            plan = lab.plan_legs(trade["strategy"], trade["atm_strike"], trade["premium_ref"])
            self.assertEqual([(l["side"], l["type"], l["strike"]) for l in trade["legs"]], [(p["side"], p["type"], p["strike"]) for p in plan])
        self.assertEqual([l["side"] for l in w1["legs"]], ["BUY", "BUY", "SELL", "SELL"], "wings (buys) first, then the sells")

        # fills: SELL at the bid, BUY at the ask - never LTP - with everything recorded
        for trade in (s1, w1, w2):
            for leg in trade["legs"]:
                e = leg["entry"]
                self.assertEqual(e["fill"], e["bid"] if leg["side"] == "SELL" else e["ask"])
                self.assertEqual(e["source"], "bid" if leg["side"] == "SELL" else "ask")
                self.assertNotEqual(e["fill"], e["ltp"])
                self.assertTrue(e["ts"].startswith(f"{MONDAY}T09:30"))
        atm_ce = [l for l in s1["legs"] if l["type"] == "CE"][0]
        self.assertEqual((atm_ce["entry"]["bid"], atm_ce["entry"]["ask"], atm_ce["entry"]["fill"]), (104.5, 105.5, 104.5))

        # a second tick in the same minute does not enter again
        engine.tick()
        self.assertEqual(len(storage.query_paper_trades(None, MONDAY, MONDAY)), 4)

        # marks: one per minute; a short is marked at the ask. CE ask 90, PE ask 110 -> (104.5-90)+(104.5-110) = 9.0 pts
        self.advance(engine, "10:00")
        self.quotes.set(ck(25000, "CE"), bid=89.0, ask=90.0, ltp=89.5)
        self.quotes.set(ck(25000, "PE"), bid=109.0, ask=110.0, ltp=109.5)
        engine.tick()  # 10:00:00
        marks = {m["minute"]: m["mtm_rs"] for m in storage.query_paper_marks(MONDAY, "straddle_sell")}
        self.assertEqual(marks["10:00"], round(9.0 * LOT, 2))
        self.assertEqual(sorted(m for m in marks if m <= "10:00"), [f"{h:02d}:{m:02d}" for h, m in [(9, x) for x in range(30, 60)] + [(10, 0)]], "one mark every minute from 09:30")

        # exit starts at 15:29:50 (the same moment the candle backtest prices its own 15:29 close): shorts bought
        # back at the ask FIRST, then the wings sold at the bid
        self.quotes.set(ck(25000, "CE"), bid=59.0, ask=60.0, ltp=59.5)
        self.quotes.set(ck(25000, "PE"), bid=39.0, ask=40.0, ltp=39.5)
        self.advance(engine, "15:29")
        self.assertTrue(all(t["status"] == "open" for t in self.trades().values()))
        self.clock.to("15:29", 50)
        self.quotes.calls.clear()
        engine.tick()
        done = self.trades()
        s1 = done["straddle_sell"]
        self.assertEqual(s1["status"], "closed")
        self.assertEqual(s1["gross_pts"], (104.5 - 60.0) + (104.5 - 40.0))  # sold at bids, bought back at asks
        expected = lab.settle([{"side": "SELL", "entry": 104.5, "exit": 60.0}, {"side": "SELL", "entry": 104.5, "exit": 40.0}], LOT, 1)
        self.assertEqual((s1["net_pts"], s1["net_rs"], s1["charges_pts"]), (expected["net_pts"], expected["net_rs"], expected["charges_pts"]))
        for leg in s1["legs"]:
            self.assertEqual(leg["exit"]["source"], "ask")
        w1 = done["iron_fly_w1"]
        wing_exit = [l["exit"] for l in w1["legs"] if l["role"] == "wing"]
        self.assertTrue(all(x["fill"] == x["bid"] and x["source"] == "bid" for x in wing_exit))  # a bought wing is sold at the bid
        self.assertEqual(done[lab.S5]["status"], "open", "S5 does not exit today - it closes tomorrow at 09:30")
        # the tail of the quote reads is the exit itself (the tick marks first); the engine walks the trades in strategy-name order
        # (S5 never exits today, so it is left out here - it reads no quotes at all in this tick's exit phase)
        ordered = [done[name] for name in sorted(done) if name != lab.S5]
        exit_reads = self.quotes.calls[-sum(len(t["legs"]) for t in ordered):]
        at = 0
        for trade in ordered:
            chunk, at = exit_reads[at:at + len(trade["legs"])], at + len(trade["legs"])
            body_keys = {l["instrument_key"] for l in trade["legs"] if l["role"] == "body"}
            self.assertEqual([k in body_keys for k in chunk], [True] * len(body_keys) + [False] * (len(chunk) - len(body_keys)),
                             f"{trade['strategy']}: shorts are closed before the wings")

        # after the close: reconciled once (S5 never reconciles - see test_s5_is_never_reconciled_against_a_candle_backtest)
        self.clock.to("15:50")
        engine.tick()
        self.assertEqual(sorted(self.reconciled), sorted(f"{s}|{MONDAY}" for s in lab.STRATEGIES))
        engine.tick()
        self.assertEqual(len(self.reconciled), 3, "reconciled exactly once")
        self.assertEqual(self.trades()["straddle_sell"]["bt_net_rs"], 6370.0)

    def test_a_missing_bid_at_entry_falls_back_to_estimated_and_is_logged(self):
        self.market()
        self.quotes.set(ck(25000, "CE"), bid=None, ask=105.5, ltp=105.0)  # no bid: a SELL entry cannot use the book
        engine = self.engine()
        self.advance(engine, "09:30")
        self.clock.to("09:30")
        engine.tick()
        s1 = self.trades()["straddle_sell"]
        ce_leg = next(l for l in s1["legs"] if l["type"] == "CE")
        self.assertEqual(ce_leg["entry"]["source"], "estimated")
        self.assertEqual(ce_leg["entry"]["fill"], round(105.0 - paper.ESTIMATE_OFFSET, 2))
        match = next(a for a in storage.query_paper_alerts() if "entry fill for body CE 25000" in a["message"])
        self.assertEqual(match["level"], "info")
        self.assertIn("no bid in the book", match["message"])

    def test_the_strike_comes_from_the_shared_atm_rule(self):
        self.market(spot=24975.0)  # exactly halfway between 24950 and 25000: rounds up, like the backtest
        self.market(spot=24975.0, atm=25000)
        engine = self.engine()
        self.advance(engine, "09:30")
        self.clock.to("09:30")
        engine.tick()
        self.assertEqual(self.trades()["straddle_sell"]["atm_strike"], 25000.0)

    def test_two_lots_scale_the_result(self):
        self.market()
        engine = self.engine(lots=2)
        self.advance(engine, "09:30")
        engine.tick()
        self.quotes.set(ck(25000, "CE"), bid=59.0, ask=60.0, ltp=59.5)
        self.quotes.set(ck(25000, "PE"), bid=39.0, ask=40.0, ltp=39.5)
        self.advance(engine, "15:39")
        self.clock.to("15:39")
        engine.tick()
        s1 = self.trades()["straddle_sell"]
        self.assertEqual((s1["lots"], s1["status"]), (2, "closed"))
        self.assertEqual(s1["net_rs"], lab.settle([{"side": "SELL", "entry": 104.5, "exit": 60.0}, {"side": "SELL", "entry": 104.5, "exit": 40.0}], LOT, 2)["net_rs"])

    def test_no_bid_ask_gives_an_estimated_fill_that_is_flagged(self):
        self.market()
        for leg in ("CE", "PE"):
            self.quotes.set(ck(25000, leg), ltp=100.0)  # depth missing on the ATM legs
        engine = self.engine()
        self.advance(engine, "09:30")
        engine.tick()
        s1 = self.trades()["straddle_sell"]
        self.assertEqual([(l["entry"]["fill"], l["entry"]["source"]) for l in s1["legs"]], [(99.8, "estimated"), (99.8, "estimated")])
        self.assertEqual([(l["entry"]["bid"], l["entry"]["ask"], l["entry"]["ltp"]) for l in s1["legs"]][0], (None, None, 100.0))


class NeverSilent(EngineCase):
    def test_a_health_check_still_failing_at_0930_is_an_alert_and_a_missed_entry_for_every_strategy(self):
        engine = self.engine()  # no quotes at all: the feed is not live
        self.advance(engine, "09:22")
        self.assertIsNone(storage.paper_get_health(MONDAY), "not final before 09:30 - re-checked every tick")
        self.assertEqual(self.trades(), {})
        self.assertEqual(storage.query_paper_alerts()[0]["level"], "warn")
        self.advance(engine, "09:31")
        health = storage.paper_get_health(MONDAY)
        self.assertEqual(health["ok"], 0)
        self.assertIn("no NIFTY spot quote", health["detail"])
        trades = self.trades()
        self.assertEqual({t["status"] for t in trades.values()}, {"missed_entry"})
        self.assertEqual(set(trades) - {lab.S5}, set(lab.STRATEGIES), "S5 follows S1's miss once its own 09:30 tick runs")
        self.assertIn("health check failed", trades["iron_fly_w2"]["notes"])
        self.assertTrue(any(a["level"] == "error" and "health check failed" in a["message"] for a in storage.query_paper_alerts()))
        # the feed coming back after 09:30 does not rescue the day
        self.market()
        self.advance(engine, "09:35")
        self.assertEqual({t["status"] for t in self.trades().values()}, {"missed_entry"})

    def test_a_feed_that_comes_back_before_0930_rescues_the_day(self):
        engine = self.engine()  # no quotes at 09:20
        self.advance(engine, "09:24")
        self.market()  # e.g. a late login at 09:24
        self.advance(engine, "09:31")
        self.assertEqual(storage.paper_get_health(MONDAY)["ok"], 1)
        self.assertEqual(self.trades()["straddle_sell"]["status"], "open")

    def test_an_expired_token_is_a_missed_entry_with_the_reason(self):
        self.market()
        engine = self.engine(token_provider=lambda: fake_jwt(time.time() - 3600))
        self.advance(engine, "09:31")
        self.assertIn("token has expired", storage.paper_get_health(MONDAY)["detail"])
        self.assertEqual({t["status"] for t in self.trades().values()}, {"missed_entry"})

    def test_a_stale_feed_fails_the_health_check(self):
        self.market()
        self.quotes.freeze()
        self.clock.now += timedelta(minutes=10)  # the feed stopped ten minutes ago
        engine = self.engine()
        self.clock.to("09:30")
        self.quotes.frozen_at = self.clock.now - timedelta(minutes=5)
        engine.tick()
        self.assertIn("stale", storage.paper_get_health(MONDAY)["detail"])

    def test_an_engine_that_starts_after_the_entry_window_logs_the_day_as_missed(self):
        self.market()
        self.clock.to("09:45")
        engine = self.engine()
        engine.tick()
        trades = self.trades()
        self.assertEqual({t["status"] for t in trades.values()}, {"missed_entry"})
        self.assertIn("not running at 09:30", trades["straddle_sell"]["notes"])

    def test_a_wing_with_no_quote_is_waited_for_then_reported_never_dropped(self):
        self.market()
        for k in (25200, 24800):
            for leg in ("CE", "PE"):
                self.quotes.prices.pop(ck(k, leg), None)  # the wings are not quoted yet
        engine = self.engine()
        self.advance(engine, "09:30")
        engine.tick()
        trades = self.trades()
        self.assertIn("straddle_sell", trades)
        self.assertNotIn("iron_fly_w1", trades, "no partial iron fly is ever entered")
        self.assertIn(ck(25200, "CE"), paper.wanted_stream_instruments(), "the missing wing is requested from the stream")
        # its quote arrives 20 seconds late - still inside the 60 second entry window
        self.clock.now += timedelta(seconds=20)
        self.quotes.set(ck(25200, "CE"), bid=10.0, ask=11.0, ltp=10.5)
        self.quotes.set(ck(24800, "PE"), bid=10.0, ask=11.0, ltp=10.5)
        engine.tick()
        w1 = self.trades()["iron_fly_w1"]
        self.assertEqual(w1["status"], "open")
        self.assertTrue(w1["legs"][0]["entry"]["ts"].startswith(f"{MONDAY}T09:30:2"))

    def test_a_missing_quote_past_the_window_is_a_missed_entry_with_the_blocker(self):
        self.market()
        self.quotes.prices.pop(ck(25200, "CE"), None)
        engine = self.engine()
        self.advance(engine, "09:32")
        w1 = self.trades()["iron_fly_w1"]
        self.assertEqual(w1["status"], "missed_entry")
        self.assertIn("no fresh quote for CE 25200", w1["notes"])
        self.assertEqual(self.trades()["straddle_sell"]["status"], "open", "the strategies that could enter did")

    def test_no_trading_at_the_weekend(self):
        self.clock = FakeClock("2026-09-19T09:19:55")  # Saturday
        self.quotes.clock = self.clock
        self.market()
        engine = self.engine(clock=self.clock)
        self.advance(engine, "09:40")
        self.assertEqual(storage.query_paper_trades(), [])
        self.assertIsNone(storage.paper_get_health("2026-09-19"))


class RestartsAndExits(EngineCase):
    def test_a_restart_mid_day_resumes_the_open_position_and_never_enters_twice(self):
        self.market()
        first = self.engine()
        self.advance(first, "09:31")
        before = self.trades()
        self.assertEqual({t["status"] for t in before.values()}, {"open"})
        marks_before = len(storage.query_paper_marks(MONDAY))

        # the backend restarts at 11:00: a brand-new engine, empty memory, same database
        self.clock.to("11:00")
        second = self.engine()
        second.tick()
        self.assertEqual(len(storage.query_paper_trades(None, MONDAY, MONDAY)), 4, "no second entry")
        self.assertEqual({(t["strategy"], t["legs"][0]["entry"]["ts"]) for t in self.trades().values()}, {(s, before[s]["legs"][0]["entry"]["ts"]) for s in before}, "the original fills are untouched")
        self.assertGreater(len(storage.query_paper_marks(MONDAY)), marks_before, "marking resumed")

        # ... and the new engine closes the position the old one opened (S5 stays open - it closes tomorrow at 09:30)
        self.quotes.set(ck(25000, "CE"), bid=59.0, ask=60.0, ltp=59.5)
        self.quotes.set(ck(25000, "PE"), bid=39.0, ask=40.0, ltp=39.5)
        self.advance(second, "15:39")
        self.clock.to("15:39")
        second.tick()
        done = self.trades()
        self.assertEqual({t["status"] for name, t in done.items() if name != lab.S5}, {"closed"})
        self.assertEqual(done[lab.S5]["status"], "open")

    def test_a_restart_after_a_missed_entry_does_not_enter_late(self):
        engine = self.engine()
        self.advance(engine, "09:22")
        self.market()
        self.clock.to("09:40")
        self.engine().tick()
        self.assertEqual({t["status"] for t in self.trades().values()}, {"missed_entry"})

    def test_an_exit_with_no_usable_quote_at_the_attempt_start_waits_then_is_flagged_missed_exit_with_the_delay(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:31")
        self.advance(engine, "15:29")
        self.quotes.freeze()  # the feed stops just before the exit attempt starts
        self.clock.to("15:29", 50)
        engine.tick()
        self.assertTrue(all(t["status"] == "open" for t in self.trades().values()), "no exit on a stale quote before the 11-minute or hard-deadline windows open")
        self.clock.to("15:31", 50)  # 2 minutes later - well short of either escalation window, still just retrying
        self.quotes.thaw()
        self.quotes.set(ck(25000, "CE"), bid=59.0, ask=60.0, ltp=59.5)
        self.quotes.set(ck(25000, "PE"), bid=39.0, ask=40.0, ltp=39.5)
        engine.tick()
        s1 = self.trades()["straddle_sell"]
        self.assertEqual(s1["status"], "missed_exit")
        self.assertEqual(s1["exit_delay_s"], 120.0)
        self.assertIn("120s late", s1["notes"])
        self.assertEqual(s1["gross_pts"], (104.5 - 60.0) + (104.5 - 40.0), "the next available quote is used")
        self.assertTrue(any("late" in a["message"] for a in storage.query_paper_alerts()))
        self.assertIsNone(s1["invalid_reason"], "exactly 120s late is not yet OVER the invalid threshold")

    def test_a_feed_stall_through_the_whole_exit_window_forces_a_stale_close_at_market_close_marked_invalid(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:31")
        self.advance(engine, "15:20")
        self.quotes.freeze()  # the feed stops well before the exit attempt starts, and never recovers
        self.advance(engine, "15:38")
        self.assertTrue(all(t["status"] == "open" for t in self.trades().values()), "still retrying with only a stale quote available")
        self.clock.to("15:39")
        engine.tick()
        s1 = self.trades()["straddle_sell"]
        self.assertEqual(s1["status"], "missed_exit")
        self.assertTrue(all(l["exit"]["source"].endswith("_stale") for l in s1["legs"]))
        self.assertIsNotNone(s1["invalid_reason"], "market close forced a close on a ~9-minute-old quote - cannot represent EXIT_ATTEMPT_START")
        self.assertTrue(any("invalid" in a["message"] for a in storage.query_paper_alerts()))

    def test_no_quote_at_all_by_market_close_is_an_error_never_a_made_up_price(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:31")
        self.advance(engine, "15:20")
        self.quotes.prices.clear()
        self.advance(engine, "15:38")
        self.assertTrue(all(t["status"] == "open" for t in self.trades().values()))
        self.clock.to("15:39")
        engine.tick()
        s1 = self.trades()["straddle_sell"]
        self.assertEqual(s1["status"], "error")
        self.assertIsNone(s1["net_rs"])
        self.assertIn("no exit quote", s1["notes"])
        self.assertIsNotNone(s1["invalid_reason"])

    def test_a_position_left_open_overnight_is_closed_on_the_last_quote_or_reported(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:31")
        self.advance(engine, "12:00")  # the backend is now down for the rest of the day
        self.clock.now = datetime(2026, 9, 22, 9, 0, tzinfo=IST)
        self.quotes.prices.clear()
        self.engine().tick()
        trades = {t["strategy"]: t for t in storage.query_paper_trades(None, MONDAY, MONDAY)}
        self.assertEqual({s: t["status"] for s, t in trades.items() if s != lab.S5}, {s: "error" for s in lab.STRATEGIES})
        self.assertEqual(trades[lab.S5]["status"], "open", "S5 is not stale yet at 09:00 - its own 09:30 exit has not come around")
        self.assertTrue(any("could not be closed" in a["message"] for a in storage.query_paper_alerts()))

    def test_a_failed_reconcile_is_recorded_and_retried_later(self):
        self.market()
        attempts = []

        def flaky(trade):
            attempts.append(self.clock.now)
            if len(attempts) <= 3:
                raise RuntimeError("candles not available yet")
            return self.fake_reconcile(trade)

        engine = self.engine(reconcile=flaky)
        self.advance(engine, "09:31")
        self.quotes.set(ck(25000, "CE"), bid=59.0, ask=60.0, ltp=59.5)
        self.advance(engine, "15:39")
        self.clock.to("15:51")
        engine.tick()
        t = self.trades()["straddle_sell"]
        self.assertIsNone(t["reconciled_at"])
        self.assertIn("reconcile failed: RuntimeError", t["bt_note"])
        engine.tick()
        self.assertEqual(len(attempts), 3, "not retried within ten minutes")
        self.clock.now += timedelta(minutes=11)
        engine.tick()
        self.assertIsNotNone(self.trades()["straddle_sell"]["reconciled_at"])


class MarketHolidays(base.TempDatabase):
    """The trading calendar comes from Upstox's published holiday list (fetched once a day and saved), so a holiday
    such as 2 Oct 2026 is never treated as a trading day."""

    LIST = {"status": "success", "data": [
        {"date": "2026-10-02", "holiday_type": "TRADING_HOLIDAY", "closed_exchanges": ["NSE", "NFO", "BSE"]},
        {"date": "2026-11-08", "holiday_type": "SETTLEMENT_HOLIDAY", "closed_exchanges": []},
    ]}

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"MARKET_HOLIDAYS_FETCH": "true"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_published_trading_holiday_is_not_a_market_day(self):
        import http_util
        import reliability

        with mock.patch.object(http_util, "request_json", return_value=self.LIST) as fetch:
            self.assertFalse(reliability.is_market_day(date(2026, 10, 2)))
            self.assertTrue(reliability.is_market_day(date(2026, 10, 1)))
            self.assertTrue(reliability.is_market_day(date(2026, 11, 9)), "a settlement-only holiday still trades")
            self.assertEqual(fetch.call_count, 1, "fetched once a day, not on every call")

    def test_a_failed_fetch_falls_back_to_the_saved_list(self):
        import http_util
        import reliability

        with mock.patch.object(http_util, "request_json", return_value=self.LIST):
            reliability.is_market_day(date(2026, 10, 1))  # saves the list to disk
        reliability._holidays.update(fetched_on=None, days=None, retry_at=0.0)  # a restart
        with mock.patch.object(http_util, "request_json", side_effect=OSError("offline")):
            self.assertFalse(reliability.is_market_day(date(2026, 10, 2)))


class ManualExit(EngineCase):
    """PaperEngine.exit_now(): a person clicking "Exit now" on Today - closes at CURRENT quotes and flags the trade,
    since it is not the strategy's own exit."""

    def test_an_open_position_can_be_exited_now_and_is_flagged(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:31")
        self.clock.to("11:00")
        self.assertIsNone(engine.exit_now("straddle_sell", MONDAY, self.clock.now))
        s1 = self.trades()["straddle_sell"]
        self.assertEqual(s1["status"], "closed")
        self.assertTrue(all(leg["exit"]["ts"].startswith(f"{MONDAY}T11:00") for leg in s1["legs"]))
        self.assertIn("exited manually", s1["invalid_reason"])
        self.assertIsNotNone(s1["net_rs"])

    def test_only_an_open_position_can_be_exited(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:31")
        self.clock.to("11:00")
        self.assertIsNone(engine.exit_now("straddle_sell", MONDAY, self.clock.now))
        self.assertEqual(engine.exit_now("straddle_sell", MONDAY, self.clock.now), "only an open position can be exited")

    def test_no_manual_exit_while_the_market_is_closed(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:31")
        self.clock.to("16:00")
        self.assertIn("market is closed", engine.exit_now(lab.S5, MONDAY, self.clock.now))

    def test_the_overnight_s5_position_can_be_exited_the_next_morning(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:31")
        self.clock.now = datetime(2026, 9, 22, 9, 20, tzinfo=IST)
        self.market()  # fresh quotes the next morning
        self.assertIsNone(engine.exit_now(lab.S5, MONDAY, self.clock.now))
        s5 = storage.query_paper_trades(lab.S5, MONDAY, MONDAY)[0]
        self.assertEqual(s5["status"], "closed")
        self.assertIn("exited manually at 09:20:00", s5["invalid_reason"])


class OvernightS5MarkedUntilItsExit(EngineCase):
    """An S5 position held overnight is marked through the next session's morning (09:15 open to its 09:30 exit) - the
    user saw its MTM stop at the previous close (2026-10-08). Those marks are filed under its own entry day with the
    minute past 24:00, so they follow its own session and never mix into the next day's own S5 line."""

    TUESDAY = "2026-09-22"

    def held_overnight(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:31")
        self.assertEqual(storage.query_paper_trades(lab.S5, MONDAY, MONDAY)[0]["status"], "open")
        self.clock.now = datetime(2026, 9, 22, 9, 10, tzinfo=IST)
        return engine

    def test_the_morning_is_marked_under_the_entry_day_until_the_0930_exit(self):
        engine = self.held_overnight()
        self.market(spot=25100.0, atm=25100)  # the market moved overnight: the held straddle is marked at its new prices
        self.advance(engine, "09:31", step=20)
        mornings = [m["minute"] for m in paper.marks_for(MONDAY, lab.S5) if m["minute"] >= "24:00"]
        self.assertEqual(mornings[0], "33:15", "nothing before the 09:15 open")
        self.assertEqual(mornings[-1], "33:30", "marked up to the exit, then no more")
        self.assertEqual(len(mornings), 16)
        self.assertEqual(storage.query_paper_trades(lab.S5, MONDAY, MONDAY)[0]["status"], "closed")
        own = [m["minute"] for m in paper.marks_for(self.TUESDAY)]
        self.assertTrue(own and all("09:30" <= minute < "24:00" for minute in own), "the new session's own lines start at its 09:30 entries")

    def test_the_today_screen_shows_the_held_position_s_morning_on_every_poll(self):
        engine = self.held_overnight()
        self.market()
        self.advance(engine, "09:22", step=30)
        recorded = lab.paper_trades_for_today(self.TUESDAY)
        session, earlier = paper.today_marks(self.TUESDAY, recorded)
        self.assertEqual(session, [], "nothing of today's own yet")
        self.assertEqual([m["minute"] for m in earlier if m["minute"] >= "24:00"], [f"33:{minute}" for minute in range(15, 22)])

    def test_nothing_is_marked_without_an_overnight_position(self):
        engine = self.engine()
        self.market()
        self.clock.to("09:16")
        engine.tick()
        self.assertEqual(paper.marks_for(MONDAY), [])


class RetryMissedEntry(EngineCase):
    """PaperEngine.retry_missed_entry(): a person clicking "retry" on Today after the automatic 09:30 entry was
    missed (a health-check failure, a feed gap...) - fills at CURRENT quotes, writes into the SAME row (never a
    second one), and always marks the result invalid_reason since it cannot represent a normal 09:30 entry."""

    def fail_health_check(self, engine):
        """No market() at all: the 09:20 health check finds no NIFTY spot quote and marks every strategy missed,
        cascading to S5 too once its own 09:30+grace tick runs - exactly the real incident this feature is for."""
        self.advance(engine, "09:31")
        self.clock.to("09:31")
        engine.tick()

    def test_a_missed_entry_can_be_manually_retried_once_quotes_return(self):
        engine = self.engine()
        self.fail_health_check(engine)
        self.assertEqual(self.trades()["straddle_sell"]["status"], "missed_entry")

        self.market()  # the feed recovers
        self.clock.to("10:15")
        reason = engine.retry_missed_entry("straddle_sell", MONDAY, self.clock.now)
        self.assertIsNone(reason)
        s1 = self.trades()["straddle_sell"]
        self.assertEqual(s1["status"], "open")
        self.assertIn("entered manually", s1["invalid_reason"])
        self.assertEqual(len(s1["legs"]), 2)
        self.assertTrue(all(leg["entry"]["ts"].startswith(f"{MONDAY}T10:15") for leg in s1["legs"]))
        self.assertEqual(len(storage.query_paper_trades(None, MONDAY, MONDAY)), 4, "still one row per strategy - updated in place, not duplicated")

    def test_a_manually_recovered_trade_is_still_flagged_after_it_exits(self):
        engine = self.engine()
        self.fail_health_check(engine)
        self.market()
        self.clock.to("10:15")
        self.assertIsNone(engine.retry_missed_entry("straddle_sell", MONDAY, self.clock.now))
        self.advance(engine, "15:31")
        s1 = self.trades()["straddle_sell"]
        self.assertIn(s1["status"], ("closed", "missed_exit"))
        self.assertIn("entered manually", s1["invalid_reason"] or "", "the exit must not clear the entry's own flag")

    def test_s5_retry_reuses_straddle_sells_own_recovered_fill(self):
        engine = self.engine()
        self.fail_health_check(engine)
        self.market()
        self.clock.to("10:15")
        self.assertIsNone(engine.retry_missed_entry("straddle_sell", MONDAY, self.clock.now))
        self.clock.to("10:16")
        reason = engine.retry_missed_entry(lab.S5, MONDAY, self.clock.now)
        self.assertIsNone(reason)
        s1, s5 = self.trades()["straddle_sell"], self.trades()[lab.S5]
        self.assertEqual(s5["status"], "open")
        self.assertIn("entered manually", s5["invalid_reason"])
        self.assertEqual({l["instrument_key"] for l in s5["legs"]}, {l["instrument_key"] for l in s1["legs"]}, "the same contracts straddle_sell just recovered, not a fresh resolution")
        self.assertEqual([l["entry"]["fill"] for l in s5["legs"]], [l["entry"]["fill"] for l in s1["legs"]])

    def test_s5_retry_before_straddle_sell_is_recovered_is_refused(self):
        engine = self.engine()
        self.fail_health_check(engine)
        self.market()
        reason = engine.retry_missed_entry(lab.S5, MONDAY, self.clock.now)
        self.assertIn("straddle_sell has no open position", reason)
        self.assertEqual(self.trades()[lab.S5]["status"], "missed_entry")

    def test_retrying_an_already_entered_or_closed_trade_is_refused(self):
        engine = self.engine()
        self.market()
        self.advance(engine, "09:30")
        engine.tick()
        self.assertEqual(self.trades()["straddle_sell"]["status"], "open")
        reason = engine.retry_missed_entry("straddle_sell", MONDAY, self.clock.now)
        self.assertIn("already open", reason)

    def test_retrying_with_no_quote_yet_returns_the_same_blocker_the_automatic_entry_would(self):
        engine = self.engine()
        self.fail_health_check(engine)
        # the feed is STILL down - no market() call at all
        reason = engine.retry_missed_entry("straddle_sell", MONDAY, self.clock.now)
        self.assertIn("no NIFTY spot quote", reason)
        self.assertEqual(self.trades()["straddle_sell"]["status"], "missed_entry")

    def test_retrying_too_late_in_the_day_is_refused(self):
        engine = self.engine()
        self.fail_health_check(engine)
        self.market()
        self.clock.to("15:39")
        reason = engine.retry_missed_entry("straddle_sell", MONDAY, self.clock.now)
        self.assertIn("too late", reason)


class LiveMarkToMarket(EngineCase):
    def marks(self, strategy="straddle_sell"):
        return {m["minute"]: m["mtm_rs"] for m in storage.query_paper_marks(MONDAY, strategy)}

    def test_the_current_minutes_mark_follows_every_tick_not_just_the_first(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "10:00")
        self.quotes.set(ck(25000, "CE"), bid=89.0, ask=90.0, ltp=89.5)
        self.quotes.set(ck(25000, "PE"), bid=109.0, ask=110.0, ltp=109.5)
        engine.tick()  # 10:00:00
        self.assertEqual(self.marks()["10:00"], round(9.0 * LOT, 2))
        self.quotes.set(ck(25000, "CE"), bid=79.0, ask=80.0, ltp=79.5)  # the price moves 20 s later, inside the same minute
        self.clock.to("10:00", 20)
        engine.tick()
        self.assertEqual(self.marks()["10:00"], round((104.5 - 80.0 + 104.5 - 110.0) * LOT, 2), "the mark shows the newest quote")
        self.assertEqual(len([m for m in self.marks() if m.startswith("10:0")]), 1, "still one row per minute")

    def test_a_stalled_feed_pauses_the_marks_says_so_and_recovers(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "10:00")
        engine.tick()
        self.assertFalse(engine.feed_stalled)
        self.quotes.freeze()  # the live feed stops
        for _ in range(60):  # two minutes of 2-second ticks with no data
            self.clock.now += timedelta(seconds=2)
            engine.tick()
        self.assertTrue(engine.feed_stalled)
        self.assertGreater(engine.feed_age_seconds, paper.FEED_STALL_SECONDS)
        self.assertNotIn("10:02", self.marks(), "no mark is written once the newest price is older than the stall limit")
        self.assertNotIn("10:03", self.marks())
        self.assertTrue(any("live feed stalled" in a["message"] for a in storage.query_paper_alerts()), "the stall is reported, not silent")
        self.assertEqual(paper.status_snapshot(engine)["feed_stalled"], True)
        self.quotes.thaw()
        engine.tick()
        self.assertFalse(engine.feed_stalled)
        self.assertIn(self.clock.now.strftime("%H:%M"), self.marks(), "marking resumes the moment the feed is back")
        self.assertEqual(paper.status_snapshot(engine)["feed_stalled"], False)


class FeedHeartbeat(EngineCase):
    def test_a_quiet_nifty_is_not_a_stall_while_the_stream_still_delivers(self):
        self.market()
        beat = {"at": None}
        engine = self.engine(heartbeat=lambda: beat["at"])
        self.advance(engine, "10:00")
        self.quotes.freeze()  # NIFTY itself goes quiet ...
        for _ in range(30):
            self.clock.now += timedelta(seconds=2)
            beat["at"] = self.clock.now  # ... but the stream keeps delivering other instruments
            engine.tick()
        self.assertFalse(engine.feed_stalled)
        self.assertLess(engine.feed_age_seconds, 5)
        for _ in range(20):  # now the stream itself goes silent for 40 s
            self.clock.now += timedelta(seconds=2)
            engine.tick()
        self.assertTrue(engine.feed_stalled)
        self.assertGreater(engine.feed_age_seconds, paper.FEED_STALL_SECONDS)


class TodayHeader(base.TempDatabase):
    """The Today screen's top bar: when the next 09:30 is, and NIFTY's day so far."""

    def test_next_session_is_today_until_0930_then_the_next_market_day(self):
        with mock.patch("paper.is_market_day", side_effect=lambda d: d.weekday() < 5 and d.isoformat() != "2026-10-02"):
            at = lambda stamp: datetime.fromisoformat(stamp).replace(tzinfo=IST)
            self.assertEqual(paper.next_session_day(at("2026-10-07T09:10:00")), "2026-10-07", "before 09:30 the session is today's")
            self.assertEqual(paper.next_session_day(at("2026-10-07T17:00:00")), "2026-10-08")
            self.assertEqual(paper.next_session_day(at("2026-10-01T16:00:00")), "2026-10-05", "Friday 2 Oct is a holiday, then the weekend")
            self.assertEqual(paper.next_session_day(at("2026-10-03T09:10:00")), "2026-10-05", "a Saturday morning is not a session")

    def test_the_session_shown_is_todays_from_its_open_otherwise_the_last_one(self):
        """After midnight, before the 09:15 open, on a weekend or a holiday the Today screen keeps the session that ended
        (its marks, closing quotes, the S5 position held overnight) - an empty new date has no data yet."""
        with mock.patch("paper.is_market_day", side_effect=lambda d: d.weekday() < 5 and d.isoformat() != "2026-10-02"):
            at = lambda stamp: datetime.fromisoformat(stamp).replace(tzinfo=IST)
            self.assertEqual(paper.session_day(at("2026-10-07T23:59:00")), "2026-10-07")
            self.assertEqual(paper.session_day(at("2026-10-08T00:27:00")), "2026-10-07", "just after midnight: still Wednesday's session")
            self.assertEqual(paper.session_day(at("2026-10-08T09:14:59")), "2026-10-07", "pre-open is not the new session yet")
            self.assertEqual(paper.session_day(at("2026-10-08T09:15:00")), "2026-10-08", "the open starts it")
            self.assertEqual(paper.session_day(at("2026-10-05T08:00:00")), "2026-10-01", "Monday morning: back past the weekend and Friday's holiday")
            self.assertEqual(paper.session_day(at("2026-10-03T12:00:00")), "2026-10-01", "a Saturday shows Thursday's session (Friday was a holiday)")

    def test_an_open_position_from_an_earlier_day_brings_its_own_marks(self):
        marks_by_day = {"2026-10-07": [{"strategy": "straddle_sell_overnight", "trading_date": "2026-10-07", "minute": "15:35", "mtm_rs": 2418.0}],
                        "2026-10-08": [{"strategy": "straddle_sell", "trading_date": "2026-10-08", "minute": "09:31", "mtm_rs": 50.0}]}
        with mock.patch("paper.marks_for", side_effect=lambda day, strategy=None: [m for m in marks_by_day.get(day, []) if strategy in (None, m["strategy"])]):
            held = {"strategy": "straddle_sell_overnight", "trading_date": "2026-10-07", "status": "open", "derived_from": None}
            closed = {**held, "trading_date": "2026-10-06", "status": "closed"}
            session, earlier = paper.today_marks("2026-10-08", [held, closed])
            self.assertEqual([m["minute"] for m in session], ["09:31"])
            self.assertEqual([(m["trading_date"], m["minute"]) for m in earlier], [("2026-10-07", "15:35")], "only the still-open position's own day, not a closed one's")

    def test_a_position_bought_back_this_morning_keeps_its_entry_day_marks(self):
        marks_by_day = {"2026-10-07": [{"strategy": "straddle_sell_overnight", "trading_date": "2026-10-07", "minute": "15:35", "mtm_rs": 2418.0}],
                        "2026-10-06": [{"strategy": "straddle_sell_overnight", "trading_date": "2026-10-06", "minute": "14:00", "mtm_rs": -300.0}]}
        with mock.patch("paper.marks_for", side_effect=lambda day, strategy=None: [m for m in marks_by_day.get(day, []) if strategy in (None, m["strategy"])]):
            leg = lambda exit_ts: {"type": "CE", "strike": 22500, "side": "SELL", "entry": {"fill": 150.0, "ts": "2026-10-07T09:30:00+05:30"},
                                   "exit": {"fill": 140.0, "ts": exit_ts}}
            bought_back = {"strategy": "straddle_sell_overnight", "trading_date": "2026-10-07", "status": "closed", "derived_from": None,
                           "legs": [leg("2026-10-08T09:30:01+05:30")]}
            older = {**bought_back, "trading_date": "2026-10-06", "legs": [leg("2026-10-07T09:30:01+05:30")]}
            _, earlier = paper.today_marks("2026-10-08", [bought_back, older])
            self.assertEqual([(m["trading_date"], m["minute"]) for m in earlier], [("2026-10-07", "15:35")], "this morning's exit keeps its day's line; an older exit does not")

    def test_spot_day_reads_the_previous_close_and_the_days_bar(self):
        payload = {"fullFeed": {"indexFF": {"ltpc": {"ltp": 22603.05, "cp": 22776.1},
                                             "marketOHLC": {"ohlc": [{"interval": "1d", "open": 22690.45, "high": 22717.65, "low": 22546.3, "close": 22603.05},
                                                                     {"interval": "I1", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}]}}}}
        storage.initialize_database()
        with storage.get_connection() as connection:
            connection.execute("INSERT INTO market_observations (received_at, trading_date, instrument_key, ltp, payload) VALUES (?, ?, ?, ?, ?)",
                               (f"{MONDAY}T15:50:28.251403+05:30", MONDAY, INDEX, 22603.05, json.dumps(payload)))
        now = datetime.fromisoformat(f"{MONDAY}T16:00:00").replace(tzinfo=IST)
        self.assertEqual(paper.spot_day(now), {"prev_close": 22776.1, "open": 22690.45, "high": 22717.65, "low": 22546.3})
        self.assertIsNone(paper.spot_day(now + timedelta(days=1)), "nothing received that day yet")
        self.assertEqual(paper.spot_day(now + timedelta(days=1), MONDAY)["low"], 22546.3, "after midnight the session's own day is read")


class LiveSnapshot(base.TempDatabase):
    def put(self, key, stamp, ltp, bid, ask):
        payload = {"fullFeed": {"marketFF": {"ltpc": {"ltp": ltp}, "marketLevel": {"bidAskQuote": [{"bidP": bid, "askP": ask}] if bid else []}}}}
        storage.initialize_database()
        with storage.get_connection() as connection:
            connection.execute("INSERT INTO market_observations (received_at, trading_date, instrument_key, ltp, payload) VALUES (?, ?, ?, ?, ?)", (f"{MONDAY}T{stamp}+05:30", MONDAY, key, ltp, json.dumps(payload)))

    def test_it_returns_the_newest_quote_and_the_cost_to_close_each_side(self):
        self.put("NSE_FO|1", "10:00:00.100000", 100.0, 99.5, 100.5)
        self.put("NSE_FO|1", "10:00:05.100000", 102.0, 101.5, 102.5)
        self.put("NSE_FO|2", "10:00:04.100000", 40.0, None, None)  # nothing on the book: LTP stands in
        self.put(INDEX, "10:00:05.500000", 25012.5, None, None)
        now = datetime.fromisoformat(f"{MONDAY}T10:00:10").replace(tzinfo=IST)
        live = paper.live_snapshot(["NSE_FO|1", "NSE_FO|2", "NSE_FO|1", "NSE_FO|9"], now)
        one = live["quotes"]["NSE_FO|1"]
        self.assertEqual((one["ltp"], one["bid"], one["ask"], one["age_s"]), (102.0, 101.5, 102.5, 4.9))
        self.assertEqual((one["close_short"], one["close_long"]), (102.5, 101.5), "a short is closed at the ask, a long at the bid")
        self.assertEqual((live["quotes"]["NSE_FO|2"]["close_short"], live["quotes"]["NSE_FO|2"]["close_long"]), (40.0, 40.0))
        self.assertIsNone(live["quotes"]["NSE_FO|9"], "an instrument never quoted today is reported as missing, not invented")
        self.assertEqual(live["spot"]["ltp"], 25012.5)
        self.assertEqual(len(live["quotes"]), 3, "duplicates collapse")
        tomorrow = now + timedelta(hours=15)  # past midnight
        self.assertIsNone(paper.live_snapshot(["NSE_FO|1"], tomorrow)["quotes"]["NSE_FO|1"], "the new date has no quotes yet")
        kept = paper.live_snapshot(["NSE_FO|1"], tomorrow, MONDAY)
        self.assertEqual(kept["quotes"]["NSE_FO|1"]["ltp"], 102.0, "the session's last quote, read from its own day")
        self.assertEqual(kept["spot"]["ltp"], 25012.5)


    def test_spot_now_is_the_newest_tick_and_the_day_of_the_session_shown(self):
        """/api/lab/spot - what the Today screen's NIFTY tile polls every second."""
        payload = {"fullFeed": {"indexFF": {"ltpc": {"ltp": 25012.5, "cp": 25100.0},
                                             "marketOHLC": {"ohlc": [{"interval": "1d", "open": 25090.0, "high": 25120.0, "low": 24990.0}]}}}}
        storage.initialize_database()
        with storage.get_connection() as connection:
            for stamp, ltp in (("10:00:04.900000", 25010.0), ("10:00:05.500000", 25012.5)):
                connection.execute("INSERT INTO market_observations (received_at, trading_date, instrument_key, ltp, payload) VALUES (?, ?, ?, ?, ?)",
                                   (f"{MONDAY}T{stamp}+05:30", MONDAY, INDEX, ltp, json.dumps(payload)))
        with mock.patch("paper.is_market_day", side_effect=lambda d: d.weekday() < 5):
            now = datetime.fromisoformat(f"{MONDAY}T10:00:06").replace(tzinfo=IST)
            spot = paper.spot_now(now)
            self.assertEqual((spot["trading_date"], spot["spot"]["ltp"], spot["spot"]["age_s"]), (MONDAY, 25012.5, 0.5))
            self.assertEqual(spot["spot_day"], {"prev_close": 25100.0, "open": 25090.0, "high": 25120.0, "low": 24990.0})
            night = paper.spot_now(datetime.fromisoformat("2026-09-22T00:30:00").replace(tzinfo=IST))
            self.assertEqual((night["trading_date"], night["spot"]["ltp"]), (MONDAY, 25012.5), "after midnight: the session that ended, not an empty new date")


    def test_spot_minutes_keeps_each_minutes_last_tick_and_reads_only_new_ones_after(self):
        storage.initialize_database()
        paper._spot_minutes.update(day=None, through="", minutes={})

        def put(stamp, ltp):
            with storage.get_connection() as connection:
                connection.execute("INSERT INTO market_observations (received_at, trading_date, instrument_key, ltp, payload) VALUES (?, ?, ?, ?, ?)",
                                   (f"{MONDAY}T{stamp}+05:30", MONDAY, INDEX, ltp, "{}"))

        for stamp, ltp in (("09:14:59.000000", 1.0), ("09:15:02.000000", 25000.0), ("09:15:40.000000", 25010.0), ("09:18:05.000000", 25030.0)):
            put(stamp, ltp)
        with mock.patch("paper.is_market_day", side_effect=lambda d: d.weekday() < 5):
            now = datetime.fromisoformat(f"{MONDAY}T09:18:30").replace(tzinfo=IST)
            first = paper.spot_minutes(now)
            self.assertEqual(first["minutes"], [{"minute": "09:15", "ltp": 25010.0}, {"minute": "09:18", "ltp": 25030.0}], "the open's last tick; 09:16-09:17 (no ticks) absent")
            put("09:18:50.000000", 25025.0)
            put("09:19:10.000000", 25040.0)
            second = paper.spot_minutes(now + timedelta(seconds=45))
            self.assertEqual([(m["minute"], m["ltp"]) for m in second["minutes"]][-2:], [("09:18", 25025.0), ("09:19", 25040.0)])
            self.assertEqual(paper._spot_minutes["through"], f"{MONDAY}T09:19:10.000000+05:30", "the next call reads only what is newer")


class ExitMinute(EngineCase):
    def test_the_exit_minute_follows_the_session_the_options_trade(self):
        self.assertEqual((lab.exit_minute("2026-08-03"), lab.exit_minute("2026-09-21")), ("15:39", "15:39"), "F&O trades to 15:40 from 2026-08-03")
        self.assertEqual((lab.exit_minute("2026-07-31"), lab.exit_minute("2025-01-06")), ("15:29", "15:29"), "before that the session ended at 15:30")
        self.assertEqual((paper.exit_time_for("2026-09-21").strftime("%H:%M"), paper.exit_time_for("2026-07-31").strftime("%H:%M")), ("15:39", "15:29"))

    def test_the_position_is_held_and_marked_through_1529_49_and_closed_at_1529_50(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "15:29")
        self.clock.to("15:29", 49)
        engine.tick()
        self.assertTrue(all(t["status"] == "open" for t in self.trades().values()), "15:29:49 is not yet EXIT_ATTEMPT_START")
        self.assertIn("15:29", {m["minute"] for m in storage.query_paper_marks(MONDAY, "straddle_sell")}, "marks continue right up to the exit attempt")
        self.clock.to("15:29", 50)
        engine.tick()
        done = self.trades()
        self.assertEqual({s: t["status"] for s, t in done.items() if s != lab.S5}, {s: "closed" for s in lab.STRATEGIES})
        self.assertEqual(done[lab.S5]["status"], "open", "S5 closes tomorrow at 09:30, not today")
        self.assertEqual(done["straddle_sell"]["exit_delay_s"], 0.0)
        self.assertTrue(done["straddle_sell"]["legs"][0]["exit"]["ts"].startswith(f"{MONDAY}T15:29:5"), "the exit lands at EXIT_ATTEMPT_START itself - the same moment the backtest prices its own 15:29 close")
        self.advance(engine, "15:40")
        self.assertNotIn("15:30", {m["minute"] for m in storage.query_paper_marks(MONDAY, "straddle_sell")}, "no marks once the position is closed, however long the session runs on")


class ActualExitMinute(base.TempDatabase):
    def test_a_trade_keeps_the_minute_it_really_exited(self):
        fill = lambda ts: {"ts": ts, "fill": 1.0}
        old = {"trading_date": "2026-09-21", "legs": [{"exit": fill("2026-09-21T15:29:01+05:30")}, {"exit": fill("2026-09-21T15:29:02+05:30")}]}
        new = {"trading_date": "2026-09-22", "legs": [{"exit": fill("2026-09-22T15:39:00+05:30")}, {"exit": fill("2026-09-22T15:39:03+05:30")}]}
        late = {"trading_date": "2026-09-22", "legs": [{"exit": fill("2026-09-22T15:39:00+05:30")}, {"exit": fill("2026-09-22T15:41:10+05:30")}]}
        still_open = {"trading_date": "2026-09-22", "legs": [{"exit": None}]}
        self.assertEqual([lab.paper_exit_minute(t) for t in (old, new, late, still_open)], ["15:29", "15:39", "15:41", "15:39"])


class AlignedReconcile(base.TempDatabase):
    DAY = "2026-09-21"

    def legs(self):
        return [{"side": "SELL", "entry": 100.0, "exit": 60.0, "type": "CE", "strike": 25000.0, "role": "body", "instrument_key": "NSE_FO|A"},
                {"side": "SELL", "entry": 90.0, "exit": 95.0, "type": "PE", "strike": 25000.0, "role": "body", "instrument_key": "NSE_FO|B"}]

    def test_each_leg_exits_at_its_last_candle_up_to_the_paper_exit_minute(self):
        storage.save_historical_candles("NSE_FO|A", [base.candle(self.DAY, "15:29", 60, 60, 60, 60), base.candle(self.DAY, "15:39", 50, 50, 50, 50)])
        storage.save_historical_candles("NSE_FO|B", [base.candle(self.DAY, "15:29", 95, 95, 95, 95), base.candle(self.DAY, "15:35", 99, 99, 99, 99)])  # no trade after 15:35
        moved = lab._legs_closing_at(self.legs(), self.DAY, "15:39")
        self.assertEqual([leg["exit"] for leg in moved], [50.0, 99.0])
        self.assertEqual([leg["entry"] for leg in moved], [100.0, 90.0], "entries untouched")

    def test_without_any_candle_after_1529_the_comparison_says_so_instead_of_pretending(self):
        storage.save_historical_candles("NSE_FO|A", [base.candle(self.DAY, "15:29", 60, 60, 60, 60)])
        storage.save_historical_candles("NSE_FO|B", [base.candle(self.DAY, "15:29", 95, 95, 95, 95)])
        self.assertIsNone(lab._legs_closing_at(self.legs(), self.DAY, "15:39"))
        self.assertIsNone(lab._legs_closing_at(self.legs(), "2026-09-22", "15:39"), "a leg with no candles at all")


class BacktestAndLabViews(base.TempDatabase):
    def test_straddle_history_comes_from_straddle_daily_with_the_cost_model(self):
        storage.save_instrument_contracts([{"instrument_key": "NSE_FO|C1|22-09-2026", "expiry": "2026-09-22", "instrument_type": "CE", "strike_price": 25000.0,
                                            "underlying_key": INDEX, "trading_symbol": "x", "lot_size": 65}])
        storage.upsert_straddle_row({**base.NIFTY_BASE, "trading_date": "2026-09-16", "expiry": "2026-09-22", "atm_strike": 25000.0, "ce_0930": 100.0, "pe_0930": 100.0,
                                     "ce_close": 60.0, "pe_close": 90.0, "straddle_0930": 200.0, "ce_instrument_key": "NSE_FO|C1|22-09-2026", "pe_instrument_key": "NSE_FO|P1|22-09-2026"})
        self.assertEqual(lab.build_straddle_history(), {"built": 1, "skipped": 0})
        (row,) = storage.query_backtest_rows("straddle_sell")
        self.assertEqual((row["gross_pts"], row["charges_rs"], row["net_rs"], row["lot_size"]), (50.0, 117.12, 3132.88, 65))
        self.assertEqual([(l["side"], l["entry"], l["exit"]) for l in row["legs"]], [("SELL", 100.0, 60.0), ("SELL", 100.0, 90.0)])
        two = lab.rescale(row, 2)
        self.assertEqual((two["lots"], two["gross_rs"]), (2, 6500.0))
        self.assertNotEqual(two["charges_rs"], 2 * row["charges_rs"], "charges are re-settled, not just doubled")

    def test_the_iron_fly_history_uses_the_same_rules_as_paper(self):
        """WingBuilder (backtest) and PaperEngine build their legs from the same plan_legs()."""
        day = "2026-09-16"
        keys = {}
        contracts = []
        for strike in range(24400, 25700, 50):
            for leg in ("CE", "PE"):
                key = f"NSE_FO|{leg[0]}{strike}|22-09-2026"
                keys[(strike, leg)] = key
                contracts.append({"instrument_key": key, "expiry": "2026-09-22", "instrument_type": leg, "strike_price": float(strike),
                                  "underlying_key": INDEX, "trading_symbol": "x", "lot_size": 65})
        storage.save_instrument_contracts(contracts)
        row = {**base.NIFTY_BASE, "trading_date": day, "expiry": "2026-09-22", "atm_strike": 25000.0, "ce_0930": 110.0, "pe_0930": 100.0, "ce_close": 60.0, "pe_close": 90.0,
               "straddle_0930": 210.0, "ce_instrument_key": keys[(25000, "CE")], "pe_instrument_key": keys[(25000, "PE")]}
        bars = lambda entry, exit_: [base.candle(day, "09:30", entry, entry, entry, entry), base.candle(day, "15:29", exit_, exit_, exit_, exit_)]
        storage.save_historical_candles(keys[(25200, "CE")], bars(12.0, 5.0))
        storage.save_historical_candles(keys[(24800, "PE")], bars(15.0, 40.0))
        builder = lab.WingBuilder("iron_fly_w1", lambda: None, fetch=False, today=datetime(2026, 9, 25).date(), log=lambda line: None)
        built = builder.build(day, row)
        plan = lab.plan_legs("iron_fly_w1", 25000.0, 210.0)
        self.assertEqual([(l["side"], l["type"], l["strike"]) for l in built["legs"]], [(p["side"], p["type"], p["strike"]) for p in plan])
        self.assertEqual(built["wing_width"], 200.0)
        expected = lab.settle([
            {"side": "BUY", "entry": 12.0, "exit": 5.0}, {"side": "BUY", "entry": 15.0, "exit": 40.0},
            {"side": "SELL", "entry": 110.0, "exit": 60.0}, {"side": "SELL", "entry": 100.0, "exit": 90.0}], 65, 1)
        self.assertEqual((built["gross_pts"], built["net_rs"]), (expected["gross_pts"], expected["net_rs"]))
        self.assertEqual(built["gross_pts"], (5.0 - 12.0) + (40.0 - 15.0) + 50.0 + 10.0)

    def test_a_wing_without_a_0930_bar_skips_the_day(self):
        day = "2026-09-16"
        storage.save_instrument_contracts([{"instrument_key": f"NSE_FO|{l[0]}{k}|22-09-2026", "expiry": "2026-09-22", "instrument_type": l, "strike_price": float(k),
                                            "underlying_key": INDEX, "trading_symbol": "x", "lot_size": 65} for k in (25000, 25200, 24800) for l in ("CE", "PE")])
        storage.save_historical_candles("NSE_FO|C25200|22-09-2026", [base.candle(day, "10:00", 5, 5, 5, 5), base.candle(day, "15:29", 5, 5, 5, 5)])
        storage.save_historical_candles("NSE_FO|P24800|22-09-2026", [base.candle(day, "09:30", 5, 5, 5, 5), base.candle(day, "15:29", 5, 5, 5, 5)])
        row = {"expiry": "2026-09-22", "atm_strike": 25000.0, "ce_0930": 110.0, "pe_0930": 100.0, "ce_close": 60.0, "pe_close": 90.0,
               "ce_instrument_key": "NSE_FO|C25000|22-09-2026", "pe_instrument_key": "NSE_FO|P25000|22-09-2026"}
        builder = lab.WingBuilder("iron_fly_w1", lambda: None, fetch=False, today=datetime(2026, 9, 25).date(), log=lambda line: None)
        with self.assertRaises(lab.SkipDay) as caught:
            builder.build(day, row)
        self.assertEqual((caught.exception.reason, caught.exception.detail), ("wing_no_0930_bar", "CE 25200"))

    def test_monthly_summary(self):
        for i, (day, net) in enumerate([("2026-01-05", 1000.0), ("2026-01-06", -3000.0), ("2026-01-07", 500.0), ("2026-01-08", 200.0), ("2026-02-02", 700.0)]):
            legs = [{"side": "SELL", "entry": 100.0, "exit": 100.0 - net / 65 / 2, "type": "CE", "strike": 1.0, "role": "body"}]
            storage.upsert_backtest_row({**lab.backtest_row("straddle_sell", day, "2026-01-27", 25000.0, 200.0, legs, 65), "net_rs": net})
        months = {m["month"]: m for m in lab.monthly("backtest", "straddle_sell")}
        jan = months["2026-01"]
        self.assertEqual((jan["days"], jan["net_rs"], jan["winning_days"], jan["worst_day"], jan["worst_day_rs"]), (4, -1300.0, 3, "2026-01-06", -3000.0))
        self.assertEqual(jan["max_drawdown_rs"], 3000.0)  # peak 1000 -> trough -2000
        self.assertEqual(months["2026-02"]["max_drawdown_rs"], 0.0)


def paper_row(strategy, day, status="closed", net_rs=100.0, net_pts=1.0, bt_net_pts=1.0, bt_net_rs=100.0, fill_edge=0.0, source="bid", lots=1):
    legs = [{"role": "body", "type": "CE", "strike": 25000.0, "side": "SELL", "instrument_key": "k", "entry": {"fill": 100.0 - fill_edge, "source": source, "bid": 100.0, "ask": 101.0, "ltp": 100.5, "ts": day},
             "exit": {"fill": 50.0 + fill_edge, "source": "ask", "bid": 49.0, "ask": 50.0, "ltp": 49.5, "ts": day}}]
    bt_legs = [{"role": "body", "type": "CE", "strike": 25000.0, "side": "SELL", "entry": 100.0, "exit": 50.0}]
    now = f"{day}T15:40:00"
    return {"strategy": strategy, "trading_date": day, "expiry": "x", "lots": lots, "lot_size": 65, "status": status, "legs": legs, "net_rs": net_rs, "net_pts": net_pts,
            "gross_pts": net_pts, "charges_pts": 0.0, "build_version": "t", "created_at": now, "updated_at": now, "bt_net_pts": bt_net_pts, "bt_net_rs": bt_net_rs,
            "bt_gross_pts": bt_net_pts, "bt_charges_pts": 0.0, "bt_legs": bt_legs, "reconciled_at": now}


def days_from(start, n):
    from datetime import date

    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class SizeSelection(base.TempDatabase):
    """Lots or a fixed quantity: fills are per-unit prices, so every rupee figure can be shown at any size."""

    def seed_backtest(self, day, lot_size, net=1000.0):
        legs = [{"side": "SELL", "entry": 100.0, "exit": 60.0, "type": "CE", "strike": 25000.0, "role": "body"},
                {"side": "SELL", "entry": 100.0, "exit": 90.0, "type": "PE", "strike": 25000.0, "role": "body"}]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, "2026-09-22", 25000.0, 200.0, legs, lot_size))

    def test_a_request_names_lots_or_qty_never_both(self):
        self.assertEqual(lab.Size.from_params(None, None), None)
        self.assertEqual(lab.Size.from_params(2, None), lab.Size(lots=2))
        self.assertEqual(lab.Size.from_params(None, 130), lab.Size(qty=130))
        for bad in ((1, 5), (0, None), (None, 0), (None, -3), (lab.MAX_LOTS + 1, None), (None, lab.MAX_QTY + 1)):
            with self.assertRaises(ValueError, msg=str(bad)):
                lab.Size.from_params(*bad)

    def test_a_missed_entry_with_no_lot_size_yet_never_crashes_sizing(self):
        """A missed_entry (or error) row recorded before any contract was resolved has lot_size=None -
        paper_at() must report qty/lots as 0 rather than crash trying to multiply lot_size by anything. Real
        incident: a live-feed health-check failure left every strategy's missed_entry row with lot_size=None,
        and /api/lab/paper/today 500'd trying to size it (paper_daily() has the exact same call shape)."""
        trade = {"strategy": "straddle_sell", "trading_date": "2026-09-25", "lots": 1, "lot_size": None, "status": "missed_entry",
                 "legs": None, "net_rs": None, "net_pts": None, "gross_pts": None, "charges_pts": None, "notes": "health check failed",
                 "bt_legs": None, "bt_net_rs": None, "bt_net_pts": None}
        out = lab.paper_at(trade, lab.Size(lots=3))
        self.assertEqual((out["qty"], out["lots"], out["whole_lots"], out["status"]), (0, 0, True, "missed_entry"))
        self.assertEqual(lab.paper_at(trade, None)["qty"], 0, "the 'as recorded' (no size given) path must not crash either")

    def test_lots_follow_each_days_lot_size_and_qty_is_the_same_units_every_day(self):
        self.seed_backtest("2026-01-05", 75)  # the lot size changes over history
        self.seed_backtest("2026-09-16", 65)
        by_lots = {r["trading_date"]: r for r in lab.backtest_daily("straddle_sell", None, None, lab.Size(lots=2))}
        self.assertEqual([(r["qty"], r["lots"]) for r in by_lots.values()], [(150, 2), (130, 2)])
        by_qty = {r["trading_date"]: r for r in lab.backtest_daily("straddle_sell", None, None, lab.Size(qty=130))}
        self.assertEqual([r["qty"] for r in by_qty.values()], [130, 130])
        self.assertEqual(by_qty["2026-09-16"]["net_rs"], by_lots["2026-09-16"]["net_rs"], "2 lots at lot size 65 is qty 130")
        self.assertEqual(by_qty["2026-09-16"]["whole_lots"], True)
        # 130 units is not a whole number of 75-unit lots: still shown (as units), and flagged
        self.assertEqual((by_qty["2026-01-05"]["whole_lots"], by_qty["2026-01-05"]["lots"]), (False, round(130 / 75, 4)))
        self.assertEqual(by_qty["2026-01-05"]["gross_rs"], 50.0 * 130)

    def test_the_default_size_is_unchanged(self):
        self.seed_backtest("2026-09-16", 65)
        (row,) = lab.backtest_daily("straddle_sell", None, None)
        self.assertEqual((row["qty"], row["lots"], row["gross_rs"]), (65, 1, 3250.0))
        self.assertEqual(lab.backtest_daily("straddle_sell", None, None, 1)[0]["net_rs"], row["net_rs"], "a bare int still means lots")

    def test_a_paper_trade_is_re_settled_from_its_fills_at_the_chosen_size(self):
        trade = paper_row("straddle_sell", "2026-09-16", net_rs=0.0, net_pts=0.0)
        storage.paper_insert_trade(trade)
        (as_recorded,) = lab.paper_daily("straddle_sell", None, None)
        self.assertEqual(as_recorded["qty"], 65)
        (three,) = lab.paper_daily("straddle_sell", None, None, lab.Size(lots=3))
        expected = lab.settle_units([{"side": "SELL", "entry": 100.0, "exit": 50.0}], 195)
        self.assertEqual((three["qty"], three["lots"], three["net_rs"], three["gross_pts"]), (195, 3, expected["net_rs"], expected["gross_pts"]))
        self.assertEqual(three["bt_net_rs"], lab.settle_units(trade["bt_legs"], 195)["net_rs"], "the candle twin is re-settled at the same size")
        (qty,) = lab.paper_daily("straddle_sell", None, None, lab.Size(qty=195))
        self.assertEqual(qty["net_rs"], three["net_rs"])

    def test_an_open_or_missed_trade_stays_unsettled_at_any_size(self):
        storage.paper_insert_trade({**paper_row("straddle_sell", "2026-09-16"), "status": "missed_entry", "legs": [], "net_rs": None, "net_pts": None, "gross_pts": None,
                                    "bt_legs": None, "bt_net_rs": None, "bt_net_pts": None, "bt_gross_pts": None, "bt_charges_pts": None})
        (row,) = lab.paper_daily("straddle_sell", None, None, lab.Size(lots=4))
        self.assertEqual((row["status"], row["net_rs"], row["qty"]), ("missed_entry", None, 260))

    def test_minute_marks_scale_with_units(self):
        trades = [paper_row("straddle_sell", "2026-09-16")]
        marks = [{"strategy": "straddle_sell", "trading_date": "2026-09-16", "minute": "10:00", "mtm_rs": 650.0}]
        self.assertEqual(lab.marks_at(marks, trades, None), marks)
        self.assertEqual(lab.marks_at(marks, trades, lab.Size(lots=2))[0]["mtm_rs"], 1300.0)
        self.assertEqual(lab.marks_at(marks, trades, lab.Size(qty=130))[0]["mtm_rs"], 1300.0)

    def test_a_trade_with_no_lot_size_yet_never_crashes_mark_scaling(self):
        """A missed_entry never has marks (nothing was ever open to mark-to-market), but it still rides along in
        `trades` on the Today screen - marks_at() must not crash trying to scale by its None lot_size, and a real
        trade's own marks must still scale normally alongside it."""
        trades = [paper_row("straddle_sell", "2026-09-25"), {**paper_row("iron_fly_w1", "2026-09-25"), "lot_size": None}]
        marks = [{"strategy": "straddle_sell", "trading_date": "2026-09-25", "minute": "10:00", "mtm_rs": 650.0}]
        self.assertEqual(lab.marks_at(marks, trades, lab.Size(lots=2))[0]["mtm_rs"], 1300.0)


class DerivedStrategyS4(base.TempDatabase):
    """S4 = S1's result each day, S3's on expiry days. It has no orders and no storage of its own."""

    NORMAL, EXPIRY = "2026-09-15", "2026-09-22"

    def seed(self, strategy, day, exit_price, expiry=None):
        legs = [{"side": "SELL", "entry": 100.0, "exit": exit_price, "type": "CE", "strike": 25000.0, "role": "body"}]
        storage.upsert_backtest_row(lab.backtest_row(strategy, day, expiry or "2026-09-22", 25000.0, 200.0, legs, 65))

    def seed_all(self):
        for i, strategy in enumerate(lab.STRATEGIES):
            for day in (self.NORMAL, self.EXPIRY):
                self.seed(strategy, day, 60.0 + 10 * i)  # every strategy has a different result on each day

    def test_the_three_real_strategies_are_unchanged_and_s4_is_only_a_view(self):
        self.assertEqual(lab.STRATEGIES, ("straddle_sell", "iron_fly_w1", "iron_fly_w2"))
        self.assertEqual(lab.SAME_DAY_STRATEGIES, lab.STRATEGIES + (lab.S4,))
        self.assertEqual(lab.LAB_STRATEGIES, lab.STRATEGIES + (lab.S4, lab.S5, lab.S6, lab.S7, lab.S8, lab.S10))
        self.assertEqual(paper.PaperEngine.__init__.__kwdefaults__.get("strategies", lab.STRATEGIES) if paper.PaperEngine.__init__.__kwdefaults__ else lab.STRATEGIES, lab.STRATEGIES, "the engine still enters only S1-S3")
        with self.assertRaises(ValueError):
            lab.plan_legs(lab.S4, 25000.0, 200.0)  # nothing can build orders for S4

    def test_s4_takes_s1_on_normal_days_and_s3_on_expiry_days(self):
        self.seed_all()
        rows = {r["trading_date"]: r for r in lab.backtest_daily(lab.S4, None, None, 1)}
        s1 = {r["trading_date"]: r for r in lab.backtest_daily("straddle_sell", None, None, 1)}
        s3 = {r["trading_date"]: r for r in lab.backtest_daily("iron_fly_w2", None, None, 1)}
        self.assertEqual(rows[self.NORMAL]["net_rs"], s1[self.NORMAL]["net_rs"])
        self.assertEqual(rows[self.EXPIRY]["net_rs"], s3[self.EXPIRY]["net_rs"])
        self.assertNotEqual(s1[self.EXPIRY]["net_rs"], s3[self.EXPIRY]["net_rs"])
        self.assertEqual((rows[self.NORMAL]["derived_from"], rows[self.EXPIRY]["derived_from"]), ("straddle_sell", "iron_fly_w2"))
        self.assertEqual({r["strategy"] for r in rows.values()}, {lab.S4})
        self.assertEqual(rows[self.EXPIRY]["running_net_rs"], round(rows[self.NORMAL]["net_rs"] + rows[self.EXPIRY]["net_rs"], 2))

    def test_at_every_size_s4_equals_its_source_row_at_that_size(self):
        self.seed_all()
        for size in (lab.Size(lots=1), lab.Size(lots=3), lab.Size(qty=195)):
            s4 = {r["trading_date"]: r for r in lab.backtest_daily(lab.S4, None, None, size)}
            s3 = {r["trading_date"]: r for r in lab.backtest_daily("iron_fly_w2", None, None, size)}
            self.assertEqual((s4[self.EXPIRY]["net_rs"], s4[self.EXPIRY]["qty"]), (s3[self.EXPIRY]["net_rs"], s3[self.EXPIRY]["qty"]))

    def test_building_s4_does_not_touch_the_stored_rows_or_the_other_strategies(self):
        self.seed_all()
        before = {s: lab.backtest_daily(s, None, None, 3) for s in lab.STRATEGIES}
        lab.backtest_daily(None, None, None, 3)
        lab.backtest_daily(lab.S4, None, None, 3)
        self.assertEqual(before, {s: lab.backtest_daily(s, None, None, 3) for s in lab.STRATEGIES})
        self.assertEqual(sorted({r["strategy"] for r in storage.query_backtest_rows()}), sorted(lab.STRATEGIES), "S4 is never stored")

    def test_a_missing_source_row_is_reported_and_never_filled_from_the_other_strategy(self):
        self.seed("straddle_sell", self.NORMAL, 60.0)
        self.seed("straddle_sell", self.EXPIRY, 60.0)  # an expiry day with no S3 row
        rows, skipped = lab.derived_backtest_rows(lab.S4)
        self.assertEqual([r["trading_date"] for r in rows], [self.NORMAL])
        self.assertEqual([s["trading_date"] for s in skipped], [self.EXPIRY])
        self.assertIn("iron_fly_w2", skipped[0]["reason"])
        self.assertEqual(lab.derived_skips()[lab.S4]["backtest"], skipped)

    def paper_pair(self):
        self.seed_all()
        normal = {**paper_row("straddle_sell", self.NORMAL), "expiry": "2026-09-22"}
        expiry = {**paper_row("iron_fly_w2", self.EXPIRY, net_rs=-500.0, net_pts=-2.0), "expiry": self.EXPIRY}
        other = {**paper_row("iron_fly_w1", self.EXPIRY, net_rs=999.0), "expiry": self.EXPIRY}
        for trade in (normal, expiry, other, {**paper_row("straddle_sell", self.EXPIRY, net_rs=777.0), "expiry": self.EXPIRY}):
            storage.paper_insert_trade(trade)

    def test_paper_s4_is_the_s1_trade_or_the_s3_trade_of_the_day(self):
        self.paper_pair()
        trades = {t["trading_date"]: t for t in lab.paper_trades(lab.S4)}
        self.assertEqual((trades[self.NORMAL]["net_rs"], trades[self.NORMAL]["derived_from"]), (100.0, "straddle_sell"))
        self.assertEqual((trades[self.EXPIRY]["net_rs"], trades[self.EXPIRY]["derived_from"]), (-500.0, "iron_fly_w2"))
        everything = lab.paper_trades()
        self.assertEqual(sum(1 for t in everything if t["strategy"] == lab.S4), 2)
        self.assertEqual(len(storage.query_paper_trades()), 4, "no S4 trade is stored, so nothing can be entered for it")
        (daily,) = [r for r in lab.paper_daily(lab.S4, None, None, lab.Size(lots=2)) if r["trading_date"] == self.EXPIRY]
        self.assertEqual((daily["qty"], daily["source"]), (130, "paper"))

    def test_an_open_or_missed_source_trade_is_mirrored_as_it_is(self):
        self.seed_all()
        storage.paper_insert_trade({**paper_row("iron_fly_w2", self.EXPIRY), "expiry": self.EXPIRY, "status": "open", "net_rs": None, "net_pts": None, "gross_pts": None,
                                    "bt_legs": None, "bt_net_rs": None, "bt_net_pts": None, "bt_gross_pts": None, "bt_charges_pts": None})
        (trade,) = lab.paper_trades(lab.S4)
        self.assertEqual((trade["status"], trade["net_rs"], trade["derived_from"]), ("open", None, "iron_fly_w2"))
        marks = [{"strategy": "iron_fly_w2", "trading_date": self.EXPIRY, "minute": "10:00", "mtm_rs": 650.0},
                 {"strategy": "straddle_sell", "trading_date": self.EXPIRY, "minute": "10:00", "mtm_rs": -1.0}]
        shown = lab.derived_marks(marks, [trade])
        self.assertEqual([(m["strategy"], m["mtm_rs"]) for m in shown if m["strategy"] == lab.S4], [(lab.S4, 650.0)], "the S4 marks are the source's, not S1's")
        self.assertEqual(len(shown), 3)

    def test_the_lab_views_list_s4_next_to_the_others(self):
        self.seed_all()
        self.assertIn(lab.S4, {m["strategy"] for m in lab.monthly("backtest", None)})


class PaperTradesForToday(base.TempDatabase):
    """strategy_lab.paper_trades_for_today() - what main.py's /api/lab/paper/today shows: today's own trades plus
    S5's overnight position from an earlier day, for as long as it is still relevant to today. Found via a real
    report: an S5 position that closed at today's own 09:30 disappeared from Today entirely, because the old
    widening only ever looked for a STILL-OPEN prior-day S5 row - the moment _s5_exit() resolved it, it fell into
    the gap between "not open anymore" and "not dated today either"."""

    def seed_s5(self, day, status="open", updated_at=None):
        row = paper_row(lab.S5, day, status=status, net_rs=100.0 if status != "open" else None)
        row["updated_at"] = updated_at or f"{day}T09:30:01"
        storage.paper_insert_trade(row)

    def test_a_still_open_prior_day_s5_position_is_included(self):
        self.seed_s5("2026-09-23", status="open")
        recorded = lab.paper_trades_for_today("2026-09-24")
        self.assertEqual([t["trading_date"] for t in recorded if t["strategy"] == lab.S5], ["2026-09-23"])

    def test_a_prior_day_s5_position_closed_at_todays_own_0930_is_still_included(self):
        self.seed_s5("2026-09-23", status="closed", updated_at="2026-09-24T09:30:01")
        recorded = lab.paper_trades_for_today("2026-09-24")
        self.assertEqual([t["trading_date"] for t in recorded if t["strategy"] == lab.S5], ["2026-09-23"], "closed at today's 09:30 - must not vanish from Today the moment it resolves")

    def test_a_prior_day_s5_position_closed_on_an_earlier_day_is_no_longer_included(self):
        self.seed_s5("2026-09-22", status="closed", updated_at="2026-09-22T09:30:01")  # resolved the day before yesterday
        recorded = lab.paper_trades_for_today("2026-09-24")
        self.assertEqual([t for t in recorded if t["strategy"] == lab.S5], [], "long since closed - not today's business anymore")

    def test_todays_own_s5_entrant_is_included_via_the_normal_day_scoped_fetch(self):
        self.seed_s5("2026-09-24", status="open")
        recorded = lab.paper_trades_for_today("2026-09-24")
        self.assertEqual([t["trading_date"] for t in recorded if t["strategy"] == lab.S5], ["2026-09-24"])

    def test_a_missed_entry_from_a_health_check_failure_never_crashes_the_today_endpoint(self):
        """Real incident: the live feed had no NIFTY spot quote one morning, so every strategy's health check
        failed and each got a missed_entry row with no lot_size at all (never resolved). /api/lab/paper/today
        500'd sizing it - reproduced here end to end, the same way the endpoint itself builds its response."""
        today = "2026-09-25"
        for strategy in (lab.S5, "straddle_sell", "iron_fly_w1", "iron_fly_w2"):
            row = paper_row(strategy, today, status="missed_entry", net_rs=None, net_pts=None, bt_net_pts=None, bt_net_rs=None)
            row["lot_size"] = None
            storage.paper_insert_trade(row)
        recorded = lab.paper_trades_for_today(today)
        size = lab.Size(lots=3)
        trades = [lab.paper_at(trade, size) for trade in recorded]  # the exact calls /api/lab/paper/today makes
        marks = lab.marks_at(lab.derived_marks(paper.marks_for(today), recorded), recorded, size)
        self.assertTrue(all(t["qty"] == 0 for t in trades))
        self.assertEqual({t["status"] for t in trades}, {"missed_entry"})
        self.assertEqual(marks, [])  # no marks were ever recorded for a missed entry - nothing to scale, and nothing crashes trying to


class S8PaperIsS5WithoutFridays(base.TempDatabase):
    """S8's paper trades are S5's own minus the Friday entries (the user, 2026-10-08: "s5 paper trade is same for s8, only
    thing is that there is no trade on Friday") - derived on read like S4's, never stored, never entered by the engine."""

    THU, FRI, MON = "2026-10-01", "2026-10-02", "2026-10-05"

    def seed(self):
        storage.paper_insert_trade(paper_row(lab.S5, self.THU, net_rs=300.0))
        storage.paper_insert_trade(paper_row(lab.S5, self.FRI, net_rs=-900.0))
        storage.paper_insert_trade({**paper_row(lab.S5, self.MON, status="open", net_rs=None, net_pts=None), "updated_at": f"{self.MON}T09:30:01"})
        storage.paper_insert_trade(paper_row("straddle_sell", self.THU))

    def test_s8_takes_every_s5_trade_except_the_friday_entry(self):
        self.seed()
        s8 = lab.paper_trades(lab.S8)
        self.assertEqual([(t["trading_date"], t["status"], t["derived_from"]) for t in s8], [(self.THU, "closed", lab.S5), (self.MON, "open", lab.S5)])
        self.assertEqual({t["strategy"] for t in s8}, {lab.S8})
        self.assertEqual(len(storage.query_paper_trades()), 4, "nothing is stored for S8, so the engine can never act on it")
        self.assertEqual(storage.query_paper_trades(lab.S8), [])

    def test_at_any_size_an_s8_paper_day_is_s5s_same_day(self):
        self.seed()
        for size in (lab.Size(lots=1), lab.Size(lots=3)):
            s5 = {r["trading_date"]: r for r in lab.paper_daily(lab.S5, None, None, size)}
            s8 = {r["trading_date"]: r for r in lab.paper_daily(lab.S8, None, None, size)}
            self.assertEqual(sorted(s8), [self.THU, self.MON])
            self.assertEqual((s8[self.THU]["net_rs"], s8[self.THU]["qty"], s8[self.THU]["source"]), (s5[self.THU]["net_rs"], s5[self.THU]["qty"], "paper"))
        (s5_month,) = lab.monthly("paper", lab.S5, lab.Size(lots=1))
        (s8_month,) = lab.monthly("paper", lab.S8, lab.Size(lots=1))
        friday = next(r for r in lab.paper_daily(lab.S5, None, None, lab.Size(lots=1)) if r["trading_date"] == self.FRI)
        self.assertEqual((s8_month["days"], s8_month["net_rs"]), (s5_month["days"] - 1, round(s5_month["net_rs"] - friday["net_rs"], 2)))

    def test_every_strategy_lists_s8_next_to_s5_and_the_friday_is_reported_as_skipped(self):
        self.seed()
        self.assertEqual(sorted(t["trading_date"] for t in lab.paper_trades() if t["strategy"] == lab.S8), [self.THU, self.MON])
        self.assertEqual([(s["trading_date"], s["reason"]) for s in lab.derived_skips()[lab.S8]["paper"]], [(self.FRI, "not_entered_friday")])

    def test_today_shows_the_s5_position_once_not_again_as_s8(self):
        self.seed()
        recorded = lab.paper_trades_for_today(self.MON)
        self.assertEqual([t["strategy"] for t in recorded if t["trading_date"] == self.MON], [lab.S5])
        self.assertFalse(any(t["strategy"] == lab.S8 for t in recorded))


NEXT_EXPIRY = "2026-09-29"  # the weekly expiry after EXPIRY - only used by the roll tests


def rollkey(strike, leg):
    return f"NSE_FO|{leg[0]}{int(strike)}N"


class OvernightPaperTrading(EngineCase):
    """S5's live paper trading: the same straddle straddle_sell sells at 09:30, held overnight at PaperEngine level
    (no new order - S1's own fill is reused) and bought back at the NEXT trading day's 09:30, exactly mirroring
    s5_overnight_rows()'s backtest rule but on live quotes instead of candles."""

    def expiry_for(self, day: str) -> str:
        return EXPIRY if day <= EXPIRY else NEXT_EXPIRY

    def seed_roll_contracts(self, spot=25012.0, atm=25000):
        contracts = []
        for strike in range(24300, 25750, 50):
            for leg in ("CE", "PE"):
                contracts.append({"instrument_key": rollkey(strike, leg), "expiry": NEXT_EXPIRY, "instrument_type": leg,
                                   "strike_price": float(strike), "underlying_key": INDEX, "trading_symbol": f"NIFTY {strike} {leg} roll", "lot_size": LOT})
        storage.save_instrument_contracts(contracts)
        for strike in range(24300, 25750, 50):
            for leg in ("CE", "PE"):
                intrinsic = max(0.0, (atm - strike) if leg == "CE" else (strike - atm))
                ltp = round(intrinsic + max(3.0, 105.0 - abs(strike - atm) * 0.3), 2)
                self.quotes.set(rollkey(strike, leg), bid=round(ltp - 0.5, 2), ask=round(ltp + 0.5, 2), ltp=ltp)

    def test_the_normal_day_reuses_straddle_sells_own_fill_no_new_order(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:30")
        engine.tick()
        s1, s5 = self.trades()["straddle_sell"], self.trades()["straddle_sell_overnight"]
        self.assertEqual(s5["status"], "open")
        self.assertEqual((s5["atm_strike"], s5["expiry"], s5["lot_size"], s5["spot_at_entry"], s5["premium_ref"]),
                          (s1["atm_strike"], s1["expiry"], s1["lot_size"], s1["spot_at_entry"], s1["premium_ref"]))
        self.assertEqual([(l["instrument_key"], l["entry"]) for l in s5["legs"]], [(l["instrument_key"], l["entry"]) for l in s1["legs"]],
                          "the SAME fill object S1 got - no separate quote taken")

    def test_bought_back_at_the_next_trading_days_0930(self):
        self.market()
        engine = self.engine()
        self.advance(engine, "09:30")
        engine.tick()
        s5_entry = self.trades()["straddle_sell_overnight"]
        # the next trading day: fresh quotes, then 09:30
        self.clock.now = datetime(2026, 9, 22, 9, 19, 55, tzinfo=IST)
        self.quotes.set(ck(25000, "CE"), bid=59.0, ask=60.0, ltp=59.5)
        self.quotes.set(ck(25000, "PE"), bid=39.0, ask=40.0, ltp=39.5)
        self.advance(engine, "09:30")
        engine.tick()
        s5 = [t for t in storage.query_paper_trades("straddle_sell_overnight", "2026-09-21", "2026-09-21")][0]
        self.assertEqual(s5["status"], "closed")
        for leg in s5["legs"]:
            self.assertEqual(leg["exit"]["source"], "ask", "bought back at the ask")
        expected = lab.settle([{"side": "SELL", "entry": l["entry"]["fill"], "exit": l["exit"]["fill"]} for l in s5["legs"]], LOT, 1)
        self.assertEqual((s5["net_pts"], s5["net_rs"]), (expected["net_pts"], expected["net_rs"]))
        self.assertEqual(s5["legs"][0]["entry"], s5_entry["legs"][0]["entry"], "the entry fill is untouched by the exit")

    def test_a_late_start_never_abandons_the_overnight_position(self):
        """The backend starts at 09:55 with no quotes yet (e.g. the token expired): the 09:30 exit is long past its
        give-up time, but the position is still held, so it is retried until quotes arrive and then closed, flagged late."""
        self.market()
        engine = self.engine()
        self.advance(engine, "09:30")
        engine.tick()
        self.quotes.prices.clear()
        self.clock.now = datetime(2026, 9, 22, 9, 55, tzinfo=IST)
        late = self.engine()
        late.tick()
        s5 = storage.query_paper_trades("straddle_sell_overnight", "2026-09-21", "2026-09-21")[0]
        self.assertEqual(s5["status"], "open", "still held: not written off as 'result unknown'")
        self.assertTrue(any("still no exit quote" in a["message"] for a in storage.query_paper_alerts()))
        self.quotes.set(ck(25000, "CE"), bid=59.0, ask=60.0, ltp=59.5)
        self.quotes.set(ck(25000, "PE"), bid=39.0, ask=40.0, ltp=39.5)
        self.clock.now += timedelta(minutes=2)
        late.tick()
        s5 = storage.query_paper_trades("straddle_sell_overnight", "2026-09-21", "2026-09-21")[0]
        self.assertEqual(s5["status"], "missed_exit")
        self.assertIn("late", s5["invalid_reason"])

    def test_the_overnight_position_is_never_treated_as_stale_before_tomorrows_0930(self):
        """_close_stale_positions() must leave S5 alone: an S5 row open from yesterday is NORMAL until 09:30, not a
        backend-downtime error the way it would be for straddle_sell."""
        self.market()
        engine = self.engine()
        self.advance(engine, "09:31")
        self.quotes.set(ck(25000, "CE"), bid=59.0, ask=60.0, ltp=59.5)
        self.quotes.set(ck(25000, "PE"), bid=39.0, ask=40.0, ltp=39.5)
        self.advance(engine, "15:39")
        self.clock.to("15:39")
        engine.tick()  # straddle_sell, iron_fly_w1/w2 close normally today; S5 does not
        self.assertEqual(self.trades()["straddle_sell_overnight"]["status"], "open")
        self.clock.now = datetime(2026, 9, 22, 6, 0, tzinfo=IST)  # well before tomorrow's session even opens
        engine.tick()
        s5 = self.trades()["straddle_sell_overnight"]  # trades() reads MONDAY; row is still there, still open
        self.assertEqual(s5["status"], "open")
        self.assertEqual(storage.query_paper_alerts(), [], "no stale-position alert for S5")

    def test_a_missed_s1_entry_is_a_missed_s5_entry_too_with_a_reason(self):
        self.clock.to("09:45")  # straddle_sell itself was never entered
        self.market()
        engine = self.engine()
        engine.tick()
        trades = self.trades()
        self.assertEqual(trades["straddle_sell"]["status"], "missed_entry")
        self.assertEqual(trades["straddle_sell_overnight"]["status"], "missed_entry")
        self.assertIn("straddle_sell", trades["straddle_sell_overnight"]["notes"])

    def test_a_health_check_failure_misses_s5_too(self):
        engine = self.engine()  # no quotes: the feed is not live
        self.advance(engine, "09:35")
        self.assertEqual({t["status"] for t in self.trades().values()}, {"missed_entry"})
        self.assertIn("straddle_sell", self.trades()["straddle_sell_overnight"]["notes"])

    def test_an_expiry_day_entrant_rolls_into_next_weeks_same_atm_strike(self):
        """straddle_sell enters on its OWN expiry day (nothing left to hold overnight): S5 sells the SAME ATM strike
        fresh in next week's expiry instead, at that contract's own 09:30 fill - not a copy of anything."""
        self.market()
        self.seed_roll_contracts()
        self.clock.now = datetime(2026, 9, 22, 9, 19, 55, tzinfo=IST)  # EXPIRY itself
        engine = self.engine(expiry_for=self.expiry_for)
        self.advance(engine, "09:30")
        engine.tick()
        trades = [t for t in storage.query_paper_trades(None, EXPIRY, EXPIRY)]
        by_strategy = {t["strategy"]: t for t in trades}
        s1, s5 = by_strategy["straddle_sell"], by_strategy["straddle_sell_overnight"]
        self.assertEqual(s1["expiry"], EXPIRY, "straddle_sell really did enter on its own expiry day")
        self.assertEqual(s5["status"], "open")
        self.assertEqual((s5["expiry"], s5["atm_strike"]), (NEXT_EXPIRY, s1["atm_strike"]))
        self.assertEqual({l["instrument_key"] for l in s5["legs"]}, {rollkey(25000, "CE"), rollkey(25000, "PE")})
        self.assertTrue(all(l["side"] == "SELL" for l in s5["legs"]))
        for leg in s5["legs"]:
            self.assertEqual(leg["entry"]["source"], "bid", "sold at the bid, like any other SELL entry")

    def test_the_roll_waits_for_a_fresh_quote_then_is_reported_if_it_never_comes(self):
        self.market()
        self.seed_roll_contracts()
        self.quotes.prices.pop(rollkey(25000, "CE"), None)  # the roll contract's quote is not live yet
        self.clock.now = datetime(2026, 9, 22, 9, 19, 55, tzinfo=IST)
        engine = self.engine(expiry_for=self.expiry_for)
        self.advance(engine, "09:32")
        s5 = [t for t in storage.query_paper_trades(None, EXPIRY, EXPIRY) if t["strategy"] == "straddle_sell_overnight"][0]
        self.assertEqual(s5["status"], "missed_entry")
        self.assertIn("no fresh quote for CE", s5["notes"])

    def test_a_restart_still_closes_the_overnight_position_the_next_morning(self):
        self.market()
        first = self.engine()
        self.advance(first, "09:31")
        before = self.trades()["straddle_sell_overnight"]
        self.assertEqual(before["status"], "open")
        self.clock.now = datetime(2026, 9, 22, 9, 19, 55, tzinfo=IST)
        self.quotes.set(ck(25000, "CE"), bid=59.0, ask=60.0, ltp=59.5)
        self.quotes.set(ck(25000, "PE"), bid=39.0, ask=40.0, ltp=39.5)
        second = self.engine()  # brand-new engine, empty memory, same database
        self.advance(second, "09:30")
        second.tick()
        s5 = [t for t in storage.query_paper_trades("straddle_sell_overnight", "2026-09-21", "2026-09-21")][0]
        self.assertEqual(s5["status"], "closed")

    def test_s5_is_never_reconciled_against_a_candle_backtest(self):
        """reconcile_trade() -> plan_legs() would raise for S5 (it is not in STRATEGIES); the engine must not even try."""
        self.market()
        engine = self.engine()
        self.advance(engine, "09:30")
        engine.tick()
        self.clock.now = datetime(2026, 9, 22, 9, 19, 55, tzinfo=IST)
        self.quotes.set(ck(25000, "CE"), bid=59.0, ask=60.0, ltp=59.5)
        self.quotes.set(ck(25000, "PE"), bid=39.0, ask=40.0, ltp=39.5)
        self.advance(engine, "09:30")
        engine.tick()
        self.clock.now = self.clock.now.replace(hour=16, minute=0, second=0)  # well past today's own 15:50 reconcile time
        engine.tick()
        s5 = [t for t in storage.query_paper_trades("straddle_sell_overnight", "2026-09-21", "2026-09-21")][0]
        self.assertEqual(s5["status"], "closed")
        self.assertIsNone(s5["reconciled_at"])
        self.assertIsNone(s5["bt_net_rs"])
        self.assertNotIn("straddle_sell_overnight|2026-09-21", self.reconciled)


class TwoOvernightStrategyS6(base.TempDatabase):
    """S6: the same idea as S5, held for TWO overnights - sold at day D's 09:30, bought back at the day-AFTER-next
    trading day's 09:30 close. Backtest only (not paper-traded). The interesting new case S5 never had: the held
    contract's OWN weekly expiry can fall on the MIDDLE day, not just the entry day - then it is bought back and
    re-sold in the next week's expiry partway through the hold (see s6_overnight_rows()'s docstring)."""

    def setUp(self):
        super().setUp()
        lab._s6_cache.clear()

    def seed_s1(self, day, expiry, entry_ce=100.0, entry_pe=90.0, lot_size=65, strike=25000.0):
        ce_key, pe_key = f"NSE_FO|CE{day}", f"NSE_FO|PE{day}"
        legs = [{"side": "SELL", "entry": entry_ce, "exit": entry_ce - 5, "type": "CE", "strike": strike, "role": "body", "instrument_key": ce_key},
                {"side": "SELL", "entry": entry_pe, "exit": entry_pe - 5, "type": "PE", "strike": strike, "role": "body", "instrument_key": pe_key}]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, expiry, strike, entry_ce + entry_pe, legs, lot_size))
        return ce_key, pe_key

    def seed_next_open(self, key, day, close_price, open_price=None):
        storage.save_historical_candles(key, [base.candle(day, "09:30", open_price if open_price is not None else close_price + 1000, close_price + 1000, close_price - 1000, close_price)])

    def test_a_continuous_hold_is_two_legs_s1s_entry_reused_and_the_exit_two_trading_days_later(self):
        ce, pe = self.seed_s1("2026-09-16", "2026-09-22", 100.0, 90.0)  # expiry is well after the middle day: no roll needed anywhere
        self.seed_s1("2026-09-17", "2026-09-22")
        self.seed_s1("2026-09-18", "2026-09-22")
        self.seed_next_open(ce, "2026-09-18", close_price=70.0)
        self.seed_next_open(pe, "2026-09-18", close_price=60.0)
        rows, skipped = lab.s6_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == "2026-09-16")
        self.assertEqual((row["strategy"], row["exit_date"], row["rolled_to_next_expiry"], row["rolled_mid_hold"]), (lab.S6, "2026-09-18", False, False))
        self.assertEqual(len(row["legs"]), 2)
        self.assertEqual(sorted(leg["entry"] for leg in row["legs"]), [90.0, 100.0], "S1's own recorded entry, reused as-is")
        self.assertEqual(sorted(leg["exit"] for leg in row["legs"]), [60.0, 70.0])
        expected = lab.settle([{"side": "SELL", "entry": 100.0, "exit": 70.0}, {"side": "SELL", "entry": 90.0, "exit": 60.0}], 65, 1)
        self.assertEqual((row["net_pts"], row["net_rs"], row["charges_pts"]), (expected["net_pts"], expected["net_rs"], expected["charges_pts"]))

    def test_overlapping_holds_of_the_same_contract_each_exit_at_their_own_exit_days_close(self):
        """09-16 (exit 09-18) and 09-17 (exit 09-21) hold the same contract at once: each must get its own exit day's close."""
        ce, pe = "NSE_FO|CEsame", "NSE_FO|PEsame"
        for day in ("2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21"):
            legs = [{"side": "SELL", "entry": 100.0, "exit": 95.0, "type": type_, "strike": 25000.0, "role": "body", "instrument_key": key}
                    for key, type_ in ((ce, "CE"), (pe, "PE"))]
            storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, "2026-09-22", 25000.0, 200.0, legs, 65))
        for key in (ce, pe):
            self.seed_next_open(key, "2026-09-18", close_price=70.0)
            self.seed_next_open(key, "2026-09-21", close_price=40.0)
        rows, _ = lab.s6_overnight_rows()
        self.assertEqual({r["trading_date"]: [leg["exit"] for leg in r["legs"]] for r in rows},
                         {"2026-09-16": [70.0, 70.0], "2026-09-17": [40.0, 40.0]})

    def test_own_expiry_entrant_is_reported_the_same_way_s5_reports_it(self):
        self.seed_s1("2026-09-15", "2026-09-15")  # own expiry day, roll not resolved
        self.seed_s1("2026-09-16", "2026-09-22")
        self.seed_s1("2026-09-17", "2026-09-22")
        rows, skipped = lab.s6_overnight_rows()
        self.assertEqual(rows, [])
        self.assertIn(("2026-09-15", "own_expiry_not_rolled"), [(s["trading_date"], s["reason"]) for s in skipped])

    def test_the_trailing_two_days_are_reported_no_second_next_day_not_dropped(self):
        self.seed_s1("2026-09-16", "2026-09-22")
        self.seed_s1("2026-09-17", "2026-09-22")
        rows, skipped = lab.s6_overnight_rows()
        self.assertEqual(rows, [])
        self.assertEqual({s["trading_date"] for s in skipped}, {"2026-09-16", "2026-09-17"})
        self.assertTrue(all(s["reason"] == "no_second_next_day" for s in skipped))

    # Well in the past for the same reason as OvernightStrategyS5's own roll tests: find_option_contract()'s
    # lapsed/live key-shape distinction depends on the real wall clock.
    ENTRY, MIDDLE, EXIT_DAY, ROLL_EXPIRY = "2024-10-15", "2024-10-22", "2024-10-23", "2024-10-29"

    def seed_mid_hold_roll_contract(self, atm_strike, entry_open, exit_close):
        """The contract S6 rolls into partway through the hold - the middle day's own 09:30 OPEN (a fresh sale, not
        a reused price) through the exit day's 09:30 CLOSE, listed expired-shaped like every other well-in-the-past roll."""
        ce_key, pe_key = f"NSE_FO|9101|{self.ROLL_EXPIRY}", f"NSE_FO|9102|{self.ROLL_EXPIRY}"
        storage.save_instrument_contracts([
            {"instrument_key": key, "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.ROLL_EXPIRY, "strike_price": atm_strike, "instrument_type": side, "lot_size": 65}
            for key, side in ((ce_key, "CE"), (pe_key, "PE"))
        ])
        storage.save_historical_candles(ce_key, [base.candle(self.MIDDLE, "09:30", entry_open, entry_open + 2, entry_open - 1, entry_open + 0.5)])
        storage.save_historical_candles(pe_key, [base.candle(self.MIDDLE, "09:30", entry_open, entry_open + 2, entry_open - 1, entry_open + 0.5)])
        storage.save_historical_candles(ce_key, [base.candle(self.EXIT_DAY, "09:30", exit_close - 1, exit_close + 1, exit_close - 2, exit_close)])
        storage.save_historical_candles(pe_key, [base.candle(self.EXIT_DAY, "09:30", exit_close - 1, exit_close + 1, exit_close - 2, exit_close)])
        return ce_key, pe_key

    def test_a_contract_that_expires_on_the_middle_day_is_bought_back_and_rolled_there_four_legs(self):
        # entered normally (not its own expiry day) but its own expiry IS the middle day of the two-night hold
        ce1, pe1 = self.seed_s1(self.ENTRY, self.MIDDLE, entry_ce=120.0, entry_pe=110.0, strike=25000.0)
        self.seed_s1(self.MIDDLE, self.ROLL_EXPIRY)  # gives ENTRY a next_day; MIDDLE's own row is irrelevant here
        self.seed_s1(self.EXIT_DAY, self.ROLL_EXPIRY)  # gives ENTRY (via MIDDLE) an exit_day
        self.seed_next_open(ce1, self.MIDDLE, close_price=80.0)  # leg1 bought back at the middle day's close (it expires that day)
        self.seed_next_open(pe1, self.MIDDLE, close_price=70.0)
        ce2, pe2 = self.seed_mid_hold_roll_contract(25000.0, entry_open=50.0, exit_close=40.0)
        rows, skipped = lab.s6_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == self.ENTRY)
        self.assertEqual((row["exit_date"], row["expiry"], row["rolled_to_next_expiry"], row["rolled_mid_hold"]), (self.EXIT_DAY, self.ROLL_EXPIRY, False, True))
        self.assertEqual(len(row["legs"]), 4)
        by_key = {leg["instrument_key"]: leg for leg in row["legs"]}
        self.assertEqual((by_key[ce1]["entry"], by_key[ce1]["exit"]), (120.0, 80.0), "leg1 CE: S1's own entry, bought back at the middle day's close")
        self.assertEqual((by_key[pe1]["entry"], by_key[pe1]["exit"]), (110.0, 70.0))
        self.assertEqual((by_key[ce2]["entry"], by_key[ce2]["exit"]), (50.0, 40.0), "leg2 CE: a FRESH sale at the middle day's open, bought back at the exit day's close")
        self.assertEqual((by_key[pe2]["entry"], by_key[pe2]["exit"]), (50.0, 40.0))
        self.assertTrue(all(leg["side"] == "SELL" for leg in row["legs"]))
        expected = lab.settle([{"side": "SELL", "entry": e, "exit": x} for e, x in ((120.0, 80.0), (110.0, 70.0), (50.0, 40.0), (50.0, 40.0))], 65, 1)
        self.assertEqual((row["net_pts"], row["net_rs"]), (expected["net_pts"], expected["net_rs"]))
        leg1_only = lab.settle([{"side": "SELL", "entry": 120.0, "exit": 80.0}, {"side": "SELL", "entry": 110.0, "exit": 70.0}], 65, 1)
        self.assertGreater(expected["charges_rs"], leg1_only["charges_rs"], "4 legs (8 orders) is strictly more turnover and orders than just leg1's pair, so strictly more charges")

    def test_a_mid_hold_roll_not_resolved_yet_is_reported_not_dropped(self):
        self.seed_s1(self.ENTRY, self.MIDDLE, entry_ce=120.0, entry_pe=110.0)
        self.seed_s1(self.MIDDLE, self.ROLL_EXPIRY)
        self.seed_s1(self.EXIT_DAY, self.ROLL_EXPIRY)
        ce1, pe1 = f"NSE_FO|CE{self.ENTRY}", f"NSE_FO|PE{self.ENTRY}"
        self.seed_next_open(ce1, self.MIDDLE, close_price=80.0)
        self.seed_next_open(pe1, self.MIDDLE, close_price=70.0)
        # the mid-hold roll's own next-week contracts are never listed
        rows, skipped = lab.s6_overnight_rows()
        self.assertEqual(rows, [])
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped if s["trading_date"] == self.ENTRY], [(self.ENTRY, "midpoint_not_rolled")])


class ThreeOvernightStrategyS7(base.TempDatabase):
    """S7: the same idea as S6, held for THREE overnights instead of two - sold at day D's 09:30, bought back at
    the THIRD trading day after D's 09:30 close. Backtest only (not paper-traded). The genuinely new case S6
    never had: the held contract's own expiry can fall on EITHER of the two days between entry and exit, and -
    rarely - on both of them in a row (see s7_overnight_rows()'s and _s7_segments()'s own docstrings)."""

    def setUp(self):
        super().setUp()
        lab._s7_cache.clear()

    def seed_s1(self, day, expiry, entry_ce=100.0, entry_pe=90.0, lot_size=65, strike=25000.0):
        ce_key, pe_key = f"NSE_FO|CE{day}", f"NSE_FO|PE{day}"
        legs = [{"side": "SELL", "entry": entry_ce, "exit": entry_ce - 5, "type": "CE", "strike": strike, "role": "body", "instrument_key": ce_key},
                {"side": "SELL", "entry": entry_pe, "exit": entry_pe - 5, "type": "PE", "strike": strike, "role": "body", "instrument_key": pe_key}]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, expiry, strike, entry_ce + entry_pe, legs, lot_size))
        return ce_key, pe_key

    def seed_next_open(self, key, day, close_price, open_price=None):
        storage.save_historical_candles(key, [base.candle(day, "09:30", open_price if open_price is not None else close_price + 1000, close_price + 1000, close_price - 1000, close_price)])

    def seed_roll_leg(self, expiry, atm_strike, entry_day, entry_open, exit_day, exit_close):
        """Registers a next-week ATM CE/PE contract (what resolve_s5_rolls()/resolve_s7_mid_rolls() would have
        listed) and its own entry/exit 09:30 candles - expired-shaped like every other well-in-the-past roll."""
        ce_key, pe_key = f"NSE_FO|{expiry}CE|{expiry}", f"NSE_FO|{expiry}PE|{expiry}"
        storage.save_instrument_contracts([
            {"instrument_key": key, "underlying_key": "NSE_INDEX|Nifty 50", "expiry": expiry, "strike_price": atm_strike, "instrument_type": side, "lot_size": 65}
            for key, side in ((ce_key, "CE"), (pe_key, "PE"))
        ])
        storage.save_historical_candles(ce_key, [base.candle(entry_day, "09:30", entry_open, entry_open + 2, entry_open - 1, entry_open + 0.5)])
        storage.save_historical_candles(pe_key, [base.candle(entry_day, "09:30", entry_open, entry_open + 2, entry_open - 1, entry_open + 0.5)])
        storage.save_historical_candles(ce_key, [base.candle(exit_day, "09:30", exit_close - 1, exit_close + 1, exit_close - 2, exit_close)])
        storage.save_historical_candles(pe_key, [base.candle(exit_day, "09:30", exit_close - 1, exit_close + 1, exit_close - 2, exit_close)])
        return ce_key, pe_key

    def test_a_continuous_hold_is_two_legs_s1s_entry_reused_and_the_exit_three_trading_days_later(self):
        ce, pe = self.seed_s1("2026-09-16", "2026-09-25", 100.0, 90.0)  # expiry well after the hold: no roll needed anywhere
        self.seed_s1("2026-09-17", "2026-09-25")
        self.seed_s1("2026-09-18", "2026-09-25")
        self.seed_s1("2026-09-21", "2026-09-25")
        self.seed_next_open(ce, "2026-09-21", close_price=70.0)
        self.seed_next_open(pe, "2026-09-21", close_price=60.0)
        rows, skipped = lab.s7_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == "2026-09-16")
        self.assertEqual((row["strategy"], row["exit_date"], row["rolled_to_next_expiry"], row["rolled_mid_hold"]), (lab.S7, "2026-09-21", False, False))
        self.assertEqual(len(row["legs"]), 2)
        self.assertEqual(sorted(leg["entry"] for leg in row["legs"]), [90.0, 100.0], "S1's own recorded entry, reused as-is")
        self.assertEqual(sorted(leg["exit"] for leg in row["legs"]), [60.0, 70.0])

    def test_own_expiry_entrant_is_reported_the_same_way_s5_and_s6_report_it(self):
        self.seed_s1("2026-09-15", "2026-09-15")  # own expiry day, roll not resolved
        self.seed_s1("2026-09-16", "2026-09-25")
        self.seed_s1("2026-09-17", "2026-09-25")
        self.seed_s1("2026-09-18", "2026-09-25")
        rows, skipped = lab.s7_overnight_rows()
        self.assertEqual(rows, [])
        self.assertIn(("2026-09-15", "own_expiry_not_rolled"), [(s["trading_date"], s["reason"]) for s in skipped])

    def test_the_trailing_three_days_are_reported_no_third_next_day_not_dropped(self):
        self.seed_s1("2026-09-16", "2026-09-25")
        self.seed_s1("2026-09-17", "2026-09-25")
        self.seed_s1("2026-09-18", "2026-09-25")
        rows, skipped = lab.s7_overnight_rows()
        self.assertEqual(rows, [])
        self.assertEqual({s["trading_date"] for s in skipped}, {"2026-09-16", "2026-09-17", "2026-09-18"})
        self.assertTrue(all(s["reason"] == "no_third_next_day" for s in skipped))

    # Well in the past for the same reason as OvernightStrategyS5's own roll tests: find_option_contract()'s
    # lapsed/live key-shape distinction depends on the real wall clock.
    ENTRY, MID1, MID2, EXIT_DAY, ROLL_EXPIRY = "2024-10-15", "2024-10-22", "2024-10-23", "2024-10-24", "2024-10-29"

    def test_a_contract_expiring_on_the_first_middle_day_is_rolled_there_four_legs(self):
        ce1, pe1 = self.seed_s1(self.ENTRY, self.MID1, entry_ce=120.0, entry_pe=110.0)  # own expiry IS the first middle day
        self.seed_s1(self.MID1, self.ROLL_EXPIRY)
        self.seed_s1(self.MID2, self.ROLL_EXPIRY)
        self.seed_s1(self.EXIT_DAY, self.ROLL_EXPIRY)
        self.seed_next_open(ce1, self.MID1, close_price=80.0)  # leg1 bought back at MID1's close (it expires that day)
        self.seed_next_open(pe1, self.MID1, close_price=70.0)
        ce2, pe2 = self.seed_roll_leg(self.ROLL_EXPIRY, 25000.0, self.MID1, entry_open=50.0, exit_day=self.EXIT_DAY, exit_close=40.0)
        rows, skipped = lab.s7_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == self.ENTRY)
        self.assertEqual((row["exit_date"], row["expiry"], row["rolled_to_next_expiry"], row["rolled_mid_hold"]), (self.EXIT_DAY, self.ROLL_EXPIRY, False, True))
        self.assertEqual(len(row["legs"]), 4)
        by_key = {leg["instrument_key"]: leg for leg in row["legs"]}
        self.assertEqual((by_key[ce1]["entry"], by_key[ce1]["exit"]), (120.0, 80.0))
        self.assertEqual((by_key[pe1]["entry"], by_key[pe1]["exit"]), (110.0, 70.0))
        self.assertEqual((by_key[ce2]["entry"], by_key[ce2]["exit"]), (50.0, 40.0), "a FRESH sale at the first middle day's open, bought back at the exit day's close")
        self.assertEqual((by_key[pe2]["entry"], by_key[pe2]["exit"]), (50.0, 40.0))

    def test_a_contract_expiring_on_the_second_middle_day_is_rolled_there_instead_four_legs(self):
        """The case S6 could never have (it only ever had ONE middle day): S1's own contract survives the FIRST
        middle day untouched and only expires on the SECOND."""
        ce1, pe1 = self.seed_s1(self.ENTRY, self.MID2, entry_ce=120.0, entry_pe=110.0)  # own expiry IS the second middle day
        self.seed_s1(self.MID1, self.ROLL_EXPIRY)
        self.seed_s1(self.MID2, self.ROLL_EXPIRY)
        self.seed_s1(self.EXIT_DAY, self.ROLL_EXPIRY)
        self.seed_next_open(ce1, self.MID2, close_price=80.0)  # leg1 bought back at MID2's close, NOT MID1's
        self.seed_next_open(pe1, self.MID2, close_price=70.0)
        ce2, pe2 = self.seed_roll_leg(self.ROLL_EXPIRY, 25000.0, self.MID2, entry_open=50.0, exit_day=self.EXIT_DAY, exit_close=40.0)
        rows, skipped = lab.s7_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == self.ENTRY)
        self.assertEqual((row["exit_date"], row["expiry"], row["rolled_to_next_expiry"], row["rolled_mid_hold"]), (self.EXIT_DAY, self.ROLL_EXPIRY, False, True))
        self.assertEqual(len(row["legs"]), 4)
        by_key = {leg["instrument_key"]: leg for leg in row["legs"]}
        self.assertEqual((by_key[ce1]["entry"], by_key[ce1]["exit"]), (120.0, 80.0))
        self.assertEqual((by_key[ce2]["entry"], by_key[ce2]["exit"]), (50.0, 40.0), "held from the SECOND middle day's open, not the first")

    def test_a_contract_expiring_on_both_middle_days_in_a_row_rolls_twice_six_legs(self):
        """The rare case documented but not assumed away: two weekly expiries fall inside one entry-to-exit window."""
        ce1, pe1 = self.seed_s1(self.ENTRY, self.MID1, entry_ce=120.0, entry_pe=110.0)  # rolls once at MID1...
        self.seed_s1(self.MID1, self.MID2)  # the roll target (see below) also happens to expire on MID2
        self.seed_s1(self.MID2, "2024-11-05")
        self.seed_s1(self.EXIT_DAY, "2024-11-05")
        self.seed_next_open(ce1, self.MID1, close_price=80.0)
        self.seed_next_open(pe1, self.MID1, close_price=70.0)
        ce2, pe2 = self.seed_roll_leg(self.MID2, 25000.0, self.MID1, entry_open=55.0, exit_day=self.MID2, exit_close=45.0)  # ...and rolls AGAIN at MID2
        ce3, pe3 = self.seed_roll_leg("2024-11-05", 25000.0, self.MID2, entry_open=35.0, exit_day=self.EXIT_DAY, exit_close=25.0)
        rows, skipped = lab.s7_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == self.ENTRY)
        self.assertEqual((row["exit_date"], row["expiry"], row["rolled_mid_hold"]), (self.EXIT_DAY, "2024-11-05", True))
        self.assertEqual(len(row["legs"]), 6)
        by_key = {leg["instrument_key"]: leg for leg in row["legs"]}
        self.assertEqual((by_key[ce1]["entry"], by_key[ce1]["exit"]), (120.0, 80.0))
        self.assertEqual((by_key[ce2]["entry"], by_key[ce2]["exit"]), (55.0, 45.0), "second leg: sold at MID1's open, bought back at MID2's close (it expires there too)")
        self.assertEqual((by_key[ce3]["entry"], by_key[ce3]["exit"]), (35.0, 25.0), "third leg: sold at MID2's open, held to the exit day's close")
        expected = lab.settle([{"side": "SELL", "entry": e, "exit": x} for e, x in (
            (120.0, 80.0), (110.0, 70.0), (55.0, 45.0), (55.0, 45.0), (35.0, 25.0), (35.0, 25.0),
        )], 65, 1)
        self.assertEqual((row["net_pts"], row["net_rs"]), (expected["net_pts"], expected["net_rs"]))

    def test_a_mid_hold_roll_not_resolved_yet_is_reported_not_dropped(self):
        self.seed_s1(self.ENTRY, self.MID1, entry_ce=120.0, entry_pe=110.0)
        self.seed_s1(self.MID1, self.ROLL_EXPIRY)
        self.seed_s1(self.MID2, self.ROLL_EXPIRY)
        self.seed_s1(self.EXIT_DAY, self.ROLL_EXPIRY)
        ce1, pe1 = f"NSE_FO|CE{self.ENTRY}", f"NSE_FO|PE{self.ENTRY}"
        self.seed_next_open(ce1, self.MID1, close_price=80.0)
        self.seed_next_open(pe1, self.MID1, close_price=70.0)
        # the mid-hold roll's own next-week contracts are never listed
        rows, skipped = lab.s7_overnight_rows()
        self.assertEqual(rows, [])
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped if s["trading_date"] == self.ENTRY], [(self.ENTRY, "midpoint_not_rolled")])


class FilteredOvernightStrategyS8(base.TempDatabase):
    """S8: S5 itself (see s5_overnight_rows()), just never entered on a Friday - a weekend hold, not a
    single-trading-day one. Everything else, including the expiry-day roll into next week's ATM strike, is
    identical to S5: this is a pure filter over s5_overnight_rows()'s own result, sharing its cache, its roll
    machinery and its candle-backfill (see s8_overnight_rows()'s own docstring). Backtest only."""

    def seed_s1(self, day, expiry, entry_ce=100.0, entry_pe=90.0, lot_size=65):
        ce_key, pe_key = f"NSE_FO|CE{day}", f"NSE_FO|PE{day}"
        legs = [{"side": "SELL", "entry": entry_ce, "exit": entry_ce - 5, "type": "CE", "strike": 25000.0, "role": "body", "instrument_key": ce_key},
                {"side": "SELL", "entry": entry_pe, "exit": entry_pe - 5, "type": "PE", "strike": 25000.0, "role": "body", "instrument_key": pe_key}]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, expiry, 25000.0, entry_ce + entry_pe, legs, lot_size))
        return ce_key, pe_key

    def seed_next_open(self, key, day, close_price, open_price=None):
        storage.save_historical_candles(key, [base.candle(day, "09:30", open_price if open_price is not None else close_price + 1000, close_price + 1000, close_price - 1000, close_price)])

    def test_a_normal_entrant_is_two_legs_s1s_entry_reused_and_the_exit_next_day_close(self):
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)  # Tuesday, not its own expiry day
        self.seed_s1("2026-09-16", "2026-09-22")  # Wednesday - gives 09-15 a next trading day
        self.seed_next_open(ce, "2026-09-16", close_price=70.0)
        self.seed_next_open(pe, "2026-09-16", close_price=60.0)
        rows, skipped = lab.s8_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == "2026-09-15")
        self.assertEqual((row["strategy"], row["exit_date"], row["rolled_to_next_expiry"]), (lab.S8, "2026-09-16", False))
        self.assertEqual(len(row["legs"]), 2)
        self.assertEqual(sorted(leg["entry"] for leg in row["legs"]), [90.0, 100.0], "S1's own recorded entry, reused as-is")
        self.assertEqual(sorted(leg["exit"] for leg in row["legs"]), [60.0, 70.0])

    def test_a_friday_entrant_is_not_entered_reported_not_dropped(self):
        ce, pe = self.seed_s1("2026-09-18", "2026-09-22")  # a Friday, not its own expiry day
        self.seed_s1("2026-09-21", "2026-09-22")  # gives 09-18 a next trading day
        self.seed_next_open(ce, "2026-09-21", close_price=70.0)
        self.seed_next_open(pe, "2026-09-21", close_price=60.0)
        rows, skipped = lab.s8_overnight_rows()
        self.assertEqual([r["trading_date"] for r in rows], [], "S5 would have a real row here - S8 filters it out")
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped if s["trading_date"] == "2026-09-18"], [("2026-09-18", "not_entered_friday")])
        s5_rows, _ = lab.s5_overnight_rows()
        self.assertEqual([r["trading_date"] for r in s5_rows], ["2026-09-18"], "confirms S5 itself DOES enter here - only S8 skips it")

    def test_the_trailing_day_is_reported_no_next_day_not_dropped(self):
        self.seed_s1("2026-09-15", "2026-09-22")
        rows, skipped = lab.s8_overnight_rows()
        self.assertEqual(rows, [])
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped], [("2026-09-15", "no_next_day")])

    def test_a_leg_missing_its_next_day_0930_candle_is_skipped_not_dropped(self):
        self.seed_s1("2026-09-15", "2026-09-22")
        self.seed_s1("2026-09-16", "2026-09-22")
        # no candle (or tick) ever seeded for 2026-09-16 - the exit close is genuinely missing
        rows, skipped = lab.s8_overnight_rows()
        self.assertEqual(rows, [])
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped if s["trading_date"] == "2026-09-15"], [("2026-09-15", "no_0930_next_day_candle")])

    def test_s1s_own_stored_row_is_untouched(self):
        self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        before = lab.backtest_daily("straddle_sell", None, None, 1)
        lab.s8_overnight_rows()
        after = lab.backtest_daily("straddle_sell", None, None, 1)
        self.assertEqual(before, after)

    # Well in the past for the same reason as OvernightStrategyS5's own roll tests: find_option_contract()'s
    # lapsed/live key-shape distinction depends on the real wall clock.
    ROLL_ENTRY, ROLL_NEXT_DAY, ROLL_EXPIRY = "2024-10-17", "2024-10-18", "2024-10-24"  # Thursday -> Friday

    def seed_roll_contract(self, atm_strike, roll_expiry, entry_day, entry_open, exit_day, exit_close, lot_size=65):
        ce_key, pe_key = f"NSE_FO|9001|{roll_expiry}", f"NSE_FO|9002|{roll_expiry}"
        storage.save_instrument_contracts([
            {"instrument_key": key, "underlying_key": "NSE_INDEX|Nifty 50", "expiry": roll_expiry, "strike_price": atm_strike, "instrument_type": side, "lot_size": lot_size}
            for key, side in ((ce_key, "CE"), (pe_key, "PE"))
        ])
        storage.save_historical_candles(ce_key, [base.candle(entry_day, "09:30", entry_open, entry_open + 2, entry_open - 1, entry_open + 0.5)])
        storage.save_historical_candles(pe_key, [base.candle(entry_day, "09:30", entry_open, entry_open + 2, entry_open - 1, entry_open + 0.5)])
        storage.save_historical_candles(ce_key, [base.candle(exit_day, "09:30", exit_close - 1, exit_close + 1, exit_close - 2, exit_close)])
        storage.save_historical_candles(pe_key, [base.candle(exit_day, "09:30", exit_close - 1, exit_close + 1, exit_close - 2, exit_close)])
        return ce_key, pe_key

    def test_an_expiry_day_entrant_still_rolls_into_next_weeks_atm_exactly_like_s5(self):
        self.seed_s1(self.ROLL_ENTRY, self.ROLL_ENTRY, entry_ce=100.0, entry_pe=90.0)  # own expiry day, Thursday
        self.seed_s1(self.ROLL_NEXT_DAY, "2024-10-24")  # gives it a next trading day
        ce, pe = self.seed_roll_contract(25000.0, self.ROLL_EXPIRY, self.ROLL_ENTRY, entry_open=120.0, exit_day=self.ROLL_NEXT_DAY, exit_close=80.0)
        rows, skipped = lab.s8_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == self.ROLL_ENTRY)
        self.assertEqual((row["strategy"], row["exit_date"], row["expiry"], row["rolled_to_next_expiry"]), (lab.S8, self.ROLL_NEXT_DAY, self.ROLL_EXPIRY, True))
        self.assertEqual({leg["instrument_key"] for leg in row["legs"]}, {ce, pe}, "the NEXT week's contracts, not S1's own expiring ones")
        self.assertEqual(sorted(leg["entry"] for leg in row["legs"]), [120.0, 120.0])
        self.assertEqual(sorted(leg["exit"] for leg in row["legs"]), [80.0, 80.0])

    FRIDAY_ROLL_ENTRY, FRIDAY_ROLL_NEXT_DAY, FRIDAY_ROLL_EXPIRY = "2024-10-25", "2024-10-28", "2024-11-01"  # Friday -> Monday

    def test_a_friday_expiry_day_entrant_whose_roll_resolved_is_still_filtered_as_friday(self):
        """The roll resolving successfully means S5 itself has a real row here - so S8's OWN reason (it is a
        Friday) is what should show, not S5's roll machinery reporting anything."""
        self.seed_s1(self.FRIDAY_ROLL_ENTRY, self.FRIDAY_ROLL_ENTRY, entry_ce=100.0, entry_pe=90.0)  # own expiry day AND a Friday
        self.seed_s1(self.FRIDAY_ROLL_NEXT_DAY, "2024-11-01")
        self.seed_roll_contract(25000.0, self.FRIDAY_ROLL_EXPIRY, self.FRIDAY_ROLL_ENTRY, entry_open=120.0, exit_day=self.FRIDAY_ROLL_NEXT_DAY, exit_close=80.0)
        rows, skipped = lab.s8_overnight_rows()
        self.assertEqual([r["trading_date"] for r in rows if r["trading_date"] == self.FRIDAY_ROLL_ENTRY], [])
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped if s["trading_date"] == self.FRIDAY_ROLL_ENTRY], [(self.FRIDAY_ROLL_ENTRY, "not_entered_friday")])

    def test_a_friday_expiry_day_entrant_whose_roll_is_unresolved_keeps_s5s_own_reason(self):
        """The roll is NOT resolved, so S5 itself has no row here yet either - S8 must not claim "not entered
        because Friday" for a day that was never computable in the first place, regardless of weekday."""
        self.seed_s1(self.FRIDAY_ROLL_ENTRY, self.FRIDAY_ROLL_ENTRY, entry_ce=100.0, entry_pe=90.0)
        self.seed_s1(self.FRIDAY_ROLL_NEXT_DAY, "2024-11-01")
        # the roll's own next-week contracts are never listed
        rows, skipped = lab.s8_overnight_rows()
        self.assertEqual([r["trading_date"] for r in rows if r["trading_date"] == self.FRIDAY_ROLL_ENTRY], [])
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped if s["trading_date"] == self.FRIDAY_ROLL_ENTRY], [(self.FRIDAY_ROLL_ENTRY, "own_expiry_not_rolled")])



class FilteredOvernightStrategyS10(base.TempDatabase):
    """S10: S8 itself, also never entered in December - a pure filter over s8_overnight_rows(). Backtest only."""

    def seed_day(self, day, next_day, expiry="2026-12-29"):
        ce, pe = f"NSE_FO|CE{day}", f"NSE_FO|PE{day}"
        legs = [{"side": "SELL", "entry": 100.0, "exit": 95.0, "type": t, "strike": 25000.0, "role": "body", "instrument_key": k} for k, t in ((ce, "CE"), (pe, "PE"))]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, expiry, 25000.0, 200.0, legs, 65))
        for key in (ce, pe):
            storage.save_historical_candles(key, [base.candle(next_day, "09:30", 90.0, 91.0, 60.0, 70.0)])

    def test_december_entries_are_reported_not_dropped_and_other_days_match_s8(self):
        self.seed_day("2025-11-27", "2025-11-28", expiry="2025-12-02")  # Thursday in November
        self.seed_day("2025-11-28", "2025-12-01", expiry="2025-12-02")  # Friday: S8 already skips it
        self.seed_day("2025-12-01", "2025-12-02", expiry="2025-12-09")  # Monday in December
        self.seed_day("2025-12-02", "2025-12-03", expiry="2025-12-09")
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", "2025-12-03", "2025-12-09", 25000.0, 200.0, [], 65))  # gives 12-02 a next day
        s8_rows, _ = lab.s8_overnight_rows()
        rows, skipped = lab.s10_overnight_rows()
        self.assertEqual([r["trading_date"] for r in rows], ["2025-11-27"])
        self.assertEqual({r["strategy"] for r in rows}, {lab.S10})
        s8_nov = next(r for r in s8_rows if r["trading_date"] == "2025-11-27")
        self.assertEqual((rows[0]["net_rs"], rows[0]["legs"]), (s8_nov["net_rs"], s8_nov["legs"]), "identical to S8 on a day it keeps")
        reasons = {s["trading_date"]: s["reason"] for s in skipped}
        self.assertEqual(reasons["2025-11-28"], "not_entered_friday", "S8's own reason is kept")
        self.assertEqual(reasons["2025-12-01"], "not_entered_december")
        self.assertEqual(reasons["2025-12-02"], "not_entered_december")

    def test_it_is_served_with_every_other_strategy(self):
        self.seed_day("2025-11-27", "2025-11-28", expiry="2025-12-02")
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", "2025-11-28", "2025-12-02", 25000.0, 200.0, [], 65))
        served = [r for r in lab.backtest_daily(None, None, None, 1) if r["strategy"] == lab.S10]
        self.assertEqual([r["trading_date"] for r in served], ["2025-11-27"])
        self.assertIn(lab.S10, lab.derived_skips())

class OvernightStrategyS5(base.TempDatabase):
    """S5: the same ATM straddle S1 sells at 09:30, held overnight and closed at the NEXT trading day's 09:30 candle
    close instead of the same day - a computed strategy (not stored, not derived from a pick between two others), with
    no single day's window (day charts leave it out; see day_chart.py's own S5 chart)."""

    def seed_s1(self, day, expiry, entry_ce=100.0, entry_pe=90.0, lot_size=65):
        ce_key, pe_key = f"NSE_FO|CE{day}", f"NSE_FO|PE{day}"
        legs = [{"side": "SELL", "entry": entry_ce, "exit": entry_ce - 5, "type": "CE", "strike": 25000.0, "role": "body", "instrument_key": ce_key},
                {"side": "SELL", "entry": entry_pe, "exit": entry_pe - 5, "type": "PE", "strike": 25000.0, "role": "body", "instrument_key": pe_key}]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, expiry, 25000.0, entry_ce + entry_pe, legs, lot_size))
        return ce_key, pe_key

    def seed_next_open(self, key, day, close_price, open_price=None):
        """A 09:30 candle on `day` with a CLOSE distinct from its OPEN, so a test that reads the wrong one is caught."""
        storage.save_historical_candles(key, [base.candle(day, "09:30", open_price if open_price is not None else close_price + 1000, close_price + 1000, close_price - 1000, close_price)])

    def seed_ticks(self, key, day, prices, minute="09:30"):
        """Live ticks (fractional-second received_at, no downloaded candle) at the given minute - what the app
        already has in real time before Upstox publishes that day's finalized candle. `prices` in arrival order;
        the fallback takes the FIRST as the open, the LAST as the close, exactly like day_chart.py's own ticks
        fallback and _0930_from_ticks()."""
        storage.initialize_database()
        with storage.get_connection() as connection:
            for i, price in enumerate(prices):
                connection.execute(
                    "INSERT INTO market_observations (received_at, trading_date, instrument_key, ltp, payload) VALUES (?, ?, ?, ?, '{}')",
                    (f"{day}T{minute}:{i:02d}.{i:06d}+05:30", day, key, price),
                )

    def test_sold_at_s1s_own_entry_and_bought_back_at_the_next_days_0930_close(self):
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)  # gives 09-15 a next trading day
        self.seed_next_open(ce, "2026-09-16", close_price=70.0, open_price=200.0)  # open is a decoy: CLOSE must be used
        self.seed_next_open(pe, "2026-09-16", close_price=60.0, open_price=300.0)
        rows, skipped = lab.s5_overnight_rows()
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped], [("2026-09-16", "no_next_day")])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["strategy"], row["trading_date"], row["exit_date"]), (lab.S5, "2026-09-15", "2026-09-16"))
        self.assertTrue(all(leg["side"] == "SELL" for leg in row["legs"]), "the same SELL side S1 used, not flipped")
        self.assertEqual([leg["entry"] for leg in row["legs"]], [100.0, 90.0], "S1's own entry price, untouched")
        self.assertEqual([leg["exit"] for leg in row["legs"]], [70.0, 60.0], "the 09:30 candle's CLOSE the next day, not its open")
        self.assertEqual(row["gross_pts"], (100.0 - 70.0) + (90.0 - 60.0))  # SELL: entry - exit, collected at 09:30 and paid to close

    def test_a_missing_exit_candle_falls_back_to_the_last_live_tick_in_the_0930_minute(self):
        """The same "candle first, live ticks as a fallback" rule day_chart.py's contract_bars() already uses -
        today's finalized candle is not on Upstox's historical endpoint yet, but the live ticks are already
        stored, so the day is not left blank waiting for a download that has not landed."""
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        # no downloaded candle for 2026-09-16 - only live ticks in the 09:30 minute
        self.seed_ticks(ce, "2026-09-16", [71.0, 70.5, 70.0])  # last tick wins for a close
        self.seed_ticks(pe, "2026-09-16", [59.0, 59.5, 60.0])
        rows, skipped = lab.s5_overnight_rows()
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped], [("2026-09-16", "no_next_day")])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(sorted(leg["exit"] for leg in row["legs"]), [60.0, 70.0], "the LAST tick in the minute, not the first")
        self.assertTrue(all(leg["exit_estimated"] for leg in row["legs"]), "priced from ticks, not a downloaded candle: marked estimated")
        self.assertFalse(any(leg["entry_estimated"] for leg in row["legs"]), "the entry is S1's own reused value, never estimated")

    def test_a_downloaded_candle_is_preferred_over_ticks_when_both_exist(self):
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        self.seed_next_open(ce, "2026-09-16", close_price=70.0)
        self.seed_next_open(pe, "2026-09-16", close_price=60.0)
        self.seed_ticks(ce, "2026-09-16", [999.0])  # a decoy: the real candle must win, not this
        self.seed_ticks(pe, "2026-09-16", [999.0])
        rows, _ = lab.s5_overnight_rows()
        row = rows[0]
        self.assertEqual(sorted(leg["exit"] for leg in row["legs"]), [60.0, 70.0])
        self.assertFalse(any(leg["entry_estimated"] or leg["exit_estimated"] for leg in row["legs"]), "a real candle exists: nothing here is an estimate")

    def test_consecutive_days_holding_the_same_contract_each_exit_at_their_own_next_days_close(self):
        """1 Oct and 5 Oct 2026 both sold the same 22550 CE/PE (6 Oct expiry): 1 Oct exits at 5 Oct's 09:30 close and
        5 Oct at 6 Oct's. With prices keyed by contract alone, 1 Oct was given 6 Oct's close too."""
        ce, pe = "NSE_FO|40699", "NSE_FO|40700"
        for day, entry in (("2026-09-15", 100.0), ("2026-09-16", 80.0), ("2026-09-17", 1.0)):
            legs = [{"side": "SELL", "entry": entry, "exit": entry - 5, "type": type_, "strike": 25000.0, "role": "body", "instrument_key": key}
                    for key, type_ in ((ce, "CE"), (pe, "PE"))]
            storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, "2026-09-22", 25000.0, 2 * entry, legs, 65))
        for key in (ce, pe):
            self.seed_next_open(key, "2026-09-16", close_price=70.0)
            self.seed_next_open(key, "2026-09-17", close_price=40.0)
        rows, _ = lab.s5_overnight_rows()
        self.assertEqual({r["trading_date"]: [leg["exit"] for leg in r["legs"]] for r in rows},
                         {"2026-09-15": [70.0, 70.0], "2026-09-16": [40.0, 40.0]})

    def test_s5_missing_candles_still_reports_the_gap_even_when_a_tick_estimate_filled_the_row(self):
        """A tick estimate lets s5_overnight_rows() show a number today, but it is not the real candle - the build
        job must keep trying to fetch that until it actually lands, not think the gap is closed."""
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        self.seed_ticks(ce, "2026-09-16", [70.0])
        self.seed_ticks(pe, "2026-09-16", [60.0])
        rows, skipped = lab.s5_overnight_rows()
        self.assertEqual(len(rows), 1, "the tick estimate did let it resolve")
        self.assertEqual(sorted(lab.s5_missing_candles()), sorted([(ce, "2026-09-16", "2026-09-22"), (pe, "2026-09-16", "2026-09-22")]), "but the real candle is still wanted")

    def test_s1s_own_stored_row_is_untouched(self):
        self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        before = lab.backtest_daily("straddle_sell", None, None, 1)
        lab.s5_overnight_rows()
        after = lab.backtest_daily("straddle_sell", None, None, 1)
        self.assertEqual(before, after)

    def test_entered_on_its_own_expiry_rolls_into_next_weeks_atm_when_resolved_and_is_reported_when_not(self):
        self.seed_s1("2026-09-15", "2026-09-15")  # trading_date == expiry: nothing left to hold overnight in THIS contract
        self.seed_s1("2026-09-16", "2026-09-22")  # the most recent day: no next trading day stored yet
        rows, skipped = lab.s5_overnight_rows()
        self.assertEqual(rows, [])
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped], [("2026-09-15", "own_expiry_not_rolled"), ("2026-09-16", "no_next_day")])

    # A roll's dates are all well in the past (unlike the rest of this class, which follows the project's fictional
    # "today"), because find_option_contract()'s lapsed/live key-shape distinction depends on the REAL wall clock via
    # _today_iso() - see the identical note on test_straddle.OvernightCandlesBuild.
    ROLL_ENTRY, ROLL_NEXT_DAY, ROLL_EXPIRY = "2024-10-17", "2024-10-18", "2024-10-24"

    def seed_roll_contract(self, atm_strike, roll_expiry, entry_day, entry_open, exit_day, exit_close, lot_size=65):
        """Registers the next-week ATM CE/PE instrument_contracts row (what resolve_s5_rolls() would have listed)
        and their entry/exit 09:30 candles, so s5_overnight_rows() can resolve and price the roll purely from storage.
        The key shape (two pipes) matches an already-lapsed expired-contract twin, since roll_expiry here is in the
        past relative to the real wall clock - see find_option_contract()'s docstring for why the shape matters."""
        ce_key, pe_key = f"NSE_FO|9001|{roll_expiry}", f"NSE_FO|9002|{roll_expiry}"
        storage.save_instrument_contracts([
            {"instrument_key": key, "underlying_key": "NSE_INDEX|Nifty 50", "expiry": roll_expiry, "strike_price": atm_strike, "instrument_type": side, "lot_size": lot_size}
            for key, side in ((ce_key, "CE"), (pe_key, "PE"))
        ])
        storage.save_historical_candles(ce_key, [base.candle(entry_day, "09:30", entry_open, entry_open + 2, entry_open - 1, entry_open + 0.5)])
        storage.save_historical_candles(pe_key, [base.candle(entry_day, "09:30", entry_open, entry_open + 2, entry_open - 1, entry_open + 0.5)])
        storage.save_historical_candles(ce_key, [base.candle(exit_day, "09:30", exit_close - 1, exit_close + 1, exit_close - 2, exit_close)])
        storage.save_historical_candles(pe_key, [base.candle(exit_day, "09:30", exit_close - 1, exit_close + 1, exit_close - 2, exit_close)])
        return ce_key, pe_key

    def test_a_rolled_expiry_day_entrant_sells_the_same_atm_strike_in_the_next_weeks_expiry(self):
        self.seed_s1(self.ROLL_ENTRY, self.ROLL_ENTRY, entry_ce=100.0, entry_pe=90.0)  # own expiry day, ATM strike 25000
        self.seed_s1(self.ROLL_NEXT_DAY, "2024-10-24")  # gives it a next trading day
        ce, pe = self.seed_roll_contract(25000.0, self.ROLL_EXPIRY, self.ROLL_ENTRY, entry_open=120.0, exit_day=self.ROLL_NEXT_DAY, exit_close=80.0)
        rows, skipped = lab.s5_overnight_rows()
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped], [(self.ROLL_NEXT_DAY, "no_next_day")], "the entry rolled cleanly; only the trailing day (no next day yet) is left out")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["strategy"], row["trading_date"], row["exit_date"], row["expiry"], row["atm_strike"]), (lab.S5, self.ROLL_ENTRY, self.ROLL_NEXT_DAY, self.ROLL_EXPIRY, 25000.0))
        self.assertTrue(row["rolled_to_next_expiry"])
        self.assertTrue(all(leg["side"] == "SELL" for leg in row["legs"]))
        self.assertEqual({leg["instrument_key"] for leg in row["legs"]}, {ce, pe}, "the NEXT week's contracts, not S1's own expiring ones")
        self.assertEqual(sorted(leg["entry"] for leg in row["legs"]), [120.0, 120.0], "the rolled contract's OWN 09:30 open, not S1's entry")
        self.assertEqual(sorted(leg["exit"] for leg in row["legs"]), [80.0, 80.0], "the next day's 09:30 close")

    def test_a_rolled_entrys_entry_and_exit_are_estimated_independently(self):
        """A rolled contract's entry and exit are two unrelated lookups (its own 09:30 open on the entry day, its
        own 09:30 close on the next day) - each can independently come from a candle or fall back to ticks."""
        self.seed_s1(self.ROLL_ENTRY, self.ROLL_ENTRY, entry_ce=100.0, entry_pe=90.0)
        self.seed_s1(self.ROLL_NEXT_DAY, "2024-10-24")
        ce, pe = f"NSE_FO|9001|{self.ROLL_EXPIRY}", f"NSE_FO|9002|{self.ROLL_EXPIRY}"
        storage.save_instrument_contracts([
            {"instrument_key": key, "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.ROLL_EXPIRY, "strike_price": 25000.0, "instrument_type": side, "lot_size": 65}
            for key, side in ((ce, "CE"), (pe, "PE"))
        ])
        self.seed_ticks(ce, self.ROLL_ENTRY, [120.0])  # entry: only a tick, no downloaded candle
        self.seed_ticks(pe, self.ROLL_ENTRY, [110.0])
        self.seed_next_open(ce, self.ROLL_NEXT_DAY, close_price=80.0)  # exit: a real candle
        self.seed_next_open(pe, self.ROLL_NEXT_DAY, close_price=75.0)
        rows, _ = lab.s5_overnight_rows()
        self.assertEqual(len(rows), 1)
        for leg in rows[0]["legs"]:
            self.assertTrue(leg["entry_estimated"], "entry came from a tick")
            self.assertFalse(leg["exit_estimated"], "exit came from a real candle")
        self.assertEqual(sorted(leg["entry"] for leg in rows[0]["legs"]), [110.0, 120.0])
        self.assertEqual(sorted(leg["exit"] for leg in rows[0]["legs"]), [75.0, 80.0])

    def test_a_roll_not_resolved_yet_is_reported_and_a_roll_missing_only_its_candles_is_reported_differently(self):
        self.seed_s1(self.ROLL_ENTRY, self.ROLL_ENTRY, entry_ce=100.0, entry_pe=90.0)
        self.seed_s1(self.ROLL_NEXT_DAY, "2024-10-24")
        rows, skipped = lab.s5_overnight_rows()
        self.assertEqual(rows, [])
        self.assertEqual([s["reason"] for s in skipped if s["trading_date"] == self.ROLL_ENTRY], ["own_expiry_not_rolled"])
        storage.save_instrument_contracts([
            {"instrument_key": f"NSE_FO|9001|{self.ROLL_EXPIRY}", "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.ROLL_EXPIRY, "strike_price": 25000.0, "instrument_type": "CE", "lot_size": 65},
            {"instrument_key": f"NSE_FO|9002|{self.ROLL_EXPIRY}", "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.ROLL_EXPIRY, "strike_price": 25000.0, "instrument_type": "PE", "lot_size": 65},
        ])
        # contracts are now listed but no candles were seeded for them
        lab._s5_cache.clear()  # this test mutates storage directly between calls, not through a path that clears it itself
        rows, skipped = lab.s5_overnight_rows()
        self.assertEqual(rows, [])
        self.assertEqual([s["reason"] for s in skipped if s["trading_date"] == self.ROLL_ENTRY], ["no_0930_entry_candle"])

    def test_a_leg_missing_its_next_day_0930_candle_is_skipped_not_dropped_silently(self):
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22")
        self.seed_s1("2026-09-16", "2026-09-22")
        self.seed_next_open(ce, "2026-09-16", close_price=70.0)
        # no candle at all for pe on 2026-09-16
        rows, skipped = lab.s5_overnight_rows()
        self.assertEqual(rows, [])
        self.assertEqual([(s["trading_date"], s["reason"]) for s in skipped], [("2026-09-15", "no_0930_next_day_candle"), ("2026-09-16", "no_next_day")])

    def test_a_row_with_no_stored_instrument_key_is_skipped_not_crashed(self):
        """A candle backtest row can (rarely) lack instrument_key on a leg; s5 must skip it, never raise."""
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", "2026-09-15", "2026-09-22", 25000.0, 190.0,
                                                       [{"side": "SELL", "entry": 100.0, "exit": 95.0, "type": "CE", "strike": 25000.0, "role": "body"}], 65))
        self.seed_s1("2026-09-16", "2026-09-22")
        rows, skipped = lab.s5_overnight_rows()  # must not raise
        self.assertEqual(rows, [])
        self.assertEqual(skipped[0]["trading_date"], "2026-09-15")

    def test_appears_through_backtest_daily_alone_and_in_the_loop_all_pass(self):
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        self.seed_next_open(ce, "2026-09-16", close_price=70.0)
        self.seed_next_open(pe, "2026-09-16", close_price=60.0)
        direct = lab.backtest_daily(lab.S5, None, None, 1)
        self.assertEqual([r["trading_date"] for r in direct], ["2026-09-15"])
        everything = lab.backtest_daily(None, None, None, 1)
        self.assertIn(lab.S5, {r["strategy"] for r in everything})
        self.assertEqual([r for r in everything if r["strategy"] == lab.S5], direct)

    def test_nse_daily_basis_does_not_apply_and_returns_nothing(self):
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        self.seed_next_open(ce, "2026-09-16", close_price=70.0)
        self.seed_next_open(pe, "2026-09-16", close_price=60.0)
        self.assertEqual(lab.s5_overnight_rows(basis=lab.NSE_DAILY), ([], []))
        self.assertEqual(lab.backtest_daily(lab.S5, None, None, 1, basis=lab.NSE_DAILY), [])

    def test_is_in_lab_strategies_but_not_same_day_strategies(self):
        self.assertIn(lab.S5, lab.LAB_STRATEGIES)
        self.assertNotIn(lab.S5, lab.SAME_DAY_STRATEGIES)

    def test_day_chart_explains_itself_instead_of_computing_something_wrong_when_it_cannot_be_drawn(self):
        self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)  # no next day stored yet - the same reason s5_overnight_rows() itself reports
        data = day_chart.day_chart("2026-09-15", lab.S5)
        self.assertFalse(data["available"])
        self.assertIn("no later trading day", data["reason"].lower())

    def test_day_chart_draws_the_entry_day_and_the_exit_day_separately(self):
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        self.seed_next_open(ce, "2026-09-15", close_price=999.0, open_price=100.0)  # the entry day's own 09:30 candle, matching the recorded entry
        self.seed_next_open(pe, "2026-09-15", close_price=999.0, open_price=90.0)
        self.seed_next_open(ce, "2026-09-16", close_price=70.0)
        self.seed_next_open(pe, "2026-09-16", close_price=60.0)
        data = day_chart.day_chart("2026-09-15", lab.S5)
        self.assertTrue(data["available"])
        self.assertEqual((data["date"], data["exit_date"]), ("2026-09-15", "2026-09-16"))
        self.assertFalse(data["rolled_to_next_expiry"])
        entry_ce = next(leg for leg in data["entry_legs"] if leg["type"] == "CE")
        exit_ce = next(leg for leg in data["exit_legs"] if leg["type"] == "CE")
        self.assertEqual((entry_ce["entry"], entry_ce["exit"]), (100.0, None), "the entry-day panel marks only the entry")
        self.assertEqual((exit_ce["entry"], exit_ce["exit"]), (None, 70.0), "the exit-day panel marks only the exit")
        self.assertTrue(data["entry_complete"] and data["exit_complete"])
        self.assertEqual(data["s5_checks"], {"entry_matches_0930_open": True, "exit_matches_0930_close": True})

    def test_paper_day_chart_uses_the_paper_trades_own_contracts_and_real_fill_times(self):
        """A manually recovered entry at 10:04 is drawn at 10:04 (with candles up to 10:19), and the exit at its own
        09:30 fill on the next day - built from the paper trade, not the backtest row (which may not exist yet)."""
        ce, pe = "NSE_FO|PCE", "NSE_FO|PPE"
        legs = [{"role": "body", "type": t, "strike": 22650.0, "side": "SELL", "instrument_key": k,
                 "entry": {"ts": "2026-09-15T10:04:37+05:30", "bid": e, "ask": e + 0.5, "ltp": e, "fill": e, "source": "bid"},
                 "exit": {"ts": "2026-09-16T09:30:00+05:30", "bid": x - 0.2, "ask": x, "ltp": x, "fill": x, "source": "ask"}}
                for t, k, e, x in (("CE", ce, 193.55, 142.75), ("PE", pe, 157.10, 171.0))]
        storage.paper_insert_trade({**paper_row(lab.S5, "2026-09-15"), "legs": legs, "atm_strike": 22650.0, "invalid_reason": "entered manually"})
        for key in (ce, pe):
            storage.save_historical_candles(key, [base.candle("2026-09-15", "10:04", 190.0, 195.0, 189.0, 194.0), base.candle("2026-09-15", "10:19", 1.0, 1.0, 1.0, 1.0),
                                                  base.candle("2026-09-15", "10:20", 2.0, 2.0, 2.0, 2.0), base.candle("2026-09-16", "09:30", 143.0, 144.0, 141.0, 142.5)])
        data = day_chart.day_chart("2026-09-15", lab.S5, "paper")
        self.assertTrue(data["available"])
        self.assertEqual((data["source"], data["exit_date"], data["entry_bar"], data["exit_bar"]), ("paper", "2026-09-16", "10:04", "09:30"))
        self.assertEqual((data["entry_window_end"], data["exit_window_end"]), ("10:19", "09:45"))
        entry_ce = next(leg for leg in data["entry_legs"] if leg["type"] == "CE")
        exit_pe = next(leg for leg in data["exit_legs"] if leg["type"] == "PE")
        self.assertEqual((entry_ce["entry"], entry_ce["entry_time"], entry_ce["exit"]), (193.55, "10:04", None))
        self.assertEqual((exit_pe["exit"], exit_pe["exit_time"], exit_pe["entry"]), (171.0, "09:30", None))
        self.assertEqual([bar["t"] for bar in entry_ce["bars"]], ["10:04", "10:19"], "candles to 15 minutes past the fill, not beyond")
        self.assertIsNone(data["s5_checks"], "paper fills are bid/ask - never required to equal a candle")

    def test_paper_day_chart_explains_a_day_without_an_s5_paper_trade(self):
        data = day_chart.day_chart("2026-09-15", lab.S5, "paper")
        self.assertFalse(data["available"])
        self.assertIn("no S5 paper trade", data["reason"])

    def test_day_chart_marks_a_leg_estimated_when_its_price_came_from_ticks(self):
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        self.seed_next_open(ce, "2026-09-15", close_price=999.0, open_price=100.0)
        self.seed_next_open(pe, "2026-09-15", close_price=999.0, open_price=90.0)
        self.seed_ticks(ce, "2026-09-16", [70.0])  # no downloaded candle for the exit day - only a live tick
        self.seed_ticks(pe, "2026-09-16", [60.0])
        data = day_chart.day_chart("2026-09-15", lab.S5)
        self.assertTrue(data["available"])
        entry_ce = next(leg for leg in data["entry_legs"] if leg["type"] == "CE")
        exit_ce = next(leg for leg in data["exit_legs"] if leg["type"] == "CE")
        self.assertFalse(entry_ce["estimated"], "the entry day has a real candle")
        self.assertTrue(exit_ce["estimated"], "the exit day only has a tick")
        self.assertEqual(exit_ce["exit"], 70.0)

    def test_day_chart_is_unavailable_when_s5_itself_could_not_resolve_the_exit_candle(self):
        """day_chart draws only what s5_overnight_rows() itself resolved into a row - a day it had to skip (for
        any reason, including a missing exit candle) is reported the same way test_day_chart_explains_itself... is."""
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        self.seed_next_open(ce, "2026-09-15", close_price=999.0, open_price=100.0)
        self.seed_next_open(pe, "2026-09-15", close_price=999.0, open_price=90.0)
        # nothing seeded for the exit day (2026-09-16)
        data = day_chart.day_chart("2026-09-15", lab.S5)
        self.assertFalse(data["available"])
        self.assertIn("09:30 candle", data["reason"])

    def test_day_chart_marks_a_rolled_entrant_and_the_frontend_can_tell(self):
        # well in the past, same reason as OvernightCandlesBuild/OvernightRollResolve: find_option_contract()'s
        # lapsed/live key-shape distinction depends on the REAL wall clock, not this class's fictional "today".
        entry, next_day, roll_expiry = "2024-10-17", "2024-10-18", "2024-10-24"
        self.seed_s1(entry, entry, entry_ce=100.0, entry_pe=90.0)  # own expiry day
        self.seed_s1(next_day, "2024-10-24")
        ce, pe = f"NSE_FO|9001|{roll_expiry}", f"NSE_FO|9002|{roll_expiry}"
        storage.save_instrument_contracts([
            {"instrument_key": ce, "underlying_key": "NSE_INDEX|Nifty 50", "expiry": roll_expiry, "strike_price": 25000.0, "instrument_type": "CE", "lot_size": 65},
            {"instrument_key": pe, "underlying_key": "NSE_INDEX|Nifty 50", "expiry": roll_expiry, "strike_price": 25000.0, "instrument_type": "PE", "lot_size": 65},
        ])
        self.seed_next_open(ce, entry, close_price=999.0, open_price=120.0)
        self.seed_next_open(pe, entry, close_price=999.0, open_price=110.0)
        self.seed_next_open(ce, next_day, close_price=80.0)
        self.seed_next_open(pe, next_day, close_price=75.0)
        data = day_chart.day_chart(entry, lab.S5)
        self.assertTrue(data["available"])
        self.assertTrue(data["rolled_to_next_expiry"])
        self.assertEqual(data["expiry"], roll_expiry, "the rolled (next week's) expiry, not S1's own")
        self.assertEqual({leg["instrument_key"] for leg in data["entry_legs"]}, {ce, pe})

    def test_the_result_is_cached_and_invalidates_when_a_new_s1_day_is_stored(self):
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        self.seed_next_open(ce, "2026-09-16", close_price=70.0)
        self.seed_next_open(pe, "2026-09-16", close_price=60.0)
        first_rows, first_skipped = lab.s5_overnight_rows()
        # query_backtest_rows (cheap: it is what the signature is built from) runs every time; the expensive part - one
        # batched candle query per next trading day - must not run again on a cache hit
        with mock.patch("strategy_lab._next_day_0930_closes", side_effect=AssertionError("should have come from the cache")):
            cached_rows, cached_skipped = lab.s5_overnight_rows()
        self.assertEqual((cached_rows, cached_skipped), (first_rows, first_skipped))
        self.seed_s1("2026-09-17", "2026-09-22", 5.0, 5.0)  # a new S1 day changes the signature
        _, grown_skipped = lab.s5_overnight_rows()
        self.assertEqual([(s["trading_date"], s["reason"]) for s in grown_skipped],
                          [("2026-09-16", "no_0930_next_day_candle"), ("2026-09-17", "no_next_day")],
                          "09-16 now has a next day (09-17) but no candle was seeded for it, so its skip reason changed; 09-17 is the new trailing day")

    def test_pnl_csv_carries_it_with_the_right_side_and_sort_order(self):
        ce, pe = self.seed_s1("2026-09-15", "2026-09-22", 100.0, 90.0)
        self.seed_s1("2026-09-16", "2026-09-22", 1.0, 1.0)
        self.seed_next_open(ce, "2026-09-16", close_price=70.0)
        self.seed_next_open(pe, "2026-09-16", close_price=60.0)
        text = lab.pnl_csv(lab.Size(lots=1))
        rows = list(csv.DictReader(io.StringIO(text)))
        s5_rows = [r for r in rows if r["strategy"] == lab.S5]
        self.assertEqual(len(s5_rows), 1)
        self.assertEqual(s5_rows[0]["entry_credit_pts"], "190.0")  # sold both legs at 09:30: a credit, same convention as S1


class OvernightCandlesBuild(base.TempDatabase):
    """Fills the "no_0930_next_day_candle" gap: downloads whatever next-day candle S5 is missing, resumable and
    rate-aware like the S1-S3 history build. Dates are well in the past so expiry < today always holds regardless
    of the real wall clock, matching the expired-contract download path this exercises."""

    ENTRY, NEXT_DAY, EXPIRY = "2024-10-15", "2024-10-16", "2024-10-17"

    def setUp(self):
        super().setUp()
        lab._s5_cache.clear()

    def seed_s1(self, day, expiry, entry_ce=100.0, entry_pe=90.0, lot_size=65):
        # expired-shaped (two pipes): build_s5_candles() now picks the download endpoint from the KEY's own shape,
        # not from expiry < today, so exercising the expired-contract path (this whole class's point) needs a key
        # actually shaped that way - the same convention find_option_contract() uses everywhere else.
        ce_key, pe_key = f"NSE_FO|CE{day}|{expiry}", f"NSE_FO|PE{day}|{expiry}"
        legs = [{"side": "SELL", "entry": entry_ce, "exit": entry_ce - 5, "type": "CE", "strike": 25000.0, "role": "body", "instrument_key": ce_key},
                {"side": "SELL", "entry": entry_pe, "exit": entry_pe - 5, "type": "PE", "strike": 25000.0, "role": "body", "instrument_key": pe_key}]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, expiry, 25000.0, entry_ce + entry_pe, legs, lot_size))
        return ce_key, pe_key

    def two_entrant_days(self):
        ce, pe = self.seed_s1(self.ENTRY, self.EXPIRY)
        self.seed_s1(self.NEXT_DAY, self.EXPIRY)  # gives ENTRY a next day; NEXT_DAY itself has none yet (left out, not "missing candles")
        return ce, pe

    def fake_download(self, close_0930=70.0):
        """A stand-in for expired.download_expired_candles: writes a whole realistic session, like the real one would."""
        def download(token, key, from_date, to_date, interval, pause=0.0):
            storage.save_historical_candles(key, [
                base.candle(to_date, "09:30", close_0930 + 1, close_0930 + 2, close_0930, close_0930),
                base.candle(to_date, "15:25", 1.0, 1.0, 1.0, 1.0),
            ])
            return 1
        return download

    def test_lists_exactly_what_s5_would_report_as_missing_and_nothing_else(self):
        ce, pe = self.two_entrant_days()
        self.assertEqual(sorted(lab.s5_missing_candles()), sorted([(ce, self.NEXT_DAY, self.EXPIRY), (pe, self.NEXT_DAY, self.EXPIRY)]))
        self.seed_s1("2024-10-14", "2024-10-14")  # an earlier, own-expiry entry: nothing to fetch for it, and it must not affect the first two
        self.assertEqual(sorted(lab.s5_missing_candles()), sorted([(ce, self.NEXT_DAY, self.EXPIRY), (pe, self.NEXT_DAY, self.EXPIRY)]))

    def test_already_present_candles_are_not_re_requested(self):
        ce, pe = self.two_entrant_days()
        self.fake_download()("tok", ce, self.NEXT_DAY, self.NEXT_DAY, "1minute")
        self.assertEqual(lab.s5_missing_candles(), [(pe, self.NEXT_DAY, self.EXPIRY)])

    def test_build_downloads_only_the_missing_ones_and_s5_sees_them_afterward(self):
        ce, pe = self.two_entrant_days()
        with mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=self.fake_download()) as downloader:
            report = lab.build_s5_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual((report["total"], report["attempted"], report["downloaded"], report["errors"], report["stopped_because"]), (2, 2, 2, 0, "completed"))
        self.assertEqual(downloader.call_count, 2)
        self.assertTrue(all(call.args[0] == "tok" for call in downloader.call_args_list))
        self.assertEqual(lab.s5_missing_candles(), [], "the gap is closed")
        rows, skipped = lab.s5_overnight_rows()
        self.assertEqual([r["trading_date"] for r in rows], [self.ENTRY], "S5 can now be computed for the day that was missing a candle")
        self.assertEqual(rows[0]["legs"][0]["exit"], 70.0)  # the 09:30 close the fake download wrote

    def test_a_second_run_re_requests_nothing_it_already_has(self):
        self.two_entrant_days()
        with mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=self.fake_download()):
            lab.build_s5_candles(lambda: "tok", log=lambda line: None)
        with mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=AssertionError("should not be called again")):
            second = lab.build_s5_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual((second["total"], second["attempted"]), (0, 0))

    def test_no_token_stops_immediately_and_nothing_is_downloaded(self):
        self.two_entrant_days()
        with mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=AssertionError("no token: must not call Upstox")):
            report = lab.build_s5_candles(lambda: None, log=lambda line: None)["run"]
        self.assertIn("UPSTOX_ACCESS_TOKEN", report["stopped_because"])
        self.assertEqual(report["downloaded"], 0)

    def test_an_expired_token_stops_the_whole_run_and_earlier_progress_is_kept(self):
        self.seed_s1(self.ENTRY, self.EXPIRY)
        self.seed_s1(self.NEXT_DAY, self.EXPIRY)
        self.seed_s1("2024-10-17", self.EXPIRY)  # a second entrant day, so there is "earlier progress" to check survives
        self.seed_s1("2024-10-18", self.EXPIRY)

        calls = []

        def flaky(token, key, from_date, to_date, interval, pause=0.0):
            calls.append(key)
            if len(calls) == 3:
                raise HTTPError(url=None, code=401, msg="unauthorized", hdrs=None, fp=None)
            return self.fake_download()(token, key, from_date, to_date, interval, pause)

        with mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=flaky):
            report = lab.build_s5_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertIn("401", report["stopped_because"])
        self.assertEqual(report["downloaded"], 2, "the two calls before the 401 are kept, not rolled back")
        self.assertLess(report["done"], report["total"], "the run stopped early")

    def test_repeated_ordinary_failures_stop_after_the_limit_but_keep_earlier_successes(self):
        keys = []
        for i in range(straddle.MAX_CONSECUTIVE_ERRORS + 2):
            day = f"2024-10-{15 + i:02d}"
            ce, pe = self.seed_s1(day, "2024-10-31")
            keys += [ce, pe]
        self.seed_s1("2024-11-01", "2024-11-15")  # one final day so every seeded day above has a next trading day

        def always_fails(token, key, from_date, to_date, interval, pause=0.0):
            raise ConnectionError("network drop")

        with mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=always_fails):
            report = lab.build_s5_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual(report["errors"], straddle.MAX_CONSECUTIVE_ERRORS)
        self.assertEqual(report["stopped_because"], f"{straddle.MAX_CONSECUTIVE_ERRORS} failures in a row")
        self.assertLess(report["done"], report["total"])

    def test_the_job_runs_in_the_background_and_reports_a_live_missing_count_when_idle(self):
        self.two_entrant_days()
        idle = lab.s5_candles_job_status()
        self.assertEqual((idle["state"], idle["missing"]), ("idle", 2))
        # the mock must stay active for as long as the background thread might still be running, not just around start()
        with mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=self.fake_download()):
            started = lab.start_s5_candles_job(lambda: "tok")
            self.assertFalse(started["already_running"])
            for _ in range(50):
                if not lab._S5_CANDLES_JOB.running():
                    break
                time.sleep(0.05)
        self.assertFalse(lab._S5_CANDLES_JOB.running())
        done = lab.s5_candles_job_status()
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["report"]["run"]["downloaded"], 2)


class OvernightRollResolve(base.TempDatabase):
    """Resolving an S1 expiry-day entrant's next-week ATM contracts (resolve_s5_rolls()/s5_missing_rolls()) - the
    step build_s5_candles() runs before its ordinary candle download, and s5_overnight_rows() reads purely from
    storage once it has happened. Dates are well in the past, same reason as OvernightCandlesBuild."""

    ENTRY, NEXT_DAY, EXPIRY = "2024-10-17", "2024-10-18", "2024-10-24"

    def setUp(self):
        super().setUp()
        lab._s5_cache.clear()

    def seed_s1(self, day, expiry, entry_ce=100.0, entry_pe=90.0, lot_size=65):
        ce_key, pe_key = f"NSE_FO|CE{day}", f"NSE_FO|PE{day}"
        legs = [{"side": "SELL", "entry": entry_ce, "exit": entry_ce - 5, "type": "CE", "strike": 25000.0, "role": "body", "instrument_key": ce_key},
                {"side": "SELL", "entry": entry_pe, "exit": entry_pe - 5, "type": "PE", "strike": 25000.0, "role": "body", "instrument_key": pe_key}]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, expiry, 25000.0, entry_ce + entry_pe, legs, lot_size))

    def seed_own_expiry_entrant(self):
        self.seed_s1(self.ENTRY, self.ENTRY)  # trading_date == expiry: an own-expiry entrant, nothing to hold overnight
        self.seed_s1(self.NEXT_DAY, self.EXPIRY)  # gives it a next trading day

    @staticmethod
    def fake_list_expiry(token, index_key, expiry, contract_type, pause=0.0):
        """Stands in for expired.get_expired_contracts: writes the whole option chain's CE/PE at the ATM strike this
        fixture cares about, like the real one would after parsing Upstox's response."""
        contracts = [
            {"instrument_key": f"NSE_FO|9001|{expiry}", "underlying_key": index_key, "expiry": expiry, "strike_price": 25000.0, "instrument_type": "CE", "lot_size": 65},
            {"instrument_key": f"NSE_FO|9002|{expiry}", "underlying_key": index_key, "expiry": expiry, "strike_price": 25000.0, "instrument_type": "PE", "lot_size": 65},
        ]
        storage.save_instrument_contracts(contracts)
        return contracts

    def test_s5_missing_rolls_lists_exactly_the_unresolved_expiry_day_entrants(self):
        self.seed_own_expiry_entrant()
        self.assertEqual(lab.s5_missing_rolls(), [(self.ENTRY, 25000.0)])
        storage.save_instrument_contracts([
            {"instrument_key": f"NSE_FO|9001|{self.EXPIRY}", "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.EXPIRY, "strike_price": 25000.0, "instrument_type": "CE", "lot_size": 65},
            {"instrument_key": f"NSE_FO|9002|{self.EXPIRY}", "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.EXPIRY, "strike_price": 25000.0, "instrument_type": "PE", "lot_size": 65},
        ])
        self.assertEqual(lab.s5_missing_rolls(), [])

    def test_resolve_s5_rolls_lists_the_expiry_once_and_it_is_a_pure_storage_read_afterward(self):
        self.seed_own_expiry_entrant()
        with mock.patch.object(straddle.expired_api, "get_expiries", return_value=[self.EXPIRY, "2024-11-30"]), \
             mock.patch.object(straddle.expired_api, "get_expired_contracts", side_effect=self.fake_list_expiry) as lister:
            report = lab.resolve_s5_rolls(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual((report["total"], report["resolved"], report["errors"], report["stopped_because"]), (1, 1, 0, "completed"))
        self.assertEqual(lister.call_count, 1, "one contract-list call for the one distinct next expiry, not one per entry day")
        self.assertEqual(lab.s5_missing_rolls(), [])
        self.assertEqual(lab._roll_contract(self.ENTRY, 25000.0), (self.EXPIRY, f"NSE_FO|9001|{self.EXPIRY}", f"NSE_FO|9002|{self.EXPIRY}"))

    def test_resolve_s5_rolls_stops_on_a_dead_token_and_resolves_nothing(self):
        self.seed_own_expiry_entrant()
        with mock.patch.object(straddle.expired_api, "get_expiries", side_effect=AssertionError("no token: must not call Upstox")):
            report = lab.resolve_s5_rolls(lambda: None, log=lambda line: None)["run"]
        self.assertIn("UPSTOX_ACCESS_TOKEN", report["stopped_because"])
        self.assertEqual(report["resolved"], 0)
        self.assertEqual(lab.s5_missing_rolls(), [(self.ENTRY, 25000.0)])

    def test_build_s5_candles_resolves_the_roll_then_downloads_its_candles_and_s5_sees_the_row(self):
        self.seed_own_expiry_entrant()

        def fake_download_candles(token, key, from_date, to_date, interval, pause=0.0):
            storage.save_historical_candles(key, [base.candle(to_date, "09:30", 70.0, 71.0, 69.0, 70.0)])
            return 1

        with mock.patch.object(straddle.expired_api, "get_expiries", return_value=[self.EXPIRY, "2024-11-30"]), \
             mock.patch.object(straddle.expired_api, "get_expired_contracts", side_effect=self.fake_list_expiry), \
             mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=fake_download_candles):
            report = lab.build_s5_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual(report["rolls"]["resolved"], 1)
        self.assertEqual(report["downloaded"], 4, "entry-day and next-day candles for both the rolled CE and PE - none of that contract was ever fetched before")
        self.assertEqual(report["stopped_because"], "completed")
        rows, skipped = lab.s5_overnight_rows()
        self.assertEqual([r["trading_date"] for r in rows], [self.ENTRY])
        self.assertTrue(rows[0]["rolled_to_next_expiry"])
        self.assertEqual(rows[0]["expiry"], self.EXPIRY)

    def test_a_live_shaped_roll_key_upstox_has_since_reclassified_falls_back_to_its_expired_twin(self):
        """The roll was resolved live-shaped while its expiry was still current (this class's other test covers
        the still-lapsed-at-resolve-time case) - some time later Upstox stops serving candles for that live key
        at all. build_s5_candles() must react to the download itself failing, not just give up on the gap."""
        self.seed_own_expiry_entrant()
        live_ce, live_pe = "NSE_FO|9101", "NSE_FO|9102"
        storage.save_instrument_contracts([
            {"instrument_key": live_ce, "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.EXPIRY, "strike_price": 25000.0, "instrument_type": "CE", "lot_size": 65},
            {"instrument_key": live_pe, "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.EXPIRY, "strike_price": 25000.0, "instrument_type": "PE", "lot_size": 65},
        ])
        self.assertEqual(lab.s5_missing_rolls(), [], "already resolvable (live-shaped), so resolve_s5_rolls() has nothing to do")

        def dead_live_endpoint(token, key, trading_date, *, unit, interval, pause=0.0):
            raise HTTPError(url=None, code=400, msg="Bad Request", hdrs=None, fp=None)

        def fake_download_expired(token, key, from_date, to_date, interval, pause=0.0):
            storage.save_historical_candles(key, [base.candle(to_date, "09:30", 70.0, 71.0, 69.0, 70.0)])
            return 1

        with mock.patch.object(straddle.historical, "download_candles", side_effect=dead_live_endpoint) as dead, \
             mock.patch.object(straddle.expired_api, "get_expired_contracts", side_effect=self.fake_list_expiry) as lister, \
             mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=fake_download_expired):
            report = lab.build_s5_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual(dead.call_count, 4, "each of the 4 candles is tried on the live key first, exactly once")
        self.assertEqual(lister.call_count, 1, "the expired listing is fetched once and reused for every leg/day after that")
        self.assertEqual((report["downloaded"], report["errors"], report["stopped_because"]), (4, 0, "completed"))
        rows, skipped = lab.s5_overnight_rows()
        self.assertEqual([r["trading_date"] for r in rows], [self.ENTRY])
        self.assertEqual({leg["instrument_key"] for leg in rows[0]["legs"]}, {f"NSE_FO|9001|{self.EXPIRY}", f"NSE_FO|9002|{self.EXPIRY}"},
                          "reads back from the expired-shaped twin, not the dead live key")

    def test_an_http_401_on_the_live_key_stops_the_run_without_trying_a_fallback(self):
        """A dead token fails every request the same way - trying the expired twin too would just waste a call
        before the run stops anyway."""
        self.seed_own_expiry_entrant()
        storage.save_instrument_contracts([
            {"instrument_key": "NSE_FO|9101", "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.EXPIRY, "strike_price": 25000.0, "instrument_type": "CE", "lot_size": 65},
            {"instrument_key": "NSE_FO|9102", "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.EXPIRY, "strike_price": 25000.0, "instrument_type": "PE", "lot_size": 65},
        ])

        def dead_token(token, key, trading_date, *, unit, interval, pause=0.0):
            raise HTTPError(url=None, code=401, msg="Unauthorized", hdrs=None, fp=None)

        with mock.patch.object(straddle.historical, "download_candles", side_effect=dead_token), \
             mock.patch.object(straddle.expired_api, "get_expired_contracts", side_effect=AssertionError("must not be called on a dead token")):
            report = lab.build_s5_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertIn("401", report["stopped_because"])
        self.assertEqual(report["downloaded"], 0)


class TwoOvernightCandlesBuild(base.TempDatabase):
    """S6's own candle need beyond anything S5's build ever fetches: the exit-day close, two trading days after
    entry (S5 only ever looks one day ahead) - for a plain continuous hold, with no roll on either side."""

    def setUp(self):
        super().setUp()
        lab._s5_cache.clear()
        lab._s6_cache.clear()

    def seed_s1(self, day, expiry, entry_ce=100.0, entry_pe=90.0, lot_size=65):
        ce_key, pe_key = f"NSE_FO|CE{day}", f"NSE_FO|PE{day}"
        legs = [{"side": "SELL", "entry": entry_ce, "exit": entry_ce - 5, "type": "CE", "strike": 25000.0, "role": "body", "instrument_key": ce_key},
                {"side": "SELL", "entry": entry_pe, "exit": entry_pe - 5, "type": "PE", "strike": 25000.0, "role": "body", "instrument_key": pe_key}]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, expiry, 25000.0, entry_ce + entry_pe, legs, lot_size))
        return ce_key, pe_key

    def three_entrant_days(self):
        ce, pe = self.seed_s1("2026-09-16", "2026-09-22")
        self.seed_s1("2026-09-17", "2026-09-22")  # gives 09-16 a next_day (also its own trailing entrant, ignored here)
        self.seed_s1("2026-09-18", "2026-09-22")  # gives 09-16 an exit_day
        return ce, pe

    def fake_download(self, close_0930=70.0):
        """A stand-in for historical.download_candles: writes a whole realistic session, like the real one would."""
        def download(token, key, day, *, unit, interval, pause=0.0):
            storage.save_historical_candles(key, [
                base.candle(day, "09:30", close_0930 + 1, close_0930 + 2, close_0930, close_0930),
                base.candle(day, "15:25", 1.0, 1.0, 1.0, 1.0),
            ])
            return 1
        return download

    def test_lists_exactly_the_exit_day_gap_and_nothing_else(self):
        ce, pe = self.three_entrant_days()
        self.assertEqual(sorted(lab.s6_missing_candles()), sorted([(ce, "2026-09-18", "2026-09-22"), (pe, "2026-09-18", "2026-09-22")]))

    def test_already_present_candles_are_not_re_requested(self):
        ce, pe = self.three_entrant_days()
        self.fake_download()("tok", ce, "2026-09-18", unit="minutes", interval=1)
        self.assertEqual(lab.s6_missing_candles(), [(pe, "2026-09-18", "2026-09-22")])

    def test_build_downloads_the_gap_and_s6_sees_the_row_afterward(self):
        ce, pe = self.three_entrant_days()
        with mock.patch.object(straddle.historical, "download_candles", side_effect=self.fake_download()) as downloader:
            report = lab.build_s6_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual((report["total"], report["attempted"], report["downloaded"], report["errors"], report["stopped_because"]), (2, 2, 2, 0, "completed"))
        self.assertEqual(downloader.call_count, 2)
        self.assertEqual(lab.s6_missing_candles(), [], "the gap is closed")
        rows, skipped = lab.s6_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == "2026-09-16")
        self.assertEqual(row["exit_date"], "2026-09-18")
        self.assertEqual(sorted(leg["exit"] for leg in row["legs"]), [70.0, 70.0])

    def test_a_second_run_re_requests_nothing_it_already_has(self):
        self.three_entrant_days()
        with mock.patch.object(straddle.historical, "download_candles", side_effect=self.fake_download()):
            lab.build_s6_candles(lambda: "tok", log=lambda line: None)
        with mock.patch.object(straddle.historical, "download_candles", side_effect=AssertionError("should not be called again")):
            second = lab.build_s6_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual((second["total"], second["attempted"]), (0, 0))

    def test_no_token_stops_immediately_and_nothing_is_downloaded(self):
        self.three_entrant_days()
        with mock.patch.object(straddle.historical, "download_candles", side_effect=AssertionError("no token: must not call Upstox")):
            report = lab.build_s6_candles(lambda: None, log=lambda line: None)["run"]
        self.assertIn("UPSTOX_ACCESS_TOKEN", report["stopped_because"])
        self.assertEqual(report["downloaded"], 0)

    def test_the_job_runs_in_the_background_and_reports_a_live_missing_count_when_idle(self):
        self.three_entrant_days()
        idle = lab.s6_candles_job_status()
        self.assertEqual((idle["state"], idle["missing"]), ("idle", 2))
        with mock.patch.object(straddle.historical, "download_candles", side_effect=self.fake_download()):
            started = lab.start_s6_candles_job(lambda: "tok")
            self.assertFalse(started["already_running"])
            for _ in range(50):
                if not lab._S6_CANDLES_JOB.running():
                    break
                time.sleep(0.05)
        self.assertFalse(lab._S6_CANDLES_JOB.running())
        done = lab.s6_candles_job_status()
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["report"]["run"]["downloaded"], 2)


class TwoOvernightMidRollResolve(base.TempDatabase):
    """S6's own second roll - resolve_s6_mid_rolls()/s6_missing_mid_rolls() - for an entrant whose held contract
    expires on the MIDDLE day of the two-night hold (see TwoOvernightStrategyS6's own mid-hold-roll tests for the
    row this unblocks). Dates well in the past, same reason as OvernightRollResolve's own roll tests."""

    ENTRY, MIDDLE, EXIT_DAY, ROLL_EXPIRY = "2024-10-15", "2024-10-22", "2024-10-23", "2024-10-29"

    def setUp(self):
        super().setUp()
        lab._s5_cache.clear()
        lab._s6_cache.clear()

    def seed_s1(self, day, expiry, entry_ce=100.0, entry_pe=90.0, lot_size=65):
        ce_key, pe_key = f"NSE_FO|CE{day}", f"NSE_FO|PE{day}"
        legs = [{"side": "SELL", "entry": entry_ce, "exit": entry_ce - 5, "type": "CE", "strike": 25000.0, "role": "body", "instrument_key": ce_key},
                {"side": "SELL", "entry": entry_pe, "exit": entry_pe - 5, "type": "PE", "strike": 25000.0, "role": "body", "instrument_key": pe_key}]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, expiry, 25000.0, entry_ce + entry_pe, legs, lot_size))
        return ce_key, pe_key

    def seed_mid_hold_entrant(self):
        ce1, pe1 = self.seed_s1(self.ENTRY, self.MIDDLE, entry_ce=120.0, entry_pe=110.0)  # own expiry IS the middle day
        self.seed_s1(self.MIDDLE, self.ROLL_EXPIRY)  # gives ENTRY a next_day
        self.seed_s1(self.EXIT_DAY, self.ROLL_EXPIRY)  # gives ENTRY (via MIDDLE) an exit_day
        return ce1, pe1

    @staticmethod
    def fake_list_expiry(token, index_key, expiry, contract_type, pause=0.0):
        """Stands in for expired.get_expired_contracts: writes the whole option chain's CE/PE at the ATM strike this
        fixture cares about, like the real one would after parsing Upstox's response."""
        contracts = [
            {"instrument_key": f"NSE_FO|9101|{expiry}", "underlying_key": index_key, "expiry": expiry, "strike_price": 25000.0, "instrument_type": "CE", "lot_size": 65},
            {"instrument_key": f"NSE_FO|9102|{expiry}", "underlying_key": index_key, "expiry": expiry, "strike_price": 25000.0, "instrument_type": "PE", "lot_size": 65},
        ]
        storage.save_instrument_contracts(contracts)
        return contracts

    def test_s6_missing_mid_rolls_lists_exactly_the_unresolved_mid_hold_entrant(self):
        self.seed_mid_hold_entrant()
        self.assertEqual(lab.s6_missing_mid_rolls(), [(self.MIDDLE, 25000.0, self.EXIT_DAY)])
        storage.save_instrument_contracts([
            {"instrument_key": f"NSE_FO|9101|{self.ROLL_EXPIRY}", "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.ROLL_EXPIRY, "strike_price": 25000.0, "instrument_type": "CE", "lot_size": 65},
            {"instrument_key": f"NSE_FO|9102|{self.ROLL_EXPIRY}", "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.ROLL_EXPIRY, "strike_price": 25000.0, "instrument_type": "PE", "lot_size": 65},
        ])
        self.assertEqual(lab.s6_missing_mid_rolls(), [])

    def test_resolve_s6_mid_rolls_lists_the_week_after_once_and_it_is_a_pure_storage_read_afterward(self):
        self.seed_mid_hold_entrant()
        with mock.patch.object(straddle.expired_api, "get_expiries", return_value=[self.ROLL_EXPIRY, "2024-11-05"]), \
             mock.patch.object(straddle.expired_api, "get_expired_contracts", side_effect=self.fake_list_expiry) as lister:
            report = lab.resolve_s6_mid_rolls(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual((report["total"], report["resolved"], report["errors"], report["stopped_because"]), (1, 1, 0, "completed"))
        self.assertEqual(lister.call_count, 1, "one contract-list call for the one distinct next expiry")
        self.assertEqual(lab.s6_missing_mid_rolls(), [])
        self.assertEqual(lab._roll_contract(self.MIDDLE, 25000.0), (self.ROLL_EXPIRY, f"NSE_FO|9101|{self.ROLL_EXPIRY}", f"NSE_FO|9102|{self.ROLL_EXPIRY}"))

    def test_resolve_s6_mid_rolls_stops_on_a_dead_token_and_resolves_nothing(self):
        self.seed_mid_hold_entrant()
        with mock.patch.object(straddle.expired_api, "get_expiries", side_effect=AssertionError("no token: must not call Upstox")):
            report = lab.resolve_s6_mid_rolls(lambda: None, log=lambda line: None)["run"]
        self.assertIn("UPSTOX_ACCESS_TOKEN", report["stopped_because"])
        self.assertEqual(report["resolved"], 0)
        self.assertEqual(lab.s6_missing_mid_rolls(), [(self.MIDDLE, 25000.0, self.EXIT_DAY)])

    def test_build_s6_candles_resolves_the_mid_hold_roll_then_downloads_its_candles_and_s6_sees_the_row(self):
        ce1, pe1 = self.seed_mid_hold_entrant()

        def fake_live_download(token, key, day, *, unit, interval, pause=0.0):
            # leg1's own middle-day close (it expires that day) - a plain, already-live-shaped key
            close = 80.0 if key == ce1 else 70.0
            storage.save_historical_candles(key, [base.candle(day, "09:30", close + 1, close + 2, close - 1, close)])
            return 1

        def fake_expired_download(token, key, from_date, to_date, interval, pause=0.0):
            # leg2's own entry (the middle day's open) and exit (the exit day's close) - the rolled contract itself
            if to_date == self.MIDDLE:
                storage.save_historical_candles(key, [base.candle(to_date, "09:30", 50.0, 52.0, 49.0, 50.5)])
            else:
                storage.save_historical_candles(key, [base.candle(to_date, "09:30", 39.0, 41.0, 38.0, 40.0)])
            return 1

        with mock.patch.object(straddle.expired_api, "get_expiries", return_value=[self.ROLL_EXPIRY, "2024-11-05"]), \
             mock.patch.object(straddle.expired_api, "get_expired_contracts", side_effect=self.fake_list_expiry), \
             mock.patch.object(straddle.historical, "download_candles", side_effect=fake_live_download) as live, \
             mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=fake_expired_download) as expired:
            report = lab.build_s6_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual(report["mid_rolls"]["resolved"], 1)
        self.assertEqual(live.call_count, 2, "leg1's own middle-day close, CE and PE")
        self.assertEqual(expired.call_count, 4, "leg2's own middle-day open and exit-day close, CE and PE")
        self.assertEqual(report["downloaded"], 6)
        self.assertEqual(report["stopped_because"], "completed")
        rows, skipped = lab.s6_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == self.ENTRY)
        self.assertEqual((row["exit_date"], row["expiry"], row["rolled_mid_hold"]), (self.EXIT_DAY, self.ROLL_EXPIRY, True))
        self.assertEqual(len(row["legs"]), 4)
        by_key = {leg["instrument_key"]: leg for leg in row["legs"]}
        self.assertEqual((by_key[ce1]["entry"], by_key[ce1]["exit"]), (120.0, 80.0))
        self.assertEqual((by_key[pe1]["entry"], by_key[pe1]["exit"]), (110.0, 70.0))


class ThreeOvernightCandlesBuild(base.TempDatabase):
    """S7's own candle need beyond anything S5/S6's builds ever fetch: the exit-day close, three trading days
    after entry (S6 only ever looks two days ahead) - for a plain continuous hold, with no roll anywhere."""

    def setUp(self):
        super().setUp()
        lab._s5_cache.clear()
        lab._s6_cache.clear()
        lab._s7_cache.clear()

    def seed_s1(self, day, expiry, entry_ce=100.0, entry_pe=90.0, lot_size=65):
        ce_key, pe_key = f"NSE_FO|CE{day}", f"NSE_FO|PE{day}"
        legs = [{"side": "SELL", "entry": entry_ce, "exit": entry_ce - 5, "type": "CE", "strike": 25000.0, "role": "body", "instrument_key": ce_key},
                {"side": "SELL", "entry": entry_pe, "exit": entry_pe - 5, "type": "PE", "strike": 25000.0, "role": "body", "instrument_key": pe_key}]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, expiry, 25000.0, entry_ce + entry_pe, legs, lot_size))
        return ce_key, pe_key

    def four_entrant_days(self):
        ce, pe = self.seed_s1("2026-09-16", "2026-09-25")
        self.seed_s1("2026-09-17", "2026-09-25")  # gives 09-16 a first middle day
        self.seed_s1("2026-09-18", "2026-09-25")  # gives 09-16 a second middle day
        self.seed_s1("2026-09-21", "2026-09-25")  # gives 09-16 an exit_day
        return ce, pe

    def fake_download(self, close_0930=70.0):
        """A stand-in for historical.download_candles: writes a whole realistic session, like the real one would."""
        def download(token, key, day, *, unit, interval, pause=0.0):
            storage.save_historical_candles(key, [
                base.candle(day, "09:30", close_0930 + 1, close_0930 + 2, close_0930, close_0930),
                base.candle(day, "15:25", 1.0, 1.0, 1.0, 1.0),
            ])
            return 1
        return download

    def test_lists_exactly_the_exit_day_gap_and_nothing_else(self):
        ce, pe = self.four_entrant_days()
        self.assertEqual(sorted(lab.s7_missing_candles()), sorted([(ce, "2026-09-21", "2026-09-25"), (pe, "2026-09-21", "2026-09-25")]))

    def test_build_downloads_the_gap_and_s7_sees_the_row_afterward(self):
        ce, pe = self.four_entrant_days()
        with mock.patch.object(straddle.historical, "download_candles", side_effect=self.fake_download()) as downloader:
            report = lab.build_s7_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual((report["total"], report["attempted"], report["downloaded"], report["errors"], report["stopped_because"]), (2, 2, 2, 0, "completed"))
        self.assertEqual(downloader.call_count, 2)
        self.assertEqual(lab.s7_missing_candles(), [], "the gap is closed")
        rows, skipped = lab.s7_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == "2026-09-16")
        self.assertEqual(row["exit_date"], "2026-09-21")
        self.assertEqual(sorted(leg["exit"] for leg in row["legs"]), [70.0, 70.0])

    def test_a_second_run_re_requests_nothing_it_already_has(self):
        self.four_entrant_days()
        with mock.patch.object(straddle.historical, "download_candles", side_effect=self.fake_download()):
            lab.build_s7_candles(lambda: "tok", log=lambda line: None)
        with mock.patch.object(straddle.historical, "download_candles", side_effect=AssertionError("should not be called again")):
            second = lab.build_s7_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual((second["total"], second["attempted"]), (0, 0))

    def test_the_job_runs_in_the_background_and_reports_a_live_missing_count_when_idle(self):
        self.four_entrant_days()
        idle = lab.s7_candles_job_status()
        self.assertEqual((idle["state"], idle["missing"]), ("idle", 2))
        with mock.patch.object(straddle.historical, "download_candles", side_effect=self.fake_download()):
            started = lab.start_s7_candles_job(lambda: "tok")
            self.assertFalse(started["already_running"])
            for _ in range(50):
                if not lab._S7_CANDLES_JOB.running():
                    break
                time.sleep(0.05)
        self.assertFalse(lab._S7_CANDLES_JOB.running())
        done = lab.s7_candles_job_status()
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["report"]["run"]["downloaded"], 2)


class ThreeOvernightMidRollResolve(base.TempDatabase):
    """S7's own roll(s) - resolve_s7_mid_rolls()/s7_missing_mid_rolls() - for an entrant whose held contract
    expires on one of the two middle days of the three-night hold (see ThreeOvernightStrategyS7's own mid-hold-roll
    tests). Dates well in the past, same reason as OvernightRollResolve's own roll tests."""

    ENTRY, MID1, MID2, EXIT_DAY, ROLL_EXPIRY = "2024-10-15", "2024-10-22", "2024-10-23", "2024-10-24", "2024-10-29"

    def setUp(self):
        super().setUp()
        lab._s5_cache.clear()
        lab._s6_cache.clear()
        lab._s7_cache.clear()

    def seed_s1(self, day, expiry, entry_ce=100.0, entry_pe=90.0, lot_size=65):
        ce_key, pe_key = f"NSE_FO|CE{day}", f"NSE_FO|PE{day}"
        legs = [{"side": "SELL", "entry": entry_ce, "exit": entry_ce - 5, "type": "CE", "strike": 25000.0, "role": "body", "instrument_key": ce_key},
                {"side": "SELL", "entry": entry_pe, "exit": entry_pe - 5, "type": "PE", "strike": 25000.0, "role": "body", "instrument_key": pe_key}]
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", day, expiry, 25000.0, entry_ce + entry_pe, legs, lot_size))
        return ce_key, pe_key

    def seed_mid_hold_entrant(self):
        ce1, pe1 = self.seed_s1(self.ENTRY, self.MID1, entry_ce=120.0, entry_pe=110.0)  # own expiry IS the first middle day
        self.seed_s1(self.MID1, self.ROLL_EXPIRY)  # gives ENTRY a first middle day
        self.seed_s1(self.MID2, self.ROLL_EXPIRY)  # gives ENTRY a second middle day
        self.seed_s1(self.EXIT_DAY, self.ROLL_EXPIRY)  # gives ENTRY an exit_day
        return ce1, pe1

    @staticmethod
    def fake_list_expiry(token, index_key, expiry, contract_type, pause=0.0):
        """Stands in for expired.get_expired_contracts: writes the whole option chain's CE/PE at the ATM strike this
        fixture cares about, like the real one would after parsing Upstox's response."""
        contracts = [
            {"instrument_key": f"NSE_FO|9101|{expiry}", "underlying_key": index_key, "expiry": expiry, "strike_price": 25000.0, "instrument_type": "CE", "lot_size": 65},
            {"instrument_key": f"NSE_FO|9102|{expiry}", "underlying_key": index_key, "expiry": expiry, "strike_price": 25000.0, "instrument_type": "PE", "lot_size": 65},
        ]
        storage.save_instrument_contracts(contracts)
        return contracts

    def test_s7_missing_mid_rolls_lists_exactly_the_unresolved_first_checkpoint(self):
        self.seed_mid_hold_entrant()
        self.assertEqual(lab.s7_missing_mid_rolls(), [(self.MID1, 25000.0, self.MID2)])
        storage.save_instrument_contracts([
            {"instrument_key": f"NSE_FO|9101|{self.ROLL_EXPIRY}", "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.ROLL_EXPIRY, "strike_price": 25000.0, "instrument_type": "CE", "lot_size": 65},
            {"instrument_key": f"NSE_FO|9102|{self.ROLL_EXPIRY}", "underlying_key": "NSE_INDEX|Nifty 50", "expiry": self.ROLL_EXPIRY, "strike_price": 25000.0, "instrument_type": "PE", "lot_size": 65},
        ])
        self.assertEqual(lab.s7_missing_mid_rolls(), [])

    def test_resolve_s7_mid_rolls_lists_the_week_after_once_and_it_is_a_pure_storage_read_afterward(self):
        self.seed_mid_hold_entrant()
        with mock.patch.object(straddle.expired_api, "get_expiries", return_value=[self.ROLL_EXPIRY, "2024-11-05"]), \
             mock.patch.object(straddle.expired_api, "get_expired_contracts", side_effect=self.fake_list_expiry) as lister:
            report = lab.resolve_s7_mid_rolls(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual((report["total"], report["resolved"], report["errors"], report["stopped_because"]), (1, 1, 0, "completed"))
        self.assertEqual(lister.call_count, 1, "one contract-list call for the one distinct next expiry")
        self.assertEqual(lab.s7_missing_mid_rolls(), [])
        self.assertEqual(lab._roll_contract(self.MID1, 25000.0), (self.ROLL_EXPIRY, f"NSE_FO|9101|{self.ROLL_EXPIRY}", f"NSE_FO|9102|{self.ROLL_EXPIRY}"))

    def test_resolve_s7_mid_rolls_stops_on_a_dead_token_and_resolves_nothing(self):
        self.seed_mid_hold_entrant()
        with mock.patch.object(straddle.expired_api, "get_expiries", side_effect=AssertionError("no token: must not call Upstox")):
            report = lab.resolve_s7_mid_rolls(lambda: None, log=lambda line: None)["run"]
        self.assertIn("UPSTOX_ACCESS_TOKEN", report["stopped_because"])
        self.assertEqual(report["resolved"], 0)
        self.assertEqual(lab.s7_missing_mid_rolls(), [(self.MID1, 25000.0, self.MID2)])

    def test_build_s7_candles_resolves_the_mid_hold_roll_then_downloads_its_candles_and_s7_sees_the_row(self):
        ce1, pe1 = self.seed_mid_hold_entrant()

        def fake_live_download(token, key, day, *, unit, interval, pause=0.0):
            close = 80.0 if key == ce1 else 70.0
            storage.save_historical_candles(key, [base.candle(day, "09:30", close + 1, close + 2, close - 1, close)])
            return 1

        def fake_expired_download(token, key, from_date, to_date, interval, pause=0.0):
            if to_date == self.MID1:
                storage.save_historical_candles(key, [base.candle(to_date, "09:30", 50.0, 52.0, 49.0, 50.5)])
            else:
                storage.save_historical_candles(key, [base.candle(to_date, "09:30", 39.0, 41.0, 38.0, 40.0)])
            return 1

        with mock.patch.object(straddle.expired_api, "get_expiries", return_value=[self.ROLL_EXPIRY, "2024-11-05"]), \
             mock.patch.object(straddle.expired_api, "get_expired_contracts", side_effect=self.fake_list_expiry), \
             mock.patch.object(straddle.historical, "download_candles", side_effect=fake_live_download) as live, \
             mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=fake_expired_download) as expired:
            report = lab.build_s7_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual(len(report["mid_rolls"]), 2, "a second, empty round confirms nothing was left unresolved")
        self.assertEqual(report["mid_rolls"][0]["resolved"], 1)
        self.assertEqual(report["mid_rolls"][1]["total"], 0, "the second middle day never needed a roll in this fixture")
        self.assertEqual(live.call_count, 2, "leg1's own first-middle-day close, CE and PE")
        self.assertEqual(expired.call_count, 4, "leg2's own first-middle-day open and exit-day close, CE and PE")
        self.assertEqual(report["downloaded"], 6)
        self.assertEqual(report["stopped_because"], "completed")
        rows, skipped = lab.s7_overnight_rows()
        row = next(r for r in rows if r["trading_date"] == self.ENTRY)
        self.assertEqual((row["exit_date"], row["expiry"], row["rolled_mid_hold"]), (self.EXIT_DAY, self.ROLL_EXPIRY, True))
        self.assertEqual(len(row["legs"]), 4)
        by_key = {leg["instrument_key"]: leg for leg in row["legs"]}
        self.assertEqual((by_key[ce1]["entry"], by_key[ce1]["exit"]), (120.0, 80.0))
        self.assertEqual((by_key[pe1]["entry"], by_key[pe1]["exit"]), (110.0, 70.0))

    def test_a_roll_needed_on_both_middle_days_is_resolved_in_one_build_run(self):
        """The bounded 2-round loop's whole point: s7_missing_mid_rolls() can only see the SECOND middle day's own
        roll once the first is resolved, so a single build_s7_candles() call must run resolve_s7_mid_rolls() twice."""
        ce1, pe1 = self.seed_s1(self.ENTRY, self.MID1, entry_ce=120.0, entry_pe=110.0)  # expires on the FIRST middle day...
        self.seed_s1(self.MID1, self.ROLL_EXPIRY)
        self.seed_s1(self.MID2, self.ROLL_EXPIRY)
        self.seed_s1(self.EXIT_DAY, self.ROLL_EXPIRY)

        def fake_list_expiry_at_mid2(token, index_key, expiry, contract_type, pause=0.0):
            # the FIRST roll (resolved in round 1) happens to land in a contract that ITSELF expires on MID2 -
            # the second roll (round 2) is only discoverable once this one is in storage.
            contracts = [
                {"instrument_key": f"NSE_FO|9201|{expiry}", "underlying_key": index_key, "expiry": expiry, "strike_price": 25000.0, "instrument_type": "CE", "lot_size": 65},
                {"instrument_key": f"NSE_FO|9202|{expiry}", "underlying_key": index_key, "expiry": expiry, "strike_price": 25000.0, "instrument_type": "PE", "lot_size": 65},
            ]
            storage.save_instrument_contracts(contracts)
            return contracts

        def fake_live_download(token, key, day, *, unit, interval, pause=0.0):
            storage.save_historical_candles(key, [base.candle(day, "09:30", 80.0, 81.0, 79.0, 80.0)])
            return 1

        def fake_expired_download(token, key, from_date, to_date, interval, pause=0.0):
            storage.save_historical_candles(key, [base.candle(to_date, "09:30", 50.0, 51.0, 49.0, 50.0)])
            return 1

        with mock.patch.object(straddle.expired_api, "get_expiries", return_value=[self.MID2, "2024-11-05"]), \
             mock.patch.object(straddle.expired_api, "get_expired_contracts", side_effect=fake_list_expiry_at_mid2) as lister, \
             mock.patch.object(straddle.historical, "download_candles", side_effect=fake_live_download), \
             mock.patch.object(straddle.expired_api, "download_expired_candles", side_effect=fake_expired_download):
            report = lab.build_s7_candles(lambda: "tok", log=lambda line: None)["run"]
        self.assertEqual(report["stopped_because"], "completed")
        self.assertEqual(lister.call_count, 2, "one listing for MID2 (round 1's roll target) and one for 2024-11-05 (round 2's)")
        self.assertEqual([r["resolved"] for r in report["mid_rolls"]], [1, 1])
        self.assertEqual(lab.s7_missing_mid_rolls(), [], "both middle days are now resolved")
        self.assertEqual(lab._roll_contract(self.MID1, 25000.0), (self.MID2, f"NSE_FO|9201|{self.MID2}", f"NSE_FO|9202|{self.MID2}"))
        self.assertEqual(lab._roll_contract(self.MID2, 25000.0), ("2024-11-05", f"NSE_FO|9201|2024-11-05", f"NSE_FO|9202|2024-11-05"))


class PnlExport(base.TempDatabase):
    def rows(self, text):
        import csv
        import io

        return list(csv.DictReader(io.StringIO(text)))

    def test_one_file_holds_every_strategy_and_both_sources_at_the_chosen_size(self):
        for strategy in lab.STRATEGIES:
            legs = [{"side": "SELL", "entry": 100.0, "exit": 60.0, "type": "CE", "strike": 25000.0, "role": "body"},
                    {"side": "SELL", "entry": 100.0, "exit": 90.0, "type": "PE", "strike": 25000.0, "role": "body"}]
            for day in ("2026-09-15", "2026-09-16"):
                storage.upsert_backtest_row(lab.backtest_row(strategy, day, "2026-09-22", 25000.0, 200.0, legs, 65))
        storage.paper_insert_trade(paper_row("iron_fly_w1", "2026-09-17", net_rs=0.0, net_pts=0.0))
        storage.paper_insert_trade({**paper_row("straddle_sell", "2026-09-17"), "status": "missed_entry", "legs": [], "net_rs": None, "net_pts": None, "gross_pts": None,
                                    "bt_legs": None, "bt_net_rs": None, "bt_net_pts": None, "bt_gross_pts": None, "bt_charges_pts": None, "notes": "token expired"})
        rows = self.rows(lab.pnl_csv(lab.Size(lots=2)))
        self.assertEqual(list(rows[0]), lab.PNL_CSV_COLUMNS)
        self.assertEqual([(r["strategy"], r["source"], r["trading_date"]) for r in rows], [
            ("straddle_sell", "backtest", "2026-09-15"), ("straddle_sell", "backtest", "2026-09-16"), ("straddle_sell", "paper", "2026-09-17"),
            ("iron_fly_w1", "backtest", "2026-09-15"), ("iron_fly_w1", "backtest", "2026-09-16"), ("iron_fly_w1", "paper", "2026-09-17"),
            ("iron_fly_w2", "backtest", "2026-09-15"), ("iron_fly_w2", "backtest", "2026-09-16"),
            ("straddle_expiry_wings", "backtest", "2026-09-15"), ("straddle_expiry_wings", "backtest", "2026-09-16"), ("straddle_expiry_wings", "paper", "2026-09-17"),
        ])
        first = rows[0]
        expected = lab.settle_units([{"side": "SELL", "entry": 100.0, "exit": 60.0}, {"side": "SELL", "entry": 100.0, "exit": 90.0}], 130)
        self.assertEqual((first["qty"], first["lots"], first["gross_rs"], first["charges_rs"], first["net_rs"]), ("130", "2", str(expected["gross_rs"]), str(expected["charges_rs"]), str(expected["net_rs"])))
        self.assertEqual((first["entry_credit_pts"], first["exit_debit_pts"]), ("200.0", "150.0"))
        self.assertEqual(rows[1]["running_net_rs"], str(round(2 * expected["net_rs"], 2)))
        self.assertEqual(first["legs"], "SELL CE 25000 100.0>60.0 | SELL PE 25000 100.0>90.0")
        missed = rows[2]
        self.assertEqual((missed["status"], missed["net_rs"], missed["running_net_rs"], missed["notes"]), ("missed_entry", "", "", "token expired"))
        fly = rows[5]
        self.assertEqual((fly["status"], fly["estimated_fill"], fly["backtest_net_rs"] != ""), ("closed", "0", True))

    def test_source_and_strategy_filters_and_bad_input(self):
        storage.upsert_backtest_row(lab.backtest_row("straddle_sell", "2026-09-16", "2026-09-22", 25000.0, 200.0, [{"side": "SELL", "entry": 1.0, "exit": 1.0, "type": "CE", "strike": 1.0, "role": "body"}], 65))
        storage.paper_insert_trade(paper_row("straddle_sell", "2026-09-17"))
        self.assertEqual([(r["strategy"], r["source"]) for r in self.rows(lab.pnl_csv(None, "paper"))], [("straddle_sell", "paper"), (lab.S4, "paper")])  # S4 mirrors the S1 trade of a normal day
        self.assertEqual([r["source"] for r in self.rows(lab.pnl_csv(None, "backtest", "straddle_sell"))], ["backtest"])
        self.assertEqual(self.rows(lab.pnl_csv(None, "backtest", "iron_fly_w1")), [])
        with self.assertRaises(ValueError):
            lab.pnl_csv(None, "everything")


class RoutesAgreeWithModules(unittest.TestCase):
    def test_every_module_attribute_the_routes_use_exists(self):
        """main.py reaches into paper / strategy_lab by attribute; a typo there is a 500 that only shows at request time."""
        tree = ast.parse((Path(__file__).parent / "main.py").read_text(encoding="utf-8"))
        wanted = {"paper": paper, "strategy_lab": lab}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in wanted:
                self.assertTrue(hasattr(wanted[node.value.id], node.attr), f"main.py uses {node.value.id}.{node.attr}, which does not exist")


if __name__ == "__main__":
    unittest.main()
