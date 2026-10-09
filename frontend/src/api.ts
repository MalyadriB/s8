export const API = 'http://127.0.0.1:8000'

export type TokenStatus = {
  configured: boolean
  expires_at: number | null
  expired: boolean | null
  format?: string
}

export type Health = { status: string; token: TokenStatus }

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API}${path}`)
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = await response.json()
      detail = body.detail || detail
    } catch {
      /* ignore body parse failure */
    }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

const jsonCache = new Map<string, { at: number; promise: Promise<unknown> }>()

/** getJson(), but one download is shared by every caller of the same URL for `ttlMs` (and while still in flight):
 * the Lab tabs all load the same multi-MB backtest, and switching tabs used to re-download and re-parse it each time. */
function cachedJson<T>(path: string, ttlMs: number): Promise<T> {
  const hit = jsonCache.get(path)
  if (hit && Date.now() - hit.at < ttlMs) return hit.promise as Promise<T>
  const promise = getJson<T>(path)
  jsonCache.set(path, { at: Date.now(), promise })
  promise.catch(() => jsonCache.get(path)?.promise === promise && jsonCache.delete(path))
  return promise
}

const BACKTEST_TTL_MS = 60_000
// the whole backtest (every strategy) only changes when a backtest build runs - not during the session - so the Calendar
// keep it for 10 minutes rather than rebuilding ~4 MB every minute; a single strategy's (Today's S5/S8) stays at a
// minute, since Today re-asks for it after the morning exit to fill in that trade's backtest twin
const FULL_BACKTEST_TTL_MS = 10 * 60_000
const PAPER_TTL_MS = 15_000

export function fetchHealth() {
  return getJson<Health>('/api/health')
}

// ----------------------------------------------------------------------------------------------- Strategy lab
// Everything here is either a candle BACKTEST or PAPER trading. No endpoint under /api/lab places a real order.

/** How big the trades are shown: `lots` lots (each day's own contract lot size) or a fixed `qty` of units on every day. */
export type LabSize = { mode: 'lots' | 'qty'; value: number }
export const DEFAULT_LAB_SIZE: LabSize = { mode: 'lots', value: 1 }
const sizeParam = (size: LabSize) => `${size.mode}=${size.value}`
const withParams = (path: string, ...params: (string | null)[]) => {
  const query = params.filter(Boolean).join('&')
  return query ? `${path}?${query}` : path
}

/** Which backtest a row comes from: `minute` (1-minute candles, entry 09:30, exit 15:29 - what every screen shows) or
 * `nse_daily` (NSE end-of-day prices; still served by the backend's basis=nse_daily, no longer shown). Never mixed. */
export type LabBasis = 'minute' | 'nse_daily'

export type LabStrategy =
  | 'straddle_sell'
  | 'iron_fly_w1'
  | 'iron_fly_w2'
  | 'straddle_expiry_wings'
  | 'straddle_sell_overnight'
  | 'straddle_sell_two_overnight'
  | 'straddle_sell_three_overnight'
  | 'straddle_sell_overnight_skip_friday'
  | 'straddle_sell_overnight_skip_friday_december'
/** S4 is derived: S1's result each day, S3's on expiry days. It has no orders of its own.
 * S5 sells the same ATM straddle as S1 at 09:30, but holds it overnight and exits at the NEXT trading day's 09:30 candle
 * close instead of the same day - paper-traded (see Today).
 * S6/S7 are the same idea held for TWO/THREE overnights instead of one - backtest only, not paper-traded at all yet.
 * S8 is S5 itself, just never entered on a Friday (avoids the weekend gap into Monday's open) - it still rolls
 * into next week's ATM strike on its own expiry day exactly like S5.
 * S10 is S8 itself, also never entered in December. Backtest only. */
export const LAB_STRATEGIES: LabStrategy[] = [
  'straddle_sell',
  'iron_fly_w1',
  'iron_fly_w2',
  'straddle_expiry_wings',
  'straddle_sell_overnight',
  'straddle_sell_two_overnight',
  'straddle_sell_three_overnight',
  'straddle_sell_overnight_skip_friday',
  'straddle_sell_overnight_skip_friday_december',
]

export type LabFill = {
  ts: string
  bid: number | null
  ask: number | null
  ltp: number | null
  fill: number
  /** "bid" / "ask" from the book, or "estimated" (LTP -/+ 0.20) when the side of the book was empty; "_stale" if the quote was old. */
  source: string
}

export type LabLeg = {
  role: 'body' | 'wing'
  type: 'CE' | 'PE'
  strike: number
  side: 'BUY' | 'SELL'
  instrument_key: string
  /** Paper legs carry a fill record; backtest legs carry the candle price. */
  entry: LabFill | number
  exit?: LabFill | number | null
  /** S5 only, independently of each other: this leg's entry (or exit) came from the last live tick in the 09:30
   * minute, not a downloaded candle (today's finalized candle is not on Upstox's historical endpoint yet) - a
   * provisional price, not a real one. S1's own reused entry is never estimated, only a rolled entry can be. */
  entry_estimated?: boolean
  exit_estimated?: boolean
}

export type LabDay = {
  source: 'backtest' | 'paper'
  basis?: LabBasis
  strategy: LabStrategy
  trading_date: string
  expiry: string | null
  atm_strike: number | null
  wing_width: number | null
  lot_size: number | null
  /** Lots at this row's lot size - fractional when the quantity is not a whole number of lots. */
  lots: number
  /** Units traded: lots x lot size. */
  qty: number
  whole_lots: boolean
  /** "backtest" for history rows; open / closed / missed_entry / missed_exit / error for paper. */
  status: string
  legs: LabLeg[] | null
  gross_pts: number | null
  charges_pts: number | null
  net_pts: number | null
  gross_rs?: number | null // the legs' rupees before charges
  charges_rs?: number | null // brokerage, STT, exchange, SEBI, stamp and GST for the round trip, in rupees
  net_rs: number | null
  running_net_rs: number
  notes?: string | null
  /** Set on a paper row whose fill cannot fairly represent its intended moment - an exit resolved too late, or an
   * entry manually recovered after being missed (see retryLabPaperEntry()) - with the reason. Still
   * shown and counted everywhere (Today, Calendar, exports, ...); the flag says why it does not compare cleanly with the backtest. */
  invalid_reason?: string | null
  /** Only on S4 rows: the strategy whose result this day is (S1 normally, S3 on an expiry day). */
  derived_from?: LabStrategy | null
  /** Only on S5/S6/S7/S8 rows: the trading day its 09:30 exit falls on (S5/S8: the day after trading_date; S6: two trading days after; S7: three). */
  exit_date?: string
  /** Only on S5/S6/S7/S8 rows: true when trading_date was itself S1's expiry day, so this sold the same ATM strike in the NEXT week's expiry instead of S1's own (same-day-expiring) contract. Always false for S8 - it never enters on its own expiry day at all. */
  rolled_to_next_expiry?: boolean
  /** Only on S6/S7 rows: true when the held contract's OWN expiry fell on a day between entry and exit, so it was
   * bought back and re-sold in the next week's expiry partway through (S6: 4 legs instead of 2; S7: 4 or, rarely
   * on a second roll, 6 legs instead of 2 - see legs' own dates). */
  rolled_mid_hold?: boolean
  exit_delay_s?: number | null
  spot_at_entry?: number | null
  premium_ref?: number | null
  bt_net_pts?: number | null
  bt_net_rs?: number | null
}

export type DerivedSkip = { trading_date: string; reason: string }
export type LabMark = { strategy: LabStrategy; trading_date: string; minute: string; mtm_rs: number }
export type LabAlert = { ts: string; trading_date: string | null; level: string; message: string }

/** The newest stored quote of an instrument; `close_short` / `close_long` are what closing a short / long would cost right now (ask / bid, LTP when that side is empty). */
export type LabLiveQuote = { ltp: number | null; bid: number | null; ask: number | null; age_s: number; close_short: number | null; close_long: number | null }

export type LabLive = { spot: LabLiveQuote | null; quotes: Record<string, LabLiveQuote | null> }

export type LabToday = {
  paper: true
  now: string
  live: LabLive
  /** The session shown: today's from its 09:15 open; before that (after midnight, on a weekend or a holiday) the last
   * one - so a position held overnight keeps its marks, closing quotes and NIFTY's day. Not always the calendar date. */
  trading_date: string
  /** The real date now (IST) - for wording like "today" / "this morning". */
  calendar_date: string
  /** Whether the session shown is a market day (it is, unless no market day was found at all). */
  market_day: boolean
  /** Whether the calendar date itself is a market day (for "pre-open" before 09:15). */
  calendar_market_day: boolean
  /** The trading day whose 09:30 (S5's exit, then the entries) comes next: today until its 09:30, else the next market day. */
  next_session: string
  /** NIFTY's previous close and today's open/high/low, from the newest index message (null before any today). */
  spot_day: { prev_close: number | null; open: number | null; high: number | null; low: number | null } | null
  exit_at: string
  engine: { running: boolean; last_tick: string | null; last_error: string | null; lots: number; strategies: LabStrategy[]; feed_age_seconds: number | null; feed_stalled: boolean }
  trades: LabDay[]
  marks: LabMark[]
  /** Echo of the request's marks_since: when set, `marks` holds only that minute onward (merge them), else the whole day. */
  marks_since: string | null
  latest_mtm_rs: Partial<Record<LabStrategy, number>>
  alerts: LabAlert[]
}

export type LabMonth = {
  source: 'backtest' | 'paper'
  strategy: LabStrategy
  month: string
  days: number
  net_rs: number
  winning_days: number
  worst_day: string
  worst_day_rs: number
  max_drawdown_rs: number
}

/** Every day's profit and loss as CSV - the candle backtest and the PAPER trades, all strategies - at the given size. */
export const labExportUrl = (size: LabSize) => `${API}${withParams('/api/lab/export', sizeParam(size))}`

export function fetchLabBacktest(size: LabSize, strategy?: LabStrategy) {
  return cachedJson<{
    source: 'backtest'; count: number; size: { lots: number | null; qty: number | null }; rows: LabDay[]
    derived_skips: Partial<Record<LabStrategy, { backtest: DerivedSkip[]; paper: DerivedSkip[] }>>
  }>(withParams('/api/lab/backtest', strategy ? `strategy=${strategy}` : null, sizeParam(size)), strategy ? BACKTEST_TTL_MS : FULL_BACKTEST_TTL_MS)
}

export function fetchLabPaper(size: LabSize, strategy?: LabStrategy) {
  return cachedJson<{ source: 'paper'; count: number; rows: LabDay[] }>(withParams('/api/lab/paper', strategy ? `strategy=${strategy}` : null, sizeParam(size)), PAPER_TTL_MS)
}

/** `marksSince` (HH:MM) asks only for the marks of that minute onward - the screen already holds the earlier ones. */
/** NIFTY right now (the newest index tick and the session's day so far) - light enough to ask every second. */
export type LabSpot = { now: string; trading_date: string; spot: LabLiveQuote | null; spot_day: LabToday['spot_day'] }
export function fetchLabSpot() {
  return getJson<LabSpot>('/api/lab/spot')
}

/** NIFTY's last tick of each minute of the session shown (a minute the feed missed is simply absent). */
export type LabSpotMinutes = { trading_date: string; minutes: { minute: string; ltp: number }[] }
export function fetchLabSpotMinutes() {
  return getJson<LabSpotMinutes>('/api/lab/spot/minutes')
}

export function fetchLabToday(size: LabSize, marksSince?: string) {
  return getJson<LabToday>(withParams('/api/lab/paper/today', sizeParam(size), marksSince ? `marks_since=${marksSince}` : null))
}

/** Manually retries TODAY's missed entry for `strategy` at whatever quotes are available right now - still never
 * a real order. The recovered trade is marked invalid_reason (flagged, still shown everywhere
 * else) since it cannot represent a normal 09:30 entry. Throws with the backend's own reason (e.g. "no fresh
 * quote for CE 25200", "too late in the day...") when it cannot be retried yet. */
/** Closes one open PAPER position now at the current quotes (never a real order). `tradingDate` is the trade's entry
 * date - yesterday's for an overnight S5 position. Flagged as a manual exit. Throws with
 * the backend's reason (e.g. "the market is closed...") when it cannot be done. */
export async function exitLabPaperTrade(strategy: LabStrategy, tradingDate: string): Promise<void> {
  const response = await fetch(`${API}/api/lab/paper/exit-now?strategy=${strategy}&date=${tradingDate}`, { method: 'POST' })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      detail = (await response.json()).detail || detail
    } catch {
      /* ignore body parse failure */
    }
    throw new Error(detail)
  }
}

export async function retryLabPaperEntry(strategy: LabStrategy): Promise<void> {
  const response = await fetch(`${API}/api/lab/paper/retry-entry?strategy=${strategy}`, { method: 'POST' })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      detail = (await response.json()).detail || detail
    } catch {
      /* ignore body parse failure */
    }
    throw new Error(detail)
  }
}

export function fetchLabMonthly(source: 'backtest' | 'paper', size: LabSize, strategy?: LabStrategy) {
  return cachedJson<{ source: string; rows: LabMonth[] }>(
    withParams('/api/lab/monthly', `source=${source}`, strategy ? `strategy=${strategy}` : null, sizeParam(size)),
    source === 'paper' ? PAPER_TTL_MS : BACKTEST_TTL_MS,
  )
}

// ----------------------------------------------------------------------------------------------- day chart (what a strategy's strikes did on one day)
export type LabDayChartBar = { t: string; o: number; h: number; l: number; c: number; v?: number | null }

export type LabDayChartLeg = {
  role: 'body' | 'wing'
  type: 'CE' | 'PE'
  strike: number
  side: 'BUY' | 'SELL'
  instrument_key: string | null
  entry: number | null
  exit: number | null
  entry_time: string
  exit_time: string
  estimated_entry: boolean
  /** S5/S8 only: this leg's own entry/exit (not bars_source, which is about the candles drawn) came from the last
   * live tick in the 09:30 minute, not a downloaded candle - today's finalized candle is not published yet. */
  estimated?: boolean
  bars: LabDayChartBar[]
  /** "candles" (downloaded), "ticks" (built from the recorded live ticks) or "none". */
  bars_source: 'candles' | 'ticks' | 'none'
}

export type LabDayChart = {
  date: string
  strategy: LabStrategy
  source: 'backtest' | 'paper'
  basis: LabBasis
  available: boolean
  reason?: string
  status?: string
  expiry?: string | null
  atm_strike?: number | null
  wing_width?: number | null
  derived_from?: LabStrategy | null
  legs: LabDayChartLeg[]
  complete: boolean
  position: { t: string; price: number }[]
  pnl: { t: string; pts: number }[]
  spot: LabDayChartBar[]
  spot_source: string
  /** The last minute of the day drawn and exited at: 15:29 for the backtest, the paper exit minute (15:39) for a paper trade. */
  /** The entry minute (09:30, or an S5 paper trade's real fill minute). */
  entry_bar?: string
  exit_bar: string
  checks: { entry_matches_0930_open: boolean; exit_matches_1529_close: boolean } | null
  stats: { worst?: { t: string; pts: number }; best?: { t: string; pts: number }; final_pts?: number; spot?: { open_0930: number; close: number; high: number; low: number } }
  trade: { gross_pts: number | null; net_pts: number | null; net_rs_at_1_lot: number | null }
  // S5/S8/S10 only (straddle_sell_overnight, _skip_friday or _skip_friday_december): held
  // across two days, sold on `date` and bought back on `exit_date`, so there is no single 09:30-15:29 window -
  // these carry the entry day's and the exit day's own legs/candles/spot separately instead of the single-day
  // fields above (legs/complete/position/pnl/spot/checks).
  exit_date?: string
  rolled_to_next_expiry?: boolean
  entry_legs?: LabDayChartLeg[]
  exit_legs?: LabDayChartLeg[]
  entry_complete?: boolean
  exit_complete?: boolean
  entry_spot?: LabDayChartBar[]
  entry_spot_source?: string
  exit_spot?: LabDayChartBar[]
  exit_spot_source?: string
  s5_checks?: { entry_matches_0930_open: boolean; exit_matches_0930_close: boolean } | null
  /** S5 PAPER only: each panel's last minute (15 minutes past its fill), the fills' own timestamps, and the trade's flag. */
  entry_window_end?: string
  exit_window_end?: string
  entry_ts?: string | null
  exit_ts?: string | null
  invalid_reason?: string | null
}

export function fetchLabDayChart(date: string, strategy: LabStrategy, source: 'backtest' | 'paper' = 'backtest') {
  return getJson<LabDayChart>(`/api/lab/day-chart?date=${date}&strategy=${strategy}&source=${source}`)
}
