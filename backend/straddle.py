"""Market-data plumbing shared by the strategy lab and the paper engine: the ATM strike rule, NIFTY spot bars,
expiry and contract resolution, option leg candles, and the one-build-at-a-time background job runner.
No strategy logic, no signals, no orders.
"""
import bisect
import calendar
import csv
import logging
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError

import expired as expired_api
import historical
import instruments
from storage import (
    find_option_contract,
    query_candle_bars,
)

logger = logging.getLogger("uvicorn.error")

RECENT_DAYS = 3  # a day this close to today may simply not be published by Upstox yet
# Skips that can be "not published yet" rather than a permanent property of the day: for a recent
# day they are reported but not recorded, so the next run tries again without retry_skipped.
TRANSIENT_SKIPS = {"no_spot_data", "spot_bars_incomplete"}
OPEN_BAR = "09:15"
ENTRY_BAR = "09:30"
EXIT_BAR = "15:29"  # last regular-session minute; options also trade to 15:39 now, spot does not
EARLIEST_ACCEPTABLE_EXIT_BAR = "15:20"  # a leg whose last bar is older than this has no usable "close"
MIN_SPOT_BARS = 300  # of 375: below this the day is a 5-minute/partial series, not 1-minute history
REQUEST_PAUSE = 0.35  # seconds between successive API calls in a build - stay well under the rate limit
MAX_CONSECUTIVE_ERRORS = 5

Log = Callable[[str], None]
Bars = dict[str, dict[str, Any]]  # "HH:MM" -> bar


@dataclass(frozen=True)
class Underlying:
    name: str
    index_key: str  # the spot index, also what Upstox's expired-contract API is keyed by
    strike_step: float  # rounding step for the ATM strike; a fixed constant, never market data
    search_query: str  # live-contract search text
    exchange: str  # live-contract search exchange


# strike_step verified 2026-09-19 against Upstox's own expired-contract lists (sampled expiries from
# 2024-10 to 2026-09 for each underlying): the modal gap between listed strikes was 50 / 100 / 50 / 25 / 100,
# unchanged across the whole period. Far-from-the-money strikes are listed more sparsely (100-500), which
# is why the check is on the dense zone; the validation report re-derives it from the stored contracts
# on every run (strike_step_check), so a change in the exchange's interval shows up there.
UNDERLYINGS: dict[str, Underlying] = {
    u.name: u
    for u in (
        Underlying("NIFTY", "NSE_INDEX|Nifty 50", 50, "NIFTY", "NSE"),
        Underlying("BANKNIFTY", "NSE_INDEX|Nifty Bank", 100, "BANKNIFTY", "NSE"),
        Underlying("FINNIFTY", "NSE_INDEX|Nifty Fin Service", 50, "FINNIFTY", "NSE"),
        Underlying("MIDCPNIFTY", "NSE_INDEX|NIFTY MID SELECT", 25, "MIDCPNIFTY", "NSE"),
        Underlying("SENSEX", "BSE_INDEX|SENSEX", 100, "SENSEX", "BSE"),
    )
}


# --------------------------------------------------------------------------- the one rule that matters


def resolve_atm_strike(spot_0930: float, strike_step: float) -> float:
    """The ATM strike: the 09:30 spot open rounded to the nearest `strike_step` (halves round up,
    deterministically - Python's round() would alternate).

    *** DO NOT WIDEN THIS SIGNATURE. *** The only market information it may ever receive is ONE
    number: the spot as of the moment the position/snapshot is taken (the 09:30 open for the
    straddle, the last price before 14:00 for the gamma chain). `strike_step` is the underlying's fixed strike interval (50 for
    NIFTY, 100 for BANKNIFTY, ...), a constant of the contract, not data. Never hand it the day's
    close, the day's bars or dataframe, settlement, or "the strike with the most volume". The
    straddle is entered at 09:30, so the strike has to be knowable at 09:30. Any later-in-the-day
    information leaking into strike selection makes the results look wonderful and be entirely
    fake. If a refactor is tempted to pass the day's dataframe in "for convenience", that
    convenience is the bug.
    """
    assert isinstance(spot_0930, (int, float)) and not isinstance(spot_0930, bool), (
        "resolve_atm_strike takes ONE scalar - the 09:30 spot open - never a series or dataframe "
        f"(got {type(spot_0930).__name__}); anything from later in the day would leak the future into the strike"
    )
    assert math.isfinite(spot_0930) and spot_0930 > 0, f"spot_0930 must be a positive number, got {spot_0930!r}"
    assert isinstance(strike_step, (int, float)) and not isinstance(strike_step, bool) and math.isfinite(strike_step) and strike_step > 0, (
        f"strike_step must be the underlying's fixed positive strike interval, got {strike_step!r}"
    )
    return float(math.floor(spot_0930 / strike_step + 0.5) * strike_step)


