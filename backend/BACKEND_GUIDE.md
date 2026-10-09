# Zero Backend Guide

This document explains the backend structure, functions, configuration, runtime behavior, and the safest places to edit.

cd D:\zero\backend
python -m uvicorn main:app --reload --port 8000


## Implementation Status

This table is the current source of truth for the NIFTY futures/options analysis work described later in this document.

| Area | Status | Current state |
|---|---|---|
| Backend-only service | Done | Frontend source and configuration have been removed. |
| Upstox OAuth callback | Done | OAuth routes and state validation are implemented. |
| Manual access-token loading | Done | `UPSTOX_ACCESS_TOKEN` is loaded from `.env` at startup. |
| Daily weekday scheduler | Done | One collection is scheduled at `COLLECTION_TIME`; weekends are skipped. |
| One-shot LTP collection | Done | `collector.py` fetches configured instrument LTP values. |
| Local JSON persistence | Done | Snapshots are written under `DATA_DIR`. |
| Optional S3 persistence | Done | Set `AWS_S3_BUCKET` to upload snapshots. |
| Continuous WebSocket feed | Foundation done | `market_stream.py` uses the official Upstox V3 SDK with opt-in startup and auto-reconnect. |
| NIFTY futures data | Not started | No futures contract discovery or futures history exists. |
| Dynamic option universe | Stream startup done | `instruments.py` discovers NIFTY futures and CE/PE contracts before the stream starts. |
| Option volume and OI | Foundation done | Full-feed parsing extracts volume and OI when Upstox includes those fields. |
| Historical sessions | Expanded | Active and expired Upstox candles can be downloaded on request. |
| Derived analytics | Foundation done | Session analytics calculate cumulative volume, configurable flow, spike levels, OI change, PCR, and max pain. |
| Raw stream persistence | Done | Stream events are normalized and appended to date-partitioned JSONL files. |
| Missing-tick detection | Done | `StreamMonitor` records per-instrument gaps above `STREAM_GAP_SECONDS`. |
| Structured observations | Foundation done | SQLite stores nullable LTP, OHLC, volume, and OI fields alongside raw payloads. |
| Synchronized analysis UI | Foundation done | The Vite/React console is restored with historical controls, analytics panels, collector status, and empty/loading/error states. |
| Holiday and token reliability | Foundation done | Configured holidays are skipped and token expiry metadata is exposed safely. |

### Completion Checklist

Complete these in order:

- [x] Backend service starts and exposes health/auth/profile routes.
- [x] Weekday scheduler and manual collection endpoint exist.
- [x] Local JSON and optional S3 snapshot persistence exist.
- [x] Add the first Upstox WebSocket ingestion layer with reconnect logging.
- [x] Persist normalized WebSocket messages as date-partitioned JSONL.
- [x] Add missing-tick detection and gap metrics.
- [x] Discover NIFTY instruments through the Upstox search API.
- [x] Add read-only NIFTY instrument search with expiry and ATM filters.
- [x] Replace static stream keys with discovered contracts at stream startup.
- [x] Periodically rediscover and resubscribe when ATM/contract keys change.
- [x] Add queryable SQLite storage for raw stream messages and gap events.
- [x] Store raw futures and structured observation fields when present.
- [x] Validate and enrich strike-level option volume and OI fields from full-feed payloads.
- [x] Add historical-session and expiry discovery APIs.
- [x] Download and persist Upstox V3 historical candles on request.
- [x] Discover expired NIFTY option contracts and monthly futures by expiry date.
- [x] Download and persist expired-contract candle data.
- [x] Add cumulative-volume, interval-flow, PCR, and max-pain calculations.
- [x] Add configurable time-window flow, baseline spike levels, and delta-OI intervals.
- [x] Rebuild the analysis frontend foundation against the backend data contract.
- [ ] Add synchronized crosshair, chart zoom/pan, and richer data-integrity warnings.
- [ ] Add market-holiday handling, token-expiry alerts, retention rules, and deployment monitoring.
- [x] Add configurable market-holiday skipping and token-status reporting.
- [ ] Add external token-expiry alerts, retention policies, and deployment monitoring.

The current implementation is therefore a working collection foundation, not yet the complete NIFTY analysis application.

Reliability settings:

```env
MARKET_HOLIDAYS=2026-10-02,2026-10-20
```

The scheduler skips weekends and dates in `MARKET_HOLIDAYS`. Check token state without exposing the credential:

```text
GET /api/auth/token-status
GET /api/health
```

### Stream monitoring

`stream_monitor.py` tracks the last received event for each instrument. When the interval exceeds `STREAM_GAP_SECONDS`, it writes a gap event to:

```text
STREAM_DATA_DIR/gaps-YYYY-MM-DD.jsonl
```

Inspect the current in-memory status with:

```text
GET /api/collector/stream-status
```

## What This Service Does

The backend is a FastAPI service that can:

- Authenticate with Upstox using OAuth 2.0.

- Accept a manually configured Upstox access token.

- Fetch a user profile from Upstox.

- Fetch LTP market quotes for configured instrument keys.

- Save market snapshots as local JSON files.

- Optionally upload snapshots to Amazon S3.

- Run one market-data collection on weekday mornings.

The frontend has been removed. The backend must run continuously for the scheduled collection to execute.

## File Structure

```text

backend/

  main.py              API routes, OAuth flow, scheduler, application startup

  collector.py         Upstox market quote request and snapshot creation

  storage.py           Local JSON and optional S3 persistence

    market_stream.py     Upstox V3 WebSocket connection and reconnect handling

    instruments.py       Upstox futures/options instrument discovery

  requirements.txt     Python dependencies

  .env                 Local secrets and runtime configuration, do not commit

  .env.example         Safe configuration template

  data/                Local snapshots, created at runtime

```

## Runtime Flow

```text

FastAPI starts

    |

    +--> lifespan() starts morning_collector()

    |

    +--> API routes become available

At COLLECTION_TIME on weekdays

    |

    +--> Read UPSTOX_ACCESS_TOKEN

    +--> collect_quotes()

    +--> save_snapshot()

    +--> Write local JSON

    +--> Upload to S3 when AWS_S3_BUCKET is configured

```

The scheduler does not perform an invisible Upstox login. Standard Upstox access tokens expire, and OAuth may require user approval. For unattended read-only market data, use an Upstox Analytics Token where the required endpoint supports it, or implement Upstox's approved notifier-webhook flow.

## `market_stream.py`

### `stream_instruments()`

```python
def stream_instruments() -> list[str]
```

Reads the comma-separated `UPSTOX_INSTRUMENT_TOKENS` setting for the WebSocket subscription.

### `run_market_stream()`

```python
def run_market_stream(access_token: str, on_message: Callable[[Any], None]) -> None
```

