import json
import logging
from datetime import date
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from typing import Any

from storage import save_instrument_contracts

logger = logging.getLogger("uvicorn.error")

SEARCH_URL = "https://api.upstox.com/v2/instruments/search"
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"


def search_instruments(
    access_token: str,
    *,
    query: str = "NIFTY",
    exchanges: str = "NSE",
    segments: str = "FO",
    instrument_types: str | None = None,
    expiry: str | None = None,
    atm_offset: int | None = None,
    page_number: int = 1,
    records: int = 30,
) -> dict[str, Any]:
    params: dict[str, str | int] = {
        "query": query,
        "exchanges": exchanges,
        "segments": segments,
        "page_number": page_number,
        "records": min(records, 30),
    }
    if instrument_types:
        params["instrument_types"] = instrument_types
    if expiry:
        params["expiry"] = expiry
    if atm_offset is not None:
        params["atm_offset"] = atm_offset

    request = Request(
        f"{SEARCH_URL}?{urlencode(params)}",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "zero-instrument-discovery/1.0",
        },
    )
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode())
        save_instrument_contracts(result.get("data", []))
        return result


def instrument_keys(response: dict[str, Any]) -> list[str]:
    return [
        item["instrument_key"]
        for item in response.get("data", [])
        if item.get("instrument_key")
    ]


def resolve_current_week_expiry(access_token: str) -> str | None:
    """Upstox's `current_week` expiry alias can lag for roughly a day right after a weekly
    expiry rolls over - confirmed empirically against the live API: the morning after the
    2026-09-15 expiry, `expiry=current_week` returned zero CE/PE contracts, while an unfiltered
    search already showed the real next expiry (2026-09-22) was live. Resolving to a concrete
    date ourselves sidesteps the alias's staleness entirely.
    """
    try:
        response = search_instruments(access_token, query="NIFTY", instrument_types="CE", records=30)
    except Exception:
        logger.exception("Could not resolve current_week to a concrete expiry")
        return None
    today = date.today().isoformat()
    expiries = sorted({str(item["expiry"]) for item in response.get("data", []) if item.get("expiry")})
    upcoming = [value for value in expiries if value >= today]
    return upcoming[0] if upcoming else None


def discover_nifty_stream_instruments(
    access_token: str,
    *,
    expiry: str = "current_week",
    strike_count: int = 5,
) -> list[str]:
    # The NIFTY 50 index itself is never returned by the FO-segment search below, but its LTP
    # is what's needed alongside the future's, so it's always subscribed regardless of what
    # futures/options come back from discovery.
    keys: list[str] = [NIFTY_INDEX_KEY]

    # NIFTY futures only expire monthly, unlike the weekly options below, so passing the
    # weekly `expiry` alias here matches zero FUT contracts most weeks and silently drops the
    # future from the stream (confirmed against Upstox's search API: current_week -> 0 FUT
    # results, current_month -> the active contract).
    try:
        futures = search_instruments(
            access_token,
            query="NIFTY",
            instrument_types="FUT",
            expiry="current_month",
            records=30,
        )
        keys.extend(instrument_keys(futures))
    except Exception:
        logger.exception("NIFTY futures instrument search failed; continuing without it")

    resolved_expiry = expiry
    if expiry == "current_week":
        concrete = resolve_current_week_expiry(access_token)
        if concrete:
            resolved_expiry = concrete
        else:
            logger.warning("current_week did not resolve to a concrete date; using the alias as-is")

    # Each of these is a separate network call, so one transient failure (rate limit, timeout)
    # must not abort the whole discovery and take the live stream down for the rest of the
    # session — skip the failed strike and keep going; the periodic resubscribe will retry it.
    for instrument_type in ("CE", "PE"):
        for atm_offset in range(-strike_count, strike_count + 1):
            try:
                options = search_instruments(
                    access_token,
                    query="NIFTY",
                    instrument_types=instrument_type,
                    expiry=resolved_expiry,
                    atm_offset=atm_offset,
                    records=5,
                )
                keys.extend(instrument_keys(options))
            except Exception:
                logger.exception(
                    "NIFTY %s search failed for ATM offset %d; skipping", instrument_type, atm_offset
                )

    return list(dict.fromkeys(keys))


def available_nifty_expiries(access_token: str) -> list[str]:
    response = search_instruments(
        access_token,
        query="NIFTY",
        segments="FO",
        records=30,
    )
    expiries = {
        str(item["expiry"])
        for item in response.get("data", [])
        if item.get("expiry")
    }
    return sorted(expiries)