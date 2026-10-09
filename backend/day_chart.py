"""Day chart data: everything needed to check what a strategy's strikes did on one day, from the stored 1-minute candles.

For a backtest or paper day of one strategy it returns, per leg (the sold ATM call and put, and the bought wings for the iron flies): the 1-minute candles from 09:15 to the exit minute (15:29 for the backtest; 15:39 for a paper trade, the last minute the options trade),
the entry and exit the trade recorded, and whether those match the candles; the whole position's price and profit through the day (marked at each bar's close, a minute with no
trade repeating the previous close); the NIFTY 1-minute spot for context; and a few facts (worst and best moment, the moves).  Reads SQLite and one CSV export only - no broker, no orders.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import strategy_lab as lab
from storage import get_connection, initialize_database, query_candle_bars

FIRST_BAR = "09:15"
ENTRY_BAR = "09:30"
LAST_BAR = "15:29"
INDEX_KEY = lab.UNDERLYING.index_key


def _minutes(start: str, end: str) -> list[str]:
    out = []
    minute = int(start[:2]) * 60 + int(start[3:])
    while minute <= int(end[:2]) * 60 + int(end[3:]):
        out.append(f"{minute // 60:02d}:{minute % 60:02d}")
        minute += 1
    return out


def contract_bars(key: str | None, day: str, last: str = LAST_BAR) -> tuple[list[dict[str, Any]], str]:
    """(bars, where they came from): downloaded candles first; for a day that has none yet (today's paper trade), minute bars built from the recorded live ticks."""
    if not key:
        return [], "none"
    bars = [
        {"t": r["received_at"][11:16], "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"], "v": r["volume"]}
        for r in query_candle_bars(key, day)
        if FIRST_BAR <= r["received_at"][11:16] <= last
    ]
    if bars:
        return bars, "candles"
    initialize_database()
    with get_connection() as connection:
        ticks = connection.execute(
            "SELECT substr(received_at, 12, 5), ltp FROM market_observations WHERE instrument_key = ? AND +trading_date = ?"
            " AND received_at >= ? AND received_at < ? AND ltp IS NOT NULL AND instr(received_at, '.') > 0 ORDER BY received_at",
            (key, day, f"{day}T{FIRST_BAR}", f"{day}T{last};"),  # seek to the window (";" sorts after every HH:MM:SS of `last`)
        ).fetchall()
    built: dict[str, dict[str, Any]] = {}
    for minute, price in ticks:
        if not FIRST_BAR <= minute <= last:
            continue
        bar = built.setdefault(minute, {"t": minute, "o": price, "h": price, "l": price, "c": price, "v": None})
        bar["h"], bar["l"], bar["c"] = max(bar["h"], price), min(bar["l"], price), price
    return [built[m] for m in sorted(built)], ("ticks" if built else "none")


_csv_days: dict[str, Any] = {"version": None, "spans": {}}


def _csv_day_lines(path: Path, day: str) -> list[str]:
    """`day`'s lines of the NIFTY CSV export, read straight from their byte range. The first call (and the first after
    the file changes) scans the file once to record where each day starts and ends; every later call reads only that
    day's few hundred lines instead of the whole multi-year file."""
    stat = path.stat()
    version = (str(path), stat.st_mtime_ns, stat.st_size)
    if _csv_days["version"] != version:
        spans: dict[str, tuple[int, int]] = {}
        offset = 0
        with path.open("rb") as handle:
            for raw in handle:
                key = raw[:10].decode("ascii", "ignore")
                start = spans[key][0] if key in spans else offset
                offset += len(raw)
                spans[key] = (start, offset)
        _csv_days.update(version=version, spans=spans)
    span = _csv_days["spans"].get(day)
    if span is None:
        return []
    with path.open("rb") as handle:
        handle.seek(span[0])
        chunk = handle.read(span[1] - span[0]).decode("utf-8")
    return [line for line in chunk.splitlines() if line.startswith(day)]  # an unsorted file can interleave other days


