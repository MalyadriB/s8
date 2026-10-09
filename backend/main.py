import json
import logging
import os
import secrets
import asyncio
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, time as dt_time, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from market_stream import StreamStalled, request_stop, run_market_stream
from storage import (
    close_jsonl_files,
    append_stream_message,
    initialize_database,
    query_paper_alerts,
)
import paper
import strategy_lab
from day_chart import day_chart
from strategy_lab import history_job_status, start_history_job
from stream_monitor import StreamMonitor
from instruments import discover_nifty_stream_instruments
from reliability import is_market_day, token_status
from auto_login import auto_login_enabled, persist_access_token, renew_access_token
from network import prefer_ipv4

prefer_ipv4()  # a dead IPv6 route made every Upstox call wait ~42 s - see network.py
load_dotenv(override=True)
logger = logging.getLogger("uvicorn.error")


def renew_and_apply_token() -> str:
    """Run the blocking TOTP login and install the resulting token everywhere it's read from."""
    global access_token
    token = renew_access_token()
    access_token = token
    os.environ["UPSTOX_ACCESS_TOKEN"] = token
    persist_access_token(token)
    return token


async def token_renewal_worker() -> None:
    if not auto_login_enabled():
        return
    timezone = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
    renewal_time = os.getenv("UPSTOX_TOKEN_RENEWAL_TIME", "03:40")
    hour, minute = (int(part) for part in renewal_time.split(":", 1))
    while True:
        now = datetime.now(timezone)
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        try:
            await asyncio.to_thread(renew_and_apply_token)
            logger.info("Upstox access token renewed automatically")
        except Exception:
            logger.exception("Automatic Upstox token renewal failed")


def handle_stream_message(message: object) -> None:
    """Stores each live message's prices (the paper engine reads its quotes from there) and tracks gaps. Runs on the Upstox SDK's own thread."""
    received_at = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata")))
    append_stream_message(message, received_at)
    gaps = stream_monitor.record(message, received_at)
    if gaps:
        logger.warning("Market stream gap detected: %s", gaps)


STALL_RECONNECT_SECONDS = 3


def stream_expected_now() -> bool:
    """True while the exchange is trading, i.e. while silence on the stream means it is broken (09:16 to the last trading minute, 15:39, on a market day)."""
    now = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata")))
    last = strategy_lab.exit_minute(now.date().isoformat())  # the last minute the options trade that day
    return is_market_day(now.date()) and dt_time(9, 16) <= now.time() <= dt_time(int(last[:2]), int(last[3:]))


async def market_stream_worker() -> None:
    if os.getenv("UPSTOX_ENABLE_STREAM", "false").lower() != "true":
        return

    timezone = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
    session_start = os.getenv("MARKET_SESSION_START", "09:15")
    session_end = os.getenv("MARKET_SESSION_END", "15:30")
    start_hour, start_minute = (int(part) for part in session_start.split(":", 1))
    end_hour, end_minute = (int(part) for part in session_end.split(":", 1))
    retry_seconds = int(os.getenv("UPSTOX_STREAM_RETRY_SECONDS", "30"))
    # One session per trading day, for as long as the process runs. This used to stream a single session and then
    # return for good, so a server left running overnight had no stream at all the next morning - no NIFTY spot at
    # the 09:20 health check (every paper entry missed), no quotes for S5's 09:30 exit, no mark-to-market.
    while True:
        now = datetime.now(timezone)
        next_start = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        if now >= now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0):
            next_start += timedelta(days=1)
        while not is_market_day(next_start.date()):
            next_start += timedelta(days=1)
        if now < next_start:
            logger.info("Market stream waiting until %s", next_start.isoformat())
            await asyncio.sleep((next_start - now).total_seconds())
        await run_stream_session(timezone, end_hour, end_minute, retry_seconds)


