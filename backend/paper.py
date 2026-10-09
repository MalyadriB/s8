"""PAPER trading engine for the NIFTY strategies in strategy_lab.py: the three same-day ones (straddle_sell,
iron_fly_w1, iron_fly_w2) plus S5 (straddle_sell_overnight), which holds straddle_sell's own position overnight
instead of closing it the same day.

*** THIS MODULE NEVER PLACES A REAL ORDER. *** It only READS market data - the quotes the live stream has already
stored in SQLite - and WRITES to SQLite. It does not import the stream, the broker SDK, the login code or any order
endpoint (test_paper.py fails if it ever does). A "fill" here is a row in paper_trades, nothing more.

The rules are strategy_lab's - plan_legs(), wing_width(), settle() - so paper and backtest cannot drift apart:
NIFTY, nearest expiry, entry 09:30 (ATM = resolve_atm_strike of the spot then), exit in the last minute the options trade
(15:39 since the F&O session was extended to 15:40 on 2026-08-03, 15:29 before: strategy_lab.exit_minute).

Schedule (driven by tick(), which the backend calls every few seconds):
  09:20  health check (token valid, feed live). A failure is an alert AND a missed_entry row per strategy - never silent.
  09:25  pre-subscribe: publish the NIFTY strikes ATM +/-10 (both sides, nearest expiry) for the stream to subscribe.
  09:30  close yesterday's S5 position (bought back at today's 09:30 quote); resolve strikes and fill straddle_sell,
         iron_fly_w1, iron_fly_w2's legs; then S5's own entry for today (straddle_sell's fill reused, or the roll -
         see _s5_enter()). Every position is stored the moment it is filled.
  09:30-exit  one mark-to-market row per minute (refreshed every tick) for whatever is open, S5 included.
  exit minute (15:39)  close straddle_sell, iron_fly_w1 and iron_fly_w2 (S5 stays open till tomorrow's 09:30).
         After the close: reconcile the day's same-day strategies against the candle-based backtest (S5 is not -
         it spans two trading days, so there is no single day's candle backtest to compare it with).

Fills: SELL at the best BID, BUY at the best ASK - never LTP or mid. With no bid/ask the fill is LTP -/+ 0.20 and
flagged "estimated". Every leg records timestamp, bid, ask, LTP, fill and its source.

Restarts: all state lives in SQLite. paper_trades' primary key (strategy, trading_date) makes a second entry
impossible and an open position is simply picked up by the next tick.
"""
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

import strategy_lab as lab
from reliability import is_market_day, token_status
from storage import (
    find_option_contract,
    paper_add_alert,
    paper_get_health,
    paper_get_trade,
    paper_insert_trade,
    paper_set_health,
    paper_update_trade,
    paper_upsert_mark,
    query_latest_tick,
    query_paper_marks,
    query_paper_trades,
)
from straddle import resolve_atm_strike

logger = logging.getLogger("uvicorn.error")

PAPER_BUILD_VERSION = "paper-v1"
UNDERLYING = lab.UNDERLYING
MARKET_OPEN = clock_time(9, 15)
HEALTH_TIME = clock_time(9, 20)
SUBSCRIBE_TIME = clock_time(9, 25)
ENTRY_TIME = clock_time(9, 30)


def exit_time_for(day: str) -> clock_time:
    """When the paper positions of `day` are closed (the last minute the options trade)."""
    hour, minute = lab.exit_minute(day).split(":")
    return clock_time(int(hour), int(minute))


def _later(t: clock_time, minutes: int) -> clock_time:
    return (datetime.combine(date.today(), t) + timedelta(minutes=minutes)).time()


ESTIMATE_OFFSET = 0.20  # with no bid/ask: LTP -/+ this, flagged "estimated"
STALE_QUOTE_SECONDS = 30  # an entry/exit fill needs a quote received this recently
FEED_LAG_MAX_SECONDS = 30  # ... and the feed must not be running behind the exchange by more than this
HEALTH_FEED_FRESH_SECONDS = 120
FEED_STALL_SECONDS = 30  # the stream delivers something every second or so even in a quiet market: this long with nothing means the live feed has stopped
ENTRY_GRACE_SECONDS = 60  # entry is attempted from 09:30:00; after 09:31:00 the day is a missed entry
EXIT_ATTEMPT_START = clock_time(15, 29, 50)  # straddle_sell/iron_fly_w1/iron_fly_w2's own exit starts being tried here, not at the
# extended session's own last tradeable minute (exit_time_for(), 15:39) - so a paper fill can land at the same moment the candle
# backtest prices its own 15:29 close, not ~10 minutes later. exit_time_for() is still the HARD deadline retries run up to (see
# _try_exit()'s `hard_deadline`) - past it there is no more real market to get a fresh quote from.
EXIT_ON_TIME_SECONDS = 30  # an exit completed within this of EXIT_ATTEMPT_START is "closed"; later it is "missed_exit"
EXIT_INVALID_AFTER_SECONDS = 120  # an exit resolved more than this long after EXIT_ATTEMPT_START cannot represent that moment
# at all (fresh quote arriving very late, or the forced last-traded-price close at the hard deadline) - see _try_exit()'s
# invalid_reason: the trade is flagged INVALID, since its fill does not represent the strategy's own exit moment.
EXIT_ACCEPT_STALE_MINUTES = 11  # this long after EXIT_ATTEMPT_START the last quote seen is used, however old - only reachable by
# S5's own next-day 09:30 exit and stale-position recovery now that S1-S3's own close has its own hard_deadline instead (below)
EXIT_GIVE_UP_MINUTES = 16  # ... and this long after it a position that still cannot be closed raises an error alert (it keeps being retried)
RECONCILE_MINUTES = 11  # the candle backtest is computed this long after the exit minute
SUBSCRIBE_STRIKES = 10
RECONCILE_RETRY_SECONDS = 600


# --------------------------------------------------------------------------- quotes (READ-ONLY)


@dataclass(frozen=True)
class Quote:
    key: str
    bid: float | None
    ask: float | None
    ltp: float | None
    received_at: datetime  # when our stream handler stored it
    ltt: datetime | None  # the exchange's last-trade time


class QuoteSource(Protocol):
    def quote(self, instrument_key: str) -> Quote | None: ...
    def spot(self) -> Quote | None: ...


def parse_quote(instrument_key: str, received_at: str, payload: dict[str, Any]) -> Quote:
    """A stored full-mode stream message -> Quote. Best bid/ask is level 1 of the depth; a zero price is no price."""
    feed = payload.get("fullFeed", {})
    leaf = feed.get("marketFF") or feed.get("indexFF") or {}
    ltpc = leaf.get("ltpc") or {}
    levels = (leaf.get("marketLevel") or {}).get("bidAskQuote") or []
    best = levels[0] if levels else {}
    positive = lambda value: float(value) if value not in (None, "") and float(value) > 0 else None
    ltt = ltpc.get("ltt")
    return Quote(
        key=instrument_key,
        bid=positive(best.get("bidP")),
        ask=positive(best.get("askP")),
        ltp=positive(ltpc.get("ltp")),
        received_at=datetime.fromisoformat(received_at),
        ltt=datetime.fromtimestamp(int(ltt) / 1000, ZoneInfo("Asia/Kolkata")) if ltt else None,
    )


