"""NSE end-of-day option prices -> the daily-bar version of S1, S2 and S3 (Jan 2022 on), stored in strategy_nse_daily.

Each leg is entered at the day's OPEN (NSE's first trade, about 09:15) and exited at NSE's CLOSE, using the project's own rules (ATM = the strike nearest the 09:15 spot,
lab.plan_legs for the iron-fly wings from the ATM straddle's open premium). This is NOT the 09:30 -> 15:29 minute backtest: where both exist it earns roughly 2-3x as much.
Charges are the lab's cost model for every year (no spread; rates before Oct 2024 were slightly different). Lot sizes: 50 until 25 Apr 2024, 25 from 26 Apr 2024 (the day NSE's
volume-to-value ratio halves in its own files), then the lot size the minute backtest already records. Reads files and writes SQLite only - no broker, no orders.

Usage:  python nse_daily.py [options_csv] [spot_2022_csv]
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any, Callable

import strategy_lab as lab
from storage import query_backtest_rows, query_straddle_rows, upsert_nse_daily_row

BUILD_VERSION = "nse-daily-v1"
DEFAULT_OPTIONS_CSV = Path(r"D:\zero\research\model\cache\nse_nifty_options_daily.csv")
DEFAULT_SPOT_2022_CSV = Path(r"D:\zero\research\model\cache\nifty_1min_2022_to_2023-09-05.csv")
LOT_50_UNTIL = "2024-04-25"
LOT_25_UNTIL = "2024-10-02"  # from 2024-10-03 the minute backtest records the lot size itself


def lot_size_schedule(day: str, recorded: dict[str, int]) -> int:
    """50 through 25 Apr 2024, 25 to 2 Oct 2024, then the lot size the minute backtest stored for that day (or the latest before it)."""
    if day <= LOT_50_UNTIL:
        return 50
    if day <= LOT_25_UNTIL:
        return 25
    known = [d for d in recorded if d <= day]
    return recorded[max(known)] if known else 25


def build_rows(option_rows: list[dict[str, Any]], spot_open: dict[str, float], lot_size: Callable[[str], int]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Strategy rows (at 1 lot) and the days left out, each with its reason."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in option_rows:
        by_day.setdefault(row["date"], []).append(row)
    out, skipped = [], []
    for day in sorted(by_day):
        if day not in spot_open:
            skipped.append({"trading_date": day, "strategy": "all", "reason": "no 09:15 spot"})
            continue
        rows = by_day[day]
        expiries = sorted({r["expiry"] for r in rows if r["contracts"] > 0 and r["expiry"] >= day})
        if not expiries:
            skipped.append({"trading_date": day, "strategy": "all", "reason": "no expiry with trades"})
            continue
        expiry = expiries[0]
        price = {(r["strike"], r["type"]): (r["open"], r["close"]) for r in rows if r["expiry"] == expiry}
        atm = float(lab.resolve_atm_strike(spot_open[day], lab.STRIKE_STEP))

        def usable(key: tuple[float, str]) -> bool:
            value = price.get(key)
            return value is not None and value[0] > 0 and value[1] > 0

        if not (usable((atm, "CE")) and usable((atm, "PE"))):
            skipped.append({"trading_date": day, "strategy": "all", "reason": f"ATM {atm:g} has no traded open/close"})
            continue
        premium = price[(atm, "CE")][0] + price[(atm, "PE")][0]
        for strategy in lab.STRATEGIES:
            plan = lab.plan_legs(strategy, atm, premium)
            keys = [(spec["strike"], spec["type"]) for spec in plan]
            missing = [k for k in keys if not usable(k)]
            if missing:
                skipped.append({"trading_date": day, "strategy": strategy, "reason": f"no traded open/close for {missing}"})
                continue
            legs = [{**spec, "entry": price[k][0], "exit": price[k][1], "instrument_key": None} for spec, k in zip(plan, keys)]
            row = lab.backtest_row(strategy, day, expiry, atm, premium, legs, lot_size(day), 1)
            row["build_version"] = BUILD_VERSION
            out.append(row)
    return out, skipped


def load_option_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(newline="", encoding="utf-8") as handle:
        for r in csv.DictReader(handle):
            try:
                rows.append({"date": r["date"], "expiry": r["expiry"], "strike": float(r["strike"]), "type": r["type"], "open": float(r["open"] or 0), "close": float(r["close"] or 0),
                             "contracts": float(r["contracts"] or 0)})
            except ValueError:
                continue
    return rows


def load_spot_open(paths: list[Path]) -> dict[str, float]:
    """The 09:15 open of NIFTY per day from 1-minute CSV exports; days the exports lack fall back to the minute backtest's own recorded 09:15 spot."""
    spot: dict[str, float] = {}
    for path in paths:
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            for stamp, open_, *_ in reader:
                if stamp[11:16] == "09:15":
                    spot.setdefault(stamp[:10], float(open_))
    for row in query_straddle_rows(underlying="NIFTY"):
        if row.get("spot_0915"):
            spot.setdefault(row["trading_date"], float(row["spot_0915"]))
    return spot


def main(argv: list[str]) -> None:
    options_csv = Path(argv[1]) if len(argv) > 1 else DEFAULT_OPTIONS_CSV
    spot_2022 = Path(argv[2]) if len(argv) > 2 else DEFAULT_SPOT_2022_CSV
    project_csv = Path("data") / "exports" / "nifty_1min.csv"
    spot = load_spot_open([spot_2022, project_csv])
    recorded = {r["trading_date"]: r["lot_size"] for r in query_backtest_rows("straddle_sell") if r["lot_size"]}
    rows, skipped = build_rows(load_option_rows(options_csv), spot, lambda day: lot_size_schedule(day, recorded))
    for row in rows:
        upsert_nse_daily_row(row)
    counts = {s: sum(1 for r in rows if r["strategy"] == s) for s in lab.STRATEGIES}
    print("rows written per strategy:", counts, "| first/last day:", min(r["trading_date"] for r in rows), max(r["trading_date"] for r in rows))
    print("days left out:", len(skipped))
    for item in skipped:
        print("  ", item)


if __name__ == "__main__":
    main(sys.argv)