# --------------------------------------------------------------------------- bars


class SkipDay(Exception):
    """This day cannot produce a complete straddle row. Deterministic, so it is recorded and not retried."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class BuildAborted(Exception):
    """Stop building rather than skip one day. `fatal` (a dead token) stops every underlying;
    otherwise (repeated failures) only the underlying that was being built."""

    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def _session_bars(rows: list[dict[str, Any]]) -> Bars:
    """Rows from query_candle_bars -> {"HH:MM": row} for the regular session only. received_at
    carries its own IST offset, so the wall-clock minute is read straight off the string."""
    bars: Bars = {}
    for row in rows:
        hhmm = row["received_at"][11:16]
        if OPEN_BAR <= hhmm <= EXIT_BAR:
            bars[hhmm] = row
    return bars


def _spot_looks_complete(bars: Bars) -> bool:
    """A usable spot day: either genuine 1-minute history, or a complete 5-minute series.

    The 5-minute case exists because the live UI caches the index at 5 minutes and Upstox
    publishes a day's 1-minute index candles a while after the close, so a very recent day may
    only have those. It is safe because every spot field the row uses is exactly reproducible
    from 5-minute bars: the 09:15 and 09:30 opens are bar opens, the 09:15-09:29 / 09:30-15:29
    windows are 5-minute aligned (so max-high / min-low agree), and the 15:25 bar's close is the
    15:29:59 close. Needing the 15:25 bar matters - without it the last close would be stale.
    """
    if OPEN_BAR not in bars or ENTRY_BAR not in bars:
        return False
    if len(bars) >= MIN_SPOT_BARS:
        return max(bars) >= EARLIEST_ACCEPTABLE_EXIT_BAR
    return len(bars) >= 70 and all(int(t[3:]) % 5 == 0 for t in bars) and "15:25" in bars


def _leg_looks_complete(bars: Bars) -> bool:
    return ENTRY_BAR in bars and max(bars) >= EARLIEST_ACCEPTABLE_EXIT_BAR


def _exit_bar(bars: Bars) -> dict[str, Any] | None:
    times = [t for t in bars if t <= EXIT_BAR]
    return bars[max(times)] if times and max(times) >= EARLIEST_ACCEPTABLE_EXIT_BAR else None


def _csv_path() -> Path:
    return Path(os.getenv("DATA_DIR", "data")) / "exports" / "nifty_1min.csv"


class SpotBars:
    """One underlying's spot-index 1-minute bars per day: market_observations first (only if it
    holds a genuine 1-minute day - the index can also be cached there at 5 minutes), then, for
    NIFTY, the nifty_1min.csv export, then - when fetching is allowed - Upstox itself (a whole
    calendar month per request, written into market_observations)."""

    def __init__(self, underlying: Underlying, token_provider: Callable[[], str | None], fetch: bool, today: date | None = None):
        self._underlying = underlying
        self._token_provider = token_provider
        self._fetch = fetch
        self._last_complete = (today or date.today()) - timedelta(days=1)
        self._csv_days: dict[str, Bars] | None = None
        self._fetched_months: set[str] = set()

    def _from_csv(self, day: str) -> Bars:
        if self._underlying.name != "NIFTY":  # the export only exists for NIFTY
            return {}
        if self._csv_days is None:
            self._csv_days = {}
            path = _csv_path()
            if path.is_file():
                with path.open(newline="", encoding="utf-8") as handle:
                    reader = csv.reader(handle)
                    next(reader, None)
                    for stamp, open_, high, low, close, *_ in reader:
                        self._csv_days.setdefault(stamp[:10], {})[stamp[11:16]] = {
                            "open": float(open_), "high": float(high), "low": float(low), "close": float(close),
                        }
        return self._csv_days.get(day, {})

    def _fetch_month(self, day: str) -> None:
        """One request for the whole calendar month containing `day` (clipped to the last completed
        day), so a build costs about one spot call per month instead of one per day. Once per month per run."""
        month = day[:7]
        if month in self._fetched_months:
            return
        self._fetched_months.add(month)
        token = self._token_provider()
        if not token:
            raise BuildAborted("UPSTOX_ACCESS_TOKEN is not configured", fatal=True)
        first = date.fromisoformat(day).replace(day=1)
        last = min(date(first.year, first.month, calendar.monthrange(first.year, first.month)[1]), self._last_complete)
        historical.download_candle_range(
            token, self._underlying.index_key, first.isoformat(), last.isoformat(), unit="minutes", interval=1, pause=REQUEST_PAUSE
        )

    def get(self, day: str) -> Bars:
        key = self._underlying.index_key
        bars = _session_bars(query_candle_bars(key, day))
        if len(bars) >= MIN_SPOT_BARS and _spot_looks_complete(bars):
            return bars
        csv_bars = self._from_csv(day)
        if _spot_looks_complete(csv_bars):
            return csv_bars
        if self._fetch:
            self._fetch_month(day)
            bars = _session_bars(query_candle_bars(key, day))
        # What is left may be a complete 5-minute series (accepted by the caller), a partial day
        # (rejected there as spot_bars_incomplete) or nothing (no_spot_data).
        return bars


# --------------------------------------------------------------------------- expiries and contracts


class ContractResolver:
    """Nearest expiry as of a date, and the CE/PE instrument keys at a strike. Keys always come
    out of instrument_contracts (filled by Upstox's own contract endpoints) - never built from
    a symbol string."""

    def __init__(self, underlying: Underlying, token_provider: Callable[[], str | None], fetch: bool, today: date, log: Log):
        self._underlying = underlying
        self._token_provider = token_provider
        self._fetch = fetch
        self._today = today
        self._log = log
        self._expired_expiries: list[str] | None = None
        self._active_expiry: str | None = None
        self._active_looked_up = False
        self._listed: set[str] = set()
        self._listed_from: dict[str, str | None] = {}

    def _token(self) -> str:
        token = self._token_provider()
        if not token:
            raise BuildAborted("UPSTOX_ACCESS_TOKEN is not configured", fatal=True)
        return token

    def expired_expiries(self) -> list[str]:
        """Every already-lapsed expiry of this underlying that Upstox still serves contracts for (one call per run)."""
        if self._expired_expiries is None:
            self._expired_expiries = sorted(expired_api.get_expiries(self._token(), self._underlying.index_key))
        return self._expired_expiries

    def _active_expiry_after(self) -> str | None:
        """The nearest expiry that has not lapsed yet: the earliest one on the live-contract search's first page."""
        if not self._active_looked_up:
            response = instruments.search_instruments(
                self._token(), query=self._underlying.search_query, exchanges=self._underlying.exchange,
                segments="FO", instrument_types="CE", records=30,
            )
            upcoming = sorted(
                str(item["expiry"]) for item in response.get("data", [])
                if item.get("expiry") and item.get("underlying_key") == self._underlying.index_key and str(item["expiry"]) >= self._today.isoformat()
            )
            self._active_expiry = upcoming[0] if upcoming else None
            self._active_looked_up = True
        return self._active_expiry

    def _first_listed_expiry_from(self, day: str) -> str | None:
        """The first weekday on or after `day` that Upstox lists this underlying's CE contracts for, probed one date at
        a time. _active_expiry_after() only ever sees the NEAREST live expiry (the unfiltered search's first page is
        all of it), so a day beyond that one - S5's paper roll asks for the expiry after today's, on an expiry day -
        found nothing and was skipped. Weekly expiries are at most a week apart and a holiday only moves one earlier,
        so ten calendar days always reach the next one."""
        if day not in self._listed_from:
            start = date.fromisoformat(day)
            found = None
            for offset in range(10):
                candidate = start + timedelta(days=offset)
                if candidate.weekday() >= 5:
                    continue
                response = instruments.search_instruments(
                    self._token(), query=self._underlying.search_query, exchanges=self._underlying.exchange,
                    segments="FO", instrument_types="CE", expiry=candidate.isoformat(), records=10,
                )
                if any(item.get("underlying_key") == self._underlying.index_key for item in response.get("data", [])):
                    found = candidate.isoformat()
                    break
            self._listed_from[day] = found
        return self._listed_from[day]

    def nearest_expiry(self, day: str) -> str:
        """Nearest expiry on or after `day` (an expiry-day session resolves to that same day)."""
        lapsed = self.expired_expiries()
        candidates = list(lapsed)
        if not lapsed or day > lapsed[-1]:
            active = self._active_expiry_after()
            if active is None:
                raise RuntimeError(f"Could not resolve the current {self._underlying.name} expiry from Upstox")
            candidates.append(active)
            if day > active and (later := self._first_listed_expiry_from(day)):
                candidates.append(later)
        index = bisect.bisect_left(candidates, day)
        if index == len(candidates):
            raise SkipDay("no_expiry_known", f"no {self._underlying.name} expiry on or after {day}")
        return candidates[index]

    def is_lapsed(self, expiry: str) -> bool:
        return expiry < self._today.isoformat()

    def _list_contracts(self, expiry: str) -> None:
        """Pulls the expiry's full option contract list into instrument_contracts, once per run."""
        if expiry in self._listed:
            return
        self._listed.add(expiry)
        if self.is_lapsed(expiry):
            expired_api.get_expired_contracts(self._token(), self._underlying.index_key, expiry, "option", pause=REQUEST_PAUSE)
            return
        for side in ("CE", "PE"):
            page = 1
            while True:
                response = instruments.search_instruments(
                    self._token(), query=self._underlying.search_query, exchanges=self._underlying.exchange, segments="FO",
                    instrument_types=side, expiry=expiry, page_number=page, records=30,
                )
                total_pages = (response.get("meta_data") or {}).get("page", {}).get("total_pages", 1)
                page += 1
                time.sleep(REQUEST_PAUSE)
                if page > total_pages:
                    break

    def option_keys(self, expiry: str, strike: float) -> tuple[str, str]:
        lapsed = self.is_lapsed(expiry)

        def lookup() -> tuple[str | None, str | None]:
            return (
                find_option_contract(self._underlying.index_key, expiry, strike, "CE", expired=lapsed),
                find_option_contract(self._underlying.index_key, expiry, strike, "PE", expired=lapsed),
            )

        ce_key, pe_key = lookup()
        if (ce_key is None or pe_key is None) and self._fetch:
            self._list_contracts(expiry)
            ce_key, pe_key = lookup()
        if ce_key is None or pe_key is None:
            missing = "/".join(side for side, key in (("CE", ce_key), ("PE", pe_key)) if key is None)
            raise SkipDay("contract_not_listed", f"{missing} {strike:g} for expiry {expiry}")
        return ce_key, pe_key


def leg_bars(
    token_provider: Callable[[], str | None], instrument_key: str, day: str, *, lapsed: bool, fetch: bool, today: date | None = None
) -> Bars:
    """One option leg's session bars. Uses what market_observations already holds; only when that
    is missing or cut short does it call Upstox - through expired.py for a lapsed contract,
    historical.py's closed-day endpoint for an earlier live one, or (when the caller says which day is
    "today") its intraday endpoint for today's own session, since the closed-day endpoint never
    includes today - it would silently return zero candles, forever, no matter how often it is retried."""
    bars = _session_bars(query_candle_bars(instrument_key, day))
    if _leg_looks_complete(bars) or not fetch:
        return bars
    token = token_provider()
    if not token:
        raise BuildAborted("UPSTOX_ACCESS_TOKEN is not configured", fatal=True)
    if lapsed:
        expired_api.download_expired_candles(token, instrument_key, day, day, "1minute", pause=REQUEST_PAUSE)
    elif today is not None and date.fromisoformat(day) >= today:
        historical.download_intraday_candles(token, instrument_key, unit="minutes", interval=1)
    else:
        historical.download_candles(token, instrument_key, day, unit="minutes", interval=1, pause=REQUEST_PAUSE)
    return _session_bars(query_candle_bars(instrument_key, day))


# --------------------------------------------------------------------------- the derived row


# --------------------------------------------------------------------------- validation


# --------------------------------------------------------------------------- the build run


def _new_stats(from_date: str | None, to_date: str | None) -> dict[str, Any]:
    return {
        "attempted": 0, "succeeded": 0, "skipped": 0, "errors": 0, "already_built": 0, "previously_skipped": 0,
        "skip_reasons": {}, "total": 0, "done": 0, "current_date": None,
        "requested_from": from_date, "requested_to": to_date, "limit_notes": [], "stopped_because": "completed",
        "error_samples": [],
    }


def _run_days(
    name: str,
    days: list[str],
    stats: dict[str, Any],
    *,
    is_built: Callable[[str], bool],
    is_skipped: Callable[[str], bool],
    build: Callable[[str], Any],
    save: Callable[[Any], None],
    record_skip: Callable[[str, str, str], None],
    describe: Callable[[Any], str],
    rebuild: bool,
    retry_skipped: bool,
    today: date,
    log: Log,
    progress: Callable[[], None],
) -> None:
    """The per-day discipline shared by every builder.

    A day that cannot produce a complete row (SkipDay) is deterministic: it is recorded and not
    retried (except a recent day whose data Upstox may simply not have published yet). Anything
    else - HTTP failures, network drops - is transient: counted, never recorded, retried next run.
    A dead token stops every builder; repeated failures stop just this one."""
    consecutive_errors = 0
    try:
        for day in days:
            stats["current_date"] = day
            if not rebuild and is_built(day):
                stats["already_built"] += 1
            elif not rebuild and is_skipped(day) and not retry_skipped:
                stats["previously_skipped"] += 1
            else:
                stats["attempted"] += 1
                try:
                    row = build(day)
                except SkipDay as skip:
                    consecutive_errors = 0
                    stats["skip_reasons"][skip.reason] = stats["skip_reasons"].get(skip.reason, 0) + 1
                    stats["skipped"] += 1
                    recent = (today - date.fromisoformat(day)).days <= RECENT_DAYS
                    unpublished = skip.reason in TRANSIENT_SKIPS and recent
                    log(f"  {name} {day}: skipped - {skip}" + (" (recent day: not recorded, the next run will retry it)" if unpublished else ""))
                    if not rebuild and not unpublished:  # a rebuild keeps the old row rather than losing it to a data gap
                        record_skip(day, skip.reason, skip.detail)
                except HTTPError as error:
                    if error.code == 401:
                        raise BuildAborted("Upstox rejected the access token (HTTP 401) - it has probably expired; log in again", fatal=True) from error
                    consecutive_errors = _note_error(stats, name, day, f"HTTP {error.code}", consecutive_errors, log)
                except BuildAborted:
                    raise
                except Exception as error:  # noqa: BLE001 - one bad day must not sink the run
                    logger.exception("Build failed on %s %s", name, day)
                    consecutive_errors = _note_error(stats, name, day, f"{type(error).__name__}: {error}", consecutive_errors, log)
                else:
                    consecutive_errors = 0
                    save(row)
                    stats["succeeded"] += 1
                    log(f"  {name} {day}: ok  {describe(row)}")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    raise BuildAborted(f"{MAX_CONSECUTIVE_ERRORS} consecutive failures - stopping (last: {stats['error_samples'][-1]})")
            stats["done"] += 1
            progress()
    except BuildAborted as aborted:
        stats["stopped_because"] = f"aborted: {aborted}"
        if aborted.fatal:
            raise
    finally:
        stats["current_date"] = None


def _note_error(stats: dict[str, Any], name: str, day: str, message: str, consecutive: int, log: Log) -> int:
    stats["errors"] += 1
    stats["error_samples"] = [*stats["error_samples"], f"{day}: {message}"][-10:]
    log(f"  {name} {day}: ERROR - {message} (not recorded as a skip; the next run will retry it)")
    return consecutive + 1


# --------------------------------------------------------------------------- gamma chain
#
# For each NIFTY expiry day: the option chain around the money (ATM +/- `radius` strikes, CE and
# PE) as it stood at 14:00, plus a per-day summary, plus how busy the last 90 minutes were compared
# with the middle of the day. Bars go to market_observations through the same expired.py path.
#
# NO LOOK-AHEAD. Every "at 14:00" value is read from bars strictly BEFORE 14:00 - the 14:00 bar
# itself spans 14:00:00-14:00:59, so using even its open/close would leak up to a minute. The chain's
# centre comes from resolve_atm_strike(<that same pre-14:00 spot>), a single scalar. The only values
# that use later bars are the outcomes (spot_close, R_late), and they are named as outcomes.

MIN_MIDDAY_BARS, MIN_LATE_BARS = 200, 75  # of 240 / 90: fewer means the spot day has holes


# --------------------------------------------------------------------------- late-session bars
#
# The raw 1-minute bars of the options nearest the money, from 14:00 to the LAST bar of the session -
# deliberately not cut at 15:29. From 2026-08-03 the F&O session runs to 15:40, so the last bar is
# 15:39 and those final minutes are the point of this export. Nothing is fetched here: it reads what
# the gamma build already stored in market_observations, which keeps every candle Upstox returned
# (only the 14:00-feature and straddle computations restrict themselves to bars before 14:00 / 15:29).


# --------------------------------------------------------------------------- background jobs


class _JobState:
    """One kind of background build: its id, log tail, progress and result, behind a lock.

    Only one build runs at a time across all kinds - each call is rate-limited by the pause between
    requests, and two jobs would silently double the request rate against the same API."""

    def __init__(self, kind: str):
        self.kind = kind
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {"state": "idle", "job_id": None, "log": [], "run": None, "report": None, "error": None}

    def running(self) -> bool:
        with self.lock:
            return self.data["state"] == "running"

    def log(self, line: str) -> None:
        logger.info(line)
        with self.lock:
            self.data["log"] = [*self.data["log"], line][-200:]

    def progress(self, stats: dict[str, Any]) -> None:
        with self.lock:
            self.data["run"] = stats

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            status = {key: value for key, value in self.data.items() if key != "log"}
            status["log_tail"] = self.data["log"][-8:]
        return status

    def start(self, params: dict[str, Any], target: Callable[[dict[str, Any], Log, Callable[[dict[str, Any]], None]], dict[str, Any]]) -> dict[str, Any]:
        """Starts `target` on a worker thread unless any build is already running. Returns {job_id, already_running}."""
        for other in _JOBS:
            if other.running():
                return {"job_id": other.data["job_id"], "already_running": True, "running_kind": other.kind}
        with self.lock:
            job_id = uuid.uuid4().hex[:8]
            self.data.update(state="running", job_id=job_id, log=[], run=None, report=None, error=None, params=params)
        threading.Thread(target=self._main, args=(target, params), name=f"{self.kind}-build-{job_id}", daemon=True).start()
        return {"job_id": job_id, "already_running": False}

    def _main(self, target: Callable[..., dict[str, Any]], params: dict[str, Any]) -> None:
        try:
            report = target(params, self.log, self.progress)
            with self.lock:
                self.data.update(state="done", report=report, run=report["run"])
        except Exception as error:  # noqa: BLE001
            logger.exception("%s build failed", self.kind)
            with self.lock:
                self.data.update(state="error", error=f"{type(error).__name__}: {error}")


_JOBS: list[_JobState] = []  # every background build registers here (strategy_lab's history and candle builds), so only one runs at a time

