"""Download NIFTY 1-minute spot (2022-01-03 .. 2023-09-05, the day before the project's own CSV starts) and India VIX 1-minute (2022-01-03 .. yesterday).

Market-data reads only (Upstox v3 historical candles). Results go to D:\\zero\\research\\model\\cache as CSV; the project's database is not touched.
One calendar month per request (Upstox's limit for 1-minute candles).
"""
from __future__ import annotations

import calendar
import csv
import os
import pathlib
import sys
import time
from datetime import date, timedelta
from urllib.parse import quote

BACKEND = pathlib.Path(r"D:\zero\backend")
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")
import historical  # noqa: E402
from http_util import request_json  # noqa: E402

TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN")
CACHE = pathlib.Path(__file__).resolve().parent / "cache"
CACHE.mkdir(exist_ok=True)


def month_ranges(start: date, end: date):
    d = start.replace(day=1)
    while d <= end:
        last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        yield max(d, start), min(last, end)
        d = last + timedelta(days=1)


def fetch(key: str, first: date, last: date, out: pathlib.Path) -> None:
    rows = []
    for a, b in month_ranges(first, last):
        url = f"{historical.HISTORICAL_CANDLE_URL}/{quote(key, safe='')}/minutes/1/{b.isoformat()}/{a.isoformat()}"
        got = request_json(url, {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}", "User-Agent": "zero-research/1.0"}, timeout=40, pause=0.4).get("data", {}).get("candles", [])
        rows += got
        print(f"{key} {a}..{b}: {len(got)}", flush=True)
    rows.sort(key=lambda r: r[0])
    with out.open("w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["datetime", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow([r[0][:19].replace("T", " "), r[1], r[2], r[3], r[4], r[5]])


if __name__ == "__main__":
    fetch("NSE_INDEX|Nifty 50", date(2022, 1, 3), date(2023, 9, 5), CACHE / "nifty_1min_2022_to_2023-09-05.csv")
    fetch("NSE_INDEX|India VIX", date(2022, 1, 3), date.today() - timedelta(days=1), CACHE / "india_vix_1min.csv")
    print("done")
