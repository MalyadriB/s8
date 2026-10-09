import base64
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("uvicorn.error")

HOLIDAYS_URL = "https://api.upstox.com/v2/market/holidays"  # public: no login needed
HOLIDAY_RETRY_SECONDS = 600
_holidays_lock = threading.Lock()
_holidays: dict[str, Any] = {"fetched_on": None, "days": None, "retry_at": 0.0}


def _holidays_file() -> Path:
    return Path(os.getenv("DATA_DIR", "data")) / "market_holidays.json"


def _fetch_fo_holidays() -> set[date]:
    """This year's NSE F&O trading holidays from Upstox's published list."""
    import http_util

    rows = http_util.request_json(HOLIDAYS_URL, {"Accept": "application/json"}, timeout=10, max_attempts=2).get("data", [])
    return {date.fromisoformat(str(r["date"])[:10]) for r in rows if r.get("holiday_type") == "TRADING_HOLIDAY" and "NFO" in (r.get("closed_exchanges") or [])}


def _published_holidays() -> set[date]:
    """Upstox's holiday list, fetched at most once a day and saved to disk, so a restart with no network still knows
    it. A failed fetch keeps whatever was known before and is retried after HOLIDAY_RETRY_SECONDS - this is read on
    every engine tick, so it must never turn into a request per call."""
    today = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))).date()
    with _holidays_lock:
        if _holidays["days"] is None and _holidays_file().is_file():
            try:
                _holidays["days"] = {date.fromisoformat(d) for d in json.loads(_holidays_file().read_text(encoding="utf-8"))}
            except (ValueError, OSError):
                _holidays["days"] = set()
        if os.getenv("MARKET_HOLIDAYS_FETCH", "true").lower() == "true" and _holidays["fetched_on"] != today and time.monotonic() >= _holidays["retry_at"]:
            try:
                fetched = _fetch_fo_holidays()
                merged = {d for d in (_holidays["days"] or set()) if d.year != today.year} | fetched  # keep other years' dates
                _holidays.update(days=merged, fetched_on=today)
                _holidays_file().parent.mkdir(parents=True, exist_ok=True)
                _holidays_file().write_text(json.dumps(sorted(d.isoformat() for d in merged)), encoding="utf-8")
            except Exception as error:  # noqa: BLE001 - keep the last known list; never break the caller
                _holidays["retry_at"] = time.monotonic() + HOLIDAY_RETRY_SECONDS
                logger.warning("Could not fetch the market holiday list (%s); using the last known one", error)
        return set(_holidays["days"] or set())


def market_holidays() -> set[date]:
    """NSE F&O trading holidays: Upstox's published list plus any dates in MARKET_HOLIDAYS. The env list alone was
    never filled in, so 2 Oct 2026 (Gandhi Jayanti) counted as a trading day and the paper engine closed an overnight
    position at the frozen quotes of a shut market."""
    holidays = _published_holidays()
    for value in os.getenv("MARKET_HOLIDAYS", "").split(","):
        if value.strip():
            holidays.add(date.fromisoformat(value.strip()))
    return holidays


def is_market_day(day: date) -> bool:
    return day.weekday() < 5 and day not in market_holidays()


def next_market_start(now: datetime, hour: int, minute: int) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while not is_market_day(candidate.date()):
        candidate += timedelta(days=1)
    return candidate


def token_status(token: str | None) -> dict[str, Any]:
    if not token:
        return {"configured": False, "expires_at": None, "expired": None}
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {"configured": True, "expires_at": None, "expired": None, "format": "opaque"}
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        expires_at = payload.get("exp")
        expired = bool(expires_at and datetime.now().timestamp() >= expires_at)
        return {"configured": True, "expires_at": expires_at, "expired": expired, "format": "jwt"}
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return {"configured": True, "expires_at": None, "expired": None, "format": "unknown"}