class StreamQuotes:
    """Latest quotes as the live stream stored them in market_observations (only ever read)."""

    def __init__(self, clock: Callable[[], datetime], day: str | None = None):
        self._clock = clock
        self._day = day  # read from this session day on (default: the clock's own date)

    def quote(self, instrument_key: str) -> Quote | None:
        now = self._clock()
        row = query_latest_tick(instrument_key, since=f"{self._day or now.date().isoformat()}T00:00:00")
        return parse_quote(instrument_key, row["received_at"], row["payload"]) if row else None

    def spot(self) -> Quote | None:
        return self.quote(UNDERLYING.index_key)


def quote_age(quote: Quote, now: datetime) -> float:
    return (now - quote.received_at).total_seconds()


def feed_lag(quote: Quote) -> float | None:
    return (quote.received_at - quote.ltt).total_seconds() if quote.ltt else None


# --------------------------------------------------------------------------- fills


def fill_for(side: str, quote: Quote | None, now: datetime) -> dict[str, Any] | None:
    """The fill for one leg, or None if there is no usable price at all.

    SELL -> the best bid, BUY -> the best ask. Only when that side of the book is missing does it fall back to
    LTP -/+ 0.20, flagged "estimated" (a sold price never below one tick). LTP itself is never a fill while a
    bid/ask exists."""
    if quote is None:
        return None
    record = {"ts": now.isoformat(timespec="seconds"), "bid": quote.bid, "ask": quote.ask, "ltp": quote.ltp}
    if side == "SELL" and quote.bid is not None:
        return {**record, "fill": quote.bid, "source": "bid"}
    if side == "BUY" and quote.ask is not None:
        return {**record, "fill": quote.ask, "source": "ask"}
    if quote.ltp is not None:
        price = max(0.05, quote.ltp - ESTIMATE_OFFSET) if side == "SELL" else quote.ltp + ESTIMATE_OFFSET
        return {**record, "fill": round(price, 2), "source": "estimated"}
    return None


def closing_side(side: str) -> str:
    return "BUY" if side == "SELL" else "SELL"


# --------------------------------------------------------------------------- the stream hook


_wanted_lock = threading.Lock()
_wanted: set[str] = set()


def wanted_stream_instruments() -> list[str]:
    """Instrument keys the paper engine wants the live stream subscribed to. The stream polls this."""
    with _wanted_lock:
        return sorted(_wanted)


def _want(keys: list[str]) -> None:
    with _wanted_lock:
        _wanted.update(keys)


# --------------------------------------------------------------------------- the engine


