from urllib.parse import quote, urlencode
from typing import Any

from http_util import request_json
from storage import save_historical_candles, save_instrument_contracts


BASE_URL = "https://api.upstox.com/v2/expired-instruments"


def upstox_get(access_token: str, path: str, params: dict[str, str], pause: float = 0.0) -> dict[str, Any]:
    return request_json(
        f"{BASE_URL}/{path}?{urlencode(params)}",
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "zero-expired-instruments/1.0",
        },
        timeout=30,
        pause=pause,
    )


def get_expiries(access_token: str, instrument_key: str) -> list[str]:
    # Unlike the contract-list endpoints below, this one's `data` is a plain list of
    # expiry date strings (not instrument contract objects), so there is nothing here
    # to persist via save_instrument_contracts.
    response = upstox_get(access_token, "expiries", {"instrument_key": instrument_key})
    return response.get("data", [])


def get_expired_contracts(
    access_token: str,
    instrument_key: str,
    expiry_date: str,
    contract_type: str,
    pause: float = 0.0,
) -> list[dict[str, Any]]:
    response = upstox_get(
        access_token,
        f"{contract_type}/contract",
        {"instrument_key": instrument_key, "expiry_date": expiry_date},
        pause,
    )
    contracts = response.get("data", [])
    # analyze_session() joins market_observations against instrument_contracts to learn each
    # row's instrument_type/strike_price; without this, every candle downloaded for an expired
    # contract has no known side and analytics silently drops it (cumulative volume stays at 0).
    save_instrument_contracts(contracts)
    return contracts


def download_expired_candles(
    access_token: str,
    expired_instrument_key: str,
    from_date: str,
    to_date: str,
    interval: str = "30minute",
    pause: float = 0.0,
) -> int:
    encoded_key = quote(expired_instrument_key, safe="")
    response = upstox_get(
        access_token,
        f"historical-candle/{encoded_key}/{interval}/{to_date}/{from_date}",
        {},
        pause,
    )
    candles = response.get("data", {}).get("candles", [])
    return save_historical_candles(expired_instrument_key, candles)