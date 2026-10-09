"""Strategy lab: the ONE definition of the three NIFTY strategies, their cost model, and the candle-based backtest.

Backtest and paper trading both call the functions in this module - plan_legs(), wing_width(), settle() - so the
two can never drift apart. Nothing here places an order: it computes from prices and reads/writes SQLite (the
history builder also downloads candles, which is market data).

STRATEGIES (NIFTY, nearest expiry, every market day; entry 09:30, exit 15:29):
  straddle_sell  sell ATM CE + ATM PE
  iron_fly_w1    the straddle + buy CE at ATM+W and PE at ATM-W, W = the 09:30 straddle premium rounded to the strike step
  iron_fly_w2    the same with W = 2 x the straddle premium
ATM = resolve_atm_strike(spot at 09:30), the same guarded function the straddle dataset uses.

DERIVED strategy (no orders of its own - it only re-labels results the three above already produce):
  straddle_expiry_wings (S4)  each day S1's result, except on expiry days (the nearest weekly expiry is that day) where it is S3's.
S4 is declared 2026-09-20, before the first paper session. It is never entered by the paper engine: its paper day is the paper
day of S1 or S3, and its backtest is built from the stored S1 and S3 rows, so the three real strategies are untouched.
"""
import csv
import io
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable
from urllib.error import HTTPError

import straddle
from storage import (
    find_option_contract,
    query_backtest_rows,
    query_nse_daily_rows,
    query_backtest_skips,
    query_straddle_rows,
    record_backtest_skip,
    upsert_backtest_row,
)
from straddle import SkipDay, UNDERLYINGS, resolve_atm_strike

STRATEGIES = ("straddle_sell", "iron_fly_w1", "iron_fly_w2")
STRATEGY_LABEL = {
    "straddle_sell": "Straddle sell", "iron_fly_w1": "Iron fly · W = 1× premium", "iron_fly_w2": "Iron fly · W = 2× premium",
    "straddle_expiry_wings": "Straddle, wings on expiry day",
}
S4 = "straddle_expiry_wings"
# derived strategy -> (strategy whose result it takes on a normal day, strategy whose result it takes on an expiry day)
DERIVED = {S4: ("straddle_sell", "iron_fly_w2")}
DERIVED_DECLARED = {S4: "2026-09-20"}
DERIVED_RULE = {S4: "S1 result each day, except on expiry days (the day is the nearest weekly expiry) where it is the S3 result"}
S5 = "straddle_sell_overnight"
STRATEGY_LABEL[S5] = "Straddle sell, overnight to next 09:30"
S6 = "straddle_sell_two_overnight"
STRATEGY_LABEL[S6] = "Straddle sell, two nights to the day-after-next 09:30"
S7 = "straddle_sell_three_overnight"
STRATEGY_LABEL[S7] = "Straddle sell, three nights to the third trading day's 09:30"
S8 = "straddle_sell_overnight_skip_friday"
STRATEGY_LABEL[S8] = "Straddle sell, overnight - skip Friday entries"
S10 = "straddle_sell_overnight_skip_friday_december"
STRATEGY_LABEL[S10] = "Straddle sell, overnight - skip Friday and December entries"
SAME_DAY_STRATEGIES = STRATEGIES + tuple(DERIVED)  # entered and closed within one trading day: what day charts and reconcile
# can show (S5 IS paper-traded too now, but it spans two trading days - entered on one, closed on the next - so it
# still does not belong in a set of same-day results; see paper.py's PaperEngine._s5_enter()/_s5_exit())
LAB_STRATEGIES = SAME_DAY_STRATEGIES + (S5, S6, S7, S8, S10)  # everything the lab shows a backtest for; STRATEGIES stays the three that are actually traded
WING_MULTIPLE = {"iron_fly_w1": 1, "iron_fly_w2": 2}
UNDERLYING = UNDERLYINGS["NIFTY"]
STRIKE_STEP = UNDERLYING.strike_step

# When a PAPER position is closed. The F&O session ran to 15:30 (last minute 15:29) and from 2026-08-03 runs to 15:40 (last
# minute 15:39), so a paper position is closed in the last minute the options trade. The candle BACKTEST keeps exiting at
# 15:29 on every day, so its 2024-> history stays one measurement; only paper trades use this.
REGULAR_EXIT = "15:29"
EXTENDED_EXIT = "15:39"
EXTENDED_CLOSE_FROM = "2026-08-03"


def exit_minute(day: str) -> str:
    """The HH:MM minute a paper position is closed on `day` (the last minute the options trade that day)."""
    return EXTENDED_EXIT if day >= EXTENDED_CLOSE_FROM else REGULAR_EXIT


def paper_exit_minute(trade: dict[str, Any]) -> str:
    """The minute a paper trade actually exited (its latest exit fill), else the day's exit minute. A trade that closed
    at 15:29 before the exit moved to 15:39 keeps 15:29, so a chart or a comparison never assumes a time it did not use."""
    exits = [leg["exit"]["ts"][11:16] for leg in trade.get("legs") or [] if isinstance(leg.get("exit"), dict) and leg["exit"].get("ts")]
    return max(exits) if exits else exit_minute(trade["trading_date"])
ENTRY_TIME = "09:30"
EXIT_TIME = "15:29"
BACKTEST_BUILD_VERSION = "lab-v1"


# --------------------------------------------------------------------------- cost model


@dataclass(frozen=True)
class CostModel:
    """Indian equity-index option charges for one round trip (entry and exit are each an order per leg).

    THESE RATES ARE AN ASSUMPTION - the codebase had no cost model. They are the NSE/broker schedule in force from
    October 2024 (which is where the history starts) and are applied identically to backtest and paper. Change them
    here, in one place, if your actual contract notes differ."""

    brokerage_per_order: float = 20.0  # flat rupees per executed order
    stt_sell_rate: float = 0.001  # 0.1% of premium on the SELL side of an option
    exchange_txn_rate: float = 0.0003503  # NSE options, % of premium turnover, both sides
    sebi_rate: float = 0.000001  # Rs 10 per crore of turnover
    stamp_buy_rate: float = 0.00003  # 0.003% of premium on the BUY side
    gst_rate: float = 0.18  # on brokerage + exchange transaction + SEBI charges


DEFAULT_COST_MODEL = CostModel()


# --------------------------------------------------------------------------- rules shared by backtest and paper


def round_to_step(value: float, step: float = STRIKE_STEP) -> float:
    """Nearest multiple of `step`, halves rounding up (deterministic, unlike Python's round())."""
    return float(math.floor(value / step + 0.5) * step)


def wing_width(strategy: str, premium: float, step: float = STRIKE_STEP) -> float:
    """W = multiple x the 09:30 straddle premium, rounded to the strike step (never less than one step)."""
    return max(step, round_to_step(WING_MULTIPLE[strategy] * premium, step))


def plan_legs(strategy: str, atm: float, premium: float, step: float = STRIKE_STEP) -> list[dict[str, Any]]:
    """The legs of a strategy IN FILL ORDER: an iron fly buys its two wings first, then sells the two body legs.

    `atm` is resolve_atm_strike(spot at 09:30) and `premium` the ATM CE + PE price at 09:30; nothing later in the
    day is ever an input."""
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    body = [
        {"role": "body", "type": "CE", "strike": atm, "side": "SELL"},
        {"role": "body", "type": "PE", "strike": atm, "side": "SELL"},
    ]
    if strategy == "straddle_sell":
        return body
    width = wing_width(strategy, premium, step)
    wings = [
        {"role": "wing", "type": "CE", "strike": atm + width, "side": "BUY"},
        {"role": "wing", "type": "PE", "strike": atm - width, "side": "BUY"},
    ]
    return wings + body


def leg_pnl_pts(side: str, entry: float, exit_: float) -> float:
    """Points made on one unit of a leg: a sold option gains what it loses in price, a bought one the opposite."""
    return entry - exit_ if side == "SELL" else exit_ - entry


def settle(legs: list[dict[str, Any]], lot_size: int, lots: int, model: CostModel = DEFAULT_COST_MODEL) -> dict[str, Any]:
    """settle_units for `lots` lots of `lot_size` units."""
    return settle_units(legs, lot_size * lots, model)


def settle_units(legs: list[dict[str, Any]], units: int, model: CostModel = DEFAULT_COST_MODEL) -> dict[str, Any]:
    """Gross, charges and net for a completed trade of `units` units. Each leg needs side, entry and exit (the price it traded at).

    Charges are for the trade's real orders: every leg is one order in and one order out, so an iron fly pays for
    eight orders, a straddle for four. net_pts is net_rs expressed back in points of one unit."""
    gross_pts = sum(leg_pnl_pts(leg["side"], leg["entry"], leg["exit"]) for leg in legs)
    turnover = sell_turnover = buy_turnover = 0.0
    orders = 0
    for leg in legs:
        for side, price in ((leg["side"], leg["entry"]), ("BUY" if leg["side"] == "SELL" else "SELL", leg["exit"])):
            value = price * units
            turnover += value
            orders += 1
            if side == "SELL":
                sell_turnover += value
            else:
                buy_turnover += value
    brokerage = model.brokerage_per_order * orders
    exchange = model.exchange_txn_rate * turnover
    sebi = model.sebi_rate * turnover
    charges_rs = (
        brokerage + model.stt_sell_rate * sell_turnover + exchange + sebi + model.stamp_buy_rate * buy_turnover
        + model.gst_rate * (brokerage + exchange + sebi)
    )
    gross_rs = gross_pts * units
    net_rs = gross_rs - charges_rs
    return {
        "gross_pts": round(gross_pts, 4),
        "charges_rs": round(charges_rs, 2),
        "charges_pts": round(charges_rs / units, 4),
        "net_pts": round(net_rs / units, 4),
        "gross_rs": round(gross_rs, 2),
        "net_rs": round(net_rs, 2),
    }


# --------------------------------------------------------------------------- backtest rows


def backtest_row(strategy: str, day: str, expiry: str, atm: float, premium: float, legs: list[dict[str, Any]], lot_size: int, lots: int = 1,
                 model: CostModel = DEFAULT_COST_MODEL, built_at: str | None = None) -> dict[str, Any]:
    """One strategy_backtest_daily row from candle prices (`legs` carry entry and exit)."""
    width = wing_width(strategy, premium) if strategy != "straddle_sell" else None
    return {
        "strategy": strategy, "trading_date": day, "expiry": expiry, "atm_strike": atm, "wing_width": width,
        "lot_size": lot_size, "lots": lots, "legs": legs, **settle(legs, lot_size, lots, model),
        "cost_model": json.dumps(asdict(model)), "build_version": BACKTEST_BUILD_VERSION,
        "built_at": built_at or datetime.now().isoformat(timespec="seconds"),
    }


@dataclass(frozen=True)
class Size:
    """How big a trade is shown: `lots` lots (each day's own contract lot size) or a fixed `qty` of units on every day."""

    lots: int | None = None
    qty: int | None = None

    def __post_init__(self) -> None:
        if (self.lots is None) == (self.qty is None):
            raise ValueError("give either lots or qty, not both")
        if self.lots is not None and not 1 <= self.lots <= MAX_LOTS:
            raise ValueError(f"lots must be between 1 and {MAX_LOTS}")
        if self.qty is not None and not 1 <= self.qty <= MAX_QTY:
            raise ValueError(f"qty must be between 1 and {MAX_QTY}")

    def units(self, lot_size: int) -> int:
        return self.qty if self.qty is not None else lot_size * (self.lots or 1)

    @classmethod
    def from_params(cls, lots: int | None, qty: int | None, default: "Size | None" = None) -> "Size | None":
        """The size a request asks for: lots, qty, or `default` when it names neither."""
        if lots is None and qty is None:
            return default
        return cls(lots, qty)


MAX_LOTS = 10_000
MAX_QTY = 1_000_000


def as_size(size: "Size | int | None") -> "Size":
    return size if isinstance(size, Size) else Size(lots=size or default_lots())