def spot_bars(day: str) -> tuple[list[dict[str, Any]], str]:
    """NIFTY 1-minute bars for context: the project's CSV export, else whatever index candles are stored (possibly 5-minute)."""
    path = Path(os.getenv("DATA_DIR", "data")) / "exports" / "nifty_1min.csv"
    out: list[dict[str, Any]] = []
    if path.is_file():
        for line in _csv_day_lines(path, day):
            stamp, o, h, l, c, *_ = line.split(",")
            if FIRST_BAR <= stamp[11:16] <= LAST_BAR:
                out.append({"t": stamp[11:16], "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    if out:
        return out, "nifty_1min.csv"
    stored = [
        {"t": r["received_at"][11:16], "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"]}
        for r in query_candle_bars(INDEX_KEY, day)
        if FIRST_BAR <= r["received_at"][11:16] <= LAST_BAR
    ]
    return stored, "stored index candles" if stored else "none"


def _price(value: Any) -> float | None:
    return value.get("fill") if isinstance(value, dict) else value


def _hhmm(fill: Any, default: str) -> str:
    return fill["ts"][11:16] if isinstance(fill, dict) and fill.get("ts") else default


def _leg_chart(leg: dict[str, Any], bars: list[dict[str, Any]], where: str, price: float | None, marks_entry: bool) -> dict[str, Any]:
    """One S5 leg's candles for ONE of its two days - the day it was entered or the day it was exited - with only
    that day's own price marked (LegChart on the frontend only draws a marker/line for whichever of entry/exit is
    not null, so leaving the other one out here, rather than pointing it at the wrong day's minute, is what keeps
    a single LegChart component usable unmodified for a two-day strategy). Carries over the side-specific flag
    from the source leg (s5_overnight_rows() sets entry_estimated/exit_estimated independently when a price came
    from a live tick, not a downloaded candle) so this exact verification view can show that too, not just the
    Calendar - picking whichever of the two matches the side THIS panel is showing, never the other one."""
    return {
        "role": leg["role"], "type": leg["type"], "strike": leg["strike"], "side": leg["side"], "instrument_key": leg.get("instrument_key"),
        "entry": price if marks_entry else None, "exit": None if marks_entry else price,
        "entry_time": ENTRY_BAR, "exit_time": ENTRY_BAR,  # unused on whichever side is None above; a valid HH:MM keeps the frontend type simple
        "bars": bars, "bars_source": where, "estimated": bool(leg.get("entry_estimated" if marks_entry else "exit_estimated")),
    }


S5_LAST_BAR = "09:45"  # a little past the 09:30 entry/exit minute - enough context to see the candle, not a whole session


def _overnight_day_chart(day: str, base: dict[str, Any], rows: list[dict[str, Any]], skipped: list[dict[str, str]]) -> dict[str, Any]:
    """Shared by S5, S8 and S10 - all three hold one position across TWO trading days (S8 is S5's own mechanics with fewer
    entry days, see s8_overnight_rows()), sold at this day's 09:30, bought back at the NEXT day's 09:30 - so there
    is no single 09:30-15:29 window to draw. What there is to verify is two separate moments: each leg's candles
    around 09:15-09:45 on the entry day (does the 09:30 open match what was recorded as sold?) and around
    09:15-09:45 on the exit day (does the 09:30 close match what was recorded as bought back?). Both are returned
    side by side so the frontend can draw two clearly-dated panels instead of forcing S1-S4's single-day shape
    onto a trade that was never open on just one day. `rows`/`skipped` come from the caller's own strategy-specific
    rows function (s5_overnight_rows()/s8_overnight_rows()), already reusing that function's own cache."""
    row = next((r for r in rows if r["trading_date"] == day), None)
    if row is None:
        reason = next((s["detail"] for s in skipped if s["trading_date"] == day), f"There is no {base['strategy']} row for {day}.")
        return {**base, "reason": reason}
    exit_date = row["exit_date"]

    entry_legs, exit_legs = [], []
    for leg in row["legs"] or []:
        key = leg.get("instrument_key")
        entry_bars, entry_where = contract_bars(key, day, S5_LAST_BAR)
        exit_bars, exit_where = contract_bars(key, exit_date, S5_LAST_BAR)
        entry_legs.append(_leg_chart(leg, entry_bars, entry_where, leg.get("entry"), marks_entry=True))
        exit_legs.append(_leg_chart(leg, exit_bars, exit_where, leg.get("exit"), marks_entry=False))

    entry_spot, entry_spot_source = spot_bars(day)
    exit_spot, exit_spot_source = spot_bars(exit_date)
    entry_complete = bool(entry_legs) and all(leg["bars"] and leg["entry"] is not None for leg in entry_legs)
    exit_complete = bool(exit_legs) and all(leg["bars"] and leg["exit"] is not None for leg in exit_legs)

    checks = None
    if entry_complete and exit_complete:
        def bar_at(bars: list[dict[str, Any]], minute: str) -> dict[str, Any] | None:
            return next((b for b in bars if b["t"] == minute), None)

        entry_opens = [bar_at(leg["bars"], ENTRY_BAR) for leg in entry_legs]
        exit_closes = [bar_at(leg["bars"], ENTRY_BAR) for leg in exit_legs]
        checks = {
            "entry_matches_0930_open": all(b is not None and abs(b["o"] - leg["entry"]) < 1e-6 for b, leg in zip(entry_opens, entry_legs)),
            "exit_matches_0930_close": all(b is not None and abs(b["c"] - leg["exit"]) < 1e-6 for b, leg in zip(exit_closes, exit_legs)),
        }

    return {
        **base, "available": True, "exit_date": exit_date, "expiry": row["expiry"], "atm_strike": row["atm_strike"], "wing_width": row["wing_width"],
        "rolled_to_next_expiry": bool(row.get("rolled_to_next_expiry")), "entry_bar": ENTRY_BAR, "exit_bar": ENTRY_BAR,
        "entry_legs": entry_legs, "exit_legs": exit_legs, "entry_complete": entry_complete, "exit_complete": exit_complete,
        "entry_spot": entry_spot, "entry_spot_source": entry_spot_source, "exit_spot": exit_spot, "exit_spot_source": exit_spot_source,
        "s5_checks": checks, "trade": {"gross_pts": row.get("gross_pts"), "net_pts": row.get("net_pts"), "net_rs_at_1_lot": row.get("net_rs")},
    }


def _shift(minute: str, delta: int) -> str:
    total = int(minute[:2]) * 60 + int(minute[3:]) + delta
    return f"{total // 60:02d}:{total % 60:02d}"


def _paper_overnight_day_chart(day: str, base: dict[str, Any]) -> dict[str, Any]:
    """S5's PAPER trade entered on `day`, to check its fills against the candles: the same two-panel shape as the
    backtest's (_overnight_day_chart()), but built from the paper trade's own legs at their REAL fill times - a
    manually recovered 10:04 entry, or a late exit, is drawn where it actually happened, not at 09:30. Each panel runs
    from 09:15 to 15 minutes past its fill (09:45 at the least). Paper fills are bid/ask prices, so they are shown
    next to the candle of their minute rather than required to equal it (no s5_checks). An open trade has only its
    entry panel."""
    from storage import query_paper_trades

    trade = next(iter(query_paper_trades(lab.S5, day, day)), None)
    if trade is None:
        return {**base, "reason": f"There is no S5 paper trade entered on {day}."}
    legs = [leg for leg in trade["legs"] or [] if leg.get("instrument_key")]
    if not legs:
        return {**base, "reason": f"The S5 paper trade entered on {day} has no legs (status {trade['status']})."}

    def fill(leg: dict[str, Any], side: str) -> dict[str, Any] | None:
        value = leg.get(side)
        return value if isinstance(value, dict) and value.get("ts") else None

    def estimated(value: dict[str, Any] | None) -> bool:
        return bool(value) and str(value.get("source", "")).startswith("estimated")

    entry_fills = [fill(leg, "entry") for leg in legs]
    exit_fills = [fill(leg, "exit") for leg in legs]
    entry_minute = min((f["ts"][11:16] for f in entry_fills if f), default=ENTRY_BAR)
    exit_ts = max((f["ts"] for f in exit_fills if f), default=None)
    exit_date = exit_ts[:10] if exit_ts else None
    exit_minute = exit_ts[11:16] if exit_ts else ENTRY_BAR
    entry_end = max(S5_LAST_BAR, _shift(entry_minute, 15))
    exit_end = max(S5_LAST_BAR, _shift(exit_minute, 15))

    entry_legs, exit_legs = [], []
    for leg, entry, out in zip(legs, entry_fills, exit_fills):
        bars, where = contract_bars(leg["instrument_key"], day, entry_end)
        entry_legs.append({**_leg_chart(leg, bars, where, entry["fill"] if entry else None, marks_entry=True),
                           "entry_time": entry["ts"][11:16] if entry else entry_minute, "estimated": estimated(entry)})
        if exit_date:
            bars, where = contract_bars(leg["instrument_key"], exit_date, exit_end)
            exit_legs.append({**_leg_chart(leg, bars, where, out["fill"] if out else None, marks_entry=False),
                              "exit_time": out["ts"][11:16] if out else exit_minute, "estimated": estimated(out)})

    entry_spot, entry_spot_source = spot_bars(day)
    exit_spot, exit_spot_source = spot_bars(exit_date) if exit_date else ([], "none")
    return {
        **base, "available": True, "status": trade["status"], "exit_date": exit_date, "expiry": trade.get("expiry"), "atm_strike": trade.get("atm_strike"),
        "wing_width": None, "rolled_to_next_expiry": False, "entry_bar": entry_minute, "exit_bar": exit_minute,
        "entry_window_end": entry_end, "exit_window_end": exit_end, "entry_ts": min((f["ts"] for f in entry_fills if f), default=None), "exit_ts": exit_ts,
        "entry_legs": entry_legs, "exit_legs": exit_legs,
        "entry_complete": all(leg["bars"] and leg["entry"] is not None for leg in entry_legs),
        "exit_complete": bool(exit_legs) and all(leg["bars"] and leg["exit"] is not None for leg in exit_legs),
        "entry_spot": [bar for bar in entry_spot if bar["t"] <= entry_end], "entry_spot_source": entry_spot_source,
        "exit_spot": [bar for bar in exit_spot if bar["t"] <= exit_end], "exit_spot_source": exit_spot_source,
        "s5_checks": None, "invalid_reason": trade.get("invalid_reason"),
        "trade": {"gross_pts": trade.get("gross_pts"), "net_pts": trade.get("net_pts"), "net_rs_at_1_lot": None},
    }


def day_chart(day: str, strategy: str, source: str = "backtest", basis: str = lab.MINUTE) -> dict[str, Any]:
    if strategy not in lab.LAB_STRATEGIES:
        raise ValueError(f"strategy must be one of {list(lab.LAB_STRATEGIES)}")
    if source not in ("backtest", "paper"):
        raise ValueError("source must be backtest or paper")
    lab.check_basis(basis)
    base = {"date": day, "strategy": strategy, "source": source, "basis": basis, "available": False}
    if basis != lab.MINUTE:
        return {**base, "reason": "The NSE end-of-day rows have only a daily open and close, so there are no minute candles to chart. Switch Data to Minute (Oct 2024 on)."}
    if strategy == lab.S5:
        if source == "paper":
            return _paper_overnight_day_chart(day, base)
        return _overnight_day_chart(day, base, *lab.s5_overnight_rows())
    if strategy == lab.S8:
        return _overnight_day_chart(day, base, *lab.s8_overnight_rows())
    if strategy == lab.S10:
        return _overnight_day_chart(day, base, *lab.s10_overnight_rows())
    if strategy == lab.S6:
        return {**base, "reason": "S6 is held for two overnights (09:30 to the day-after-next trading day's 09:30) - there is "
                                   "no single day's window to chart. See it in Calendar or Compare instead."}
    if strategy == lab.S7:
        return {**base, "reason": "S7 is held for three overnights (09:30 to the third trading day's 09:30) - there is "
                                   "no single day's window to chart. See it in Calendar or Compare instead."}
    if source == "backtest":
        rows = lab.backtest_daily(strategy, day, day, lab.Size(lots=1))
    else:
        rows = [lab.paper_at(t, None) for t in lab.paper_trades(strategy, day, day)]
    if not rows:
        return {**base, "reason": f"There is no {source} row for {strategy} on {day}."}
    row = rows[0]
    last_bar = lab.paper_exit_minute(row) if source == "paper" else LAST_BAR  # the backtest exits at 15:29; a paper trade at the minute it really exited
    legs = []
    for leg in row["legs"] or []:
        bars, where = contract_bars(leg.get("instrument_key"), day, last_bar)
        legs.append({
            "role": leg["role"], "type": leg["type"], "strike": leg["strike"], "side": leg["side"], "instrument_key": leg.get("instrument_key"),
            "entry": _price(leg.get("entry")), "exit": _price(leg.get("exit")),
            "entry_time": _hhmm(leg.get("entry"), ENTRY_BAR), "exit_time": _hhmm(leg.get("exit"), last_bar),
            "estimated_entry": bool(isinstance(leg.get("entry"), dict) and str(leg["entry"].get("source", "")).startswith("estimated")),
            "bars": bars, "bars_source": where,
        })
    spot, spot_where = spot_bars(day)
    complete = bool(legs) and all(leg["bars"] and leg["entry"] is not None for leg in legs)

    # the whole position, marked at each bar's close (a minute with no trade repeats the previous close, as in the backtest)
    grid = [m for m in _minutes(ENTRY_BAR, last_bar)]
    position, pnl = [], []
    closes = []
    for leg in legs:
        by = {b["t"]: b["c"] for b in leg["bars"]}
        last, series = leg["entry"], []
        for m in grid:
            last = by.get(m, last)
            series.append(last)
        closes.append(series)
    if complete:
        for i, m in enumerate(grid):
            price = sum((c[i] if leg["side"] == "SELL" else -c[i]) for leg, c in zip(legs, closes))
            entry = sum((leg["entry"] if leg["side"] == "SELL" else -leg["entry"]) for leg in legs)
            position.append({"t": m, "price": round(price, 2)})
            pnl.append({"t": m, "pts": round(entry - price, 2)})
    checks = None
    if source == "backtest" and complete:
        def bar_at(leg: dict[str, Any], minute: str) -> dict[str, Any] | None:
            return next((b for b in leg["bars"] if b["t"] == minute), None)

        opens = [bar_at(leg, ENTRY_BAR) for leg in legs]
        closes_last = [bar_at(leg, LAST_BAR) for leg in legs]
        checks = {
            "entry_matches_0930_open": all(b is not None and abs(b["o"] - leg["entry"]) < 1e-6 for b, leg in zip(opens, legs)),
            "exit_matches_1529_close": all(b is not None and leg["exit"] is not None and abs(b["c"] - leg["exit"]) < 1e-6 for b, leg in zip(closes_last, legs)),
        }
    stats: dict[str, Any] = {}
    if pnl:
        worst, best = min(pnl, key=lambda p: p["pts"]), max(pnl, key=lambda p: p["pts"])
        stats.update({"worst": worst, "best": best, "final_pts": pnl[-1]["pts"]})
    if spot:
        at_entry = next((b for b in spot if b["t"] == ENTRY_BAR), spot[0])
        stats["spot"] = {"open_0930": at_entry["o"], "close": spot[-1]["c"], "high": max(b["h"] for b in spot if b["t"] >= ENTRY_BAR), "low": min(b["l"] for b in spot if b["t"] >= ENTRY_BAR)}
    return {
        **base, "available": True, "status": row.get("status"), "expiry": row.get("expiry"), "atm_strike": row.get("atm_strike"), "wing_width": row.get("wing_width"),
        "derived_from": row.get("derived_from"), "entry_bar": ENTRY_BAR, "exit_bar": last_bar, "legs": legs, "complete": complete, "position": position, "pnl": pnl,
        "spot": spot, "spot_source": spot_where, "checks": checks, "stats": stats, "trade": {"gross_pts": row.get("gross_pts"), "net_pts": row.get("net_pts"), "net_rs_at_1_lot": row.get("net_rs")},
    }