Uses the official `upstox-python-sdk` `MarketDataStreamerV3` interface. It subscribes to the configured instruments, selects `UPSTOX_STREAM_MODE`, enables reconnect attempts, and forwards events to the supplied callback.

When dynamic discovery is enabled, the stream refreshes its subscriptions every `UPSTOX_RESUBSCRIBE_SECONDS` seconds. Removed contracts are unsubscribed and newly discovered contracts are subscribed without restarting the backend. The default interval is 300 seconds.

The stream is enabled only when:

```env
UPSTOX_ENABLE_STREAM=true
```

Supported modes depend on the Upstox SDK, including `ltpc`, `full`, `option_greeks`, and `full_d30`. The current default is `ltpc`.

Current limitation: `handle_stream_message()` only logs messages at debug level. The next implementation step is to normalize and persist those messages.

## `main.py`

### Environment loading

```python

load_dotenv(override=True)

```

Loads `backend/.env` and gives it priority over stale environment variables inherited by the shell. Restart the process after changing `.env`.

### `morning_collector()`

```python

async def morning_collector() -> None

```

Runs forever as a background task.

Behavior:

1. Reads `TIMEZONE`, defaulting to `Asia/Kolkata`.

2. Reads `COLLECTION_TIME`, defaulting to `09:15`.

3. Calculates the next scheduled run.

4. Skips Saturday and Sunday.

5. Reads `UPSTOX_ACCESS_TOKEN`.

6. Calls `collect_quotes()` in a worker thread.

7. Logs failures without stopping the FastAPI server.

Edit this function when changing:

- Schedule rules.

- Weekday/holiday behavior.

- Retry policy.

- Multiple daily collection times.

- Token-refresh behavior.

Current limitation: weekends are skipped, but exchange holidays are not checked. Add an exchange-holiday calendar before treating this as a complete market calendar.

### `lifespan()`

```python

@asynccontextmanager

async def lifespan(_: FastAPI)

```

Starts `morning_collector()` when FastAPI starts and cancels it cleanly during shutdown.

Edit this function when adding other long-running background tasks, such as:

- WebSocket market streaming.

- Health monitoring.

- Periodic token checks.

- Queue consumers.

### `upstox_request()`

```python

def upstox_request(

    url: str,

    *,

    data: dict[str, str] | None = None,

    token: str | None = None,

)

```

Shared HTTP helper for Upstox requests.

Features:

- Sends JSON headers for GET requests.

- Sends form-encoded data for token exchange.

- Adds `Authorization: Bearer ...` when a token is supplied.

- Uses a stable application `User-Agent`.

- Converts upstream HTTP failures into FastAPI `502` errors.

- Logs safe diagnostic information for OAuth failures.

- Supports Upstox and Cloudflare error response formats.

Never log `client_secret`, `access_token`, or the full authorization code here.

Edit this function when changing:

- Timeout behavior.

- Retry/backoff behavior.

- Common headers.

- Upstox error parsing.

- HTTP client implementation.

### `health()`

```python

@app.get("/api/health")

def health()

```

Returns:

```json

{"status": "ok"}

```

Use this endpoint for AWS load-balancer health checks and uptime monitoring.

### `upstox_login()`

```python

@app.get("/api/upstox/login")

def upstox_login()

```

Starts OAuth authorization.

It creates a random `state` value and redirects the browser to Upstox with:

- `response_type=code`

- `client_id`

- `redirect_uri`

- `state`

The redirect URI must exactly match the URI configured in the Upstox Developer App.

### `upstox_callback()`

```python

def upstox_callback(

    code: str | None = None,

    state: str | None = None,

)

```

Registered at these paths for compatibility:

```text

/api/upstox/callback

/api/auth/callback

/callback

```

Behavior:

1. Validates the one-time authorization code and OAuth state.

2. Reads `UPSTOX_CLIENT_ID` and `UPSTOX_CLIENT_SECRET`.

3. Exchanges the code at Upstox's token endpoint.

4. Stores the returned access token in process memory.

5. Redirects to `FRONTEND_URL` with a success or error result.

The access token is not persisted across restarts. For a hosted service, replace the in-memory assignment with encrypted storage such as AWS Secrets Manager or a database with encryption at rest.

### `upstox_profile()`

```python

@app.get("/api/upstox/profile")

def upstox_profile()

```

Calls Upstox `/v2/user/profile` using the current in-memory token and returns the response's `data` object.

Responses:

- `200`: profile data returned.

- `401`: backend has no access token.

- `502`: Upstox rejected the token or could not be reached.

### `run_collector()`

```python

@app.post("/api/collector/run")

def run_collector()

```

Runs market collection immediately instead of waiting for the morning schedule.

Example:

```powershell

curl.exe -X POST http://127.0.0.1:8000/api/collector/run

```

Use this endpoint to test credentials, instrument keys, local storage, and S3 configuration.

### `upstox_logout()`

```python

@app.post("/api/upstox/logout")

def upstox_logout()

```

Clears the in-memory access token. It does not revoke the token at Upstox. Add the documented Upstox logout/revoke request here if remote invalidation is required.

## `collector.py`

### `configured_instruments()`

```python

def configured_instruments() -> list[str]

```

Reads `UPSTOX_INSTRUMENT_TOKENS`, splits on commas, trims whitespace, and removes empty values.

Example:

```env

UPSTOX_INSTRUMENT_TOKENS=NSE_EQ|INE009A01021,NSE_EQ|INE002A01018

```

The values must be valid Upstox instrument keys. Do not use display names such as `SBIN` unless the Upstox endpoint specifically accepts them.

### `collect_quotes()`

```python

def collect_quotes(access_token: str, captured_at: datetime) -> str | None

```

Fetches LTP data from:

```text

GET https://api.upstox.com/v2/market-quote/ltp

```

Behavior:

1. Reads configured instruments.

2. Skips and logs when no instruments are configured.

3. Sends the bearer token to Upstox.

4. Stores the raw quote response in a snapshot object.

5. Calls `save_snapshot()`.

6. Returns the saved file path.

Edit this function to add:

- Historical candles.

- OHLC data.

- Option chains.

- Multiple Upstox endpoints.

- Response validation.

- Retry handling.

- Data normalization before storage.

The current HTTP collector takes one snapshot. The WebSocket collector uses `full` mode by default so volume and OI fields can be normalized when available. `ltpc` mode is price-only and is not sufficient for option-volume/OI analytics.

## `storage.py`

### `save_snapshot()`

```python

def save_snapshot(

    snapshot: dict[str, Any],

    captured_at: datetime,

) -> Path

```

Writes a formatted JSON snapshot to:

```text

DATA_DIR/market-YYYY-MM-DDTHH-MM-SS.json

```

Default directory:

```text

backend/data/

```

When `AWS_S3_BUCKET` is set, it also uploads the file to:

```text

market-data/date=YYYY-MM-DD/<filename>

```