async def run_stream_session(timezone: ZoneInfo, end_hour: int, end_minute: int, retry_seconds: int) -> None:
    # discover_stream()/run_market_stream() can both fail for a one-off, transient reason (a
    # slow/rate-limited Upstox response, a network blip during the websocket handshake) - with
    # no retry, a single such failure used to kill the stream silently for the rest of the
    # session, indistinguishable from everything working, until someone noticed the charts were
    # empty and manually restarted the process. Retrying with a short backoff until the session
    # actually ends turns that into a self-healing hiccup instead of a standing outage.
    session_end_today = datetime.now(timezone).replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    while datetime.now(timezone) < session_end_today:
        if not access_token:  # re-checked every retry: logging in mid-session starts the stream then
            logger.error("Market stream waiting: UPSTOX_ACCESS_TOKEN is not configured - log in to Upstox")
            await asyncio.sleep(retry_seconds)
            continue
        try:
            if os.getenv("UPSTOX_DYNAMIC_INSTRUMENTS", "true").lower() == "true":
                def discover_stream() -> list[str]:
                    return discover_nifty_stream_instruments(
                        access_token,
                        expiry=os.getenv("UPSTOX_EXPIRY", "current_week"),
                        strike_count=int(os.getenv("UPSTOX_STRIKE_COUNT", "5")),
                    )

                instruments = await asyncio.to_thread(discover_stream)
                logger.info("Discovered %d NIFTY stream instruments", len(instruments))
                refresh_instruments = discover_stream
            else:
                instruments = None
                refresh_instruments = None
            await asyncio.to_thread(
                run_market_stream,
                access_token,
                handle_stream_message,
                instruments,
                refresh_instruments,
                paper.wanted_stream_instruments,
                stream_expected_now,
            )
            # run_market_stream() blocks for as long as the stream is meant to run (see
            # market_stream.py) - it returning at all, even without an exception, means the
            # connection setup didn't actually take, so that's worth retrying too.
            logger.warning("Market stream exited unexpectedly; retrying in %ds", retry_seconds)
        except StreamStalled as stalled:
            logger.warning("Market stream stalled (%s); reconnecting in %ds", stalled, STALL_RECONNECT_SECONDS)
            await asyncio.sleep(STALL_RECONNECT_SECONDS)
            continue
        except Exception:
            logger.exception("Market stream failed to start; retrying in %ds", retry_seconds)
        await asyncio.sleep(retry_seconds)


paper_engine: paper.PaperEngine | None = None


async def paper_worker() -> None:
    """Drives the PAPER trading engine. It only reads stored quotes and writes SQLite - it can never place an order."""
    global paper_engine
    if os.getenv("PAPER_ENABLED", "true").lower() != "true":
        logger.info("Paper trading is disabled (PAPER_ENABLED != true)")
        return
    tz = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
    paper_engine = paper.PaperEngine(paper.StreamQuotes(lambda: datetime.now(tz)), heartbeat=stream_monitor.newest)
    logger.info("Paper trading engine started (%d lot(s)) - PAPER ONLY, no orders are ever placed", paper_engine.lots)
    while True:
        try:
            await asyncio.to_thread(paper_engine.tick)
            paper_engine.last_error = None
        except Exception as error:
            paper_engine.last_error = f"{type(error).__name__}: {error}"
            logger.exception("Paper engine tick failed")
        await asyncio.sleep(int(os.getenv("PAPER_TICK_SECONDS", "2")))


LAB_WARM_SECONDS = 300


