"""Measure real bid/ask spreads from the live stream files the project recorded (2026-09-15, -16 and part of -17).

The candle backtest has no spread. This gives a data-based (not invented) size for the cost of crossing the book, by distance from the money and time of day.
Only NSE_FO options of the nearest expiry are used; the spread is best-ask minus best-bid of the 5-level depth in the full feed.
"""
from __future__ import annotations

import json
from collections import defaultdict

from common import *  # noqa: F403

conn = connect()
contracts = {}
for r in conn.execute("SELECT instrument_key, expiry, instrument_type, strike_price FROM instrument_contracts WHERE underlying_key = ? AND instrument_key NOT LIKE '%|%|%'", (INDEX_KEY,)):
    contracts[r["instrument_key"]] = (r["expiry"], r["instrument_type"], float(r["strike_price"]))

files = sorted((BACKEND / "data").glob("stream-2026-09-1*.jsonl"))
buckets = defaultdict(list)  # (distance bucket, phase) -> list of (ask - bid)
per_day_seen = defaultdict(int)


def phase(t: str) -> str:
    if t < "09:35":
        return "entry 09:30-09:35"
    if t >= "15:20":
        return "exit 15:20-15:30"
    return "mid-session"


def bucket(distance: float) -> str:
    if distance <= 50:
        return "ATM (<=50)"
    if distance <= 150:
        return "100-150 away"
    if distance <= 250:
        return "200-250 away"
    if distance <= 350:
        return "300-350 away"
    if distance <= 500:
        return "400-500 away"
    return ">500 away"


expiries = sorted({m[0] for m in contracts.values()})
for path in files:
    day = path.stem[-10:]
    nearest = next(e for e in expiries if e >= day)
    spot = None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            feeds = (record.get("message") or {}).get("feeds") or {}
            t = record["received_at"][11:16]
            if not ("09:15" <= t <= "15:30"):
                continue
            idx = feeds.get("NSE_INDEX|Nifty 50")
            if idx:
                spot = idx["fullFeed"]["indexFF"]["ltpc"]["ltp"]
            if spot is None:
                continue
            for key, feed in feeds.items():
                meta = contracts.get(key)
                if not meta:
                    continue
                market = (feed.get("fullFeed") or {}).get("marketFF")
                if not market:
                    continue
                quotes = (market.get("marketLevel") or {}).get("bidAskQuote") or []
                if not quotes:
                    continue
                bid, ask = quotes[0].get("bidP"), quotes[0].get("askP")
                if not bid or not ask or ask < bid:
                    continue
                expiry, leg, strike = meta
                if expiry != nearest:
                    continue
                distance = abs(strike - spot)
                if distance > 600:
                    continue
                otm = (leg == "CE" and strike >= spot) or (leg == "PE" and strike <= spot)
                if distance > 50 and not otm:
                    continue
                buckets[(bucket(distance), phase(t))].append(ask - bid)
                per_day_seen[day] += 1

rows = []
for (b, ph), values in sorted(buckets.items()):
    rows.append({"distance": b, "phase": ph, "samples": len(values), "mean_spread": round(mean(values), 3), "median_spread": round(median(values), 3), "p90_spread": round(quantile(values, 0.9), 3)})
save_json("measured_spreads.json", {"files": [p.name for p in files], "samples_per_day": dict(per_day_seen), "note": "top-of-book ask-bid in index points, nearest-listed expiries; two full sessions (09-15, 09-16) plus part of 09-17", "rows": rows})
for r in rows:
    print(r)