AWS credentials should come from an IAM role on AWS or the AWS CLI credential chain. Do not place AWS keys in source code or commit them to `.env`.

### SQLite raw stream store

`append_stream_message()` also writes each normalized event to `MARKET_DATABASE` (default `data/market.db`). The `stream_messages` table is indexed by `received_at` and `instrument_key`; gap events are stored in `stream_gaps`.

Raw messages can be queried through:

```text
GET /api/market/messages?start=2026-09-07T09:15:00+05:30&limit=1000
```

Optional filters are `start`, `end`, and `instrument_key`. The `market_observations` table now stores `trading_date`, `ltp`, `open`, `high`, `low`, `close`, `volume`, and `oi` when those fields are present in the feed. Raw payloads are retained for later parser improvements.

Historical data endpoints:

```text
GET /api/market/sessions/{trading_date}
GET /api/instruments/expiries
```

Download candles for one instrument and date:

```text
GET /api/market/sessions/2026-09-01?instrument_key=NSE_INDEX%7CNifty%2050&download=true&unit=minutes&interval=1
```

The download uses Upstox V3 `/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}` and stores OHLC, close-as-LTP, volume, and OI in `market_observations`.

Historical session queries return locally stored data after download. The service still does not automatically discover every historical contract for a date or validate exchange holidays.

Session analytics are available at:

```text
GET /api/analytics/sessions/{trading_date}
```

The endpoint requires contract metadata to have been discovered first. It returns CE/PE cumulative volume, configurable interval flow, per-strike OI change, OI totals, PCR, max pain, spike levels, and a timestamped timeline.

Choose the flow window with `flow_seconds`:

```text
GET /api/analytics/sessions/2026-09-07?flow_seconds=5
GET /api/analytics/sessions/2026-09-07?flow_seconds=60
```

Each timeline record includes `interval_volume`, `spike_level` (`normal`, `elevated`, or `significant`), `oi`, and `delta_oi`. The baseline is intentionally simple and should be replaced with a session-aware statistical baseline when enough historical data is available.

## Expired NIFTY options and monthly futures

Use the expired-instrument flow for historical contracts:

```text
GET /api/instruments/expired/expiries?instrument_key=NSE_INDEX%7CNifty%2050
GET /api/instruments/expired/options?instrument_key=NSE_INDEX%7CNifty%2050&expiry_date=2026-09-24
GET /api/instruments/expired/futures?instrument_key=NSE_INDEX%7CNifty%2050&expiry_date=2026-09-24
```

The futures endpoint returns the NIFTY future contract for the requested expiry, including monthly expiries when that date is monthly. Use the returned `instrument_key` as `expired_instrument_key`:

```text
GET /api/market/expired-candles?expired_instrument_key=NSE_FO%7C12345%7C24-09-2026&from_date=2026-09-01&to_date=2026-09-24&interval=30minute
```

Supported expired-candle intervals are Upstox values such as `1minute`, `3minute`, `5minute`, `15minute`, `30minute`, and `day`. The returned candles are stored in `market_observations` with OHLC, volume, and OI.

Edit this function to switch to:

- Parquet files.

- DynamoDB.

- PostgreSQL/RDS.

- A time-series database.

- Compression.

- Partitioning by symbol or exchange.

## Configuration

Copy `.env.example` to `.env` and fill in real values:

| Variable | Required | Purpose |

|---|---:|---|

| `UPSTOX_CLIENT_ID` | OAuth only | Upstox API key for the app |

| `UPSTOX_CLIENT_SECRET` | OAuth only | Upstox API secret |

| `UPSTOX_REDIRECT_URI` | OAuth only | Exact registered callback URL |

| `UPSTOX_ACCESS_TOKEN` | Collector | Token used for market-data requests |

| `UPSTOX_INSTRUMENT_TOKENS` | Collector | Comma-separated instrument keys |

| `COLLECTION_TIME` | No | Local time for collection, default `09:15` |

| `TIMEZONE` | No | IANA timezone, default `Asia/Kolkata` |

| `DATA_DIR` | No | Local snapshot directory, default `data` |

| `AWS_S3_BUCKET` | No | Enables S3 upload when set |

| `FRONTEND_URL` | OAuth callback | Redirect target after OAuth |

## Authentication Modes

### Manual token

Set:

```env

UPSTOX_ACCESS_TOKEN=your_token

```

This is simplest for testing. The token expires according to Upstox policy and must be replaced when expired.

### OAuth + TOTP

TOTP is entered on Upstox's own login page. The backend never receives the TOTP secret. OAuth still requires a user-driven authorization step and is not fully unattended.

### Automatic token renewal

Upstox access tokens always expire at 03:30 IST and Upstox issues no `refresh_token` grant — there is no supported way to renew silently. `auto_login.py` scripts the same TOTP login a person does by hand, using the third-party `upstox-totp` package, and is **off by default**.

Enabling it means storing your actual Upstox login (mobile number, password, PIN, TOTP secret) in `backend/.env`, not just a read-only access token. Anyone who reads that file could trade on the account, not just read market data. Only enable it if you accept that trade-off and keep `.env` off shared machines.

To enable, add to `.env`:

```env
UPSTOX_AUTO_LOGIN_ENABLED=true
UPSTOX_USERNAME=9876543210
UPSTOX_PASSWORD=...
UPSTOX_PIN_CODE=...
UPSTOX_TOTP_SECRET=...
UPSTOX_TOKEN_RENEWAL_TIME=03:40
```

`UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET`, and `UPSTOX_REDIRECT_URI` are reused from the existing OAuth config. When enabled, a background task logs in once daily at `UPSTOX_TOKEN_RENEWAL_TIME` (IST), updates the in-memory token, and rewrites `UPSTOX_ACCESS_TOKEN` in `.env` so a restart doesn't lose it. Trigger a renewal immediately (e.g. to verify credentials) with:

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/upstox/auto-login
```

`GET /api/health` reports `auto_login_enabled` so the frontend/ops tooling can tell whether unattended renewal is configured.

### Hosted unattended collection

For a hosted service:

1. Prefer an Upstox Analytics Token where the required read-only market-data endpoint supports it.

2. Store tokens in AWS Secrets Manager rather than `.env`.

3. Use IAM roles for S3 access.

4. Run the backend as ECS/Fargate, EC2 systemd, or another always-on service.

5. Add token-expiry monitoring and alerts.

6. Use the approved Upstox notifier-webhook flow when daily approval is required.

## Running Locally

```powershell

cd D:\zero\backend

.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

uvicorn main:app --reload --port 8000