async def lab_cache_warmer() -> None:
    """Keeps S5-S7's computed-rows caches warm (S8/S10 are filters over S5's): a cold computation takes ~20s and used to land on whichever page
    was opened first after a restart (every --reload) or after new history rows invalidated the caches. Re-running
    on a warm cache is a ~0.5s hit, so the loop is cheap when nothing changed."""
    await asyncio.sleep(5)
    while True:
        try:
            await asyncio.to_thread(strategy_lab.backtest_daily, None, None, None, strategy_lab.Size(lots=strategy_lab.default_lots()), basis=strategy_lab.MINUTE)
        except Exception:
            logger.exception("Lab cache warm-up failed")
        await asyncio.sleep(LAB_WARM_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    stream_task = asyncio.create_task(market_stream_worker())
    renewal_task = asyncio.create_task(token_renewal_worker())
    paper_task = asyncio.create_task(paper_worker())
    warm_task = asyncio.create_task(lab_cache_warmer())
    if auto_login_enabled():
        logger.info("Upstox auto-login enabled: token renewal scheduled for %s %s", os.getenv("TIMEZONE", "Asia/Kolkata"), os.getenv("UPSTOX_TOKEN_RENEWAL_TIME", "03:40"))
    try:
        yield
    finally:
        request_stop()  # the stream thread would otherwise keep this process alive after shutdown / reload
        close_jsonl_files()  # everything queued for the day's JSONL files is on disk before exit
        # The SDK's websocket thread is not a daemon and a half-open socket never ends it, so Python would wait on it
        # forever at exit (a reload then never brings the new server up). Everything is committed by now (SQLite WAL).
        threading.Timer(15, os._exit, (0,)).start() if os.getenv("FORCE_EXIT_ON_SHUTDOWN", "true").lower() == "true" else None
        stream_task.cancel()
        renewal_task.cancel()
        paper_task.cancel()
        warm_task.cancel()
        await asyncio.gather(stream_task, renewal_task, paper_task, warm_task, return_exceptions=True)


app = FastAPI(title="zero", lifespan=lifespan)
initialize_database()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPSTOX_AUTHORIZE_URL = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
UPSTOX_PROFILE_URL = "https://api.upstox.com/v2/user/profile"
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
REDIRECT_URI = os.getenv(
    "UPSTOX_REDIRECT_URI", "http://localhost:8000/api/upstox/callback"
)

oauth_state: str | None = None
access_token: str | None = os.getenv("UPSTOX_ACCESS_TOKEN")
stream_monitor = StreamMonitor(float(os.getenv("STREAM_GAP_SECONDS", "5")))


def upstox_request(url: str, *, data: dict[str, str] | None = None, token: str | None = None):
    headers = {
        "accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "zero-upstox-client/1.0",
    }
    body = None
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urlencode(data).encode()
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(url, data=body, headers=headers, method="POST" if body else "GET")
    if url == UPSTOX_TOKEN_URL and data is not None:
        logger.info(
            "Upstox token exchange request: client_id=%s redirect_uri=%s fields=%s code_length=%d",
            data.get("client_id"),
            data.get("redirect_uri"),
            sorted(data.keys()),
            len(data.get("code", "")),
        )
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode())
    except HTTPError as error:
        response_body = error.read().decode(errors="replace").strip()
        logger.error(
            "Upstox response: endpoint=%s status=%d body=%s",
            url,
            error.code,
            response_body[:500] or "<empty>",
        )
        try:
            payload = json.loads(response_body)
            errors = payload.get("errors", [])
            if isinstance(errors, list) and errors:
                first_error = errors[0]
                message = first_error.get("message", "request rejected")
                error_code = first_error.get("errorCode") or first_error.get("error_code")
                if error_code:
                    message = f"{error_code}: {message}"
            elif isinstance(errors, dict):
                message = errors.get("message", "request rejected")
            else:
                error_code = payload.get("error_code") or payload.get("errorCode")
                message = payload.get("detail") or payload.get("message", "request rejected")
                if error_code:
                    message = f"{error_code}: {message}"
        except (json.JSONDecodeError, AttributeError, TypeError):
            message = response_body[:500] or "request rejected"
        raise HTTPException(
            status_code=502,
            detail=f"Upstox returned HTTP {error.code}: {message}",
        ) from error
    except (URLError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=502, detail="Could not reach Upstox") from error


@app.get("/api/health")
def health():
    status = token_status(access_token)
    return {"status": "ok", "token": status, "auto_login_enabled": auto_login_enabled()}


@app.get("/api/auth/token-status")
def auth_token_status():
    return token_status(access_token)


@app.get("/api/upstox/login")
def upstox_login():
    global oauth_state
    client_id = os.getenv("UPSTOX_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=500, detail="UPSTOX_CLIENT_ID is not configured")

    oauth_state = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": oauth_state,
        }
    )
    return RedirectResponse(f"{UPSTOX_AUTHORIZE_URL}?{query}")