def sized(row: dict[str, Any], units: int) -> dict[str, Any]:
    """The size fields every lab row carries: units (qty), lots (fractional when qty is not a whole number of lots) and whether it is whole."""
    lot_size = row["lot_size"]
    return {"qty": units, "lots": units // lot_size if units % lot_size == 0 else round(units / lot_size, 4), "whole_lots": units % lot_size == 0}


def rescale(row: dict[str, Any], size: "Size | int", model: CostModel = DEFAULT_COST_MODEL) -> dict[str, Any]:
    """The same backtest day at a different size (charges are not linear in units, so it re-settles)."""
    units = as_size(size).units(row["lot_size"])
    if units == row["lot_size"] * row["lots"]:
        return {**row, **sized(row, units)}
    return {**row, **sized(row, units), **settle_units(row["legs"], units, model)}


def lot_size_of(instrument_key: str | None, default: int | None = None) -> int | None:
    import sqlite3

    from storage import get_connection, initialize_database

    if not instrument_key:
        return default
    initialize_database()
    with get_connection() as connection:
        found = connection.execute("SELECT json_extract(payload, '$.lot_size') FROM instrument_contracts WHERE instrument_key = ?", (instrument_key,)).fetchone()
    return int(found[0]) if found and found[0] else default


def build_straddle_history(lots: int = 1, model: CostModel = DEFAULT_COST_MODEL) -> dict[str, int]:
    """straddle_sell for every NIFTY day already in straddle_daily: sell the ATM CE and PE at the 09:30 candle open,
    buy them back at the 15:29 candle close. No downloads - the prices are the ones the straddle dataset holds."""
    built = skipped = 0
    for row in query_straddle_rows(underlying="NIFTY"):
        lot_size = lot_size_of(row["ce_instrument_key"])
        if lot_size is None:
            skipped += 1
            record_backtest_skip("straddle_sell", row["trading_date"], "no_lot_size", "no stored contract for the ATM CE", BACKTEST_BUILD_VERSION)
            continue
        legs = [
            {"role": "body", "type": "CE", "strike": row["atm_strike"], "side": "SELL", "entry": row["ce_0930"], "exit": row["ce_close"], "instrument_key": row["ce_instrument_key"]},
            {"role": "body", "type": "PE", "strike": row["atm_strike"], "side": "SELL", "entry": row["pe_0930"], "exit": row["pe_close"], "instrument_key": row["pe_instrument_key"]},
        ]
        upsert_backtest_row(backtest_row("straddle_sell", row["trading_date"], row["expiry"], row["atm_strike"], row["straddle_0930"], legs, lot_size, lots, model))
        built += 1
    return {"built": built, "skipped": skipped}


class WingBuilder:
    """Builds one iron-fly day: the body legs come from the straddle row, the two wings are fetched (or read) as candles."""

    def __init__(self, strategy: str, token_provider: Callable[[], str | None], *, fetch: bool, today: date, log: Callable[[str], None], lots: int = 1):
        self.strategy = strategy
        self.token_provider = token_provider
        self.fetch = fetch
        self.today = today
        self.lots = lots
        self.resolver = straddle.ContractResolver(UNDERLYING, token_provider, fetch, today, log)

    def _wing_key(self, expiry: str, strike: float, leg: str) -> str:
        lapsed = self.resolver.is_lapsed(expiry)
        key = find_option_contract(UNDERLYING.index_key, expiry, strike, leg, expired=lapsed)
        if key is None and self.fetch:
            self.resolver._list_contracts(expiry)
            key = find_option_contract(UNDERLYING.index_key, expiry, strike, leg, expired=lapsed)
        if key is None:
            raise SkipDay("wing_not_listed", f"{leg} {strike:g} for expiry {expiry}")
        return key

    def build(self, day: str, straddle_row: dict[str, Any]) -> dict[str, Any]:
        expiry, atm = straddle_row["expiry"], straddle_row["atm_strike"]
        premium = straddle_row["ce_0930"] + straddle_row["pe_0930"]
        lapsed = self.resolver.is_lapsed(expiry)
        legs = []
        for spec in plan_legs(self.strategy, atm, premium):
            if spec["role"] == "body":
                entry, exit_ = (straddle_row["ce_0930"], straddle_row["ce_close"]) if spec["type"] == "CE" else (straddle_row["pe_0930"], straddle_row["pe_close"])
                key = straddle_row["ce_instrument_key"] if spec["type"] == "CE" else straddle_row["pe_instrument_key"]
            else:
                key = self._wing_key(expiry, spec["strike"], spec["type"])
                bars = straddle.leg_bars(self.token_provider, key, day, lapsed=lapsed, fetch=self.fetch)
                if not bars:
                    raise SkipDay("wing_no_bars", f"{spec['type']} {spec['strike']:g}")
                if straddle.ENTRY_BAR not in bars:
                    raise SkipDay("wing_no_0930_bar", f"{spec['type']} {spec['strike']:g}")
                closing = straddle._exit_bar(bars)
                if closing is None:
                    raise SkipDay("wing_no_close_bar", f"{spec['type']} {spec['strike']:g}")
                entry, exit_ = float(bars[straddle.ENTRY_BAR]["open"]), float(closing["close"])
            legs.append({**spec, "entry": entry, "exit": exit_, "instrument_key": key})
        lot_size = lot_size_of(straddle_row["ce_instrument_key"])
        if lot_size is None:
            raise SkipDay("no_lot_size", "no stored contract for the ATM CE")
        return backtest_row(self.strategy, day, expiry, atm, premium, legs, lot_size, self.lots)


def run_strategy_history(
    strategies: list[str] | None, from_date: str | None, to_date: str | None, *, token_provider: Callable[[], str | None],
    log: Callable[[str], None] = print, on_progress: Callable[[dict[str, Any]], None] | None = None, lots: int = 1,
) -> dict[str, Any]:
    """Builds the backtest history for the requested strategies over every NIFTY day in straddle_daily. straddle_sell
    needs no download; each iron fly downloads its two wing candles per day (checkpointed, resumable, same
    skip/error discipline as every other builder)."""
    from zoneinfo import ZoneInfo
    import os
    from datetime import timedelta

    names = list(dict.fromkeys(strategies or STRATEGIES))
    if unknown := [n for n in names if n not in STRATEGIES]:
        raise ValueError(f"unknown strategy {unknown}; choose from {list(STRATEGIES)}")
    today = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))).date()
    rows_by_day = {r["trading_date"]: r for r in query_straddle_rows(from_date, to_date, underlying="NIFTY")}
    days = sorted(rows_by_day)
    per: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {"run": {}, "strategies": per}
    for name in names:
        stats = straddle._new_stats(from_date, to_date)
        stats.update(total=len(days), effective_from=days[0] if days else None, effective_to=days[-1] if days else None)
        per[name] = stats
    if "straddle_sell" in names:
        outcome = build_straddle_history(lots)
        per["straddle_sell"].update(succeeded=outcome["built"], skipped=outcome["skipped"], attempted=len(days), done=len(days))
        log(f"straddle_sell: {outcome['built']} days built from straddle_daily (no downloads)")
    wing_strategies = [n for n in names if n != "straddle_sell"]
    aborted: straddle.BuildAborted | None = None
    for name in wing_strategies:
        if aborted:
            per[name]["stopped_because"] = f"aborted: {aborted}"
            continue
        builder = WingBuilder(name, token_provider, fetch=True, today=today, log=log, lots=lots)
        built = {r["trading_date"] for r in query_backtest_rows(name)}
        skips = query_backtest_skips(name)

        def progress(_stats=per[name]) -> None:
            if on_progress:
                on_progress({**report["run"], "current": _stats.get("current_date"), "strategies": {k: dict(v) for k, v in per.items()}})

        try:
            straddle._run_days(
                name, days, per[name],
                is_built=lambda day, built=built: day in built,
                is_skipped=lambda day, skips=skips: day in skips,
                build=lambda day, builder=builder: builder.build(day, rows_by_day[day]),
                save=upsert_backtest_row,
                record_skip=lambda day, reason, detail, name=name: record_backtest_skip(name, day, reason, detail, BACKTEST_BUILD_VERSION),
                describe=lambda row: f"ATM {row['atm_strike']:g} W {row['wing_width']:g}  net {row['net_pts']} pts ({row['net_rs']} Rs)",
                rebuild=False, retry_skipped=False, today=today, log=log, progress=progress,
            )
        except straddle.BuildAborted as error:
            aborted = error
    report["run"] = {
        key: sum(s[key] for s in per.values()) for key in ("attempted", "succeeded", "skipped", "errors", "already_built", "previously_skipped", "total", "done")
    }
    report["run"]["stopped_because"] = "; ".join(f"{n}: {s['stopped_because']}" for n, s in per.items() if s["stopped_because"] != "completed") or "completed"
    return report


# --------------------------------------------------------------------------- reconciliation: the candle backtest for one day


def _bars(token_provider: Callable[[], str | None], key: str, day: str, *, today: date, lapsed: bool) -> straddle.Bars:
    """A leg's session bars for `day`. Today's session is only available from the intraday endpoint (the historical
    one excludes it), and that download goes into market_observations like every other."""
    import historical

    if date.fromisoformat(day) >= today:
        token = token_provider()
        if not token:
            raise SkipDay("no_token", "no Upstox token to download today's candles")
        historical.download_intraday_candles(token, key, unit="minutes", interval=1)
        return straddle._session_bars(straddle.query_candle_bars(key, day))
    return straddle.leg_bars(token_provider, key, day, lapsed=lapsed, fetch=True)


def backtest_for_day(strategy: str, day: str, token_provider: Callable[[], str | None], lots: int = 1, model: CostModel = DEFAULT_COST_MODEL) -> dict[str, Any]:
    """What the CANDLE backtest says for `day` under the same rules: ATM from the 09:30 spot candle, legs from plan_legs(),
    entry at each candle's 09:30 open and exit at its 15:29 close. Stored in strategy_backtest_daily (the history
    now includes this day) and returned."""
    import os
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))).date()
    if existing := query_backtest_rows(strategy, day, day):
        return rescale(existing[0], lots, model)
    resolver = straddle.ContractResolver(UNDERLYING, token_provider, True, today, lambda line: None)
    if date.fromisoformat(day) >= today:
        import historical

        token = token_provider()
        if not token:
            raise SkipDay("no_token", "no Upstox token to download today's candles")
        historical.download_intraday_candles(token, UNDERLYING.index_key, unit="minutes", interval=1)
        spot = straddle._session_bars(straddle.query_candle_bars(UNDERLYING.index_key, day))
    else:
        spot = straddle.SpotBars(UNDERLYING, token_provider, True, today).get(day)
    if straddle.ENTRY_BAR not in spot:
        raise SkipDay("no_spot_0930", "no NIFTY 09:30 candle")
    atm = resolve_atm_strike(float(spot[straddle.ENTRY_BAR]["open"]), STRIKE_STEP)
    expiry = resolver.nearest_expiry(day)
    lapsed = resolver.is_lapsed(expiry)

    def key_for(strike: float, leg: str) -> str:
        key = find_option_contract(UNDERLYING.index_key, expiry, strike, leg, expired=lapsed)
        if key is None:
            resolver._list_contracts(expiry)
            key = find_option_contract(UNDERLYING.index_key, expiry, strike, leg, expired=lapsed)
        if key is None:
            raise SkipDay("not_listed", f"{leg} {strike:g} for {expiry}")
        return key

    def prices(key: str, label: str) -> tuple[float, float]:
        bars = _bars(token_provider, key, day, today=today, lapsed=lapsed)
        if straddle.ENTRY_BAR not in bars:
            raise SkipDay("no_0930_bar", label)
        closing = straddle._exit_bar(bars)
        if closing is None:
            raise SkipDay("no_close_bar", label)
        return float(bars[straddle.ENTRY_BAR]["open"]), float(closing["close"])

    body = {leg: prices(key_for(atm, leg), f"{leg} {atm:g}") for leg in ("CE", "PE")}
    premium = body["CE"][0] + body["PE"][0]
    legs = []
    for spec in plan_legs(strategy, atm, premium):
        key = key_for(spec["strike"], spec["type"])
        entry, exit_ = body[spec["type"]] if spec["role"] == "body" else prices(key, f"{spec['type']} {spec['strike']:g}")
        legs.append({**spec, "entry": entry, "exit": exit_, "instrument_key": key})
    lot_size = lot_size_of(key_for(atm, "CE"))
    if lot_size is None:
        raise SkipDay("no_lot_size", "no stored contract for the ATM CE")
    row = backtest_row(strategy, day, expiry, atm, premium, legs, lot_size, 1, model)
    upsert_backtest_row(row)
    return rescale(row, lots, model)


def reconcile_trade(trade: dict[str, Any], token_provider: Callable[[], str | None], model: CostModel = DEFAULT_COST_MODEL) -> dict[str, Any]:
    """The bt_* columns for one finished paper trade: the candle backtest for the same day, at the paper trade's lots."""
    row = backtest_for_day(trade["strategy"], trade["trading_date"], token_provider, trade["lots"], model)
    day = trade["trading_date"]
    minute = paper_exit_minute(trade)
    aligned_note = None
    if minute != straddle.EXIT_BAR:
        # The stored backtest exits at 15:29. The paper trade exits at `minute`, so compare it with the candle closes at that
        # minute (the stored history row is left alone): otherwise the paper-vs-backtest gap would include ten minutes of price movement.
        moved = _legs_closing_at(row["legs"], day, minute)
        if moved is None:
            aligned_note = f"no candles after 15:29 are stored yet, so the backtest shown exits at 15:29 while the paper trade exited at {minute}"
        else:
            row = {**row, "legs": moved, **settle(moved, row["lot_size"], trade["lots"], model)}
    paper_strikes = {(leg["type"], leg["strike"]) for leg in trade["legs"]}
    backtest_strikes = {(leg["type"], leg["strike"]) for leg in row["legs"]}
    note = None if paper_strikes == backtest_strikes else (
        f"the candle backtest would have used different strikes ({sorted(backtest_strikes)}), so only the legs both share are compared"
    )
    note = "; ".join(n for n in (note, aligned_note) if n) or None
    return {
        "bt_gross_pts": row["gross_pts"], "bt_charges_pts": row["charges_pts"], "bt_net_pts": row["net_pts"], "bt_net_rs": row["net_rs"],
        "bt_legs": row["legs"], "bt_note": note,
    }


def _legs_closing_at(legs: list[dict[str, Any]], day: str, minute: str) -> list[dict[str, Any]] | None:
    """The backtest legs with each exit replaced by that contract's candle close at `minute` (its last bar up to then).
    None when nothing after 15:29 is stored for any leg - then the candles simply are not there yet."""
    out, later = [], False
    for leg in legs:
        bars = [r for r in straddle.query_candle_bars(leg["instrument_key"], day) if straddle.EARLIEST_ACCEPTABLE_EXIT_BAR <= r["received_at"][11:16] <= minute]
        if not bars:
            return None
        last = max(bars, key=lambda r: r["received_at"])
        later = later or last["received_at"][11:16] > straddle.EXIT_BAR
        out.append({**leg, "exit": float(last["close"])})
    return out if later else None


# --------------------------------------------------------------------------- the two bases of the backtest

MINUTE = "minute"  # the project's backtest: entry 09:30 open, exit 15:29 close, from 1-minute candles (Oct 2024 on)
NSE_DAILY = "nse_daily"  # the same rules on NSE end-of-day prices: entry at the day's OPEN (about 09:15), exit at NSE's CLOSE (Jan 2022 on)
BASES = (MINUTE, NSE_DAILY)


def check_basis(basis: str) -> str:
    if basis not in BASES:
        raise ValueError(f"basis must be one of {list(BASES)}")
    return basis


def backtest_source_rows(strategy: str | None, from_date: str | None, to_date: str | None, basis: str = MINUTE) -> list[dict[str, Any]]:
    """Stored rows of the real strategies for one basis. The two bases live in separate tables and are never mixed."""
    return query_backtest_rows(strategy, from_date, to_date) if check_basis(basis) == MINUTE else query_nse_daily_rows(strategy, from_date, to_date)


# --------------------------------------------------------------------------- derived strategy (S4)


def is_expiry_day(row: dict[str, Any]) -> bool | None:
    """True when the row's trading day is its own expiry (the nearest weekly expiry is that day); None if the row has no expiry."""
    expiry = row.get("expiry")
    return None if not expiry else expiry == row["trading_date"]