```

The scheduler only runs while this process is alive.

## AWS Deployment Checklist

- Build a small Docker image or deploy the backend on ECS/Fargate.

- Put the service behind an HTTPS load balancer.

- Set `/api/health` as the health check.

- Store Upstox credentials in AWS Secrets Manager.

- Attach an IAM role allowing `s3:PutObject` only to the market-data prefix.

- Set `AWS_S3_BUCKET`, `UPSTOX_INSTRUMENT_TOKENS`, `COLLECTION_TIME`, and `TIMEZONE` as task environment variables.

- Send application logs to CloudWatch.

- Add an alert when collection fails or a token is near expiry.

- Test with Upstox Sandbox before enabling live trading features.

## Common Status Codes

| Status | Meaning |

|---:|---|

| `200` | Request succeeded |

| `307` | Expected redirect during OAuth |

| `401` | No token is loaded or token was cleared |

| `502` | Upstox rejected the request or could not be reached |

| `500` | Local configuration or application error |

## Safe Change Order

When changing behavior:

1. Update `.env.example` if a new variable is introduced.

2. Update the relevant module only.

3. Add or update a focused test.

4. Run `python -m py_compile main.py collector.py storage.py`.

5. Test `/api/health`.

6. Run `POST /api/collector/run` with a non-production token.

7. Check the created local snapshot before enabling S3 or live data collection.

Never commit `.env`, access tokens, API secrets, AWS credentials, or real user profile data.

NIFTY 50 Futures + Options Flow & OI Analysis — Required Changes

This section defines the product changes required for the NIFTY 50 market-analysis screen discussed after the original backend guide.

1. Product Goal

Build a synchronized intraday research screen that lets the trader compare:

NIFTY 50 Futures price movement.

Cumulative CE option volume.

Cumulative PE option volume.

Short-interval CE/PE volume flow to identify spikes.

Strike-wise CE/PE Open Interest.

Change in Open Interest.

The futures reaction at the exact time of an option-volume or OI event.

All charts must share the same time axis and crosshair so a specific second can be inspected across all panels.

The screen is primarily a research/analysis tool, not an order-entry or trading screen.

2. Historical Date Selection

Add a global date selector.

Example:

Date: 01 Sep 2026

The user must be able to select a previous trading day and analyse the complete session for that date.

Required behavior:

Support live/current-session mode.

Support historical-session mode.

When a historical date is selected, all displayed market data must belong to that selected date.

Do not mix the selected historical option data with the current day's futures data.

NIFTY futures must use the appropriate futures contract/data for the selected date.

Display the selected date prominently in the header.

Clearly indicate LIVE or HISTORICAL mode.

Disable live-only controls when historical data is being viewed if they do not apply.

Provide previous/next trading-day navigation where practical.

Handle weekends and exchange holidays correctly.

Example:

NIFTY 50 Futures
Date: 01 Sep 2026
Mode: HISTORICAL
Session: 09:15 — 15:30

3. Futures Chart

Keep NIFTY 50 Futures as the primary chart at the top.

Display:

OHLC/candlestick chart.

LTP/current/selected-time price.

Session high/low.

Futures volume if available.

Time axis from 09:15 to 15:30.

Shared crosshair.

For historical mode, the futures chart must represent the selected historical trading session.

The current backend only collects LTP snapshots and explicitly identifies historical candles/OHLC as future work, so the collector must be expanded accordingly.

4. Options Universe — Number of Strikes

Add a configurable strike-count selector.

Default:

5 strikes

The user must be able to select values such as:

1
2
3
5
10
15
20
Custom

Interpret the selection as:

ATM ± N strikes

Example:

ATM = 25,500
N = 5

Analyse:
25,250
25,300
25,350
25,400
25,450
25,500
25,550
25,600
25,650
25,700
25,750

The exact number of strikes shown should be clearly defined in the UI so there is no ambiguity between 5 strikes total and ATM ± 5.

Recommended UI label:

Strikes: ATM ± 5

Use the same selected option universe for:

Cumulative CE volume.

Cumulative PE volume.

Volume-flow calculations.

Strike-wise OI.

Change in OI.

The user must be able to change the strike count without changing the selected date.

5. ATM Determination

For each timestamp/session:

Determine the relevant NIFTY ATM strike from the underlying/futures/spot price according to the selected methodology.

The ATM reference should be visible on the OI chart.

When the market moves enough to change ATM, the option universe must be recalculated according to the selected strike-count rule.

Historical analysis must use the price/ATM applicable to that historical timestamp rather than today's ATM.

Display:

Spot / Futures LTP: 25,486.5
ATM: 25,500
Universe: ATM ± 5

6. Cumulative Option Volume

Add a dedicated OPTIONS VOLUME panel.

Plot two cumulative lines:

CE Cumulative Volume
PE Cumulative Volume

Important rule:

Cumulative volume must never decrease during a session.

For every timestamp:

Cumulative volume(t)
=
sum of option volume for the selected CE/PE strikes
from session start through t

Do NOT calculate the displayed cumulative series as:

current volume - previous volume

That subtraction belongs to the separate interval-flow calculation.

The cumulative chart should make it easy to see:

Which side accumulated more activity.

When the slope increased.

Whether CE or PE activity accelerated.

How option activity developed through the session.

7. Option Volume Flow / Spike Detection

Below or within the options-volume panel, add a separate interval-flow view.

This represents:

Volume Flow(t)
=
Cumulative Volume(t)
-
Cumulative Volume(t - interval)

Supported intervals:

1 sec
5 sec
15 sec
1 min

Default:

5 sec

Display CE and PE as bars.

Example:

CE: ███████████
PE: ███

This is the chart that should move up and down.

The purpose is to identify sudden bursts of option activity.

Spike detection

Add a configurable spike/high-volume indication.

A spike should be based on the selected interval volume relative to a recent baseline, rather than simply marking every large absolute bar.

At minimum the UI should visually identify:

Normal flow.

Elevated flow.

Significant spike.

The exact statistical threshold can be configurable later.

8. Synchronized Event Analysis

All panels must use one shared time coordinate.

When the user moves the mouse/crosshair to:

11:42:17

show the corresponding values simultaneously for:

NIFTY Futures LTP
CE cumulative volume
PE cumulative volume
CE interval volume
PE interval volume
CE OI
PE OI
CE ΔOI
PE ΔOI

The vertical crosshair should extend through all charts.

This is one of the most important UX requirements.

The purpose is to let the trader visually answer:

"At the exact second when option volume spiked, what did NIFTY Futures do and what happened to OI?"

9. Open Interest — Strike-Wise View

Add an OPTIONS OPEN INTEREST panel based on the strike-wise OI layout.

The visualization should resemble:

Strike       CE OI       PE OI

25,300       ███         ███████
25,350       █████       ████████
25,400       ███████     ███████████
25,450       █████████   █████████████
25,500       ████████    ██████
25,550       ██████████  ███████
25,600       █████████   ████

Show the ATM/spot reference as a vertical marker.

The chart must support:

CE & PE
CE
PE

10. OI Metric Controls

The OI section should support:

OI
Change in OI
PCR
Max Pain

OI is the default.

OI

Show current/selected-time strike-wise call and put open interest.

Change in OI

Show change in OI for the selected time interval/session.

Preferably support:

ΔOI 1 min
ΔOI 5 min
ΔOI 15 min
ΔOI since session open

Default:

5 min

PCR

Provide strike/universe-based Put-Call Ratio using the same selected option universe.

Max Pain

Calculate/display max pain for the selected expiry and option universe as appropriate.

Historical mode must calculate these values from the historical dataset rather than current live values.

11. Historical Option Volume

Historical mode must preserve the same calculations as live mode.

For example, selecting:

Date: 01 Sep 2026
Expiry: 03 Sep 2026
Strikes: ATM ± 5

should produce:

01 Sep session
    ↓
Historical futures data
    ↓
Historical option trades/volume
    ↓
Cumulative CE/PE volume
    ↓
5-sec CE/PE volume flow
    ↓
Historical OI / ΔOI

The user should be able to move the crosshair through the entire historical session.

12. Expiry Selection

Add an expiry selector.

Examples:

Current Weekly
Next Weekly
Monthly
Specific Expiry

For historical mode, expiry selection must be valid for the selected historical date.

Do not silently use the current expiry when analysing an older session.

The available expiry/contract list should come from the appropriate Upstox instrument data.

13. Data Collection / Backend Changes

The existing backend currently:

Fetches LTP market quotes.

Saves snapshots as JSON.

Can upload snapshots to S3.

Runs a scheduled collection.

Is not currently a real-time WebSocket collector.

The existing guide explicitly identifies historical candles, OHLC, option chains, multiple Upstox endpoints, validation, retries, and normalization as additions to collect_quotes(). Those changes are now required for this project.

The real-time analysis requirement means the backend should be extended to maintain market data at approximately one-second resolution during the trading session.

14. Real-Time Data

Add a real-time market-data collection layer.

Preferred architecture:

Upstox WebSocket
       |
       v
Market Data Collector
       |
       +--> NIFTY Futures
       |
       +--> Selected NIFTY Options
       |
       v
Normalized 1-second data
       |
       +--> Live API/WebSocket
       |
       +--> Historical storage

Do not repeatedly poll every instrument with independent HTTP requests if the appropriate Upstox streaming endpoint can provide the required data.

The existing backend guide states that the current collector is not a real-time WebSocket collector; this needs to change for the intended application.

15. Instrument Management

The backend needs to dynamically determine the required option instruments based on:

Underlying: NIFTY 50
Date/session
Expiry
ATM
Strike count
CE/PE

Do not hard-code a permanent list of option instrument keys.

The instrument universe should be recalculated when:

Date changes.

Expiry changes.

Strike count changes.

ATM changes materially.

A new trading session starts.

Upstox option contracts expose fields including instrument key, expiry, instrument type, strike price, underlying key, and lot size, which can be used to build this universe.

16. Storage Model

The existing backend stores market snapshots as timestamped JSON files.

For this application, the data volume and second-level analysis make a structured/time-series storage model preferable.

The existing guide already identifies Parquet, DynamoDB, PostgreSQL/RDS, and a time-series database as possible storage changes.

Minimum logical data model should support:

Futures tick/session data

timestamp
trading_date
instrument_key
ltp
open
high
low
close
volume

Option data

timestamp
trading_date
expiry
strike
option_type
instrument_key
ltp
volume
oi

Derived data

timestamp
trading_date
expiry
strike_universe
ce_cumulative_volume
pe_cumulative_volume
ce_volume_flow
pe_volume_flow
ce_oi
pe_oi
ce_delta_oi
pe_delta_oi

Raw data should preferably be retained so derived calculations can be recalculated later.

17. Data Integrity

The system must distinguish between:

Raw market data
Derived analytical data
UI aggregation

Do not permanently store only the aggregated ATM ± N values.

Retain strike-level raw data so the user can later change:

ATM ± 5

to:

ATM ± 10

without needing to recollect the historical market session.

This is especially important for historical analysis.

18. UI Header

Recommended header:

NIFTY 50

[Futures] [Options Flow]

Date: [01 Sep 2026]
Expiry: [03 Sep 2026]
Strikes: [ATM ± 5]

[1m] [5m] [15m] [1h]

LIVE / HISTORICAL

For the current session:

● MARKET OPEN
09:15 — 15:30

For historical:

HISTORICAL
01 Sep 2026

19. Main Screen Layout

Recommended order:

┌────────────────────────────────────────────────────────────┐
│ HEADER / DATE / EXPIRY / STRIKE COUNT / TIMEFRAME         │
├────────────────────────────────────────────────────────────┤
│ NIFTY 50 FUTURES                                          │
│ Price / Candles / Futures Volume                          │
├────────────────────────────────────────────────────────────┤
│ OPTIONS VOLUME                                            │
│ CE cumulative                                             │
│ PE cumulative                                             │
│                                                            │
│ VOLUME FLOW                                                │
│ CE / PE interval bars                                     │
├────────────────────────────────────────────────────────────┤
│ OPTIONS OPEN INTEREST — BY STRIKE                         │
│ CE / PE bars + ATM marker                                 │
└────────────────────────────────────────────────────────────┘

Do not add a permanent right sidebar for option flow, recent events, tips, or a CE/PE volume-ratio card.

The main objective is to maximize synchronized chart width.

20. Chart Interaction

Required interactions:

Shared vertical crosshair.

Hover tooltip.

Click a timestamp to lock the crosshair.

Zoom horizontally.

Pan through historical sessions.

Reset zoom.

Fullscreen chart mode.

Toggle cumulative/interval volume.

Change interval without changing the selected date.

Change strike universe without losing the current timestamp.

21. Current-Time Indicator

In live mode, show a clear right-edge current-time marker.

In historical mode, show the selected playback/analysis timestamp.

Do not imply that historical data is live.

22. Loading and Data States

The UI must clearly handle:

Loading historical data...
Connecting to market feed...
Live data connected
Historical data loaded
Partial data
No data
Market closed
Token expired
Upstox unavailable

If data is incomplete, display a warning instead of silently presenting misleading calculations.

23. Performance Requirements

The screen should remain responsive while receiving approximately one-second market updates.

Avoid recalculating the entire historical dataset on every incoming tick.

Use incremental calculations for:

Cumulative volume.

Interval volume.

OI changes.

Spike detection.

Historical calculations should be cached where appropriate.

24. Configuration Defaults

Recommended defaults:

Mode: Live
Date: Current trading day
Expiry: Current weekly
Strike universe: ATM ± 5
Volume display: Cumulative + 5 sec flow
OI view: OI
OI side: CE & PE
ΔOI interval: 5 min
Futures timeframe: 1 min

The user can change any of these independently.

25. Important Calculation Rule

Do not confuse these two values:

Cumulative volume

Cumulative(t)
=
Cumulative(t-1) + NewVolume(t)

This should continuously increase or remain flat.

Interval volume

Flow(t)
=
Cumulative(t) - Cumulative(t-interval)

This can move up and down and is the value used for spike detection.

Both should be displayed because they answer different questions.

26. Recommended Phase Order

Phase 1 — Data foundation

Real-time WebSocket collection.

Historical session collection/storage.

NIFTY futures data.

NIFTY option strike-level data.

OI.

Volume.

Timestamp normalization.

Phase 2 — Core analytics

Dynamic ATM calculation.

Dynamic strike universe.

Cumulative CE/PE volume.

1s/5s/15s/1m volume flow.

OI.

ΔOI.

PCR.

Max Pain.

Spike detection.

Phase 3 — UI

Futures chart.

Options cumulative-volume chart.

Volume-flow bars.

Strike-wise OI chart.

Historical date selector.

Expiry selector.

Strike-count selector.

Shared crosshair.

Phase 4 — Historical research

Date navigation.

Historical playback/inspection.

Cached calculations.

Data completeness indicators.

Historical expiry validation.

Phase 5 — Reliability

Reconnection handling.

Missing-tick handling.

Market-calendar handling.

Token-expiry monitoring.

Data integrity checks.

Storage/retention management.

27. Final UX Principle

The screen should make this sequence visually obvious:

OPTION VOLUME SPIKE
        ↓
WHEN DID IT HAPPEN?
        ↓
WHAT DID NIFTY FUTURES DO?
        ↓
DID OI CHANGE?
        ↓
WHICH STRIKES CONTRIBUTED?

The trader should be able to answer all five questions by moving one synchronized crosshair across the screen.

Existing Backend Items That Need Updating

The original backend guide currently describes a one-shot LTP snapshot collector and explicitly says it is not a real-time WebSocket collector. It also lists historical candles, OHLC, option chains, multiple Upstox endpoints, validation, retry handling, and data normalization as future additions.

For this project, those items should now be treated as implementation requirements rather than optional future work.

The scheduler should remain useful for background/maintenance jobs, but the market-session collector should operate continuously during the NIFTY trading session.

The storage layer should be upgraded from simple timestamped snapshots when necessary to support second-level historical analysis efficiently.

The backend should continue following the existing security requirements: never log or commit access tokens, client secrets, AWS credentials, or other secrets.

## NIFTY 50 — Options Flow & OI Research Console: UI/UX Specification

This section is the UI/UX specification for the trader-facing research screen described above. It defines layout, interaction, and visual rules for implementation; it does not change any backend API contract on its own.

A reference mockup (desktop, 1920x1080, dark theme) was supplied alongside this spec showing the header bar, session stats strip, stacked futures/volume/flow/OI panels, crosshair tooltip, data-state legend, interaction legend, color tokens, and responsive notes. Ask the person who supplied this guide for the image file if you need to see it again; it was not saved into the repository as part of this edit.

### 1. Product purpose

This is a single-screen market research and inspection tool for an expert derivatives trader.

It is not:

- an order-entry terminal
- a portfolio screen
- a P&L screen
- a position monitor
- a generic dashboard
- an educational options interface

There should be no Buy/Sell buttons, order tickets, positions, P&L, watchlists or unnecessary cards.

The primary task is:

Find an unusual option-volume event and determine what NIFTY futures and strike-level OI did at exactly that moment.

The UI should therefore optimize for:

Volume spike → timestamp → Futures reaction → OI change → contributing strikes

### 2. Historical mode is a first-class workflow

When the user wants to investigate an old session, they should not feel like they are switching to a completely different application.

The same analytical screen should operate in two modes:

```text
LIVE
LIVE ●
11:42:17 IST
Today's session
```

```text
HISTORICAL
HISTORICAL
01 Sep 2026
09:15 — 15:30 IST
```

The charts, calculations and interactions remain the same. Only the data source/time context changes.

### 3. Header

Keep the header compact. Do not make it a large dashboard header.

Recommended (historical):

```text
NIFTY 50    HISTORICAL