@app.get("/api/upstox/callback")
@app.get("/api/auth/callback")
@app.get("/callback")
def upstox_callback(code: str | None = None, state: str | None = None):
    global access_token, oauth_state
    if not code or not state or state != oauth_state:
        return RedirectResponse(f"{FRONTEND_URL}?auth=error&message=invalid_callback")

    client_id = os.getenv("UPSTOX_CLIENT_ID")
    client_secret = os.getenv("UPSTOX_CLIENT_SECRET")
    if not client_id or not client_secret:
        return RedirectResponse(f"{FRONTEND_URL}?auth=error&message=server_not_configured")

    access_token = None
    try:
        token_data = upstox_request(
            UPSTOX_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        access_token = token_data["access_token"]
        os.environ["UPSTOX_ACCESS_TOKEN"] = access_token
        persist_access_token(access_token)
    except HTTPException as error:
        logger.error("Upstox token exchange failed: %s", error.detail)
        detail = quote(str(error.detail), safe="")
        return RedirectResponse(f"{FRONTEND_URL}?auth=error&message={detail}")
    except KeyError:
        return RedirectResponse(
            f"{FRONTEND_URL}?auth=error&message=token_exchange_missing_access_token"
        )
    finally:
        oauth_state = None

    return RedirectResponse(f"{FRONTEND_URL}?auth=success")


@app.post("/api/upstox/auto-login")
def upstox_auto_login():
    if not auto_login_enabled():
        raise HTTPException(
            status_code=400,
            detail="Auto-login is disabled. Set UPSTOX_AUTO_LOGIN_ENABLED=true and the UPSTOX_USERNAME/PASSWORD/PIN_CODE/TOTP_SECRET vars in backend/.env",
        )
    try:
        renew_and_apply_token()
    except RuntimeError as error:
        logger.error("Manual Upstox auto-login failed: %s", error)
        raise HTTPException(status_code=502, detail=str(error)) from error
    return {"status": "renewed", "token": token_status(access_token)}


@app.get("/api/upstox/profile")
def upstox_profile():
    if not access_token:
        raise HTTPException(status_code=401, detail="Upstox account is not connected")
    profile_response = upstox_request(UPSTOX_PROFILE_URL, token=access_token)
    return profile_response.get("data", profile_response)


def _valid_iso_date(value: str | None, name: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD")


def _lab_strategy(name: str | None) -> str | None:
    if name in (None, "", "all"):
        return None
    if name not in strategy_lab.LAB_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"strategy must be one of {list(strategy_lab.LAB_STRATEGIES)}")
    return name


def _lab_basis(basis: str) -> str:
    """`minute` (the 09:30 -> 15:29 backtest, Oct 2024 on) or `nse_daily` (open -> close on NSE end-of-day prices, Jan 2022 on)."""
    try:
        return strategy_lab.check_basis(basis)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


def _lab_size(lots: int | None, qty: int | None) -> "strategy_lab.Size | None":
    """The trade size a request asks for - `lots` (each day's own lot size) or a fixed `qty` of units - or None for the defaults."""
    try:
        return strategy_lab.Size.from_params(lots, qty)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


@app.get("/api/lab/backtest")
@app.get("/lab/backtest")
def lab_backtest(
    strategy: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None, lots: int | None = None, qty: int | None = None,
    basis: str = "minute",
):
    """Daily backtest results (candle prices, the shared cost model) from the history tables, at `lots` lots or a fixed `qty` of units."""
    size = _lab_size(lots, qty) or strategy_lab.Size(lots=strategy_lab.default_lots())
    name = _lab_strategy(strategy)
    rows = strategy_lab.backtest_daily(name, _valid_iso_date(from_, "from"), _valid_iso_date(to, "to"), size, basis=_lab_basis(basis))
    # Returned as a ready JSONResponse: the payload is already plain JSON types, and FastAPI's default
    # jsonable_encoder pass over ~4,000 rows with nested legs took ~1.5s versus ~0.06s for json.dumps itself.
    return JSONResponse({
        "source": "backtest", "basis": basis, "count": len(rows), "size": {"lots": size.lots, "qty": size.qty}, "rows": rows,
        "derived_skips": strategy_lab.derived_skips(basis, only=name),  # one strategy asked for: only its own list
    })


@app.get("/api/lab/paper")
@app.get("/lab/paper")
def lab_paper(strategy: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None, lots: int | None = None, qty: int | None = None):
    """Daily PAPER trades. Nothing here is a real order. With lots/qty the result is re-settled from the recorded fills at that size."""
    rows = strategy_lab.paper_daily(_lab_strategy(strategy), _valid_iso_date(from_, "from"), _valid_iso_date(to, "to"), _lab_size(lots, qty))
    return {"source": "paper", "paper": True, "count": len(rows), "rows": rows}


@app.get("/api/lab/spot")
@app.get("/lab/spot")
def lab_spot():
    """NIFTY right now: the newest index tick and the session's previous close / open / high / low. The Today screen's
    index tile polls this every second while the market is live, so it moves with the market, not the 2-second Today poll."""
    return paper.spot_now(datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))))


