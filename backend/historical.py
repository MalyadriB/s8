from datetime import datetime
from urllib.parse import quote
from typing import Any

from http_util import request_json
from storage import save_historical_candles


HISTORICAL_CANDLE_URL = "https://api.upstox.com/v3/historical-candle"


def _download(url: str, access_token: str, instrument_key: str, pause: float = 0.0) -> int:
    payload: dict[str, Any] = request_json(
        url,
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "zero-historical-download/1.0",
        },
        timeout=30,
        pause=pause,
    )
    candles = payload.get("data", {}).get("candles", [])
    return save_historical_candles(instrument_key, candles)


def download_candles(
    access_token: str,
    instrument_key: str,
    trading_date: str,
    *,
    unit: str = "minutes",
    interval: int = 1,
    pause: float = 0.0,
) -> int:
    """Downloads a past, closed trading day's candles. Upstox's historical-candle endpoint
    does not include today's session - use download_intraday_candles for that."""
    return download_candle_range(access_token, instrument_key, trading_date, trading_date, unit=unit, interval=interval, pause=pause)


def download_candle_range(
    access_token: str,
    instrument_key: str,
    from_date: str,
    to_date: str,
    *,
    unit: str = "minutes",
    interval: int = 1,
    pause: float = 0.0,
) -> int:
    """Past, closed days from_date..to_date in one request. Upstox accepts at most about one
    calendar month per request for 1-minute candles."""
    encoded_key = quote(instrument_key, safe="")
    url = f"{HISTORICAL_CANDLE_URL}/{encoded_key}/{unit}/{interval}/{to_date}/{from_date}"
    return _download(url, access_token, instrument_key, pause)


def download_intraday_candles(
    access_token: str,
    instrument_key: str,
    *,
    unit: str = "minutes",
    interval: int = 1,
) -> int:
    """Downloads today's candles so far from Upstox's own record, to backfill whatever the
    live websocket stream missed (dropped ticks, a mid-session restart, etc.)."""
    encoded_key = quote(instrument_key, safe="")
    url = f"{HISTORICAL_CANDLE_URL}/intraday/{encoded_key}/{unit}/{interval}"
    return _download(url, access_token, instrument_key)