‹   01 Sep 2026   ›

Expiry   [ 03 Sep 2026 ▼ ]
Strikes  [ ATM ± 5 ▼ ]
Futures  [ 1m ▼ ]
Flow     [ 5s ▼ ]

                     Session 09:15 — 15:30
                     [ Load / Reload Data ]
```

Recommended (live):

```text
NIFTY 50    ● LIVE

Expiry [ Current Weekly ▼ ]
Strikes [ ATM ± 5 ▼ ]
Futures [ 1m ▼ ]
Flow [ 5s ▼ ]

09:15 — 15:30
```

Mode treatment:

- LIVE: green indicator, subtle pulse, label "LIVE".
- HISTORICAL: amber/yellow indicator, no pulse, label "HISTORICAL".

This distinction must be obvious without reading the text carefully.

### 4. Historical date selection

The date selector should not behave like a plain calendar. The user is selecting a trading session.

```text
‹   Mon, 01 Sep 2026   ›
```

The arrows navigate to the previous/next trading session, not simply the previous/next calendar day. Weekends and configured exchange holidays should be skipped.

If there is no data:

```text
01 Sep 2026

NO DATA

No historical market data is available
for this session.
```

Don't show empty charts pretending that the data exists.

### 5. Load Data interaction

For historical mode, use a dedicated Load Data action:

```text
HISTORICAL