@app.get("/api/lab/spot/minutes")
@app.get("/lab/spot/minutes")
def lab_spot_minutes():
    """NIFTY's last tick of each minute of the session shown (the Today header's sparkline; a minute the feed missed is absent)."""
    return paper.spot_minutes(datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))))


@app.get("/api/lab/paper/today")
@app.get("/lab/paper/today")
def lab_paper_today(lots: int | None = None, qty: int | None = None, marks_since: str | None = Query(None, pattern=r"^\d{2}:\d{2}$")):
    """Today's paper positions with their minute-by-minute mark-to-market, the engine's state and recent alerts.

    `marks_since` (HH:MM) returns only the marks of that minute and later - the screen polls every 2 seconds and already
    holds the earlier ones, so a full day's ~1,800 marks are not re-sent each time (the last minute is included again
    because a mark is updated while its minute is still running)."""
    size = _lab_size(lots, qty)
    tz = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
    now = datetime.now(tz)
    # The session shown: today's from its 09:15 open; until then - after midnight, on a weekend or a holiday - the last
    # one, so the position held overnight keeps its marks, closing quotes and NIFTY's day instead of an empty new date.
    day = paper.session_day(now)
    recorded = strategy_lab.paper_trades_for_today(day)
    trades = [strategy_lab.paper_at(trade, size) for trade in recorded]
    session_marks, earlier_marks = paper.today_marks(day, recorded)
    marks = strategy_lab.marks_at(session_marks, recorded, size)
    earlier = strategy_lab.marks_at(earlier_marks, recorded, size)  # an open position's own entry-day line (S5 before its 09:30 exit)
    latest = {}
    for mark in marks:
        latest[mark["strategy"]] = mark
    open_keys = [leg["instrument_key"] for trade in trades if trade["status"] == "open" for leg in trade["legs"] or [] if leg.get("instrument_key")]
    return {
        "paper": True,
        "now": now.isoformat(timespec="milliseconds"),  # to the millisecond: the screen's clock and countdowns sync to it
        "live": paper.live_snapshot(open_keys, now, day),
        "trading_date": day,  # the session shown (see above) - not always the calendar date
        "calendar_date": now.date().isoformat(),
        "market_day": is_market_day(datetime.fromisoformat(day).date()),
        "calendar_market_day": is_market_day(now.date()),
        "next_session": paper.next_session_day(now),  # whose 09:30 (S5 exit, then entries) comes next - holidays skipped
        "spot_day": paper.spot_day(now, day),  # NIFTY's previous close and the session's open/high/low, from its newest index message
        # When the engine starts S1-S3's same-day exit (15:29:50) - exit_minute() is only the last minute options trade.
        "exit_at": now.replace(hour=paper.EXIT_ATTEMPT_START.hour, minute=paper.EXIT_ATTEMPT_START.minute, second=paper.EXIT_ATTEMPT_START.second, microsecond=0).isoformat(timespec="seconds"),
        "engine": paper.status_snapshot(paper_engine),
        "trades": trades,
        # incremental polls carry only the session's newest minutes; the earlier days' marks are fixed, so only full loads send them
        # (and an overnight position's next-morning marks, minutes past 24:00, which grow from 09:15 until its exit)
        "marks": [mark for mark in earlier if mark["minute"] >= "24:00"] + [mark for mark in marks if mark["minute"] >= marks_since] if marks_since else earlier + marks,
        "marks_since": marks_since,
        "latest_mtm_rs": {name: mark["mtm_rs"] for name, mark in latest.items()},
        "alerts": query_paper_alerts(20),
    }