class PaperEngine:
    def __init__(
        self,
        quotes: QuoteSource,
        *,
        clock: Callable[[], datetime] | None = None,
        token_provider: Callable[[], str | None] = lambda: os.getenv("UPSTOX_ACCESS_TOKEN"),
        lots: int | None = None,
        strategies: tuple[str, ...] = lab.STRATEGIES,
        expiry_for: Callable[[str], str] | None = None,
        ensure_contracts: Callable[[str], None] | None = None,
        reconcile: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        cost_model: lab.CostModel = lab.DEFAULT_COST_MODEL,
        heartbeat: Callable[[], datetime | None] | None = None,
    ):
        tz = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
        self.clock = clock or (lambda: datetime.now(tz))
        self.quotes = quotes
        self.token_provider = token_provider
        self.lots = lots if lots is not None else int(os.getenv("PAPER_LOTS", "1"))
        self.strategies = strategies
        self.cost_model = cost_model
        self._heartbeat = heartbeat  # when the live stream last delivered anything (any instrument), if the host can say
        self._expiry_for = expiry_for or self._default_expiry_for
        self._ensure_contracts = ensure_contracts or self._default_ensure_contracts
        self._reconcile_fn = reconcile or (lambda trade: lab.reconcile_trade(trade, self.token_provider, self.cost_model))
        self._resolver = None
        self._resolver_day: date | None = None
        self._subscribed_day: str | None = None
        self._blockers: dict[tuple[str, str], str] = {}
        self._last_price: dict[str, float] = {}
        self._alerted: set[tuple[str, str]] = set()
        self._reconcile_tried: dict[tuple[str, str], datetime] = {}
        self.last_tick: datetime | None = None
        self.last_error: str | None = None
        self.feed_age_seconds: float | None = None  # seconds since the stream last delivered anything, at the last tick (None: unknown)
        self._stalled_since: datetime | None = None

    # ---- contracts and expiry (data reads)

    def _get_resolver(self, now: datetime):
        from straddle import ContractResolver

        # A fresh resolver each trading day: it caches the nearest live expiry and the lapsed list, which a server
        # left running across an expiry would otherwise keep reporting as current.
        if self._resolver is None or self._resolver_day != now.date():
            self._resolver = ContractResolver(UNDERLYING, self.token_provider, True, now.date(), lambda line: logger.info(line))
            self._resolver_day = now.date()
        return self._resolver

    def _default_expiry_for(self, day: str) -> str:
        return self._get_resolver(self.clock()).nearest_expiry(day)

    def _default_ensure_contracts(self, expiry: str) -> None:
        self._get_resolver(self.clock())._list_contracts(expiry)

    def _key(self, expiry: str, strike: float, leg_type: str) -> str | None:
        key = find_option_contract(UNDERLYING.index_key, expiry, strike, leg_type, expired=False)
        if key is None:
            self._ensure_contracts(expiry)
            key = find_option_contract(UNDERLYING.index_key, expiry, strike, leg_type, expired=False)
        return key

    # ---- one tick

    def tick(self) -> None:
        now = self.clock()
        self.last_tick = now
        if not is_market_day(now.date()):
            return
        day, t = now.date().isoformat(), now.time()
        exit_time = exit_time_for(day)
        self._subscribe_open_positions()
        self._close_stale_positions(day, now)
        if t >= MARKET_OPEN:
            self._mark_held(day, now)  # an overnight S5 from an earlier session: its morning, until its 09:30 exit is done
        if t < HEALTH_TIME:
            return
        self._health(day, now)
        if t >= SUBSCRIBE_TIME:
            self._presubscribe(day, now)
        if t >= ENTRY_TIME:
            self._s5_exit(day, now)  # yesterday's overnight position first, then today's own entries
            self._enter(day, now)
            self._s5_enter(day, now)
        if ENTRY_TIME <= t <= exit_time.replace(second=59):
            self._watch_feed(day, now)
            self._mark(day, now)
        if t >= EXIT_ATTEMPT_START:
            self._exit(day, now)
        if t >= _later(exit_time, RECONCILE_MINUTES):
            self._reconcile(now)

    def _subscribe_open_positions(self) -> None:
        """Keeps every leg of every open position on the live stream. The wanted list is in memory, so a backend
        restart empties it, and the stream's own discovery only covers today's ATM +/-5 in the current week - an
        overnight S5 position (rolled into next week, or far from today's ATM after a big move) would otherwise have
        no quote to exit on at 09:30."""
        _want([leg["instrument_key"] for trade in query_paper_trades() if trade["status"] == "open" for leg in trade["legs"] or [] if leg.get("instrument_key")])

    def _alert(self, day: str, key: str, level: str, message: str) -> None:
        if (day, key) not in self._alerted:
            self._alerted.add((day, key))
            paper_add_alert(day, level, message)
            logger.warning("PAPER %s: %s", level, message)

    def _log_if_estimated(self, strategy: str, day: str, phase: str, leg: dict[str, Any], requested_side: str, fill: dict[str, Any] | None, quote: Quote) -> None:
        """Every estimated fill (no bid in the book for a SELL, no ask for a BUY - fill_for() falls back to LTP
        -/+ ESTIMATE_OFFSET) is logged with which leg and why, never silent like a normal bid/ask fill."""
        if fill is None or not str(fill.get("source", "")).startswith("estimated"):
            return
        missing = "bid" if requested_side == "SELL" else "ask"
        self._alert(day, f"estimated-{strategy}-{phase}-{leg['role']}-{leg['type']}-{leg['strike']:g}", "info",
                    f"PAPER {strategy} {day}: {phase} fill for {leg['role']} {leg['type']} {leg['strike']:g} estimated - "
                    f"no {missing} in the book (bid={quote.bid}, ask={quote.ask}, ltp={quote.ltp})")

    # ---- 09:20 health check

    def _health(self, day: str, now: datetime) -> None:
        if paper_get_health(day) is not None:
            return
        problems = []
        status = token_status(self.token_provider())
        if not status.get("configured"):
            problems.append("no Upstox token is configured")
        elif status.get("expired"):
            problems.append("the Upstox token has expired - reconnect Upstox")
        spot = self.quotes.spot()
        if spot is None:
            problems.append("no NIFTY spot quote from the live feed today")
        elif quote_age(spot, now) > HEALTH_FEED_FRESH_SECONDS:
            problems.append(f"the live feed is stale (last NIFTY tick {quote_age(spot, now):.0f}s ago)")
        elif (lag := feed_lag(spot)) is not None and lag > FEED_LAG_MAX_SECONDS:
            problems.append(f"the live feed is running {lag:.0f}s behind the exchange")
        if problems:
            reason = "; ".join(problems)
            if now.time() < ENTRY_TIME:
                # Not final yet: logging in late or the feed coming back before 09:30 still saves the day, so this is
                # re-checked every tick and only recorded - writing the day's entries off - if 09:30 arrives still failing.
                self._alert(day, "health-at-risk", "warn", f"PAPER health check failing at {now:%H:%M}: {reason}. Re-checking until 09:30.")
                return
            paper_set_health(day, False, reason)
            self._alert(day, "health", "error", f"PAPER health check failed at {now:%H:%M}: {reason}. Today's entries are logged as missed.")
            self._write_missed(day, now, f"health check failed: {reason}")
        else:
            paper_set_health(day, True, "token valid, feed live")

    def _write_missed(self, day: str, now: datetime, reason: str) -> None:
        for strategy in self.strategies:
            if paper_get_trade(strategy, day) is None:
                paper_insert_trade(self._row(strategy, day, "missed_entry", now, notes=reason))

    def _row(self, strategy: str, day: str, status: str, now: datetime, **fields: Any) -> dict[str, Any]:
        return {
            "strategy": strategy, "trading_date": day, "lots": self.lots, "status": status, "build_version": PAPER_BUILD_VERSION,
            "created_at": now.isoformat(timespec="seconds"), "updated_at": now.isoformat(timespec="seconds"), **fields,
        }

    # ---- 09:25 pre-subscribe

    def _presubscribe(self, day: str, now: datetime) -> None:
        if self._subscribed_day == day:
            return
        health = paper_get_health(day)
        spot = self.quotes.spot()
        if not health or not health["ok"] or spot is None or spot.ltp is None:
            return
        try:
            expiry = self._expiry_for(day)
            centre = resolve_atm_strike(spot.ltp, lab.STRIKE_STEP)
            keys = [
                key
                for k in range(-SUBSCRIBE_STRIKES, SUBSCRIBE_STRIKES + 1)
                for leg in ("CE", "PE")
                if (key := self._key(expiry, centre + k * lab.STRIKE_STEP, leg))
            ]
        except Exception as error:  # noqa: BLE001 - retried on the next tick; the entry itself will report a blocker
            self.last_error = f"pre-subscribe: {type(error).__name__}: {error}"
            logger.warning("PAPER pre-subscribe failed: %s", error)
            return
        _want(keys)
        self._subscribed_day = day
        logger.info("PAPER pre-subscribing %d instruments around ATM %g for %s", len(keys), centre, expiry)

    # ---- 09:30 entry

    def _context(self, day: str, now: datetime) -> tuple[dict[str, Any] | None, str | None]:
        """Spot, ATM, expiry, premium at the moment of entry - or the reason there is none."""
        spot = self.quotes.spot()
        if spot is None or spot.ltp is None:
            return None, "no NIFTY spot quote"
        if quote_age(spot, now) > STALE_QUOTE_SECONDS:
            return None, f"NIFTY spot quote is {quote_age(spot, now):.0f}s old"
        if (lag := feed_lag(spot)) is not None and lag > FEED_LAG_MAX_SECONDS:
            return None, f"the live feed is {lag:.0f}s behind the exchange"
        atm = resolve_atm_strike(spot.ltp, lab.STRIKE_STEP)  # the same guarded function the backtest uses
        expiry = self._expiry_for(day)
        keys = {leg: self._key(expiry, atm, leg) for leg in ("CE", "PE")}
        if None in keys.values():
            return None, f"no listed ATM contract at {atm:g} for {expiry}"
        quotes = {leg: self.quotes.quote(key) for leg, key in keys.items()}
        _want(list(keys.values()))
        if any(q is None or quote_age(q, now) > STALE_QUOTE_SECONDS for q in quotes.values()):
            return None, f"no fresh ATM {atm:g} quotes"
        prices = []
        for q in quotes.values():
            price = q.ltp if q.ltp is not None else ((q.bid + q.ask) / 2 if q.bid is not None and q.ask is not None else None)
            if price is None:
                return None, f"no ATM {atm:g} price"
            prices.append(price)
        # premium = the ATM straddle's price at 09:30 (LTP; the candle backtest uses the 09:30 bar's open)
        return {"spot": spot.ltp, "atm": atm, "expiry": expiry, "premium": round(sum(prices), 4), "ce_key": keys["CE"]}, None

    def _enter(self, day: str, now: datetime) -> None:
        pending = [s for s in self.strategies if paper_get_trade(s, day) is None]
        if not pending:
            return
        entry_at = datetime.combine(now.date(), ENTRY_TIME, tzinfo=now.tzinfo)
        if (now - entry_at).total_seconds() > ENTRY_GRACE_SECONDS:
            for strategy in pending:
                blocker = self._blockers.get((day, strategy)) or "the engine was not running at 09:30"
                paper_insert_trade(self._row(strategy, day, "missed_entry", now, notes=f"not entered by 09:31: {blocker}"))
                self._alert(day, f"missed-{strategy}", "error", f"PAPER {strategy}: missed entry on {day} - {blocker}")
            return
        context, blocker = self._context(day, now)
        for strategy in pending:
            reason = blocker
            if context is not None:
                reason = self._try_enter(strategy, day, now, context)
            if reason:
                self._blockers[(day, strategy)] = reason

    def _build_entry_legs(self, strategy: str, day: str, now: datetime, context: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Fills the legs in plan order (iron flies: the two BUYS first, then the two SELLS) from CURRENT quotes.
        (legs, None) once fully filled, or (None, reason) it could not be (yet) - shared by the automatic 09:30
        entry (_try_enter()) and a manual retry of a missed one (retry_missed_entry()); only how the RESULT gets
        written (insert vs. update an existing missed_entry row) differs between them."""
        specs = lab.plan_legs(strategy, context["atm"], context["premium"], lab.STRIKE_STEP)
        keys = []
        for spec in specs:
            key = self._key(context["expiry"], spec["strike"], spec["type"])
            if key is None:
                return None, f"no listed contract for {spec['type']} {spec['strike']:g}"
            keys.append(key)
        _want(keys)  # a wing outside the pre-subscribed strikes gets subscribed now
        legs = []
        for spec, key in zip(specs, keys):
            quote = self.quotes.quote(key)
            if quote is None or quote_age(quote, now) > STALE_QUOTE_SECONDS:
                return None, f"no fresh quote for {spec['type']} {spec['strike']:g}"
            fill = fill_for(spec["side"], quote, now)
            if fill is None:
                return None, f"no price for {spec['type']} {spec['strike']:g}"
            self._log_if_estimated(strategy, day, "entry", spec, spec["side"], fill, quote)
            legs.append({**spec, "instrument_key": key, "entry": fill, "exit": None})
        return legs, None

    def _try_enter(self, strategy: str, day: str, now: datetime, context: dict[str, Any]) -> str | None:
        legs, reason = self._build_entry_legs(strategy, day, now, context)
        if legs is None:
            return reason
        lot_size = lab.lot_size_of(context["ce_key"])
        if lot_size is None:
            return "unknown lot size"
        inserted = paper_insert_trade(self._row(
            strategy, day, "open", now, expiry=context["expiry"], lot_size=lot_size, atm_strike=context["atm"],
            spot_at_entry=context["spot"], premium_ref=context["premium"],
            wing_width=lab.wing_width(strategy, context["premium"]) if strategy != "straddle_sell" else None, legs=legs,
        ))
        if inserted:
            self._blockers.pop((day, strategy), None)
            logger.info("PAPER entered %s on %s: ATM %g", strategy, day, context["atm"])
        return None

    # ---- S5 entry (09:30, after straddle_sell's own entry for the day is known)

    def _s5_enter(self, day: str, now: datetime) -> None:
        """S5's entry for `day`: the SAME straddle straddle_sell just sold, held overnight at its own fill - no new
        order, so it can never disagree with straddle_sell about what happened at entry. On an expiry-day entrant
        (straddle_sell's own contract expires this afternoon, nothing left to hold overnight) S5 instead sells the
        SAME ATM strike fresh in next week's expiry (the roll) and holds THAT overnight - the live version of the
        backtest's s5_overnight_rows() rule. Mirrors _enter()'s grace/retry shape."""
        if paper_get_trade(lab.S5, day) is not None:
            return
        entry_at = datetime.combine(now.date(), ENTRY_TIME, tzinfo=now.tzinfo)
        grace_expired = (now - entry_at).total_seconds() > ENTRY_GRACE_SECONDS
        s1 = paper_get_trade("straddle_sell", day)
        if s1 is None or s1["status"] != "open":
            if s1 is not None or grace_expired:
                reason = f"straddle_sell {'did not enter' if s1 is None else 'was ' + s1['status']} on {day}: nothing to hold overnight or roll from"
                paper_insert_trade(self._row(lab.S5, day, "missed_entry", now, notes=reason))
                self._alert(day, f"missed-{lab.S5}", "error", f"PAPER {lab.S5}: missed entry on {day} - {reason}")
            return
        if s1["expiry"] != day:
            legs = [{**leg, "exit": None} for leg in s1["legs"]]
            paper_insert_trade(self._row(
                lab.S5, day, "open", now, expiry=s1["expiry"], lot_size=s1["lot_size"], atm_strike=s1["atm_strike"],
                spot_at_entry=s1["spot_at_entry"], premium_ref=s1["premium_ref"], legs=legs,
            ))
            logger.info("PAPER S5 entered %s: holding straddle_sell's own %s contracts overnight", day, day)
            return
        reason = self._s5_try_roll(day, now, s1["atm_strike"])
        if reason:
            self._blockers[(day, lab.S5)] = reason
            if grace_expired:
                paper_insert_trade(self._row(lab.S5, day, "missed_entry", now, notes=f"not entered by 09:31 (rolled): {reason}"))
                self._alert(day, f"missed-{lab.S5}", "error", f"PAPER {lab.S5}: missed entry on {day} - {reason}")

    def _build_s5_roll_legs(self, day: str, now: datetime, atm: float) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
        """The roll's own legs - a fresh SELL of `atm` in the next weekly expiry after `day`'s own (straddle_sell's
        expiry) - or the reason they are not ready yet. (legs, None, roll_expiry) once filled, else (None, reason,
        None). Shared by the automatic entry (_s5_try_roll()) and a manual retry of a missed one."""
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        try:
            roll_expiry = self._expiry_for(next_day)
        except Exception as error:  # noqa: BLE001 - retried on the next tick, same as pre-subscribe
            return None, f"{type(error).__name__}: {error}", None
        if roll_expiry <= day:
            return None, f"no expiry known after {day} to roll into", None
        keys: dict[str, str] = {}
        for leg in ("CE", "PE"):
            key = self._key(roll_expiry, atm, leg)
            if key is None:
                return None, f"no listed contract for {leg} {atm:g} in {roll_expiry}", None
            keys[leg] = key
        _want(list(keys.values()))
        legs = []
        for leg_type, key in keys.items():
            quote = self.quotes.quote(key)
            if quote is None or quote_age(quote, now) > STALE_QUOTE_SECONDS:
                return None, f"no fresh quote for {leg_type} {atm:g} ({roll_expiry})", None
            fill = fill_for("SELL", quote, now)
            if fill is None:
                return None, f"no price for {leg_type} {atm:g} ({roll_expiry})", None
            self._log_if_estimated(lab.S5, day, "entry", {"role": "body", "type": leg_type, "strike": atm}, "SELL", fill, quote)
            legs.append({"role": "body", "type": leg_type, "strike": atm, "side": "SELL", "instrument_key": key, "entry": fill, "exit": None})
        return legs, None, roll_expiry

    def _s5_try_roll(self, day: str, now: datetime, atm: float) -> str | None:
        """The roll: a fresh SELL of `atm` in the next weekly expiry after `day`'s own (straddle_sell's expiry).
        None = entered; else the reason it has not (yet)."""
        legs, reason, roll_expiry = self._build_s5_roll_legs(day, now, atm)
        if legs is None:
            return reason
        lot_size = lab.lot_size_of(legs[0]["instrument_key"])
        if lot_size is None:
            return "unknown lot size"
        prices = [leg["entry"]["ltp"] for leg in legs if leg["entry"].get("ltp") is not None]
        inserted = paper_insert_trade(self._row(
            lab.S5, day, "open", now, expiry=roll_expiry, lot_size=lot_size, atm_strike=atm,
            spot_at_entry=None, premium_ref=round(sum(prices), 4) if len(prices) == 2 else None, legs=legs,
        ))
        if inserted:
            self._blockers.pop((day, lab.S5), None)
            logger.info("PAPER S5 rolled on %s: ATM %g into %s", day, atm, roll_expiry)
        return None

    # ---- manual exit (a person clicking "Exit now" on Today, not the strategy's own exit rule)

    def exit_now(self, strategy: str, day: str, now: datetime) -> str | None:
        """Closes one open paper position right now at the current quotes. `day` is the trade's own entry date (an
        overnight S5 position is yesterday's). A person's choice rather than the strategy's exit, so the result is
        flagged MANUAL EXIT - still shown and counted everywhere - and any earlier flag (a manual entry) is
        kept. Only while the market is trading, since every leg needs a fresh quote. None = closed; else the reason."""
        trade = paper_get_trade(strategy, day)
        if trade is None or trade["status"] != "open":
            return "only an open position can be exited"
        today = now.date().isoformat()
        if not is_market_day(now.date()) or not (MARKET_OPEN <= now.time() <= exit_time_for(today)):
            return "the market is closed - a position can only be exited while it is trading"
        if not self._try_exit(trade, now, now):
            return "no fresh quote for every leg yet - try again in a moment"
        closed = paper_get_trade(strategy, day)
        stamp = f"exited manually at {now:%H:%M:%S}"
        paper_update_trade(strategy, day, {
            "status": "closed",
            "invalid_reason": "; ".join(r for r in (trade.get("invalid_reason"), f"{stamp} - not the strategy's own exit") if r),
            "notes": f"{closed['notes']}; {stamp}" if closed.get("notes") else stamp,
        })
        self._alert(day, f"manual-exit-{strategy}", "warn", f"PAPER {strategy} on {day}: {stamp} - flagged MANUAL EXIT")
        return None

    # ---- manual retry of a missed entry (a person clicking "retry" on Today, not the automatic 09:30 attempt)

    def retry_missed_entry(self, strategy: str, day: str, now: datetime) -> str | None:
        """Manually retry a strategy's missed entry for `day` (today only, while the market is still open) - the
        automatic 09:30 attempt already gave up (a health-check failure, a feed gap, a listing not resolved
        yet...) and never tries again on its own; a `missed_entry` row just sits there for the rest of the day.

        Fills at whatever CURRENT quotes are available, not 09:30's, so the result can never be fairly compared to
        the 09:30 backtest the way a normal entry can - it is written with invalid_reason set (the same treatment
        _try_exit() gives a very-late exit: flagged, but still
        fully visible everywhere, including this same Today screen). Writes with paper_update_trade() into
        the EXISTING missed_entry row, never paper_insert_trade() - the normal entry path only ever inserts, so it
        would silently no-op against a row that already exists. None = entered; else the reason it could not be."""
        if not is_market_day(now.date()) or day != now.date().isoformat():
            return "only today's own missed entry can be retried, while the market is open"
        trade = paper_get_trade(strategy, day)
        if trade is None:
            return "no entry has been recorded yet for this strategy today"
        if trade["status"] != "missed_entry":
            return f"already {trade['status']} - nothing to retry"
        exit_at = datetime.combine(date.fromisoformat(day), exit_time_for(day), tzinfo=now.tzinfo)
        if now >= exit_at:
            return "too late in the day for a fresh entry - the market is effectively closed"
        if strategy == lab.S5:
            return self._retry_s5_missed_entry(day, now)
        context, blocker = self._context(day, now)
        if context is None:
            return blocker
        legs, reason = self._build_entry_legs(strategy, day, now, context)
        if legs is None:
            return reason
        lot_size = lab.lot_size_of(context["ce_key"])
        if lot_size is None:
            return "unknown lot size"
        delay = (now - datetime.combine(date.fromisoformat(day), ENTRY_TIME, tzinfo=now.tzinfo)).total_seconds()
        paper_update_trade(strategy, day, {
            "status": "open", "expiry": context["expiry"], "lot_size": lot_size, "atm_strike": context["atm"],
            "spot_at_entry": context["spot"], "premium_ref": context["premium"],
            "wing_width": lab.wing_width(strategy, context["premium"]) if strategy != "straddle_sell" else None, "legs": legs,
            "invalid_reason": f"entered manually {delay:.0f}s after 09:30 (missed by the automatic entry) - not comparable to the 09:30 backtest",
            "notes": f"recovered manually at {now:%H:%M:%S}",
        })
        self._blockers.pop((day, strategy), None)
        logger.info("PAPER %s manually entered on %s at %s (recovered from missed_entry)", strategy, day, now.strftime("%H:%M:%S"))
        self._alert(day, f"recovered-{strategy}", "warn", f"PAPER {strategy} on {day}: entry manually recovered at {now:%H:%M:%S} ({delay:.0f}s late) - marked invalid")
        return None

    def _retry_s5_missed_entry(self, day: str, now: datetime) -> str | None:
        """S5's own manual retry: reuse straddle_sell's own CURRENT fill, or attempt the same-week roll, exactly
        like the automatic entry would (see _s5_enter()) - but writing into the EXISTING missed_entry row and
        marking the result invalid, same reasoning as retry_missed_entry()'s own docstring."""
        s1 = paper_get_trade("straddle_sell", day)
        if s1 is None or s1["status"] != "open":
            return "straddle_sell has no open position today to hold overnight or roll from - retry it first"
        delay = (now - datetime.combine(date.fromisoformat(day), ENTRY_TIME, tzinfo=now.tzinfo)).total_seconds()
        if s1["expiry"] != day:
            legs = [{**leg, "exit": None} for leg in s1["legs"]]
            paper_update_trade(lab.S5, day, {
                "status": "open", "expiry": s1["expiry"], "lot_size": s1["lot_size"], "atm_strike": s1["atm_strike"],
                "spot_at_entry": s1["spot_at_entry"], "premium_ref": s1["premium_ref"], "legs": legs,
                "invalid_reason": f"entered manually {delay:.0f}s after 09:30 (missed by the automatic entry) - straddle_sell's own current fill reused",
                "notes": f"recovered manually at {now:%H:%M:%S}",
            })
            self._blockers.pop((day, lab.S5), None)
            logger.info("PAPER S5 manually entered %s: holding straddle_sell's own %s contracts overnight (recovered)", day, day)
            return None
        legs, reason, roll_expiry = self._build_s5_roll_legs(day, now, s1["atm_strike"])
        if legs is None:
            return reason
        lot_size = lab.lot_size_of(legs[0]["instrument_key"])
        if lot_size is None:
            return "unknown lot size"
        prices = [leg["entry"]["ltp"] for leg in legs if leg["entry"].get("ltp") is not None]
        paper_update_trade(lab.S5, day, {
            "status": "open", "expiry": roll_expiry, "lot_size": lot_size, "atm_strike": s1["atm_strike"],
            "spot_at_entry": None, "premium_ref": round(sum(prices), 4) if len(prices) == 2 else None, "legs": legs,
            "invalid_reason": f"entered manually {delay:.0f}s after 09:30 (missed by the automatic entry) - rolled into next week's expiry",
            "notes": f"recovered manually at {now:%H:%M:%S}",
        })
        self._blockers.pop((day, lab.S5), None)
        logger.info("PAPER S5 manually rolled %s into %s (recovered)", day, roll_expiry)
        return None

    # ---- marks

    def _open_trades(self, day: str) -> list[dict[str, Any]]:
        return [t for t in query_paper_trades(None, day, day) if t["status"] == "open"]

    def _leg_mark_price(self, leg: dict[str, Any], now: datetime) -> float | None:
        """What it would cost to close the leg right now: a short at the ask, a long at the bid (LTP if that side is empty)."""
        quote = self.quotes.quote(leg["instrument_key"])
        if quote is not None and quote_age(quote, now) <= STALE_QUOTE_SECONDS * 3:
            price = (quote.ask if leg["side"] == "SELL" else quote.bid) or quote.ltp
            if price is not None:
                self._last_price[leg["instrument_key"]] = price
                return price
        return self._last_price.get(leg["instrument_key"])

    @property
    def feed_stalled(self) -> bool:
        return self._stalled_since is not None

    def _watch_feed(self, day: str, now: datetime) -> None:
        """The live feed is alive while the stream keeps delivering something. One quiet option (or NIFTY itself in a
        lull) is not a stall, so the heartbeat is the newest message of any instrument, falling back to NIFTY's newest
        tick when the host cannot say."""
        newest = self._heartbeat() if self._heartbeat else None
        if newest is None:
            spot = self.quotes.spot()
            newest = spot.received_at if spot is not None else None
        self.feed_age_seconds = round(max(0.0, (now - newest).total_seconds()), 1) if newest is not None else None
        stalled = self.feed_age_seconds is not None and self.feed_age_seconds > FEED_STALL_SECONDS
        if stalled and self._stalled_since is None:
            self._stalled_since = now
            self._alert(day, f"feed-stall-{now:%H%M}", "warn", f"PAPER live feed stalled at {now:%H:%M:%S} (nothing received for {self.feed_age_seconds:.0f}s) - mark-to-market is paused until it resumes")
        elif not stalled and self._stalled_since is not None:
            logger.info("PAPER live feed resumed at %s after %.0fs", now.strftime("%H:%M:%S"), (now - self._stalled_since).total_seconds())
            self._stalled_since = None

    def _mtm(self, trade: dict[str, Any], now: datetime) -> float | None:
        """The trade's mark-to-market in rupees right now, or None while any leg has no price yet."""
        pnl = 0.0
        for leg in trade["legs"]:
            price = self._leg_mark_price(leg, now)
            if price is None:
                return None
            pnl += lab.leg_pnl_pts(leg["side"], leg["entry"]["fill"], price)
        return round(pnl * trade["lot_size"] * trade["lots"], 2)

    def _mark(self, day: str, now: datetime) -> None:
        """The running mark-to-market. Refreshed on every tick, so the current minute's row always holds the newest value
        (and the minute's last one once it is over). Nothing is written while the feed is stalled: an old price would
        be shown as a live mark."""
        if self.feed_stalled:
            return
        minute = now.strftime("%H:%M")
        for trade in self._open_trades(day):
            value = self._mtm(trade, now)
            if value is not None:
                paper_upsert_mark(trade["strategy"], day, minute, value)

    def _mark_held(self, day: str, now: datetime) -> None:
        """An overnight S5 position from an earlier session, marked from this session's 09:15 open until its exit is done
        (09:30, or later while the exit is still being worked) - without this its line stopped at the previous close and
        the morning it is still held for went unrecorded. Filed under its OWN entry day with the minute past 24:00 ("33:20"
        is 09:20 the next session), so it sorts after that day's 15:39 and never mixes into today's own S5 line, which
        starts at 09:30 under today's date."""
        held = [t for t in query_paper_trades(lab.S5) if t["status"] == "open" and t["trading_date"] < day]
        if not held:
            return
        if now.time() < ENTRY_TIME:
            self._watch_feed(day, now)  # from 09:30 the session's own marking watches the feed
        if self.feed_stalled:
            return
        minute = f"{now.hour + 24:02d}:{now.minute:02d}"
        for trade in held:
            value = self._mtm(trade, now)
            if value is not None:
                paper_upsert_mark(trade["strategy"], trade["trading_date"], minute, value)

    # ---- exit (starts 15:29:50, the same moment the candle backtest prices its own 15:29 close; retries run up
    # to exit_time_for() - 15:39, the extended session's real last tradeable minute - see EXIT_ATTEMPT_START)

    def _exit(self, day: str, now: datetime) -> None:
        """straddle_sell, iron_fly_w1 and iron_fly_w2's own same-day close. Never S5: today's S5 entry (dated `day`,
        same as everything else) stays open past today's close on purpose - it is _s5_exit(), not this, that closes
        it, at tomorrow's 09:30."""
        exit_at = datetime.combine(date.fromisoformat(day), EXIT_ATTEMPT_START, tzinfo=now.tzinfo)
        hard_deadline = datetime.combine(date.fromisoformat(day), exit_time_for(day), tzinfo=now.tzinfo)
        for trade in self._open_trades(day):
            if trade["strategy"] != lab.S5:
                self._try_exit(trade, now, exit_at, hard_deadline=hard_deadline)

    def _close_stale_positions(self, today: str, now: datetime) -> None:
        """A position still open from an earlier day (the backend was down through its whole exit window) is closed
        on whatever quote can still be found - always invalid (see _try_exit()'s `hard_deadline`), since by the time
        this runs the real market for that day is unambiguously long closed. Never S5: it is SUPPOSED to still be
        open from yesterday until _s5_exit() closes it at today's 09:30 - that is not staleness."""
        for trade in [t for t in query_paper_trades() if t["status"] == "open" and t["trading_date"] < today and t["strategy"] != lab.S5]:
            exit_at = datetime.combine(date.fromisoformat(trade["trading_date"]), EXIT_ATTEMPT_START, tzinfo=now.tzinfo)
            self._try_exit(trade, now, exit_at, accept_stale=True, forced=True, hard_deadline=now)

    def _s5_exit(self, day: str, now: datetime) -> None:
        """S5 positions still open from an earlier day are bought back today at 09:30 - the live version of the backtest's
        overnight exit rule (held 09:30 to the next trading day's 09:30). Reuses _try_exit()'s normal fresh/stale/give-up
        escalation exactly as S1-S3's own same-day exit did before it gained a `hard_deadline` (there is no "market
        closes" constraint at 09:30 the way there is at day's end, so S5 keeps the old, more patient ladder)."""
        exit_at = datetime.combine(date.fromisoformat(day), ENTRY_TIME, tzinfo=now.tzinfo)
        for trade in query_paper_trades(lab.S5):
            if trade["status"] == "open" and trade["trading_date"] < day:
                self._try_exit(trade, now, exit_at)

    def _try_exit(
        self, trade: dict[str, Any], now: datetime, exit_at: datetime, accept_stale: bool = False, forced: bool = False,
        hard_deadline: datetime | None = None,
    ) -> bool:
        """Buy back the shorts first (the wings still hedge them), then sell the wings. False = not (fully) closed yet.
        `exit_at` is when this trade is SUPPOSED to close - 15:29:50 for S1-S3 (EXIT_ATTEMPT_START, matching the
        candle backtest's own 15:29 close), tomorrow's 09:30 for S5 - so it is the caller's job to say, not derived
        from trade["trading_date"] (which for S5 is the ENTRY day).

        `hard_deadline`, only passed by _exit()/_close_stale_positions() (S1-S3's own same-day close), is the real
        last-tradeable minute - past it there is no more market left to get a fresh quote from, so this stops
        waiting and resolves the trade right here: closed on whatever price is available (the last traded price,
        however old), or - if there is truly nothing for a leg at all - an error. Either way the result is flagged
        invalid_reason, since it can no longer represent EXIT_ATTEMPT_START; a fresh quote obtained very late is
        flagged the same way once its own delay exceeds EXIT_INVALID_AFTER_SECONDS. Without a hard_deadline (S5's
        own 09:30 exit), the older, more patient stale/give-up ladder below still applies unchanged."""
        day = trade["trading_date"]
        exit_time = exit_at.time()
        past_hard_deadline = hard_deadline is not None and now >= hard_deadline
        stale_ok = accept_stale or past_hard_deadline or now.time() >= _later(exit_time, EXIT_ACCEPT_STALE_MINUTES)
        legs = [dict(leg) for leg in trade["legs"]]
        order = sorted(range(len(legs)), key=lambda i: 0 if legs[i]["side"] == "SELL" else 1)
        for i in order:
            if legs[i].get("exit"):
                continue
            quote = self.quotes.quote(legs[i]["instrument_key"])
            fresh = quote is not None and quote_age(quote, now) <= STALE_QUOTE_SECONDS
            if quote is None or not (fresh or stale_ok):
                break
            requested_side = closing_side(legs[i]["side"])
            fill = fill_for(requested_side, quote, now)
            if fill is None:
                break
            self._log_if_estimated(trade["strategy"], day, "exit", legs[i], requested_side, fill, quote)
            if not fresh:
                fill = {**fill, "source": f"{fill['source']}_stale"}
            legs[i]["exit"] = fill
        done = all(leg.get("exit") for leg in legs)
        if not done:
            if past_hard_deadline:
                paper_update_trade(trade["strategy"], day, {
                    "legs": legs, "status": "error", "invalid_reason": "no exit quote for one or more legs by market close",
                    "notes": f"no exit quote by {hard_deadline.time():%H:%M} (market close) - position could not be closed, result unknown",
                })
                self._alert(day, f"exit-{trade['strategy']}", "error", f"PAPER {trade['strategy']} on {day} could not be closed by market close - result unknown, marked invalid")
                return True
            give_up = _later(exit_time, EXIT_GIVE_UP_MINUTES)
            if now.time() >= give_up and not forced:
                # The position is still held, so it is never abandoned: keep trying every tick and close it, flagged late,
                # as soon as a quote exists. Marking it "result unknown" here (the old rule) left a backend that started
                # late with an overnight position nobody managed any more.
                self._alert(day, f"exit-{trade['strategy']}", "error", f"PAPER {trade['strategy']} on {day}: still no exit quote by {give_up:%H:%M} - retrying until one arrives")
            if legs != trade["legs"]:
                paper_update_trade(trade["strategy"], day, {"legs": legs})  # keep the legs already closed
            return False
        delay = max(0.0, (datetime.fromisoformat(max(leg["exit"]["ts"] for leg in legs)) - exit_at).total_seconds())
        result = lab.settle(
            [{"side": leg["side"], "entry": leg["entry"]["fill"], "exit": leg["exit"]["fill"]} for leg in legs],
            trade["lot_size"], trade["lots"], self.cost_model,
        )
        late = delay > EXIT_ON_TIME_SECONDS
        invalid = delay > EXIT_INVALID_AFTER_SECONDS
        notes = trade["notes"] or ""
        if late:
            notes = (notes + " " if notes else "") + f"exit was {delay:.0f}s late (no usable quote at {exit_time:%H:%M})"
            self._alert(day, f"late-{trade['strategy']}", "warn", f"PAPER {trade['strategy']} on {day}: exit {delay:.0f}s late")
        if invalid:
            self._alert(day, f"invalid-exit-{trade['strategy']}", "error",
                        f"PAPER {trade['strategy']} on {day}: exit {delay:.0f}s late (limit {EXIT_INVALID_AFTER_SECONDS}s) - marked invalid (its fill does not represent the 15:29:50 exit)")
        # Kept, not replaced: a trade already flagged at entry (a manual retry) stays flagged however its exit goes.
        reasons = [trade.get("invalid_reason"), f"exit {delay:.0f}s late (limit {EXIT_INVALID_AFTER_SECONDS:g}s)" if invalid else None]
        paper_update_trade(trade["strategy"], day, {
            "legs": legs, "status": "missed_exit" if late else "closed", "exit_delay_s": round(delay, 1), "notes": notes or None,
            "invalid_reason": "; ".join(reason for reason in reasons if reason) or None,
            "gross_pts": result["gross_pts"], "charges_pts": result["charges_pts"], "net_pts": result["net_pts"], "net_rs": result["net_rs"],
        })
        return True

    # ---- after the close

    def _reconcile(self, now: datetime) -> None:
        """Compute the candle-based backtest for each finished day and store it next to the paper result. Never S5: it
        spans two trading days and reconcile_trade()'s single-day candle backtest does not apply to it."""
        for trade in query_paper_trades():
            if trade["strategy"] == lab.S5 or trade["status"] not in ("closed", "missed_exit") or trade["reconciled_at"]:
                continue
            key = (trade["strategy"], trade["trading_date"])
            if key in self._reconcile_tried and (now - self._reconcile_tried[key]).total_seconds() < RECONCILE_RETRY_SECONDS:
                continue
            self._reconcile_tried[key] = now
            try:
                bt = self._reconcile_fn(trade)
            except Exception as error:  # noqa: BLE001 - retried later; recorded so it is never silent
                paper_update_trade(trade["strategy"], trade["trading_date"], {"bt_note": f"reconcile failed: {type(error).__name__}: {error}"})
                logger.warning("PAPER reconcile %s failed: %s", key, error)
                continue
            paper_update_trade(trade["strategy"], trade["trading_date"], {**bt, "reconciled_at": now.isoformat(timespec="seconds"), "bt_note": bt.get("bt_note")})


SESSION_VIEW_FROM = clock_time(9, 15)  # the Today screen moves on to a new day's session when that session opens


def session_day(now: datetime) -> str:
    """The trading day whose session the Today screen shows: today's from its 09:15 open, otherwise the last market
    day before it. So after midnight, before the open, over a weekend or a holiday, the screen keeps showing the
    session that ended - its marks, its closing quotes, NIFTY's day, the S5 position held overnight - instead of an
    empty new date that has no data yet."""
    day = now.date()
    if is_market_day(day) and now.time() >= SESSION_VIEW_FROM:
        return day.isoformat()
    for _ in range(21):  # no gap between market days is ever this long; the fallback below is only a guard
        day -= timedelta(days=1)
        if is_market_day(day):
            return day.isoformat()
    return now.date().isoformat()


def today_marks(day: str, recorded: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The Today screen's marks: `day`'s own (with each derived strategy's, from its source), and - for a trade shown
    with that session from an earlier entry day that is still OPEN (S5 held overnight, between the 09:15 open and its
    09:30 exit) or was bought back on `day` - that trade's own entry-day marks: its line does not vanish the moment the
    date moves on, and the timeline still draws that session (left of today) after the exit."""
    def exited_on_day(trade: dict[str, Any]) -> bool:
        return any(isinstance(leg.get("exit"), dict) and str(leg["exit"].get("ts", ""))[:10] == day for leg in trade.get("legs") or [])

    session = lab.derived_marks(marks_for(day), recorded)
    earlier = [
        mark
        for trade in recorded
        if trade["trading_date"] != day and (trade["status"] == "open" or exited_on_day(trade))
        for mark in marks_for(trade["trading_date"], trade["strategy"])
    ]
    return session, earlier


def next_session_day(now: datetime) -> str:
    """The trading day whose 09:30 (S5's exit, then the entries) comes next: today until its own 09:30, otherwise the
    next market day - weekends and the published holidays skipped (reliability.is_market_day())."""
    day = now.date()
    if is_market_day(day) and now.time() < ENTRY_TIME:
        return day.isoformat()
    day += timedelta(days=1)
    while not is_market_day(day):
        day += timedelta(days=1)
    return day.isoformat()


def spot_day(now: datetime, day: str | None = None) -> dict[str, float | None] | None:
    """NIFTY's session so far from the newest stored index message of `day` (default `now`'s date): the previous close
    ("cp") and the day's open/high/low (the "1d" bar). None when nothing was received that day. Read-only."""
    import json

    from storage import get_connection, initialize_database

    day = day or now.date().isoformat()
    initialize_database()
    with get_connection() as connection:
        row = connection.execute(
            "SELECT payload FROM market_observations WHERE instrument_key = ? AND received_at >= ? AND received_at < ? AND instr(received_at, '.') > 0"
            " ORDER BY received_at DESC LIMIT 1",
            (UNDERLYING.index_key, f"{day}T", f"{day}U"),  # every timestamp of that date ("T" < any time < "U")
        ).fetchone()
    if row is None:
        return None
    try:
        feed = (json.loads(row[0]) or {}).get("fullFeed", {}).get("indexFF", {})
    except (TypeError, ValueError):
        return None
    bar = next((o for o in feed.get("marketOHLC", {}).get("ohlc", []) if o.get("interval") == "1d"), {})
    out = {"prev_close": feed.get("ltpc", {}).get("cp"), "open": bar.get("open"), "high": bar.get("high"), "low": bar.get("low")}
    return out if any(value is not None for value in out.values()) else None


def live_snapshot(keys: list[str], now: datetime, day: str | None = None) -> dict[str, Any]:
    """The newest stored quote of each instrument (and NIFTY itself) for the Today screen, from session `day` on
    (default `now`'s date - after the close that is the session's last quote). Read-only.

    `close_short` / `close_long` are what buying back a short / selling a long would cost right now, chosen exactly like
    the engine's mark (a short at the ask, a long at the bid, LTP when that side of the book is empty)."""
    quotes = StreamQuotes(lambda: now, day)

    def one(key: str) -> dict[str, Any] | None:
        quote = quotes.quote(key)
        if quote is None:
            return None
        return {"ltp": quote.ltp, "bid": quote.bid, "ask": quote.ask, "age_s": round(quote_age(quote, now), 1),
                "close_short": quote.ask or quote.ltp, "close_long": quote.bid or quote.ltp}

    return {"spot": one(UNDERLYING.index_key), "quotes": {key: one(key) for key in dict.fromkeys(keys)}}


_spot_minutes: dict[str, Any] = {"day": None, "through": "", "minutes": {}}
_spot_minutes_lock = threading.Lock()


def spot_minutes(now: datetime) -> dict[str, Any]:
    """NIFTY's last tick of each minute of the session shown, from its 09:15 open - the Today header's sparkline. A minute
    with no stored tick (the live feed was down) is simply absent, so the gap shows. Kept in memory per session and read
    incrementally: after the first call only the ticks newer than the last one seen are read. Read-only."""
    from storage import get_connection, initialize_database

    day = session_day(now)
    with _spot_minutes_lock:
        cache = _spot_minutes
        if cache["day"] != day:
            cache.update(day=day, through=f"{day}T09:15", minutes={})
        initialize_database()
        with get_connection() as connection:
            rows = connection.execute(
                "SELECT received_at, ltp FROM market_observations WHERE instrument_key = ? AND received_at > ? AND received_at < ? AND ltp IS NOT NULL ORDER BY received_at",
                (UNDERLYING.index_key, cache["through"], f"{day}T15:31"),
            ).fetchall()
        for received_at, ltp in rows:
            cache["minutes"][received_at[11:16]] = ltp
        if rows:
            cache["through"] = rows[-1][0]
        return {"trading_date": day, "minutes": [{"minute": minute, "ltp": ltp} for minute, ltp in sorted(cache["minutes"].items())]}


def spot_now(now: datetime) -> dict[str, Any]:
    """NIFTY right now, for the Today screen's index tile, which asks every second while the market is live (the full Today
    payload comes every 2 s and is far heavier): the session shown (session_day), its newest stored index quote, and its
    previous close / open / high / low. Read-only - two indexed reads."""
    day = session_day(now)
    return {"now": now.isoformat(timespec="milliseconds"), "trading_date": day, "spot": live_snapshot([], now, day)["spot"], "spot_day": spot_day(now, day)}


def status_snapshot(engine: PaperEngine | None) -> dict[str, Any]:
    """What the UI shows about the engine itself."""
    return {
        "running": engine is not None,
        "last_tick": engine.last_tick.isoformat(timespec="seconds") if engine and engine.last_tick else None,
        "last_error": engine.last_error if engine else None,
        "feed_age_seconds": engine.feed_age_seconds if engine else None,
        "feed_stalled": engine.feed_stalled if engine else False,
        "lots": engine.lots if engine else int(os.getenv("PAPER_LOTS", "1")),
        "strategies": [*engine.strategies, lab.S5] if engine else [*lab.STRATEGIES, lab.S5],
    }


def marks_for(day: str, strategy: str | None = None) -> list[dict[str, Any]]:
    return query_paper_marks(day, strategy)