Date       01 Sep 2026
Expiry     03 Sep 2026
Strikes    ATM ± 5

[ Load Session ]
```

After clicking:

```text
LOADING HISTORICAL SESSION

✓ Session identified
✓ Futures contract identified
✓ Expiry identified
● Loading option contracts
○ Building strike universe
○ Calculating volume
○ Calculating OI
```

Once loading starts, don't turn the whole screen into a spinner — use chart skeletons.

### 6. Data readiness strip

Immediately below the header, show a thin data-status strip.

```text
Futures ✓     Options ✓     Volume ✓     OI ✓
Data: COMPLETE                         Upstox
```

Partial data:

```text
Futures ✓     Options ✓     Volume ⚠     OI ✓
Data: PARTIAL
```

This is more useful than hiding data quality inside settings.

### 7. Session summary

A single-line summary sits above the charts (not large cards):

```text
FUT LTP       SESSION HIGH     SESSION LOW     ATM       CE VOL       PE VOL       PCR(OI)      MAX PAIN
24,618.35     25,018.60        24,528.30       24,600    68.42L       62.18L       0.91         24,650
```

Use tabular/monospace numerals. The numbers should not jump around when live data updates.

### 8. Main chart architecture

Vertically stacked, no permanent right sidebar:

```text
HEADER
SESSION STATUS / SUMMARY
1. NIFTY FUTURES
2. OPTIONS CUMULATIVE VOLUME
3. OPTIONS VOLUME FLOW
4. OPTIONS OI BY STRIKE
STATUS BAR
```

All four analytical sections should occupy almost the entire browser width.

### 9. Chart 1 — NIFTY Futures

The anchor chart. Display candlesticks, futures volume, OHLC, selected-time LTP, session high/low, and timeframe.

```text
01   NIFTY 50 FUTURES                                  1m