RETRY_QUOTE_WAIT_SECONDS = 15


@app.post("/api/lab/paper/retry-entry")
def lab_paper_retry_entry(strategy: str):
    """Manually retries TODAY's missed entry for `strategy` using current quotes - never a real order (see
    paper.py's own module docstring). Marked invalid_reason since it cannot represent a normal 09:30 entry - see
    paper.py's PaperEngine.retry_missed_entry()."""
    tradeable = (*strategy_lab.STRATEGIES, strategy_lab.S5)
    if strategy not in tradeable:
        raise HTTPException(status_code=400, detail=f"strategy must be one of {list(tradeable)}")
    if paper_engine is None:
        raise HTTPException(status_code=503, detail="the paper engine is not running")
    tz = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
    now = datetime.now(tz)
    day = now.date().isoformat()
    reason = paper_engine.retry_missed_entry(strategy, day, now)
    # A contract the stream is not subscribed to yet (e.g. S5's roll into next week) has no quote on the first try;
    # that try itself asks the stream for it, which subscribes within ~5s - so wait a little before giving up.
    deadline = time.monotonic() + RETRY_QUOTE_WAIT_SECONDS
    while reason and reason.startswith("no fresh quote") and time.monotonic() < deadline:
        time.sleep(1)
        now = datetime.now(tz)
        reason = paper_engine.retry_missed_entry(strategy, day, now)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    return {"ok": True}


@app.post("/api/lab/paper/exit-now")
def lab_paper_exit_now(strategy: str, date: str):
    """Closes one open PAPER position now at the current quotes - never a real order. `date` is the trade's entry date
    (yesterday's for an overnight S5 position). Flagged as a manual exit - see paper.py's PaperEngine.exit_now()."""
    tradeable = (*strategy_lab.STRATEGIES, strategy_lab.S5)
    if strategy not in tradeable:
        raise HTTPException(status_code=400, detail=f"strategy must be one of {list(tradeable)}")
    day = _valid_iso_date(date, "date")
    if paper_engine is None:
        raise HTTPException(status_code=503, detail="the paper engine is not running")
    tz = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
    reason = paper_engine.exit_now(strategy, day, datetime.now(tz))
    deadline = time.monotonic() + RETRY_QUOTE_WAIT_SECONDS  # same short wait as a retried entry, for a quote still arriving
    while reason and reason.startswith("no fresh quote") and time.monotonic() < deadline:
        time.sleep(1)
        reason = paper_engine.exit_now(strategy, day, datetime.now(tz))
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    return {"ok": True}


@app.get("/api/lab/monthly")
@app.get("/lab/monthly")
def lab_monthly(source: str = "backtest", strategy: str | None = None, lots: int | None = None, qty: int | None = None, basis: str = "minute"):
    try:
        rows = strategy_lab.monthly(source, _lab_strategy(strategy), _lab_size(lots, qty), _lab_basis(basis))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return {"source": source, "paper": source == "paper", "rows": rows}