def derive(name: str, rows_by_source: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Rows of a derived strategy from the rows (backtest rows or paper trades) of its two source strategies.

    Each day takes the normal-day source's row, or the expiry-day source's row on an expiry day, copied whole and
    relabelled with `strategy=name` and `derived_from`. A day whose needed source row is missing is NOT filled from the other
    source: it is left out and returned in the skip list with its reason, so nothing is dropped silently."""
    normal, on_expiry = DERIVED[name]
    by_day: dict[str, dict[str, dict[str, Any]]] = {}
    for source, rows in rows_by_source.items():
        for row in rows:
            by_day.setdefault(row["trading_date"], {})[source] = row
    out, skipped = [], []
    for day in sorted(by_day):
        have = by_day[day]
        flags = [is_expiry_day(r) for r in have.values() if is_expiry_day(r) is not None]
        if not flags:
            skipped.append({"trading_date": day, "reason": "expiry unknown"})
            continue
        needed = on_expiry if flags[0] else normal
        row = have.get(needed)
        if row is None:
            skipped.append({"trading_date": day, "reason": f"{'expiry' if flags[0] else 'normal'} day but no {needed} row"})
            continue
        out.append({**row, "strategy": name, "derived_from": needed})
    return out, skipped


def derived_backtest_rows(name: str, from_date: str | None = None, to_date: str | None = None, basis: str = MINUTE) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return derive(name, {source: backtest_source_rows(source, from_date, to_date, basis) for source in DERIVED[name]})


def s8_paper_trades(from_date: str | None = None, to_date: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """S8's paper trades: S5's own, minus the Friday entries - the same filter s8_overnight_rows() puts over S5's backtest.
    S8 has no orders of its own (the engine never enters it): an S8 paper day IS that day's S5 position, copied whole and
    relabelled with derived_from=S5 - open, closed or missed exactly as S5's is. A Friday S5 trade goes to the skip list."""
    from storage import query_paper_trades

    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for trade in query_paper_trades(S5, from_date, to_date):
        if date.fromisoformat(trade["trading_date"]).weekday() == 4:
            skipped.append({"trading_date": trade["trading_date"], "reason": "not_entered_friday", "detail": "S8 does not enter on a Friday, so S5's Friday trade is not S8's"})
        else:
            kept.append({**trade, "strategy": S8, "derived_from": S5})
    return kept, skipped


def paper_trades(strategy: str | None = None, from_date: str | None = None, to_date: str | None = None) -> list[dict[str, Any]]:
    """Paper trades of `strategy` (None = every strategy) including the derived ones: S4 (the S1 or S3 trade of the day)
    and S8 (S5's trade, except a Friday entry)."""
    from storage import query_paper_trades

    trades = [] if strategy in DERIVED or strategy == S8 else list(query_paper_trades(strategy, from_date, to_date))
    for name in [strategy] if strategy else (*DERIVED, S8):
        if name in DERIVED:
            trades += derive(name, {source: query_paper_trades(source, from_date, to_date) for source in DERIVED[name]})[0]
        elif name == S8:
            trades += s8_paper_trades(from_date, to_date)[0]
    return trades


def paper_trades_for_today(day: str) -> list[dict[str, Any]]:
    """Every paper trade the Today screen should show: `day`'s own trades (S4 is the S1 or S3 trade of the day, so
    it needs no orders of its own), plus S5's overnight position from an EARLIER day if it is still relevant to
    `day` - either still open (not yet closed at `day`'s own 09:30) or just closed AT `day`'s 09:30, so its result
    does not vanish from Today the moment _s5_exit() resolves it, only once a later day's own tick has passed and
    updated_at no longer matches `day`. S5 rows are dated by their ENTRY day, not the day they close, so the plain
    day-scoped fetch below always misses the relevant one on its own."""
    # S8 is left out: its trade is S5's own position, which Today shows once (its S5 card says whether S8 takes it)
    recorded = [t for t in paper_trades(None, day, day) if t["strategy"] != S8]
    recorded += [
        t for t in paper_trades(S5)
        if t["trading_date"] != day and (t["status"] == "open" or str(t.get("updated_at") or "").startswith(day))
    ]
    return recorded


def derived_marks(marks: list[dict[str, Any]], trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`marks` plus, for each derived trade in `trades`, the marks of the strategy it is taken from, under the derived name."""
    extra = []
    for trade in trades:
        source = trade.get("derived_from")
        if source:
            extra += [{**m, "strategy": trade["strategy"]} for m in marks if m["strategy"] == source and m["trading_date"] == trade["trading_date"]]
    return marks + extra


def derived_skips(basis: str = MINUTE, only: str | None = None) -> dict[str, dict[str, list[dict[str, str]]]]:
    """Days each derived or computed strategy could not be built for, backtest and paper - empty lists when nothing was
    left out. `only`: just that strategy's entry (a one-strategy backtest request needs no other - building them all
    re-reads and re-derives every strategy, ~0.2 s); a strategy with no such list gives an empty dict."""
    from storage import query_paper_trades

    out: dict[str, dict[str, list[dict[str, str]]]] = {}
    wanted = lambda name: only is None or only == name  # noqa: E731
    for name in DERIVED:
        if wanted(name):
            out[name] = {
                "backtest": derived_backtest_rows(name, basis=basis)[1],
                "paper": derive(name, {source: query_paper_trades(source) for source in DERIVED[name]})[1] if basis == MINUTE else [],
            }
    # S5's paper trades get a real paper_trades row every day (open/closed/missed_entry/missed_exit/error), the same
    # mechanism as the same-day strategies - so there is no separate "left out" list for it here either.
    if wanted(S5):
        out[S5] = {"backtest": s5_overnight_rows(basis=basis)[1], "paper": []}
    if wanted(S6):
        out[S6] = {"backtest": s6_overnight_rows(basis=basis)[1], "paper": []}  # not paper-traded at all yet
    if wanted(S7):
        out[S7] = {"backtest": s7_overnight_rows(basis=basis)[1], "paper": []}  # not paper-traded at all yet
    if wanted(S8):
        out[S8] = {"backtest": s8_overnight_rows(basis=basis)[1], "paper": s8_paper_trades()[1] if basis == MINUTE else []}  # S5's paper, Fridays left out
    if wanted(S10):
        out[S10] = {"backtest": s10_overnight_rows(basis=basis)[1], "paper": []}  # not paper-traded at all
    return out


# --------------------------------------------------------------------------- overnight strategy (S5, computed fresh - not derived from another
# stored strategy's rows and never written to strategy_backtest_daily, so it can never drift the real history)

S5_EXIT_MINUTE = "09:30"  # the same clock time as the entry, one trading day later - a full 24 hours held

# One-minute lookups below seek idx_observations_unique (instrument_key, received_at) per contract instead of letting
# SQLite pick the trading_date index ("+trading_date" opts that column out of index selection): the trading_date
# index walks the WHOLE day's slice, hundreds of thousands of rows on a live-feed day (~200ms per lookup, repeated
# for every day S5-S7 touch) versus ~0.05ms for the seek. received_at is "YYYY-MM-DDTHH:MM..." for candles and ticks
# alike, and ";" sorts right after ":", so [dayTHH:MM, dayTHH:MM;) is exactly that minute.
_MINUTE_SEEK = " AND received_at >= ? AND received_at < ?"


def _minute_bounds(day: str, minute: str) -> tuple[str, str]:
    return f"{day}T{minute}", f"{day}T{minute};"


def _0930_from_ticks(day: str, keys: list[str], want: str) -> dict[str, float]:
    """The open or close of the 09:30 MINUTE built from live ticks - the same "no downloaded candle yet, fall back
    to what the live feed already recorded" rule day_chart.py's contract_bars() uses for a day whose finalized
    candle Upstox has not published yet. `want` is "open" (the first tick's price in the minute) or "close" (the
    last one, rows being read in received_at order)."""
    if not keys:
        return {}
    from storage import get_connection, initialize_database

    initialize_database()
    marks = ",".join("?" * len(keys))
    with get_connection() as connection:
        rows = connection.execute(
            f"SELECT instrument_key, ltp FROM market_observations WHERE +trading_date = ? AND instrument_key IN ({marks}){_MINUTE_SEEK}"
            " AND ltp IS NOT NULL AND instr(received_at, '.') > 0 AND substr(received_at, 12, 5) = ? ORDER BY received_at",
            (day, *keys, *_minute_bounds(day, S5_EXIT_MINUTE), S5_EXIT_MINUTE),
        ).fetchall()
    out: dict[str, float] = {}
    for key, ltp in rows:
        if want == "open" and key in out:
            continue  # first tick in the minute wins
        out[key] = ltp  # "close": rows arrive in received_at order, so the last assignment wins
    return out


def _0930_candle_closes(day: str, keys: list[str]) -> dict[str, float]:
    """{instrument_key: close of the 09:30 DOWNLOADED candle} for every key on `day` that has one - never a tick
    estimate. This is the raw "do we actually have the real candle yet" check s5_missing_candles() needs; a key
    missing here is a genuine download gap regardless of whether _0930_from_ticks() could approximate it."""
    if not keys:
        return {}
    from storage import get_connection, initialize_database

    initialize_database()
    marks = ",".join("?" * len(keys))
    with get_connection() as connection:
        rows = connection.execute(
            f"SELECT instrument_key, close FROM market_observations WHERE +trading_date = ? AND instrument_key IN ({marks}){_MINUTE_SEEK}"
            " AND open IS NOT NULL AND instr(received_at, '.') = 0 AND substr(received_at, 12, 5) = ?",
            (day, *keys, *_minute_bounds(day, S5_EXIT_MINUTE), S5_EXIT_MINUTE),
        ).fetchall()
    return {key: close for key, close in rows}


def _0930_candle_opens(day: str, keys: list[str]) -> dict[str, float]:
    """{instrument_key: open of the 09:30 DOWNLOADED candle} - see _0930_candle_closes(), same rule for opens."""
    if not keys:
        return {}
    from storage import get_connection, initialize_database

    initialize_database()
    marks = ",".join("?" * len(keys))
    with get_connection() as connection:
        rows = connection.execute(
            f"SELECT instrument_key, open FROM market_observations WHERE +trading_date = ? AND instrument_key IN ({marks}){_MINUTE_SEEK}"
            " AND open IS NOT NULL AND instr(received_at, '.') = 0 AND substr(received_at, 12, 5) = ?",
            (day, *keys, *_minute_bounds(day, S5_EXIT_MINUTE), S5_EXIT_MINUTE),
        ).fetchall()
    return {key: open_ for key, open_ in rows}


def _next_day_0930_closes(day: str, keys: list[str]) -> tuple[dict[str, float], set[str]]:
    """({instrument_key: close of the 09:30 candle}, the keys whose close came from live ticks instead of a
    downloaded candle) for every key on `day`, one SQL query for however many entry days share that next trading
    day (there is at most one per key, so no forward-fill or "nearest before" is needed). A key with no downloaded
    candle yet - today's finalized data, which Upstox does not publish until later - falls back to the close of
    the last live tick recorded in that exact minute, same as day_chart.py already does; the caller marks that
    leg "estimated" so it reads as provisional, not silently indistinguishable from an exact candle close. Use
    _0930_candle_closes() instead of this when the tick fallback must NOT count (s5_missing_candles())."""
    out = _0930_candle_closes(day, keys)
    missing = [k for k in keys if k not in out]
    from_ticks = _0930_from_ticks(day, missing, "close") if missing else {}
    out.update(from_ticks)
    return out, set(from_ticks)


def _0930_opens(day: str, keys: list[str]) -> tuple[dict[str, float], set[str]]:
    """({instrument_key: open of the 09:30 candle}, the keys whose open came from live ticks) for every key on
    `day` - only needed for a ROLLED entry (an S1 expiry-day entrant); S1's own contracts already have their entry
    price stored on the row. See _next_day_0930_closes() for the tick fallback this shares."""
    out = _0930_candle_opens(day, keys)
    missing = [k for k in keys if k not in out]
    from_ticks = _0930_from_ticks(day, missing, "open") if missing else {}
    out.update(from_ticks)
    return out, set(from_ticks)


def _0930_by_day(
    lookup: Callable[[str, list[str]], tuple[dict[str, float], set[str]]], keys_by_day: dict[str, list[str]],
) -> tuple[dict[tuple[str, str], float], set[tuple[str, str]]]:
    """Runs _0930_opens()/_next_day_0930_closes() once per day and keys every price by (day, instrument_key). Keyed
    by the instrument alone, one day's price overwrote another's whenever two entries held the same contract on
    different days - the usual case for consecutive days at the same strike and expiry (e.g. S5's 1 Oct entry,
    exiting 5 Oct, was given 6 Oct's close because 5 Oct's own entry holds the same contract until 6 Oct)."""
    values: dict[tuple[str, str], float] = {}
    estimated: set[tuple[str, str]] = set()
    for day, keys in keys_by_day.items():
        found, from_ticks = lookup(day, list(dict.fromkeys(keys)))
        values.update({(day, key): value for key, value in found.items()})
        estimated |= {(day, key) for key in from_ticks}
    return values, estimated


def _today_iso() -> str:
    import os
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))).date().isoformat()


def _roll_contract(day: str, atm_strike: float, next_day: str | None = None) -> tuple[str, str, str] | None:
    """For an S1 entrant that was itself entered on its own expiry day - the contract expires that same afternoon,
    so there is nothing left to hold overnight. Per the user's rule: on such a day, S5 does not skip it, it instead
    sells the SAME ATM strike in the NEXT weekly expiry (e.g. a Tuesday-expiry entrant rolls into next Tuesday's
    contract) and holds THAT overnight like any other S5 day.

    Returns (roll_expiry, ce_key, pe_key) resolved PURELY from storage (instrument_contracts), or None if that
    expiry's option chain has never been listed - which needs resolve_s5_rolls() (a network call) to happen once;
    after that this is a plain read forever, same as every other S5 lookup.

    A contract can end up listed under BOTH key shapes at once: the roll expiry is usually listed live-shaped
    when the roll is first resolved (every week's own trading lists its whole current chain), and Upstox
    eventually reclassifies a lapsed contract into its separate expired-instruments system - but listing THAT
    (resolving some OTHER strike's own gap, or build_s5_candles()'s own fallback when a live-shaped download
    starts failing - see its docstring) lists the WHOLE chain at that expiry as a side effect, including strikes
    whose live-shaped candle was already downloaded successfully and is still sitting there, sometimes for only
    ONE of the entry/exit days if the reclassification happened to fall between the two. So when both shapes are
    known, this counts how many of `day`'s open and (if given) `next_day`'s close each shape actually has and
    prefers whichever covers more (expired breaks a tie) - never a blind "expired is newer, so it must be right"
    guess that would orphan a perfectly good earlier live download of the OTHER day.

    Checks _roll_cache first - _preload_rolls() populates it in bulk for the common unambiguous case (exactly one
    shape listed), so a caller that preloads never pays this function's own per-call query cost at all; anything
    not preloaded (ambiguous both-shapes case, or simply never batched) falls through to the logic below exactly
    as before."""
    cached = _roll_cache.get((day, atm_strike, next_day))
    if cached is not None:
        return cached
    from storage import find_option_contract, known_option_expiries

    index_key = straddle.UNDERLYINGS["NIFTY"].index_key
    later = [e for e in known_option_expiries(index_key) if e > day]
    if not later:
        return None
    roll_expiry = min(later)

    def coverage(key: str) -> int:
        return bool(_0930_candle_opens(day, [key])) + bool(next_day and _0930_candle_closes(next_day, [key]))

    def find(leg_type: str) -> str | None:
        live = find_option_contract(index_key, roll_expiry, atm_strike, leg_type, expired=False)
        expired = find_option_contract(index_key, roll_expiry, atm_strike, leg_type, expired=True)
        if live and expired:
            return live if coverage(live) > coverage(expired) else expired
        return live or expired

    ce_key, pe_key = find("CE"), find("PE")
    if not ce_key or not pe_key:
        return None
    return roll_expiry, ce_key, pe_key


_roll_cache: dict[tuple[str, float, str | None], tuple[str, str, str]] = {}


def _preload_rolls(requests: list[tuple[str, float, str | None]]) -> None:
    """Resolve many _roll_contract() requests - (day, atm_strike, next_day) triples - in a small, fixed number of
    bulk queries instead of up to 6 individual ones per request. s5_overnight_rows() has ~100 "entered on its own
    expiry day" rows across its full history, each needing its own _roll_contract() call; with a live feed writing
    to the same database throughout the day, a few hundred individual connections opened back to back in a tight
    loop can each stall on lock contention, turning a computation that should take well under a second into
    minutes. This exists so the hot path resolves everything it is going to need in one or two round trips instead.

    Only caches the unambiguous case - exactly one shape (live or expired) listed for a strike/expiry/leg-type.
    Anything with both shapes (needing _roll_contract()'s own coverage() tie-break) or no match at all is left
    alone, uncached, so it falls through to _roll_contract()'s existing per-call logic unchanged - this can never
    produce a different answer than calling _roll_contract() directly would, only skip redundant queries for the
    common case.

    Clears _roll_cache first rather than accumulating across calls: instrument_contracts can gain a second shape
    for a strike between one call and the next (resolve_s5_rolls()/build_s5_candles() add rows as they run), which
    would turn a previously-unambiguous cached resolution stale and wrong. Rebuilding fresh every time this is
    called (once per s5_overnight_rows() computation) costs nothing extra - the whole point is one bulk read
    instead of many - and can never serve an answer older than the storage it was just read from."""
    _roll_cache.clear()
    if not requests:
        return
    from storage import find_option_contracts_bulk, known_option_expiries

    index_key = straddle.UNDERLYINGS["NIFTY"].index_key
    expiries = known_option_expiries(index_key)
    roll_expiry_for: dict[str, str | None] = {}
    targets: set[tuple[str, float, str]] = set()
    for day, atm_strike, _next_day in requests:
        if day not in roll_expiry_for:
            later = [e for e in expiries if e > day]
            roll_expiry_for[day] = min(later) if later else None
        roll_expiry = roll_expiry_for[day]
        if roll_expiry is None:
            continue
        targets.add((roll_expiry, atm_strike, "CE"))
        targets.add((roll_expiry, atm_strike, "PE"))
    if not targets:
        return
    found = find_option_contracts_bulk(index_key, list(targets))
    for day, atm_strike, next_day in requests:
        roll_expiry = roll_expiry_for.get(day)
        if roll_expiry is None:
            continue
        ce_keys = found.get((roll_expiry, atm_strike, "CE"), [])
        pe_keys = found.get((roll_expiry, atm_strike, "PE"), [])
        if len(ce_keys) == 1 and len(pe_keys) == 1:
            _roll_cache[(day, atm_strike, next_day)] = (roll_expiry, ce_keys[0], pe_keys[0])


_s5_cache: dict[tuple[Any, ...], tuple[list[dict[str, Any]], list[dict[str, str]]]] = {}


def s5_overnight_rows(from_date: str | None = None, to_date: str | None = None, basis: str = MINUTE) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """S5: the same ATM straddle S1 sells at 09:30, held overnight and bought back at the NEXT trading day's 09:30 candle
    close instead of the same day's - S1's own stored legs are reused as they are (same strikes, same entry price, same
    SELL side), only the exit is replaced. "Next trading day" is the next day S1 itself has a row for, so it is exactly
    the project's own minute-data day sequence (Oct 2024 on), not an independent market-holiday calendar.

    An entrant that was itself entered on its own expiry day has no contract left to carry overnight, so it is not
    S1's own legs that get held - it is a fresh SELL of the SAME ATM strike in the NEXT weekly expiry (see
    _roll_contract()), entered at that contract's 09:30 open and, like every other S5 day, closed at the next
    trading day's 09:30 close.

    A leg's 09:30 price prefers the downloaded candle, but if that has not landed yet (typically only today - the
    market's data is not on Upstox's historical endpoint until later) it falls back to the close (or, for a rolled
    entry, the open) of the last live tick recorded in that exact minute - the SAME live ticks already stored for
    it either way, and the same fallback day_chart.py already uses for its charts, so this never sits blank just
    because the finalized candle download has not caught up. A leg priced this way carries "entry_estimated" and/or
    "exit_estimated": True (independently - a rolled leg's entry and exit can each be estimated or not on their
    own; S1's own reused entry is never estimated, only its exit ever can be), so it reads as provisional rather
    than silently identical to an exact candle price; s5_missing_candles() still reports it as a real gap
    regardless (a tick estimate is not the same as the real candle it will still fetch).

    Left out, and reported rather than dropped, when: it is the last day S1 has a row for yet; an expiry-day entrant's
    roll contracts have not been resolved from storage yet (resolve_s5_rolls() has not run for that expiry); or a leg
    (S1's own, or the rolled one) has no candle AND no live tick at exactly 09:30 on the day it is needed.

    Cached (unlike S1-S3, which are read straight from storage, this recomputes a candle lookup for every S1 day every
    time): every /api/lab/backtest, /monthly, /export and Calendar/Monthly/Compare load calls this for
    "every strategy", and would otherwise repeat the same ~480-day batched query on every page view."""
    if basis != MINUTE:  # a different table entirely (NSE end-of-day rows); S5 does not apply to it
        return [], []
    source = query_backtest_rows("straddle_sell", from_date, to_date)
    signature = (from_date, to_date, len(source), source[-1]["trading_date"] if source else "")
    if signature in _s5_cache:
        return _s5_cache[signature]
    roll_requests: list[tuple[str, float, str | None]] = []
    for i, row in enumerate(source):
        if i + 1 < len(source) and row["expiry"] == row["trading_date"]:
            roll_requests.append((row["trading_date"], row["atm_strike"], source[i + 1]["trading_date"]))
    _preload_rolls(roll_requests)

    entrants: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for i, row in enumerate(source):
        day = row["trading_date"]
        if i + 1 >= len(source):
            skipped.append({"trading_date": day, "reason": "no_next_day", "detail": "no later trading day is stored yet"})
            continue
        next_day = source[i + 1]["trading_date"]
        if row["expiry"] == day:
            rolled = _roll_contract(day, row["atm_strike"], next_day)
            if rolled is None:
                skipped.append({"trading_date": day, "reason": "own_expiry_not_rolled",
                                 "detail": "entered on its own expiry day; the next week's ATM contracts have not been resolved yet (needs the S5 build)"})
                continue
            roll_expiry, ce_key, pe_key = rolled
            entrants.append({"row": row, "next_day": next_day, "expiry": roll_expiry, "keys": [ce_key, pe_key], "roll": True})
        else:
            keys = [leg["instrument_key"] for leg in row["legs"] if leg.get("instrument_key")]
            entrants.append({"row": row, "next_day": next_day, "expiry": row["expiry"], "keys": keys, "roll": False})

    by_entry_day: dict[str, list[str]] = {}
    for e in entrants:
        if e["roll"]:
            by_entry_day.setdefault(e["row"]["trading_date"], []).extend(e["keys"])
    entry_opens, entry_estimated = _0930_by_day(_0930_opens, by_entry_day)

    by_next_day: dict[str, list[str]] = {}
    for e in entrants:
        by_next_day.setdefault(e["next_day"], []).extend(e["keys"])
    exit_closes, exit_estimated = _0930_by_day(_next_day_0930_closes, by_next_day)

    rows = []
    for e in entrants:
        row, next_day, expiry = e["row"], e["next_day"], e["expiry"]
        day = row["trading_date"]
        if e["roll"]:
            ce_key, pe_key = e["keys"]
            if (day, ce_key) not in entry_opens or (day, pe_key) not in entry_opens:
                skipped.append({"trading_date": day, "reason": "no_0930_entry_candle", "detail": f"no {S5_EXIT_MINUTE} candle for the rolled contract on {day} itself"})
                continue
            if (next_day, ce_key) not in exit_closes or (next_day, pe_key) not in exit_closes:
                skipped.append({"trading_date": day, "reason": "no_0930_next_day_candle", "detail": f"no {S5_EXIT_MINUTE} candle for a leg on {next_day}"})
                continue
            legs = [
                {"side": "SELL", "entry": entry_opens[(day, key)], "exit": exit_closes[(next_day, key)], "type": type_, "strike": row["atm_strike"], "role": "body",
                 "instrument_key": key, "entry_estimated": (day, key) in entry_estimated, "exit_estimated": (next_day, key) in exit_estimated}
                for key, type_ in ((ce_key, "CE"), (pe_key, "PE"))
            ]
            lot_size = lot_size_of(ce_key, row["lot_size"])
        else:
            legs = [
                {**leg, "exit": exit_closes[(next_day, leg["instrument_key"])], "entry_estimated": False,
                 "exit_estimated": (next_day, leg["instrument_key"]) in exit_estimated}
                for leg in row["legs"] if (next_day, leg.get("instrument_key")) in exit_closes
            ]
            if len(legs) != len(row["legs"]):
                skipped.append({"trading_date": day, "reason": "no_0930_next_day_candle", "detail": f"no {S5_EXIT_MINUTE} candle for a leg on {next_day}"})
                continue
            lot_size = row["lot_size"]
        rows.append({
            "strategy": S5, "trading_date": day, "exit_date": next_day, "expiry": expiry, "atm_strike": row["atm_strike"],
            "wing_width": None, "lot_size": lot_size, "lots": 1, "legs": legs, **settle(legs, lot_size, 1, DEFAULT_COST_MODEL),
            "rolled_to_next_expiry": e["roll"],
        })
    rows.sort(key=lambda r: r["trading_date"])
    skipped.sort(key=lambda s: s["trading_date"])
    if len(_s5_cache) > 8:
        _s5_cache.clear()
    _s5_cache[signature] = (rows, skipped)
    return rows, skipped


_s6_cache: dict[tuple[Any, ...], tuple[list[dict[str, Any]], list[dict[str, str]]]] = {}


def s6_overnight_rows(from_date: str | None = None, to_date: str | None = None, basis: str = MINUTE) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """S6: the same idea as S5 (see s5_overnight_rows()), held for TWO nights instead of one - sold at day D's 09:30,
    bought back at the day-AFTER-next trading day's 09:30 close. Entered every trading day like S5 (not every
    OTHER day), so on any given day there is usually one S6 position on its first night and another on its second.

    The held contract does not always survive both nights unchanged: S1's own weekly expiry can fall on the
    MIDDLE day (the day between entry and exit), not just on the entry day itself. Two cases:
      * the contract's expiry is later than the middle day - it is simply held straight through, exactly like
        one S5 leg stretched to two nights (one pair of legs, one entry price, one exit price two days later).
      * the contract's own expiry IS the middle day - the same "sell the SAME ATM strike in the next weekly
        expiry" roll S5 uses for an entrant on ITS OWN expiry day happens here too, just one day later: the
        first contract is bought back at the middle day's 09:30 close (it would expire that afternoon), and a
        fresh one is sold in the next expiry at the middle day's 09:30 open, held to the exit day's close. This
        produces FOUR legs (two chained SELL/BUY round trips per side) instead of two - settle() already sums
        an arbitrary list of legs, so this is not a special case for it, only for how many legs get built here.

    Never paper-traded (see S5's own paper engine - a third, two-night variant is not built). Cached the same way
    s5_overnight_rows() is, for the same reason (recomputed candle lookups, not stored history)."""
    if basis != MINUTE:
        return [], []
    source = query_backtest_rows("straddle_sell", from_date, to_date)
    signature = (from_date, to_date, len(source), source[-1]["trading_date"] if source else "")
    if signature in _s6_cache:
        return _s6_cache[signature]
    entrants: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for i, row in enumerate(source):
        day = row["trading_date"]
        if i + 2 >= len(source):
            skipped.append({"trading_date": day, "reason": "no_second_next_day", "detail": "two later trading days are not both stored yet"})
            continue
        next_day, exit_day = source[i + 1]["trading_date"], source[i + 2]["trading_date"]
        if row["expiry"] == day:
            rolled = _roll_contract(day, row["atm_strike"], next_day)
            if rolled is None:
                skipped.append({"trading_date": day, "reason": "own_expiry_not_rolled",
                                 "detail": "entered on its own expiry day; the next week's ATM contracts have not been resolved yet (needs the S5 build)"})
                continue
            expiry1, ce1, pe1 = rolled
            leg1_rolled = True
        else:
            expiry1 = row["expiry"]
            ce1 = next((leg["instrument_key"] for leg in row["legs"] if leg["type"] == "CE" and leg.get("instrument_key")), None)
            pe1 = next((leg["instrument_key"] for leg in row["legs"] if leg["type"] == "PE" and leg.get("instrument_key")), None)
            leg1_rolled = False
            if not ce1 or not pe1:
                skipped.append({"trading_date": day, "reason": "no_instrument_key", "detail": "a leg on the stored S1 row has no instrument_key"})
                continue
        midpoint_roll = expiry1 == next_day
        expiry2 = ce2 = pe2 = None
        if midpoint_roll:
            rolled2 = _roll_contract(next_day, row["atm_strike"], exit_day)
            if rolled2 is None:
                skipped.append({"trading_date": day, "reason": "midpoint_not_rolled",
                                 "detail": f"the held contract expires on {next_day} itself; the week after's ATM contracts have not been resolved yet (needs the S5 build)"})
                continue
            expiry2, ce2, pe2 = rolled2
        entrants.append({
            "row": row, "day": day, "next_day": next_day, "exit_day": exit_day, "leg1_rolled": leg1_rolled,
            "expiry1": expiry1, "ce1": ce1, "pe1": pe1, "midpoint_roll": midpoint_roll, "expiry2": expiry2, "ce2": ce2, "pe2": pe2,
        })

    # every 09:30 OPEN this needs: a fresh leg1 sale on `day` (roll entrants only), and a fresh leg2 sale on
    # `next_day` (midpoint-roll entrants only) - S1's own reused entry never needs a lookup here.
    opens_by_day: dict[str, list[str]] = {}
    for e in entrants:
        if e["leg1_rolled"]:
            opens_by_day.setdefault(e["day"], []).extend([e["ce1"], e["pe1"]])
        if e["midpoint_roll"]:
            opens_by_day.setdefault(e["next_day"], []).extend([e["ce2"], e["pe2"]])
    opens, opens_estimated = _0930_by_day(_0930_opens, opens_by_day)

    # every 09:30 CLOSE this needs: leg1 bought back on `next_day` for a midpoint roll (it expires that day), and
    # whichever leg (leg1 straight through, or leg2 after a midpoint roll) is the one that closes on `exit_day`.
    closes_by_day: dict[str, list[str]] = {}
    for e in entrants:
        if e["midpoint_roll"]:
            closes_by_day.setdefault(e["next_day"], []).extend([e["ce1"], e["pe1"]])
            closes_by_day.setdefault(e["exit_day"], []).extend([e["ce2"], e["pe2"]])
        else:
            closes_by_day.setdefault(e["exit_day"], []).extend([e["ce1"], e["pe1"]])
    closes, closes_estimated = _0930_by_day(_next_day_0930_closes, closes_by_day)

    def leg(key: str, type_: str, strike: float, entry: float | None, exit_: float | None, entry_est: bool, exit_est: bool) -> dict[str, Any] | None:
        if entry is None or exit_ is None:
            return None
        return {"side": "SELL", "entry": entry, "exit": exit_, "type": type_, "strike": strike, "role": "body", "instrument_key": key,
                "entry_estimated": entry_est, "exit_estimated": exit_est}

    rows = []
    for e in entrants:
        day, next_day, exit_day, strike = e["day"], e["next_day"], e["exit_day"], e["row"]["atm_strike"]
        if e["leg1_rolled"]:
            entry_source = {key: opens[(day, key)] for key in (e["ce1"], e["pe1"]) if (day, key) in opens}
            entry_est_keys = {key for key in (e["ce1"], e["pe1"]) if (day, key) in opens_estimated}
        else:
            entry_source = {l["instrument_key"]: l["entry"] for l in e["row"]["legs"] if l.get("instrument_key")}
            entry_est_keys = set()
        leg1_end = next_day if e["midpoint_roll"] else exit_day
        if e["midpoint_roll"]:
            legs = [
                leg(e["ce1"], "CE", strike, entry_source.get(e["ce1"]), closes.get((leg1_end, e["ce1"])), e["ce1"] in entry_est_keys, (leg1_end, e["ce1"]) in closes_estimated),
                leg(e["pe1"], "PE", strike, entry_source.get(e["pe1"]), closes.get((leg1_end, e["pe1"])), e["pe1"] in entry_est_keys, (leg1_end, e["pe1"]) in closes_estimated),
                leg(e["ce2"], "CE", strike, opens.get((next_day, e["ce2"])), closes.get((exit_day, e["ce2"])),
                    (next_day, e["ce2"]) in opens_estimated, (exit_day, e["ce2"]) in closes_estimated),
                leg(e["pe2"], "PE", strike, opens.get((next_day, e["pe2"])), closes.get((exit_day, e["pe2"])),
                    (next_day, e["pe2"]) in opens_estimated, (exit_day, e["pe2"]) in closes_estimated),
            ]
            expiry = e["expiry2"]
        else:
            legs = [
                leg(e["ce1"], "CE", strike, entry_source.get(e["ce1"]), closes.get((leg1_end, e["ce1"])), e["ce1"] in entry_est_keys, (leg1_end, e["ce1"]) in closes_estimated),
                leg(e["pe1"], "PE", strike, entry_source.get(e["pe1"]), closes.get((leg1_end, e["pe1"])), e["pe1"] in entry_est_keys, (leg1_end, e["pe1"]) in closes_estimated),
            ]
            expiry = e["expiry1"]
        if any(l is None for l in legs):
            missing_entry = entry_source.get(e["ce1"]) is None or entry_source.get(e["pe1"]) is None
            if missing_entry:
                skipped.append({"trading_date": day, "reason": "no_0930_entry_candle", "detail": f"no {S5_EXIT_MINUTE} candle for the rolled contract on {day} itself"})
            elif e["midpoint_roll"] and (closes.get((next_day, e["ce1"])) is None or closes.get((next_day, e["pe1"])) is None
                                         or opens.get((next_day, e["ce2"])) is None or opens.get((next_day, e["pe2"])) is None):
                skipped.append({"trading_date": day, "reason": "no_0930_midpoint_candle", "detail": f"no {S5_EXIT_MINUTE} candle for a leg on {next_day}"})
            else:
                skipped.append({"trading_date": day, "reason": "no_0930_exit_candle", "detail": f"no {S5_EXIT_MINUTE} candle for a leg on {exit_day}"})
            continue
        lot_size = lot_size_of(e["ce1"], e["row"]["lot_size"]) if e["leg1_rolled"] else e["row"]["lot_size"]
        rows.append({
            "strategy": S6, "trading_date": day, "exit_date": exit_day, "expiry": expiry, "atm_strike": strike,
            "wing_width": None, "lot_size": lot_size, "lots": 1, "legs": legs, **settle(legs, lot_size, 1, DEFAULT_COST_MODEL),
            "rolled_to_next_expiry": e["leg1_rolled"], "rolled_mid_hold": e["midpoint_roll"],
        })
    rows.sort(key=lambda r: r["trading_date"])
    skipped.sort(key=lambda s: s["trading_date"])
    if len(_s6_cache) > 8:
        _s6_cache.clear()
    _s6_cache[signature] = (rows, skipped)
    return rows, skipped


def _s7_segments(row: dict[str, Any], mid1: str, mid2: str, exit_day: str) -> list[dict[str, Any]] | dict[str, Any]:
    """Walks the two middle days in order, rolling the held contract (S5's own rule - see _roll_contract()) each
    time its expiry lands on one, and returns the chain of 1-3 (start, end, expiry, ce, pe, fresh) segments that
    covers the whole entry-to-exit hold. `fresh` marks a segment whose ENTRY is a brand-new sale (its 09:30 open
    is needed) rather than S1's own reused entry price - true for every segment except a possible first one that
    is S1's own untouched contract. Shared verbatim by s7_overnight_rows() and s7_missing_candles()/
    s7_missing_mid_rolls(), so the three agree exactly on what a hold looks like.

    Returns a {"reason": ...} dict instead when a roll this entrant needs is not resolvable from storage yet -
    "own_expiry_not_rolled" or "midpoint_not_rolled" (the latter also carries "checkpoint"/"atm_strike"/
    "next_target", the exact (day, strike, day) s7_missing_mid_rolls() needs, so callers never have to re-walk
    the chain to find out which of the two middle days stopped it)."""
    day, strike = row["trading_date"], row["atm_strike"]
    if row["expiry"] == day:
        rolled = _roll_contract(day, strike, mid1)
        if rolled is None:
            return {"reason": "own_expiry_not_rolled"}
        cur_expiry, cur_ce, cur_pe = rolled
        cur_fresh, cur_start = True, day
    else:
        cur_ce = next((leg["instrument_key"] for leg in row["legs"] if leg["type"] == "CE" and leg.get("instrument_key")), None)
        cur_pe = next((leg["instrument_key"] for leg in row["legs"] if leg["type"] == "PE" and leg.get("instrument_key")), None)
        if not cur_ce or not cur_pe:
            return {"reason": "no_instrument_key"}
        cur_expiry, cur_fresh, cur_start = row["expiry"], False, day

    segments: list[dict[str, Any]] = []
    for checkpoint, next_target in ((mid1, mid2), (mid2, exit_day)):
        if cur_expiry != checkpoint:
            continue
        segments.append({"start": cur_start, "end": checkpoint, "expiry": cur_expiry, "ce": cur_ce, "pe": cur_pe, "fresh": cur_fresh})
        rolled = _roll_contract(checkpoint, strike, next_target)
        if rolled is None:
            return {"reason": "midpoint_not_rolled", "checkpoint": checkpoint, "atm_strike": strike, "next_target": next_target}
        cur_expiry, cur_ce, cur_pe = rolled
        cur_fresh, cur_start = True, checkpoint
    segments.append({"start": cur_start, "end": exit_day, "expiry": cur_expiry, "ce": cur_ce, "pe": cur_pe, "fresh": cur_fresh})
    return segments


_s7_cache: dict[tuple[Any, ...], tuple[list[dict[str, Any]], list[dict[str, str]]]] = {}


def s7_overnight_rows(from_date: str | None = None, to_date: str | None = None, basis: str = MINUTE) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """S7: the same idea as S6 (see its own docstring), held for THREE nights instead of two - sold at day D's
    09:30, bought back at the THIRD trading day after D's 09:30 close. Entered every trading day like S5/S6, so on
    any given day there can be up to 3 concurrent S7 positions (first, second, or third night).

    The held contract's own weekly expiry can fall on EITHER of the two days between entry and exit (not just
    one, as with S6) - _s7_segments() walks them in order and rolls again (the same S5/S6 rule) each time the
    currently-held contract's expiry lands on one, so a hold produces 2, 4, or 6 legs (1, 2, or 3 chained
    SELL/BUY round trips per side). In practice a NIFTY weekly expiry is roughly 5 trading days apart, so two
    rolls inside one entry-to-exit window (4 trading days) would be unusual, but nothing here assumes it can't
    happen - see _s7_segments()'s own docstring.

    Never paper-traded (see S5's own paper engine). Cached the same way s5_overnight_rows()/s6_overnight_rows() are."""
    if basis != MINUTE:
        return [], []
    source = query_backtest_rows("straddle_sell", from_date, to_date)
    signature = (from_date, to_date, len(source), source[-1]["trading_date"] if source else "")
    if signature in _s7_cache:
        return _s7_cache[signature]
    entrants: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for i, row in enumerate(source):
        day = row["trading_date"]
        if i + 3 >= len(source):
            skipped.append({"trading_date": day, "reason": "no_third_next_day", "detail": "three later trading days are not all stored yet"})
            continue
        mid1, mid2, exit_day = source[i + 1]["trading_date"], source[i + 2]["trading_date"], source[i + 3]["trading_date"]
        segments = _s7_segments(row, mid1, mid2, exit_day)
        if isinstance(segments, dict):
            reason = segments["reason"]
            detail = ("entered on its own expiry day; the next week's ATM contracts have not been resolved yet (needs the S5 build)"
                       if reason == "own_expiry_not_rolled" else
                       "the held contract expires on a middle day of this hold; the week after's ATM contracts have not been resolved yet (needs the S5 build)"
                       if reason == "midpoint_not_rolled" else
                       "a leg on the stored S1 row has no instrument_key")
            skipped.append({"trading_date": day, "reason": reason, "detail": detail})
            continue
        entrants.append({"row": row, "day": day, "exit_day": exit_day, "segments": segments})

    opens_by_day: dict[str, list[str]] = {}
    for e in entrants:
        for seg in e["segments"]:
            if seg["fresh"]:
                opens_by_day.setdefault(seg["start"], []).extend([seg["ce"], seg["pe"]])
    opens, opens_estimated = _0930_by_day(_0930_opens, opens_by_day)

    closes_by_day: dict[str, list[str]] = {}
    for e in entrants:
        for seg in e["segments"]:
            closes_by_day.setdefault(seg["end"], []).extend([seg["ce"], seg["pe"]])
    closes, closes_estimated = _0930_by_day(_next_day_0930_closes, closes_by_day)

    def leg(key: str, type_: str, strike: float, entry: float | None, exit_: float | None, entry_est: bool, exit_est: bool) -> dict[str, Any] | None:
        if entry is None or exit_ is None:
            return None
        return {"side": "SELL", "entry": entry, "exit": exit_, "type": type_, "strike": strike, "role": "body", "instrument_key": key,
                "entry_estimated": entry_est, "exit_estimated": exit_est}

    rows = []
    for e in entrants:
        row, day, exit_day, segments = e["row"], e["day"], e["exit_day"], e["segments"]
        strike = row["atm_strike"]
        s1_entries = {l["instrument_key"]: l["entry"] for l in row["legs"] if l.get("instrument_key")}
        legs: list[dict[str, Any]] = []
        gap: tuple[str, str] | None = None
        for seg_index, seg in enumerate(segments):
            is_first, is_last = seg_index == 0, seg_index == len(segments) - 1
            for key, type_ in ((seg["ce"], "CE"), (seg["pe"], "PE")):
                if seg["fresh"]:
                    entry_val, entry_est = opens.get((seg["start"], key)), (seg["start"], key) in opens_estimated
                else:
                    entry_val, entry_est = s1_entries.get(key), False
                exit_val, exit_est = closes.get((seg["end"], key)), (seg["end"], key) in closes_estimated
                built = leg(key, type_, strike, entry_val, exit_val, entry_est, exit_est)
                if built is None:
                    if entry_val is None:
                        gap = (("no_0930_entry_candle", f"no {S5_EXIT_MINUTE} candle for the rolled contract on {seg['start']} itself") if is_first else
                               ("no_0930_midpoint_candle", f"no {S5_EXIT_MINUTE} candle for a leg on {seg['start']}"))
                    else:
                        gap = (("no_0930_exit_candle", f"no {S5_EXIT_MINUTE} candle for a leg on {seg['end']}") if is_last else
                               ("no_0930_midpoint_candle", f"no {S5_EXIT_MINUTE} candle for a leg on {seg['end']}"))
                    break
                legs.append(built)
            if gap:
                break
        if gap:
            skipped.append({"trading_date": day, "reason": gap[0], "detail": gap[1]})
            continue
        lot_size = lot_size_of(segments[0]["ce"], row["lot_size"]) if segments[0]["fresh"] else row["lot_size"]
        rows.append({
            "strategy": S7, "trading_date": day, "exit_date": exit_day, "expiry": segments[-1]["expiry"], "atm_strike": strike,
            "wing_width": None, "lot_size": lot_size, "lots": 1, "legs": legs, **settle(legs, lot_size, 1, DEFAULT_COST_MODEL),
            "rolled_to_next_expiry": segments[0]["fresh"], "rolled_mid_hold": len(segments) > 1,
        })
    rows.sort(key=lambda r: r["trading_date"])
    skipped.sort(key=lambda s: s["trading_date"])
    if len(_s7_cache) > 8:
        _s7_cache.clear()
    _s7_cache[signature] = (rows, skipped)
    return rows, skipped


def s8_overnight_rows(from_date: str | None = None, to_date: str | None = None, basis: str = MINUTE) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """S8: S5 itself (see its own docstring), restricted to never enter on a Friday - a Friday night is really a
    weekend hold (Saturday and Sunday with the market shut, then whatever gap Monday's open brings), a materially
    different risk than every other night's single-trading-day gap. Every other day, INCLUDING an entrant on its
    own expiry day - which still rolls into next week's same ATM strike exactly like S5 does - is identical to S5.

    A straight filter over s5_overnight_rows()'s own result, not an independent computation: same rows, same roll
    machinery, same candle needs. A Friday day just never makes it past the filter (moved into this function's own
    skipped list under "not_entered_friday" instead of S5's), and everything else - the source rows, S5's cache,
    S5's candle-backfill - is shared outright. If a Friday also happens to be an unresolved-roll expiry day, S5's
    own reason (it is not computable at all yet, regardless of weekday) is kept rather than overridden.

    Paper: no orders of its own - its paper trades are S5's, minus the Friday entries (s8_paper_trades(); asked for on
    2026-10-08: "s5 paper trade is same for s8, only thing is that there is no trade on Friday")."""
    if basis != MINUTE:
        return [], []
    rows, skipped = s5_overnight_rows(from_date, to_date, basis)
    kept_rows: list[dict[str, Any]] = []
    kept_skipped = list(skipped)
    for row in rows:
        if date.fromisoformat(row["trading_date"]).weekday() == 4:
            kept_skipped.append({"trading_date": row["trading_date"], "reason": "not_entered_friday", "detail": "S8 does not enter on a Friday (by design - avoids the weekend gap into Monday's open)"})
        else:
            kept_rows.append({**row, "strategy": S8})
    kept_rows.sort(key=lambda r: r["trading_date"])
    kept_skipped.sort(key=lambda s: s["trading_date"])
    return kept_rows, kept_skipped


def s10_overnight_rows(from_date: str | None = None, to_date: str | None = None, basis: str = MINUTE) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """S10: S8 itself (see its own docstring), also never entered in December. A straight filter over
    s8_overnight_rows()'s own result, the same way S8 filters S5: a December day moves into this function's skipped
    list under "not_entered_december", and S8's own reasons (a Friday, no next day yet, ...) are kept as they are.

    Built 2026-10-07 to track an idea, not as a proven edge: both Decembers in the history (2024, 2025) lost money,
    but that is two data points, mostly from one trade (2025-12-08). Backtest only, never paper-traded."""
    if basis != MINUTE:
        return [], []
    rows, skipped = s8_overnight_rows(from_date, to_date, basis)
    kept_rows: list[dict[str, Any]] = []
    kept_skipped = list(skipped)
    for row in rows:
        if row["trading_date"][5:7] == "12":
            kept_skipped.append({"trading_date": row["trading_date"], "reason": "not_entered_december", "detail": "S10 does not enter in December (by design)"})
        else:
            kept_rows.append({**row, "strategy": S10})
    kept_skipped.sort(key=lambda s: s["trading_date"])
    return kept_rows, kept_skipped


def s5_missing_candles() -> list[tuple[str, str, str]]:
    """(instrument_key, day, expiry) for every leg S5 needs a 09:30 candle for and does not yet have - the exact gap
    s5_overnight_rows() reports as "no_0930_next_day_candle" and "no_0930_entry_candle". Covers both S1's own
    contracts (only the next day's exit candle can ever be missing - the entry day's was already required to build
    S1 itself) and a resolved roll contract (an S1 expiry-day entrant's next-week ATM legs - see _roll_contract() -
    which need BOTH the entry day's and the next day's candle, since neither S1 nor anything else has ever touched
    that contract before). An expiry-day entrant whose roll is not resolved yet is not a candle gap - see
    s5_missing_rolls() and resolve_s5_rolls() for that, earlier step."""
    source = query_backtest_rows("straddle_sell")
    wanted: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def want(key: str, day: str, expiry: str) -> None:
        if (key, day) not in seen:
            seen.add((key, day))
            wanted.append((key, day, expiry))

    for i, row in enumerate(source):
        if i + 1 >= len(source):
            continue
        day = row["trading_date"]
        next_day = source[i + 1]["trading_date"]
        if row["expiry"] == day:
            rolled = _roll_contract(day, row["atm_strike"], next_day)
            if rolled is None:
                continue  # not resolvable yet - resolve_s5_rolls() handles this, not a candle gap
            expiry, ce_key, pe_key = rolled
            keys = [ce_key, pe_key]
            have_entry = _0930_candle_opens(day, keys)  # the real candle, not a tick estimate - see its docstring
            for key in keys:
                if key not in have_entry:
                    want(key, day, expiry)
            have_exit = _0930_candle_closes(next_day, keys)
            for key in keys:
                if key not in have_exit:
                    want(key, next_day, expiry)
            continue
        keys = [leg["instrument_key"] for leg in row["legs"] if leg.get("instrument_key")]
        have = _0930_candle_closes(next_day, keys)
        for key in keys:
            if key not in have:
                want(key, next_day, row["expiry"])
    return wanted


def s6_missing_candles() -> list[tuple[str, str, str]]:
    """(instrument_key, day, expiry) for every leg S6 needs a 09:30 candle for and does not yet have - the exact
    gaps s6_overnight_rows() reports as "no_0930_entry_candle", "no_0930_midpoint_candle" and "no_0930_exit_candle".
    Shares S5's own roll (_roll_contract()/resolve_s5_rolls()) for an entrant on ITS OWN expiry day - if that has
    already run for S5, S6 reuses the same resolved contract and the same entry candle, nothing new to fetch there.
    What IS new here, beyond anything S5's own build ever needs: the exit-day close two trading days out (S5 only
    ever looks one day ahead), and - for an entrant whose held contract expires on the middle day - the mid-hold
    roll's own entry (the middle day's open) and exit (the exit day's close), on top of the first contract's own
    middle-day close. A roll (own-expiry or mid-hold) that is not resolvable from storage yet is not a candle gap -
    see s5_missing_rolls()/resolve_s5_rolls() and s6_missing_mid_rolls()/resolve_s6_mid_rolls() for those, earlier
    steps run by build_s6_candles() before this."""
    source = query_backtest_rows("straddle_sell")
    wanted: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def want(key: str, day: str, expiry: str) -> None:
        if (key, day) not in seen:
            seen.add((key, day))
            wanted.append((key, day, expiry))

    for i, row in enumerate(source):
        if i + 2 >= len(source):
            continue
        day = row["trading_date"]
        next_day, exit_day = source[i + 1]["trading_date"], source[i + 2]["trading_date"]
        if row["expiry"] == day:
            rolled = _roll_contract(day, row["atm_strike"], next_day)
            if rolled is None:
                continue  # not resolvable yet - resolve_s5_rolls() handles this, not a candle gap
            expiry1, ce1, pe1 = rolled
        else:
            expiry1 = row["expiry"]
            ce1 = next((leg["instrument_key"] for leg in row["legs"] if leg["type"] == "CE" and leg.get("instrument_key")), None)
            pe1 = next((leg["instrument_key"] for leg in row["legs"] if leg["type"] == "PE" and leg.get("instrument_key")), None)
            if not ce1 or not pe1:
                continue
        if expiry1 == next_day:
            rolled2 = _roll_contract(next_day, row["atm_strike"], exit_day)
            if rolled2 is None:
                continue  # not resolvable yet - resolve_s6_mid_rolls() handles this, not a candle gap
            expiry2, ce2, pe2 = rolled2
            have_mid_close = _0930_candle_closes(next_day, [ce1, pe1])
            for key in (ce1, pe1):
                if key not in have_mid_close:
                    want(key, next_day, expiry1)
            have_mid_open = _0930_candle_opens(next_day, [ce2, pe2])
            for key in (ce2, pe2):
                if key not in have_mid_open:
                    want(key, next_day, expiry2)
            have_exit = _0930_candle_closes(exit_day, [ce2, pe2])
            for key in (ce2, pe2):
                if key not in have_exit:
                    want(key, exit_day, expiry2)
        else:
            have_exit = _0930_candle_closes(exit_day, [ce1, pe1])
            for key in (ce1, pe1):
                if key not in have_exit:
                    want(key, exit_day, expiry1)
    return wanted


def s6_missing_mid_rolls() -> list[tuple[str, float, str]]:
    """(next_day, atm_strike, exit_day) for every S6 entrant whose held contract's own expiry falls on the MIDDLE
    day, and whose mid-hold roll - the same "sell the same ATM strike in the next weekly expiry" rule S5 uses for
    an entrant on its own expiry day (_roll_contract()), just checked one day later - is not resolvable from
    stored instrument_contracts yet. resolve_s6_mid_rolls() lists them (one Upstox contract-list call per distinct
    next expiry, however many middle days share it); after that s6_overnight_rows() reads them purely from
    storage, like every other S6 lookup. `exit_day` is carried along only so callers can see how far out the roll
    needs to reach - _resolve_rolls() itself only uses (day, atm_strike)."""
    source = query_backtest_rows("straddle_sell")
    wanted: list[tuple[str, float, str]] = []
    for i, row in enumerate(source):
        if i + 2 >= len(source):
            continue
        day = row["trading_date"]
        next_day, exit_day = source[i + 1]["trading_date"], source[i + 2]["trading_date"]
        if row["expiry"] == day:
            rolled = _roll_contract(day, row["atm_strike"], next_day)
            if rolled is None:
                continue  # S1's own roll isn't resolved yet either - resolve_s5_rolls() handles that step first
            expiry1 = rolled[0]
        else:
            expiry1 = row["expiry"]
        if expiry1 == next_day and _roll_contract(next_day, row["atm_strike"], exit_day) is None:
            wanted.append((next_day, row["atm_strike"], exit_day))
    return wanted


def s7_missing_candles() -> list[tuple[str, str, str]]:
    """(instrument_key, day, expiry) for every leg S7 needs a 09:30 candle for and does not yet have - mirrors
    s6_missing_candles() for S7's own three-night structure, walking the same _s7_segments() chain
    s7_overnight_rows() itself builds so the two agree exactly on what candle each segment needs: every fresh
    segment's own entry (its start day's open) and every segment's exit (its end day's close). A roll (own-expiry,
    or either mid-hold roll) that is not resolvable from storage yet is not a candle gap - see
    s5_missing_rolls()/resolve_s5_rolls() and s7_missing_mid_rolls()/resolve_s7_mid_rolls() for those, earlier
    steps run by build_s7_candles() before this."""
    source = query_backtest_rows("straddle_sell")
    wanted: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def want(key: str, day: str, expiry: str) -> None:
        if (key, day) not in seen:
            seen.add((key, day))
            wanted.append((key, day, expiry))

    for i, row in enumerate(source):
        if i + 3 >= len(source):
            continue
        mid1, mid2, exit_day = source[i + 1]["trading_date"], source[i + 2]["trading_date"], source[i + 3]["trading_date"]
        segments = _s7_segments(row, mid1, mid2, exit_day)
        if isinstance(segments, dict):
            continue  # a roll it needs is not resolvable yet - not a candle gap
        for seg in segments:
            if seg["fresh"]:
                have_entry = _0930_candle_opens(seg["start"], [seg["ce"], seg["pe"]])
                for key in (seg["ce"], seg["pe"]):
                    if key not in have_entry:
                        want(key, seg["start"], seg["expiry"])
            have_exit = _0930_candle_closes(seg["end"], [seg["ce"], seg["pe"]])
            for key in (seg["ce"], seg["pe"]):
                if key not in have_exit:
                    want(key, seg["end"], seg["expiry"])
    return wanted


def s7_missing_mid_rolls() -> list[tuple[str, float, str]]:
    """(checkpoint_day, atm_strike, next_target_day) for every S7 entrant whose held contract's own expiry falls
    on one of the two middle days, and whose roll there is not resolvable from stored instrument_contracts yet -
    mirrors s6_missing_mid_rolls() for S7's own two candidate roll days, via the same _s7_segments() walk. Only
    the FIRST unresolved checkpoint per entrant is reported (s7_overnight_rows() cannot see whether a SECOND roll
    would be needed until the first one is resolved), so resolve_s7_mid_rolls() may need to run more than once
    for an entrant that turns out to need two rolls - each run resolves whatever the walk can now see."""
    source = query_backtest_rows("straddle_sell")
    wanted: list[tuple[str, float, str]] = []
    for i, row in enumerate(source):
        if i + 3 >= len(source):
            continue
        mid1, mid2, exit_day = source[i + 1]["trading_date"], source[i + 2]["trading_date"], source[i + 3]["trading_date"]
        segments = _s7_segments(row, mid1, mid2, exit_day)
        if isinstance(segments, dict) and segments["reason"] == "midpoint_not_rolled":
            wanted.append((segments["checkpoint"], segments["atm_strike"], segments["next_target"]))
    return wanted


def s5_missing_rolls() -> list[tuple[str, float]]:
    """(trading_date, atm_strike) for every S1 expiry-day entrant whose next weekly expiry's ATM contracts are not
    resolvable from stored instrument_contracts yet. resolve_s5_rolls() lists them (one Upstox contract-list call
    per distinct next expiry, however many entry days share it); after that s5_overnight_rows() reads them purely
    from storage, like every other S5 lookup."""
    source = query_backtest_rows("straddle_sell")
    wanted: list[tuple[str, float]] = []
    for i, row in enumerate(source):
        if i + 1 >= len(source):
            continue
        day = row["trading_date"]
        if row["expiry"] == day and _roll_contract(day, row["atm_strike"]) is None:
            wanted.append((day, row["atm_strike"]))
    return wanted


def _resolve_rolls(wanted: list[tuple[str, float]], token_provider: Callable[[], str | None], log: Callable[[str], None], tag: str) -> dict[str, Any]:
    """Shared by resolve_s5_rolls() and resolve_s6_mid_rolls(): for each (day, atm_strike) still unresolved, looks
    up the next weekly expiry after `day` and lists its whole option chain into instrument_contracts - one Upstox
    call per DISTINCT next expiry (ContractResolver caches that within the run), not one per (day, strike), since
    several entrants a week apart never need a second listing of the same expiry. Resumable: an expiry already
    listed is a pure storage read next time (see _roll_contract()), no network call. Stops on a dead token or too
    many failures in a row, same as every other builder here.

    The next expiry itself prefers known_option_expiries() (a plain storage read - _roll_contract()'s own source),
    falling back to ContractResolver.nearest_expiry() only when nothing local is known yet. ContractResolver picks
    the next expiry from Upstox's LIVE search (soon-to-trade) plus its EXPIRED list (already reclassified there);
    an expiry that lapsed hours ago can sit in neither for a while, so nearest_expiry() alone would silently
    skip straight to the week AFTER it - wrong, since that expiry was already listed locally while it was still
    live (every earlier week's own trading listed its whole chain), so the local read finds it correctly."""
    from storage import known_option_expiries

    index_key = straddle.UNDERLYINGS["NIFTY"].index_key
    resolver = straddle.ContractResolver(straddle.UNDERLYINGS["NIFTY"], token_provider, True, date.fromisoformat(_today_iso()), log)
    stats = {"total": len(wanted), "attempted": 0, "resolved": 0, "errors": 0, "done": 0, "current": None, "stopped_because": "completed", "error_samples": []}
    consecutive_errors = 0
    for day, atm_strike in wanted:
        stats["current"] = f"{day} {atm_strike:g}"
        stats["attempted"] += 1
        try:
            known_later = [e for e in known_option_expiries(index_key) if e > day]
            roll_expiry = min(known_later) if known_later else resolver.nearest_expiry((date.fromisoformat(day) + timedelta(days=1)).isoformat())
            resolver.option_keys(roll_expiry, atm_strike)
            stats["resolved"] += 1
            consecutive_errors = 0
        except straddle.BuildAborted as error:
            stats["stopped_because"] = str(error)
            log(f"{tag} roll resolve stopped: {error}")
            break
        except HTTPError as error:
            if error.code == 401:  # the token itself is dead: every further request would fail the same way
                stats["stopped_because"] = "Upstox rejected the access token (HTTP 401) - it has probably expired; log in again"
                log(f"{tag} roll resolve stopped: {stats['stopped_because']}")
                break
            stats["errors"] += 1
            stats["error_samples"] = [*stats["error_samples"], f"{day} {atm_strike:g}: HTTP {error.code}"][-10:]
            consecutive_errors += 1
            log(f"{tag} roll resolve: {day}: ERROR - HTTP {error.code}")
            if consecutive_errors >= straddle.MAX_CONSECUTIVE_ERRORS:
                stats["stopped_because"] = f"{consecutive_errors} failures in a row"
                break
        except Exception as error:  # noqa: BLE001 - one bad (day, strike) - e.g. that strike was never listed for the next expiry - must not sink the run
            stats["errors"] += 1
            stats["error_samples"] = [*stats["error_samples"], f"{day} {atm_strike:g}: {type(error).__name__}: {error}"][-10:]
            consecutive_errors += 1
            log(f"{tag} roll resolve: {day}: ERROR - {type(error).__name__}: {error}")
            if consecutive_errors >= straddle.MAX_CONSECUTIVE_ERRORS:
                stats["stopped_because"] = f"{consecutive_errors} failures in a row"
                break
        stats["done"] += 1
    return {"run": stats}


def resolve_s5_rolls(token_provider: Callable[[], str | None], log: Callable[[str], None] = print) -> dict[str, Any]:
    """For every S1 expiry-day entrant S5 cannot roll into yet (s5_missing_rolls()), resolves its next weekly
    expiry's ATM contracts - see _resolve_rolls() for how. S6 shares this same roll for its own leg1 (an entrant on
    its own expiry day is exactly the same situation there, one day earlier than S6's own exit); S6's SECOND,
    mid-hold roll is a separate step, resolve_s6_mid_rolls()."""
    return _resolve_rolls(s5_missing_rolls(), token_provider, log, "S5")


def resolve_s6_mid_rolls(token_provider: Callable[[], str | None], log: Callable[[str], None] = print) -> dict[str, Any]:
    """For every S6 entrant whose held contract expires on the MIDDLE day (s6_missing_mid_rolls()), resolves the
    week-after's ATM contracts the same way resolve_s5_rolls() resolves an S1 expiry-day entrant's roll - see
    _resolve_rolls(). This is S6's own, second roll (its first, shared with S5's own leg1 roll, is resolved by
    resolve_s5_rolls() instead - both run as part of build_s6_candles())."""
    wanted = [(day, atm_strike) for day, atm_strike, _exit_day in s6_missing_mid_rolls()]
    return _resolve_rolls(wanted, token_provider, log, "S6 mid-hold")


def resolve_s7_mid_rolls(token_provider: Callable[[], str | None], log: Callable[[str], None] = print) -> dict[str, Any]:
    """For every S7 entrant whose held contract expires on one of the two middle days (s7_missing_mid_rolls()),
    resolves that week-after's ATM contracts - see _resolve_rolls(). S7's first roll (an entrant on its own
    expiry day) is shared with S5/S6 via resolve_s5_rolls() instead; this is S7's OWN roll step, run as part of
    build_s7_candles() - possibly more than once in a row for an entrant that needs two rolls, since
    s7_missing_mid_rolls() can only see the second one after the first is resolved."""
    wanted = [(day, atm_strike) for day, atm_strike, _next_target in s7_missing_mid_rolls()]
    return _resolve_rolls(wanted, token_provider, log, "S7 mid-hold")


def _expired_key_for(token_provider: Callable[[], str | None], key: str, expiry: str) -> str | None:
    """The expired-shaped twin of a live-shaped option key whose candle download just failed - Upstox has likely
    reclassified that contract into its separate expired-instruments system since it was first resolved (see
    _roll_contract()'s docstring). Looks it up from storage first (resolve_s5_rolls() or an earlier fallback may
    already have listed it); if not there yet, lists the expiry's expired contracts (one Upstox call, cached
    nowhere here since a whole build run rarely needs this twice) and looks again. None if `key` itself is not
    even a known contract, or the expired listing genuinely does not have it either."""
    from storage import contract_type_strike, find_option_contract

    info = contract_type_strike(key)
    if info is None:
        return None
    option_type, strike = info
    index_key = straddle.UNDERLYINGS["NIFTY"].index_key
    found = find_option_contract(index_key, expiry, strike, option_type, expired=True)
    if found:
        return found
    token = token_provider()
    if not token:
        return None
    straddle.expired_api.get_expired_contracts(token, index_key, expiry, "option", pause=straddle.REQUEST_PAUSE)
    return find_option_contract(index_key, expiry, strike, option_type, expired=True)


def build_s5_candles(token_provider: Callable[[], str | None], log: Callable[[str], None] = print, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Two phases, both resumable and both stopping on a dead token or too many failures in a row, exactly like the
    S1-S3 history build - never touches strategy_backtest_daily or computes a row itself, only adds what
    s5_overnight_rows() reads next time (its cache is cleared so the next call sees them):

    1. resolve_s5_rolls() - for an S1 expiry-day entrant (no contract survives to the next trading day), lists the
       next weekly expiry's option chain so its ATM strike can be found; one Upstox call per distinct next expiry.
    2. downloads whatever 09:30 candles s5_missing_candles() still finds missing - the ordinary next-day exit candle
       for S1's own contracts, and both the entry and exit candle for a now-resolved roll contract. Upstox's
       historical-candle endpoint always returns a whole session per request (there is no "just one minute" call),
       so each download is one contract's whole day, same as every other builder here - it also leaves that day's
       candles available for the day chart and everything else, not only S5's own 09:30 bar.

    If phase 1 stops early (dead token, too many failures), phase 2 still runs on whatever candles it can - a
    contract that was never resolved just stays a "not resolved yet" skip, not a candle gap."""
    import os
    import time
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))).date()
    roll_report = resolve_s5_rolls(token_provider, log=log)["run"]
    wanted = s5_missing_candles()
    # "stopped_because"/"errors"/"error_samples" describe the CANDLE phase specifically (matching every earlier
    # caller of this function); the resolve phase's own outcome is under "rolls" and never overwrites these.
    stats = {"total": len(wanted), "attempted": 0, "downloaded": 0, "errors": 0, "done": 0, "current": None, "stopped_because": "completed", "error_samples": [], "rolls": roll_report}
    consecutive_errors = 0
    for key, day, expiry in wanted:
        stats["current"] = f"{key} {day}"
        if on_progress:
            on_progress(dict(stats))
        stats["attempted"] += 1
        try:
            # lapsed follows the KEY's own shape (the "|expiry" suffix), not expiry < today: a key already resolved
            # live-shaped (see _roll_contract()) still downloads fine from the regular endpoint well after its own
            # expiry, and only needs the expired one once Upstox has reclassified it that way in instrument_contracts.
            lapsed = key.count("|") == 2
            try:
                straddle.leg_bars(token_provider, key, day, lapsed=lapsed, fetch=True, today=today)
            except HTTPError as error:
                # a live-shaped key can stop working once Upstox reclassifies the contract as expired (see
                # _roll_contract()'s docstring) - try its expired-shaped twin once before giving up on this gap.
                fallback_key = None if lapsed or error.code == 401 else _expired_key_for(token_provider, key, expiry)
                if fallback_key is None:
                    raise
                log(f"S5 candle build: {key} {day}: live key rejected (HTTP {error.code}) - retrying as {fallback_key}")
                straddle.leg_bars(token_provider, fallback_key, day, lapsed=True, fetch=True, today=today)
            stats["downloaded"] += 1
            consecutive_errors = 0
            time.sleep(straddle.REQUEST_PAUSE)
        except straddle.BuildAborted as error:
            stats["stopped_because"] = str(error)
            log(f"S5 candle build stopped: {error}")
            break
        except HTTPError as error:
            if error.code == 401:  # the token itself is dead: every further request would fail the same way
                stats["stopped_because"] = "Upstox rejected the access token (HTTP 401) - it has probably expired; log in again"
                log(f"S5 candle build stopped: {stats['stopped_because']}")
                break
            stats["errors"] += 1
            stats["error_samples"] = [*stats["error_samples"], f"{key} {day}: HTTP {error.code}"][-10:]
            consecutive_errors += 1
            log(f"S5 candle build: {key} {day}: ERROR - HTTP {error.code}")
            if consecutive_errors >= straddle.MAX_CONSECUTIVE_ERRORS:
                stats["stopped_because"] = f"{consecutive_errors} failures in a row"
                break
        except Exception as error:  # noqa: BLE001 - one bad (contract, day) must not sink the run
            stats["errors"] += 1
            stats["error_samples"] = [*stats["error_samples"], f"{key} {day}: {type(error).__name__}: {error}"][-10:]
            consecutive_errors += 1
            log(f"S5 candle build: {key} {day}: ERROR - {type(error).__name__}: {error}")
            if consecutive_errors >= straddle.MAX_CONSECUTIVE_ERRORS:
                stats["stopped_because"] = f"{consecutive_errors} failures in a row"
                break
        stats["done"] += 1
        if on_progress:
            on_progress(dict(stats))
    _s5_cache.clear()  # whatever candles landed above must be visible on the next read
    return {"run": stats}


_S5_CANDLES_JOB = straddle._JobState("s5-candles")
straddle._JOBS.append(_S5_CANDLES_JOB)  # one build at a time across every kind (history, gamma, S5 candles...)


def start_s5_candles_job(token_provider: Callable[[], str | None]) -> dict[str, Any]:
    def target(_params: dict[str, Any], log: Callable[[str], None], progress: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        return build_s5_candles(token_provider, log=log, on_progress=progress)

    return _S5_CANDLES_JOB.start({}, target)


def s5_candles_job_status() -> dict[str, Any]:
    status = _S5_CANDLES_JOB.snapshot()
    if _S5_CANDLES_JOB.running():
        status["missing"], status["missing_rolls"] = None, None  # a live count while idle; skip the extra work mid-run
    else:
        status["missing"], status["missing_rolls"] = len(s5_missing_candles()), len(s5_missing_rolls())
    return status


def build_s6_candles(token_provider: Callable[[], str | None], log: Callable[[str], None] = print, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """S6's own candle build - a superset of build_s5_candles()'s job, not a duplicate of it: S6's leg1 shares S5's
    own roll for an entrant on its own expiry day (resolve_s5_rolls()), and its exit-day and midpoint-day candles
    are the SAME 09:30 bars S5 either already needed (the entry day) or would need anyway (S1's own next-day close,
    reused here as the midpoint close) - what this adds on top is the day-after-next exit candle S5 never looks
    that far ahead for, plus - only for an entrant whose held contract expires on the middle day - a SECOND roll
    (resolve_s6_mid_rolls()) and both of its own candles (the middle day's open, the exit day's close).

    Three phases, all resumable and all stopping on a dead token or too many failures in a row, same as every
    other builder here - never touches strategy_backtest_daily or computes a row itself, only adds what
    s6_overnight_rows() reads next time (its cache, and S5's, are cleared so the next call sees them):
      1. resolve_s5_rolls() - shared with S5, for an entrant on its own expiry day.
      2. resolve_s6_mid_rolls() - S6's own second roll, for an entrant whose held contract expires mid-hold.
      3. downloads whatever 09:30 candles s6_missing_candles() still finds missing, via the same key-shape-driven
         `lapsed` flag and expired-twin fallback (_expired_key_for()) build_s5_candles() uses - a live-shaped roll
         contract that Upstox has since reclassified fails the same way regardless of which build resolved it.

    If either roll phase stops early (dead token, too many failures), the candle phase still runs on whatever it
    can - a contract that was never resolved just stays a "not resolved yet" skip, not a candle gap."""
    import os
    import time
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))).date()
    roll_report = resolve_s5_rolls(token_provider, log=log)["run"]
    mid_roll_report = resolve_s6_mid_rolls(token_provider, log=log)["run"]
    wanted = s6_missing_candles()
    # "stopped_because"/"errors"/"error_samples" describe the CANDLE phase specifically (matching every earlier
    # caller of this function); each roll phase's own outcome is under "rolls"/"mid_rolls" and never overwrites these.
    stats = {"total": len(wanted), "attempted": 0, "downloaded": 0, "errors": 0, "done": 0, "current": None, "stopped_because": "completed",
              "error_samples": [], "rolls": roll_report, "mid_rolls": mid_roll_report}
    consecutive_errors = 0
    for key, day, expiry in wanted:
        stats["current"] = f"{key} {day}"
        if on_progress:
            on_progress(dict(stats))
        stats["attempted"] += 1
        try:
            lapsed = key.count("|") == 2
            try:
                straddle.leg_bars(token_provider, key, day, lapsed=lapsed, fetch=True, today=today)
            except HTTPError as error:
                fallback_key = None if lapsed or error.code == 401 else _expired_key_for(token_provider, key, expiry)
                if fallback_key is None:
                    raise
                log(f"S6 candle build: {key} {day}: live key rejected (HTTP {error.code}) - retrying as {fallback_key}")
                straddle.leg_bars(token_provider, fallback_key, day, lapsed=True, fetch=True, today=today)
            stats["downloaded"] += 1
            consecutive_errors = 0
            time.sleep(straddle.REQUEST_PAUSE)
        except straddle.BuildAborted as error:
            stats["stopped_because"] = str(error)
            log(f"S6 candle build stopped: {error}")
            break
        except HTTPError as error:
            if error.code == 401:  # the token itself is dead: every further request would fail the same way
                stats["stopped_because"] = "Upstox rejected the access token (HTTP 401) - it has probably expired; log in again"
                log(f"S6 candle build stopped: {stats['stopped_because']}")
                break
            stats["errors"] += 1
            stats["error_samples"] = [*stats["error_samples"], f"{key} {day}: HTTP {error.code}"][-10:]
            consecutive_errors += 1
            log(f"S6 candle build: {key} {day}: ERROR - HTTP {error.code}")
            if consecutive_errors >= straddle.MAX_CONSECUTIVE_ERRORS:
                stats["stopped_because"] = f"{consecutive_errors} failures in a row"
                break
        except Exception as error:  # noqa: BLE001 - one bad (contract, day) must not sink the run
            stats["errors"] += 1
            stats["error_samples"] = [*stats["error_samples"], f"{key} {day}: {type(error).__name__}: {error}"][-10:]
            consecutive_errors += 1
            log(f"S6 candle build: {key} {day}: ERROR - {type(error).__name__}: {error}")
            if consecutive_errors >= straddle.MAX_CONSECUTIVE_ERRORS:
                stats["stopped_because"] = f"{consecutive_errors} failures in a row"
                break
        stats["done"] += 1
        if on_progress:
            on_progress(dict(stats))
    _s5_cache.clear()  # S5's own next-day close may have just landed too (shared with S6's midpoint close)
    _s6_cache.clear()  # whatever candles landed above must be visible on the next read
    return {"run": stats}


_S6_CANDLES_JOB = straddle._JobState("s6-candles")
straddle._JOBS.append(_S6_CANDLES_JOB)  # one build at a time across every kind (history, gamma, S5/S6 candles...)


def start_s6_candles_job(token_provider: Callable[[], str | None]) -> dict[str, Any]:
    def target(_params: dict[str, Any], log: Callable[[str], None], progress: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        return build_s6_candles(token_provider, log=log, on_progress=progress)

    return _S6_CANDLES_JOB.start({}, target)


def s6_candles_job_status() -> dict[str, Any]:
    status = _S6_CANDLES_JOB.snapshot()
    if _S6_CANDLES_JOB.running():
        status["missing"], status["missing_mid_rolls"] = None, None  # a live count while idle; skip the extra work mid-run
    else:
        status["missing"], status["missing_mid_rolls"] = len(s6_missing_candles()), len(s6_missing_mid_rolls())
    return status


def build_s7_candles(token_provider: Callable[[], str | None], log: Callable[[str], None] = print, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """S7's own candle build - the same shape as build_s6_candles(), generalized for S7's TWO candidate mid-hold
    roll days instead of one (see _s7_segments()): an entrant's held contract can expire on either middle day, or
    (rarely - see s7_overnight_rows()'s own docstring) on both in a row.

    Phases, all resumable and all stopping on a dead token or too many failures in a row, same as every other
    builder here - never touches strategy_backtest_daily or computes a row itself, only adds what
    s7_overnight_rows() reads next time (its cache, and S5's/S6's - candles this shares with them - are cleared
    so the next call sees them):
      1. resolve_s5_rolls() - shared with S5/S6, for an entrant on its own expiry day.
      2. resolve_s7_mid_rolls() - S7's own roll(s), run in a short bounded loop (at most 2 rounds, since there are
         only 2 middle days - s7_missing_mid_rolls() can only see a SECOND needed roll once the first one is
         resolved, so one round is not always enough).
      3. downloads whatever 09:30 candles s7_missing_candles() still finds missing, via the same key-shape-driven
         `lapsed` flag and expired-twin fallback (_expired_key_for()) build_s5_candles()/build_s6_candles() use.

    If either roll phase stops early (dead token, too many failures), the candle phase still runs on whatever it
    can - a contract that was never resolved just stays a "not resolved yet" skip, not a candle gap."""
    import os
    import time
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))).date()
    roll_report = resolve_s5_rolls(token_provider, log=log)["run"]
    mid_roll_reports = []
    for _round in range(2):  # at most 2 middle days, so at most 2 rounds can ever turn up something new to resolve
        report = resolve_s7_mid_rolls(token_provider, log=log)["run"]
        mid_roll_reports.append(report)
        if report["resolved"] == 0:
            break
    wanted = s7_missing_candles()
    # "stopped_because"/"errors"/"error_samples" describe the CANDLE phase specifically (matching every earlier
    # caller of this function); each roll phase's own outcome is under "rolls"/"mid_rolls" and never overwrites these.
    stats = {"total": len(wanted), "attempted": 0, "downloaded": 0, "errors": 0, "done": 0, "current": None, "stopped_because": "completed",
              "error_samples": [], "rolls": roll_report, "mid_rolls": mid_roll_reports}
    consecutive_errors = 0
    for key, day, expiry in wanted:
        stats["current"] = f"{key} {day}"
        if on_progress:
            on_progress(dict(stats))
        stats["attempted"] += 1
        try:
            lapsed = key.count("|") == 2
            try:
                straddle.leg_bars(token_provider, key, day, lapsed=lapsed, fetch=True, today=today)
            except HTTPError as error:
                fallback_key = None if lapsed or error.code == 401 else _expired_key_for(token_provider, key, expiry)
                if fallback_key is None:
                    raise
                log(f"S7 candle build: {key} {day}: live key rejected (HTTP {error.code}) - retrying as {fallback_key}")
                straddle.leg_bars(token_provider, fallback_key, day, lapsed=True, fetch=True, today=today)
            stats["downloaded"] += 1
            consecutive_errors = 0
            time.sleep(straddle.REQUEST_PAUSE)
        except straddle.BuildAborted as error:
            stats["stopped_because"] = str(error)
            log(f"S7 candle build stopped: {error}")
            break
        except HTTPError as error:
            if error.code == 401:  # the token itself is dead: every further request would fail the same way
                stats["stopped_because"] = "Upstox rejected the access token (HTTP 401) - it has probably expired; log in again"
                log(f"S7 candle build stopped: {stats['stopped_because']}")
                break
            stats["errors"] += 1
            stats["error_samples"] = [*stats["error_samples"], f"{key} {day}: HTTP {error.code}"][-10:]
            consecutive_errors += 1
            log(f"S7 candle build: {key} {day}: ERROR - HTTP {error.code}")
            if consecutive_errors >= straddle.MAX_CONSECUTIVE_ERRORS:
                stats["stopped_because"] = f"{consecutive_errors} failures in a row"
                break
        except Exception as error:  # noqa: BLE001 - one bad (contract, day) must not sink the run
            stats["errors"] += 1
            stats["error_samples"] = [*stats["error_samples"], f"{key} {day}: {type(error).__name__}: {error}"][-10:]
            consecutive_errors += 1
            log(f"S7 candle build: {key} {day}: ERROR - {type(error).__name__}: {error}")
            if consecutive_errors >= straddle.MAX_CONSECUTIVE_ERRORS:
                stats["stopped_because"] = f"{consecutive_errors} failures in a row"
                break
        stats["done"] += 1
        if on_progress:
            on_progress(dict(stats))
    _s5_cache.clear()  # S5's own next-day close may have just landed too (shared with S7's first-segment close)
    _s6_cache.clear()  # likewise for S6's own midpoint close/open (shared with S7's own two candidate roll days)
    _s7_cache.clear()  # whatever candles landed above must be visible on the next read
    return {"run": stats}


_S7_CANDLES_JOB = straddle._JobState("s7-candles")
straddle._JOBS.append(_S7_CANDLES_JOB)  # one build at a time across every kind (history, gamma, S5/S6/S7 candles...)


def start_s7_candles_job(token_provider: Callable[[], str | None]) -> dict[str, Any]:
    def target(_params: dict[str, Any], log: Callable[[str], None], progress: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        return build_s7_candles(token_provider, log=log, on_progress=progress)

    return _S7_CANDLES_JOB.start({}, target)


def s7_candles_job_status() -> dict[str, Any]:
    status = _S7_CANDLES_JOB.snapshot()
    if _S7_CANDLES_JOB.running():
        status["missing"], status["missing_mid_rolls"] = None, None  # a live count while idle; skip the extra work mid-run
    else:
        status["missing"], status["missing_mid_rolls"] = len(s7_missing_candles()), len(s7_missing_mid_rolls())
    return status


# --------------------------------------------------------------------------- lab views


def default_lots() -> int:
    import os

    return int(os.getenv("PAPER_LOTS", "1"))


def backtest_daily(
    strategy: str | None, from_date: str | None, to_date: str | None, size: "Size | int | None" = None, model: CostModel = DEFAULT_COST_MODEL,
    basis: str = MINUTE,
) -> list[dict[str, Any]]:
    """Daily backtest results at `size` (lots or a fixed qty; default PAPER_LOTS lots), each with its running total per strategy.
    `basis` picks the minute backtest (default) or the NSE end-of-day rows; they are never combined."""
    size = as_size(size)
    running: dict[str, float] = {}
    out = []
    rows = [] if strategy in DERIVED or strategy in (S5, S6, S7, S8, S10) else list(backtest_source_rows(strategy, from_date, to_date, basis))
    for name in [strategy] if strategy else (*DERIVED, S5, S6, S7, S8, S10):
        if name in DERIVED:
            rows += derived_backtest_rows(name, from_date, to_date, basis)[0]
        elif name == S5:
            rows += s5_overnight_rows(from_date, to_date, basis)[0]
        elif name == S6:
            rows += s6_overnight_rows(from_date, to_date, basis)[0]
        elif name == S7:
            rows += s7_overnight_rows(from_date, to_date, basis)[0]
        elif name == S8:
            rows += s8_overnight_rows(from_date, to_date, basis)[0]
        elif name == S10:
            rows += s10_overnight_rows(from_date, to_date, basis)[0]
    for row in rows:
        row = rescale(row, size, model)
        running[row["strategy"]] = round(running.get(row["strategy"], 0.0) + row["net_rs"], 2)
        out.append({**row, "source": "backtest", "basis": basis, "status": "backtest", "running_net_rs": running[row["strategy"]]})
    return out


def paper_at(trade: dict[str, Any], size: Size | None, model: CostModel = DEFAULT_COST_MODEL) -> dict[str, Any]:
    """A paper trade shown at `size`. Fills are per-unit prices, so a finished trade (and its candle-backtest twin) can be
    re-settled at any size; None leaves it as it was recorded. The paper engine itself always trades PAPER_LOTS.

    A trade recorded before any lot size was ever known - missed_entry (the health check or the entry itself
    failed before a contract was resolved) or, rarely, error - has nothing to re-settle: qty/lots are reported as
    0 rather than crashing on lot_size=None, since no position was ever actually entered."""
    if trade["lot_size"] is None:
        return {**trade, "qty": 0, "lots": 0, "whole_lots": True}
    units = size.units(trade["lot_size"]) if size else trade["lot_size"] * trade["lots"]
    out = {**trade, **sized(trade, units)}
    if size is None:
        return out
    legs = trade["legs"] or []
    if trade["status"] in ("closed", "missed_exit") and legs and all(leg.get("exit") for leg in legs):
        out.update(settle_units([{"side": l["side"], "entry": l["entry"]["fill"], "exit": l["exit"]["fill"]} for l in legs], units, model))
    if trade.get("bt_legs") and trade.get("bt_net_rs") is not None:
        bt = settle_units(trade["bt_legs"], units, model)
        out.update({"bt_gross_pts": bt["gross_pts"], "bt_charges_pts": bt["charges_pts"], "bt_net_pts": bt["net_pts"], "bt_net_rs": bt["net_rs"]})
    return out


def marks_at(marks: list[dict[str, Any]], trades: list[dict[str, Any]], size: Size | None) -> list[dict[str, Any]]:
    """Minute marks (gross rupees at the size the trade was recorded at) shown at `size`: rupees are linear in units.
    A trade with no lot_size yet (missed_entry/error, before any contract was resolved) never has marks either -
    excluded here so scaling never multiplies/divides by a None lot_size."""
    if size is None:
        return marks
    sized_trades = [t for t in trades if t["lot_size"] is not None]
    recorded = {t["strategy"]: t["lot_size"] * t["lots"] for t in sized_trades}
    shown = {t["strategy"]: size.units(t["lot_size"]) for t in sized_trades}
    return [{**m, "mtm_rs": None if m["mtm_rs"] is None else round(m["mtm_rs"] * shown[m["strategy"]] / recorded[m["strategy"]], 2)} for m in marks if m["strategy"] in recorded]


def paper_daily(strategy: str | None, from_date: str | None, to_date: str | None, size: Size | None = None) -> list[dict[str, Any]]:
    running: dict[str, float] = {}
    out = []
    for trade in paper_trades(strategy, from_date, to_date):
        trade = paper_at(trade, size)
        running[trade["strategy"]] = round(running.get(trade["strategy"], 0.0) + (trade["net_rs"] or 0.0), 2)
        out.append({**trade, "source": "paper", "running_net_rs": running[trade["strategy"]]})
    return out


def monthly(
    source: str, strategy: str | None, size: Size | None = None, basis: str = MINUTE,
) -> list[dict[str, Any]]:
    """Per strategy and calendar month: net Rs, days, winning days, worst day and the max drawdown inside the month."""
    if source not in ("backtest", "paper"):
        raise ValueError("source must be 'backtest' or 'paper'")
    rows = backtest_daily(strategy, None, None, size, basis=basis) if source == "backtest" else paper_daily(strategy, None, None, size)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["strategy"], row["trading_date"][:7]), []).append(row)
    out = []
    for (name, month), days in sorted(groups.items()):
        pnl = [(d["trading_date"], d["net_rs"] or 0.0) for d in days]
        cumulative = peak = drawdown = 0.0
        for _, value in pnl:
            cumulative += value
            peak = max(peak, cumulative)
            drawdown = max(drawdown, peak - cumulative)
        worst = min(pnl, key=lambda item: item[1])
        out.append({
            "source": source, "strategy": name, "month": month, "days": len(days), "net_rs": round(sum(v for _, v in pnl), 2),
            "winning_days": sum(1 for _, v in pnl if v > 0), "worst_day": worst[0], "worst_day_rs": round(worst[1], 2), "max_drawdown_rs": round(drawdown, 2),
        })
    return out


PNL_CSV_COLUMNS = [
    "source", "strategy", "trading_date", "status", "expiry", "atm_strike", "wing_width", "lot_size", "lots", "qty", "whole_lots",
    "entry_credit_pts", "exit_debit_pts", "gross_pts", "charges_pts", "net_pts", "gross_rs", "charges_rs", "net_rs", "running_net_rs",
    "legs", "estimated_fill", "exit_delay_s", "backtest_net_rs", "notes", "derived_from", "basis",
]


def _leg_price(value: Any) -> float | None:
    return value.get("fill") if isinstance(value, dict) else value


def _cash_pts(legs: list[dict[str, Any]] | None, phase: str) -> float | None:
    """Net cash in points per unit: at entry sells collect and buys pay; at exit the opposite trade happens. None if any price is missing."""
    if not legs:
        return None
    total = 0.0
    for leg in legs:
        price = _leg_price(leg.get(phase))
        if price is None:
            return None
        total += price if (leg["side"] == "SELL") == (phase == "entry") else -price
    return round(total, 4)


def pnl_csv(size: Size | None = None, source: str = "all", strategy: str | None = None, basis: str = MINUTE) -> str:
    """Every day's profit and loss as CSV: the candle backtest and the PAPER trades for all strategies (or the ones asked for),
    at `size` (default PAPER_LOTS lots). One row per strategy, source and day, with the running total within each strategy and source.

    Points are per unit; the rupee columns are points x qty, with charges from the shared cost model. A paper day that did not
    close (open, missed entry, error) keeps its status and has blank result columns."""
    if source not in ("all", "backtest", "paper"):
        raise ValueError("source must be all, backtest or paper")
    size = size or Size(lots=default_lots())
    rows = []
    if source in ("all", "backtest"):
        rows += backtest_daily(strategy, None, None, size, basis=basis)
    if source in ("all", "paper") and check_basis(basis) == MINUTE:  # paper trades are minute-based by nature: never listed next to NSE end-of-day rows
        rows += paper_daily(strategy, None, None, size)
    rows.sort(key=lambda r: (LAB_STRATEGIES.index(r["strategy"]), r["source"], r["trading_date"]))

    def money(points: float | None, qty: int) -> float | None:
        return None if points is None else round(points * qty, 2)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(PNL_CSV_COLUMNS)
    for r in rows:
        legs = r.get("legs") or []
        described = " | ".join(
            f"{leg['side']} {leg['type']} {leg['strike']:g} {_leg_price(leg['entry'])}>{_leg_price(leg.get('exit'))}" for leg in legs
        )
        exit_debit = _cash_pts(legs, "exit")
        estimated = any(str((leg.get(phase) or {}).get("source", "")).startswith("estimated") for leg in legs for phase in ("entry", "exit") if isinstance(leg.get(phase), dict))
        settled = r["net_rs"] is not None
        writer.writerow([
            r["source"], r["strategy"], r["trading_date"], r["status"], r["expiry"], r["atm_strike"], r["wing_width"], r["lot_size"], r["lots"], r["qty"], int(r["whole_lots"]),
            _cash_pts(legs, "entry"), None if exit_debit is None else -exit_debit,
            r["gross_pts"], r["charges_pts"], r["net_pts"],
            r.get("gross_rs") if r["source"] == "backtest" else money(r["gross_pts"], r["qty"]),
            r.get("charges_rs") if r["source"] == "backtest" else money(r["charges_pts"], r["qty"]),
            r["net_rs"], r["running_net_rs"] if settled else None,
            described, int(estimated) if r["source"] == "paper" else None, r.get("exit_delay_s") if r["source"] == "paper" else None,
            r.get("bt_net_rs") if r["source"] == "paper" else None, r.get("notes") or None, r.get("derived_from"), r.get("basis"),
        ])
    return buffer.getvalue()


# --------------------------------------------------------------------------- background job for the history build

_HISTORY_JOB = straddle._JobState("strategy-history")
straddle._JOBS.append(_HISTORY_JOB)  # one build at a time across every kind


def start_history_job(strategies: list[str] | None, lots: int, token_provider: Callable[[], str | None]) -> dict[str, Any]:
    params = {"strategies": strategies, "lots": lots}

    def target(p: dict[str, Any], log: Callable[[str], None], progress: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        return run_strategy_history(p["strategies"], None, None, token_provider=token_provider, log=log, on_progress=progress, lots=p["lots"])

    return _HISTORY_JOB.start(params, target)


def history_job_status() -> dict[str, Any]:
    status = _HISTORY_JOB.snapshot()
    status["backtest_days"] = {name: len(query_backtest_rows(name)) for name in STRATEGIES}
    return status