O 24,611.20   H 24,619.50   L 24,609.75   C 24,618.35
+7.15 (+0.03%)                                Vol 28.1K
```

The futures chart and option charts must share the same underlying timestamp coordinate. Do not implement three independent chart hover systems — use one shared time controller.

### 10. Chart 2 — Cumulative option volume

```text
02   OPTIONS CUMULATIVE VOLUME

CE 42.36L        PE 38.21L
```

Two lines, CE and PE, in consistent colors across the whole app (CE = blue, PE = orange — never change between panels).

Critical calculation rule: the cumulative lines must only increase or remain flat, never fall. This chart answers "how much option activity accumulated during the session?" — it does not answer instantaneous activity (see Chart 3).

### 11. Chart 3 — Volume Flow

The most important chart.

```text
03   OPTIONS VOLUME FLOW                       5s
```

Bars represent `current cumulative volume - cumulative volume 5 seconds ago`. This chart can move up and down.

Use a zero line with CE above zero and PE below zero (not two groups of tiny positive bars) so direction and magnitude are immediately readable:

```text
                CE
             █
        █    █       █
─────────────┼────────────────
       █     │
   █   █     │       █
       PE
```

### 12. Spike detection

Use the same CE/PE color with different intensity rather than three unrelated colors:

- Normal: base intensity
- Elevated: stronger intensity
- Significant: brightest intensity + small marker (e.g. tiny diamond or vertical marker)

The trader should immediately see `normal normal normal normal █████ SIGNIFICANT` without a complicated legend.

### 13. Crosshair — central interaction

The most important part of the application. There must be one crosshair state, not one per chart. If the cursor is at `11:42:17`, the same vertical line appears through Futures, cumulative volume, and volume flow, and the OI panel changes to `AS OF 11:42:17`.

### 14. Hover behaviour

Show a compact unified tooltip on hover, labels left aligned and numbers right aligned:

```text
11 Sep 2025 11:42:17

FUTURES
LTP              24,618.35

OPTIONS VOLUME
CE cumulative    42.36L
PE cumulative    38.21L

FLOW 5s
CE                1,08,420
PE                   84,310

OPEN INTEREST
CE                 18.46L
PE                 21.72L

ΔOI 5m
CE                +42,180
PE                +35,260
```

### 15. Locked crosshair

Clicking the chart locks the timestamp and changes the visual state:

```text
🔒 LOCKED — 11:42:17
```

Tooltip changes from "Click to lock" to "LOCKED AT 11:42:17 · Click to release". The UI must visibly communicate lock state at all times.

### 16. Changing settings while locked

Critical requirement: if the user locks `11:42:17`, then changes strikes (`ATM ± 5 → ATM ± 10`), interval (`5s → 15s`), or OI metric (`OI → ΔOI`), the locked timestamp must remain `11:42:17`. Otherwise the trader loses the event they were investigating.

### 17. OI panel

Use normal grouped bars, not mirrored bars, sharing the same zero baseline and scale for easy comparison:

```text
OPTIONS OPEN INTEREST BY STRIKE
As of 11:42:17

                         Call OI       Put OI

24,700                    ███            █████████
24,750                    █████          ███████████
24,800                    ███████        █████████████
24,850                    █████████      ███████████
24,900                    ███████████    ███████████████
24,950  ← ATM             ████████████   █████████████
25,000                    █████████      █████████
25,050                    ███████        ███████
25,100                    █████          █████
```

### 18. OI chart controls

Put controls directly in the OI header, no separate settings panel:

```text
OPTIONS OPEN INTEREST BY STRIKE

Metric:  [ OI ] [ ΔOI ] [ PCR ] [ Max Pain ]
ΔOI:     [ 5m ▼ ]
Side:    [ CE & PE ▼ ]
View:    [ Bars ] [ Table ]
```

### 19. OI bars + table

Support both Bars (pattern recognition) and Table (exact values), using the same underlying data — no duplicate calculations:

```text
Strike     CE OI      PE OI      Total OI     PCR
──────────────────────────────────────────────────
24,700     5.4L       6.1L       11.5L        1.13
24,750     8.7L       9.8L       18.5L        1.13
24,800     13.2L      14.6L      27.8L        1.11
24,850     19.6L      20.3L      39.9L        1.04
24,900     27.8L      28.1L      55.9L        1.01
24,950     31.1L      32.4L      63.5L        1.04
25,000     26.4L      27.6L      54.0L        1.05
```

### 20. ATM indication

The ATM strike needs a visually distinct horizontal highlight, not just a small label:

```text
24,900    ███████████     █████████████

24,950    ████████████    █████████████   ← ATM
          ─────────────────────────────
25,000    █████████       ███████████
```

And at the top: `ATM 24,950` / `Spot 24,938.20`. Historical ATM must be based on the historical timestamp/session, not today's ATM.

### 21. Strike universe behaviour

`ATM ± 5` shows `ATM-5 … ATM … ATM+5` (11 strikes total). The UI should explicitly display `Strikes: ATM ± 5`, never the ambiguous `Strikes: 5`. When ATM changes, the universe can shift; when the historical crosshair is locked, the universe should be calculated relative to the appropriate historical ATM methodology.

### 22. Historical option data architecture from UX perspective

Selecting Date / Expiry / Strikes conceptually requests:

```text
Historical Session
        ↓
Historical Futures Contract
        ↓
Historical Option Contracts
        ↓
Strike-level data
        ↓
Timestamp normalization
        ↓
Cumulative volume
        ↓
Interval flow
        ↓
OI / ΔOI
        ↓