@app.get("/api/lab/export")
@app.get("/lab/export")
def lab_export(source: str = "all", strategy: str | None = None, lots: int | None = None, qty: int | None = None, basis: str = "minute"):
    """Every day's profit and loss - candle backtest and PAPER trades, all strategies - as one CSV at the chosen size."""
    size = _lab_size(lots, qty) or strategy_lab.Size(lots=strategy_lab.default_lots())
    try:
        text = strategy_lab.pnl_csv(size, source, _lab_strategy(strategy), _lab_basis(basis))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    unit = f"{size.lots}lot{'' if size.lots == 1 else 's'}" if size.lots is not None else f"{size.qty}qty"
    tag = "" if basis == "minute" else "_nse_daily"
    return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="strategy_pnl_{unit}{tag}.csv"'})


@app.get("/api/lab/day-chart")
@app.get("/lab/day-chart")
def lab_day_chart(date: str, strategy: str, source: str = "backtest", basis: str = "minute"):
    """What one strategy's strikes did on one day: each leg's 1-minute candles with the recorded entry and exit, the position's price and profit through the day, and NIFTY. Read-only."""
    try:
        return day_chart(_valid_iso_date(date, "date"), _lab_strategy(strategy) or "", source, basis)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


class LabBuildRequest(BaseModel):
    strategies: list[str] | None = None
    lots: int = 1


@app.post("/api/lab/backtest/build")
def lab_backtest_build(request: LabBuildRequest):
    """Builds the backtest history (the iron flies download their wing candles). Resumable; one build at a time."""
    if not access_token:
        raise HTTPException(status_code=401, detail="UPSTOX_ACCESS_TOKEN is not configured")
    names = request.strategies or None
    if names and (bad := [n for n in names if n not in strategy_lab.STRATEGIES]):
        raise HTTPException(status_code=400, detail=f"unknown strategy {bad}")
    return start_history_job(names, request.lots, lambda: os.getenv("UPSTOX_ACCESS_TOKEN"))


@app.get("/api/lab/backtest/status")
def lab_backtest_status():
    return history_job_status()


@app.post("/api/lab/s5-overnight/build")
def lab_s5_candles_build():
    """Downloads whatever next-day candles S5 is still missing (the "no_0930_next_day_candle" gap). Resumable; one build at a time."""
    if not access_token:
        raise HTTPException(status_code=401, detail="UPSTOX_ACCESS_TOKEN is not configured")
    return strategy_lab.start_s5_candles_job(lambda: os.getenv("UPSTOX_ACCESS_TOKEN"))


@app.get("/api/lab/s5-overnight/status")
def lab_s5_candles_status():
    return strategy_lab.s5_candles_job_status()


@app.post("/api/lab/s6-overnight/build")
def lab_s6_candles_build():
    """Downloads whatever candles S6 is still missing beyond S5's own build (the day-after-next exit candle, and
    a mid-hold roll's own entry/exit) - the "no_0930_midpoint_candle"/"no_0930_exit_candle" gaps. Resumable; one
    build at a time across every kind of build (history, gamma, S5, S6...)."""
    if not access_token:
        raise HTTPException(status_code=401, detail="UPSTOX_ACCESS_TOKEN is not configured")
    return strategy_lab.start_s6_candles_job(lambda: os.getenv("UPSTOX_ACCESS_TOKEN"))


@app.get("/api/lab/s6-overnight/status")
def lab_s6_candles_status():
    return strategy_lab.s6_candles_job_status()


@app.post("/api/lab/s7-overnight/build")
def lab_s7_candles_build():
    """Downloads whatever candles S7 is still missing beyond S5/S6's own builds (the third-trading-day exit
    candle, and either mid-hold roll's own entry/exit) - the "no_0930_midpoint_candle"/"no_0930_exit_candle"
    gaps. Resumable; one build at a time across every kind of build (history, gamma, S5, S6, S7...)."""
    if not access_token:
        raise HTTPException(status_code=401, detail="UPSTOX_ACCESS_TOKEN is not configured")
    return strategy_lab.start_s7_candles_job(lambda: os.getenv("UPSTOX_ACCESS_TOKEN"))


@app.get("/api/lab/s7-overnight/status")
def lab_s7_candles_status():
    return strategy_lab.s7_candles_job_status()


@app.post("/api/upstox/logout")
def upstox_logout():
    global access_token
    access_token = None
    return {"status": "disconnected"}