Chart rendering
```

Don't aggregate historical data permanently into only `ATM ± 5`. The backend needs strike-level raw data so the UI can later switch to `ATM ± 10` without recollecting the session.

### 23. Partial data

Do not draw a normal continuous line through a data gap (e.g. `11:58 — 12:12`). Instead show a shaded warning region across all relevant charts labeled `Missing ticks · 11:58–12:12`. Calculations affected by the gap should not pretend to have exact continuity.

### 24. Loading state

Never use a giant spinner — use chart skeletons, and progressively reveal readiness (`Futures ✓`, `Options ✓`, `Volume calculating...`, `OI calculating...`) under a `Loading historical session...` header.

### 25. Other data states

Build these as reusable state components, not custom messages scattered through the frontend:

- Connecting — "● Connecting to Upstox..."
- Connected — "● Live feed connected"
- Historical loaded — "✓ Historical data loaded"
- Partial — "⚠ Partial data · Missing ticks: 11:58–12:12"
- No data — "NO DATA · No market data available for this session."
- Market closed — "MARKET CLOSED · Session ended at 15:30 IST"
- Token expired — "TOKEN EXPIRED · Market data cannot be refreshed."
- Upstox unavailable — "UPSTOX UNAVAILABLE · Unable to retrieve market data."

### 26. Navigation tabs

Use tabs only if they actually change the research task: Overview (default, the full synchronized screen described above), Price & OI (more vertical space for futures + OI), Volume Flow (more vertical space for cumulative volume + flow), Strike Analysis (detailed strike-level analysis), Sessions (historical session selection/comparison), Data Explorer (raw/normalized data inspection). Tabs should not remove the underlying data architecture.

### 27. Data Explorer

Since the backend retains raw stream messages and structured observations, include a secondary research page for debugging data integrity:

```text
DATA EXPLORER

Date        01 Sep 2026
Instrument  NIFTY FUT
From        11:40:00
To          11:45:00

Timestamp       Instrument      LTP       Volume      OI
────────────────────────────────────────────────────────
11:42:14        NIFTY FUT       24618.20  12,481
11:42:15        NIFTY FUT       24618.35  12,497
11:42:16        NIFTY FUT       24618.35  12,501
11:42:17        NIFTY FUT       24618.35  12,508
```

Not required for the main workflow, but useful for verifying data integrity.

### 28. Bottom status bar

Keep it very thin.

Live:

```text
● Connected to Upstox │ Last tick 11:42:17 │ Instruments 22 │ Gaps 0
Market Open 09:15–15:30 IST │ Data: Complete │ Token: Valid
```

Historical:

```text
✓ Historical data loaded │ Date 01 Sep 2026 │ Expiry 03 Sep 2026
Futures Complete │ Options Complete │ Gaps 0 │ Source Upstox
```

### 29. Color system

Define tokens once and reuse everywhere:

| Token | Value |
|---|---|
| Background | `#071018` |
| Panel | `#0B1620` |
| Panel elevated | `#0F1C27` |
| Grid | `#172733` |
| Border | `#203746` |
| Primary text | `#E6EDF3` |
| Secondary text | `#94A3B8` |
| Muted | `#64748B` |
| CE | `#2F9BFF` |
| PE | `#FF8A32` |
| Positive | `#20C997` |
| Negative | `#FF4D5A` |
| Warning | `#F5B942` |
| Error | `#FF5A67` |
| Historical | `#F5B942` |
| Live | `#20C997` |

CE must always be CE blue; PE must always be PE orange. Do not use red/green for CE/PE — those already carry market-direction semantics.

### 30. Typography

Use a normal UI sans font (Inter / Geist / system sans) for labels, and a tabular/monospaced font (JetBrains Mono / IBM Plex Mono / system monospace) for numerical values (prices, volumes, OI, deltas). This matters most in LIVE mode where numbers update continuously.

### 31. Avoid excessive cards

Do not turn summary stats into a grid of SaaS-style cards. Keep them in a single strip:

```text
PCR 0.91    Max Pain 24,650    CE Vol 68.42L    PE Vol 62.18L
```

Charts should dominate the screen.

### 32. Horizontal zoom

Historical data should support Full / 1H / 2H / Custom quick ranges plus mouse-wheel/drag zoom and pan. All time-based charts must zoom together.

### 33. Crosshair must survive zoom

If locked at `11:42:17`, zooming must leave the crosshair at `11:42:17`, not recenter it in the visible range.

### 34. Fullscreen

A chart fullscreen mode should hide unnecessary application chrome while preserving crosshair, timestamp, tooltip, data values, and controls relevant to that chart.

### 35. Recommended interaction sequence (test scenario)

1. Trader sees a significant CE spike at `11:42:17` in Options Volume Flow.
2. They hover there — one vertical line appears through all charts.
3. Tooltip shows Futures LTP, CE/PE volume, CE/PE OI, CE/PE ΔOI.
4. They click — crosshair locks: `🔒 LOCKED 11:42:17`.
5. The OI chart header updates to `AS OF 11:42:17` and strike bars reflect that timestamp.
6. Trader identifies which strikes were responsible for the OI structure.

That is the entire reason the interface exists.

### 36. OI chart must be explicitly contextual

Label it `OPTIONS OI BY STRIKE — AS OF 11:42:17`, not just "OI by strike". When the crosshair moves, the "AS OF" timestamp updates instantly, making clear these are event-time OI values, not current OI — essential for historical analysis.

### 37. Recommended final desktop hierarchy (1920x1080)

```text
┌───────────────────────────────────────────────────────────────┐
│ NIFTY 50 | HISTORICAL | DATE | EXPIRY | STRIKES | TIMEFRAME  │
├───────────────────────────────────────────────────────────────┤
│ FUT LTP | HIGH | LOW | ATM | CE VOL | PE VOL | PCR | MAXPAIN │
├───────────────────────────────────────────────────────────────┤
│  1  NIFTY FUTURES — candles + futures volume                  │
├───────────────────────────────────────────────────────────────┤
│  2  OPTIONS CUMULATIVE VOLUME — CE / PE lines                 │
├───────────────────────────────────────────────────────────────┤
│  3  OPTIONS VOLUME FLOW — CE positive / PE negative bars      │
├───────────────────────────────────────────────────────────────┤
│  4  OPTIONS OI BY STRIKE — CE/PE bars, ATM highlighted        │
├───────────────────────────────────────────────────────────────┤
│ ✓ Historical loaded | Futures ✓ | Options ✓ | Gaps 0 | Upstox│
└───────────────────────────────────────────────────────────────┘
```

### 38. Developer implementation priorities

**P0 — Must work:** historical date selection; correct historical futures contract; correct historical option contracts; dynamic ATM; dynamic ATM ± N universe; futures chart; cumulative CE/PE volume; interval CE/PE flow; strike-level OI; shared timestamp coordinate; crosshair; locked crosshair; OI updates to locked timestamp.

**P1 — Research quality:** ΔOI; PCR; Max Pain; spike classification; horizontal zoom/pan; data completeness warnings; missing-tick ranges; Bars/Table OI toggle.

**P2 — Reliability:** Upstox connection state; token state; reconnection; market calendar; historical caching; raw data explorer; performance optimization.

### 39. The key architectural rule

The frontend must not calculate different versions of the same data independently per chart. Maintain one synchronized analytical timeline keyed by timestamp, with futures, CE/PE cumulative volume, CE/PE interval flow, CE/PE OI, and CE/PE ΔOI all derived from it. The crosshair simply selects `timeline[timestamp]`; everything else derives from that selection. This prevents charts from drifting out of synchronization between live and historical modes.