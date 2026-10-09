import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { exitLabPaperTrade, fetchLabBacktest, fetchLabPaper, fetchLabSpot, fetchLabSpotMinutes, fetchLabToday, LAB_STRATEGIES, retryLabPaperEntry, type LabDay, type LabFill, type LabLeg, type LabLive, type LabMark, type LabSpot, type LabSpotMinutes, type LabSize, type LabStrategy, type LabToday as Today } from '../api'
import type { ThemeName } from '../charts/theme'
import { MtmChart } from '../charts/LabCharts'
import { LabDayChart } from './LabDayChart'
import { clockOf, creditPoints, derivedTag, formatCountdown, formatPts, formatRs, isEstimated, markClock, pnlClass, signedRs, sizeKey, STATUS_LABEL, STRATEGY_COLOR, STRATEGY_LABEL, STRATEGY_SHORT, strikesLabel } from '../lab'
import { PaperBadge } from './PaperBadge'
import { TweenRs, useGlide } from './Motion'
import { Icon } from './Icons'
import { useFitToWindow } from './useFitToWindow'

const POLL_MS = 2000
const HIDDEN_POLL_MS = 30_000 // a background tab still notices alerts, without polling every 2 s

/** What the engine is waiting for, from the server's IST wall clock (the ISO string already carries IST, so its own digits are read). */
function phaseMessage(today: Today): string {
  if (!today.market_day) return 'Market closed today - the next paper entry is at 09:30 on the next market day.'
  const clock = clockOf(today.now).slice(0, 5)
  const minutes = Number(clock.slice(0, 2)) * 60 + Number(clock.slice(3, 5))
  if (minutes < 9 * 60 + 20) return `It is ${clock} IST. 09:20 health check, 09:25 pre-subscribe, 09:30 entry.`
  if (minutes < 9 * 60 + 30) return `It is ${clock} IST. Health check done; entry at 09:30.`
  return 'No paper position exists for today. If an entry was missed, the reason is under Alerts.'
}

function FillCells({ fill }: { fill: LabFill | number | null | undefined }) {
  if (fill == null || typeof fill === 'number') return <td colSpan={5}>—</td>
  return (
    <>
      <td>{clockOf(fill.ts)}</td>
      <td>{fill.bid ?? '—'}</td>
      <td>{fill.ask ?? '—'}</td>
      <td>{fill.ltp ?? '—'}</td>
      <td className="lab-fill">
        {fill.fill.toFixed(2)}
        {isEstimated(fill) && (
          <em className="lab-estimated" title="No bid/ask on that side of the book: filled at LTP -/+ 0.20 and flagged as estimated">
            est.
          </em>
        )}
      </td>
    </>
  )
}

function FillHeads() {
  return (
    <>
      <th>Time</th>
      <th>Bid</th>
      <th>Ask</th>
      <th>LTP</th>
      <th>Fill</th>
    </>
  )
}

/** A live price that shows which way it last moved. */
function LiveLtp({ value, ageSeconds }: { value: number | null | undefined; ageSeconds: number }) {
  const [direction, setDirection] = useState<'up' | 'down' | ''>('')
  const last = useRef<number | null>(null)
  useEffect(() => {
    if (value == null) return
    if (last.current != null && value !== last.current) setDirection(value > last.current ? 'up' : 'down')
    last.current = value
  }, [value])
  return (
    <td className={`lab-live-ltp ${direction}`} title={ageSeconds > 10 ? `Last update ${Math.round(ageSeconds)}s ago - this option has not traded or re-quoted since` : 'Live'}>
      {value == null ? '—' : value.toFixed(2)}
      {direction === 'up' ? ' ▲' : direction === 'down' ? ' ▼' : ''}
      {ageSeconds > 10 && <em className="lab-stale-age">{Math.round(ageSeconds)}s</em>}
    </td>
  )
}

function LegsTable({ legs, live, qty }: { legs: LabLeg[]; live: LabLive | undefined; qty: number }) {
  const closed = legs.some((leg) => leg.exit)
  const showLive = !!live && legs.some((leg) => !leg.exit)
  return (
    <div className="research-table-scroll">
      <table className="research-table lab-legs">
        <thead>
          <tr>
            <th rowSpan={2}>Leg</th>
            <th rowSpan={2}>Side</th>
            <th colSpan={5}>Entry</th>
            {showLive && <th colSpan={6}>Live now</th>}
            {closed && <th colSpan={5}>Exit</th>}
          </tr>
          <tr>
            <FillHeads />
            {showLive && (
              <>
                <th>LTP</th>
                <th>Bid</th>
                <th>Ask</th>
                <th title="What closing this leg would cost right now: a short at the ask, a long at the bid">Close at</th>
                <th>P&amp;L pts</th>
                <th>P&amp;L ₹</th>
              </>
            )}
            {closed && <FillHeads />}
          </tr>
        </thead>
        <tbody>
          {legs.map((leg) => {
            const quote = live?.quotes[leg.instrument_key] ?? null
            const entry = typeof leg.entry === 'number' ? leg.entry : leg.entry.fill
            const closeAt = quote ? (leg.side === 'SELL' ? quote.close_short : quote.close_long) : null
            const points = closeAt == null ? null : leg.side === 'SELL' ? entry - closeAt : closeAt - entry
            return (
              <tr key={`${leg.type}${leg.strike}${leg.side}`}>
                <th>
                  {leg.type} {leg.strike} <span className="dim">{leg.role === 'wing' ? 'wing' : ''}</span>
                </th>
                <td className={leg.side === 'SELL' ? 'negative' : 'positive'}>{leg.side}</td>
                <FillCells fill={leg.entry} />
                {showLive &&
                  (leg.exit ? (
                    <td colSpan={6}>closed</td>
                  ) : (
                    <>
                      <LiveLtp value={quote?.ltp} ageSeconds={quote?.age_s ?? 0} />
                      <td>{quote?.bid ?? '—'}</td>
                      <td>{quote?.ask ?? '—'}</td>
                      <td>{closeAt == null ? '—' : closeAt.toFixed(2)}</td>
                      <td className={pnlClass(points)}>{points == null ? '—' : formatPts(points)}</td>
                      <td className={pnlClass(points)}>{points == null ? '—' : formatRs(points * qty)}</td>
                    </>
                  ))}
                {closed && (leg.exit ? <FillCells fill={leg.exit} /> : <td colSpan={5}>—</td>)}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** Shown on a missed_entry row for a strategy the paper engine actually trades (S1-S3, S5 - never S4, which has no
 * row of its own to retry). Fills at whatever quotes are available right now, so the recovered trade is marked
 * invalid_reason server-side - it cannot represent a normal 09:30 entry, so it is flagged as a manual entry,
 * and still shows here like any other trade. */
function RetryEntryButton({ strategy, onRetried }: { strategy: LabStrategy; onRetried: () => void }) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleClick = () => {
    setPending(true)
    setError(null)
    retryLabPaperEntry(strategy)
      .then(() => {
        setPending(false)
        onRetried()
      })
      .catch((caught) => {
        setPending(false)
        setError(caught instanceof Error ? caught.message : 'Could not retry')
      })
  }

  return (
    <div className="lab-retry">
      <button type="button" className="research-chip" onClick={handleClick} disabled={pending}>
        <Icon name="retry" />
        {pending ? 'Retrying…' : 'Retry entry now'}
      </button>
      <small className="dim">Fills at current quotes, not 09:30's - the trade is flagged MANUAL ENTRY.</small>
      {error && <small className="export-error">{error}</small>}
    </div>
  )
}

/** "Exit now" on an open position (not on S4's derived card, which closes with S1). Two clicks: the first asks for
 * confirmation in place, since it closes the paper position at current quotes and flags it as a manual exit. */
function ExitNowButton({ trade, onExited }: { trade: LabDay; onExited: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const exit = () => {
    setPending(true)
    setError(null)
    exitLabPaperTrade(trade.strategy, trade.trading_date)
      .then(() => {
        setPending(false)
        setConfirming(false)
        onExited()
      })
      .catch((caught) => {
        setPending(false)
        setError(caught instanceof Error ? caught.message : 'Could not exit')
      })
  }

  return (
    <div className="lab-retry">
      {confirming ? (
        <>
          <button type="button" className="research-chip lab-exit-confirm" onClick={exit} disabled={pending}>
            {pending ? 'Exiting…' : 'Confirm: exit at current prices'}
          </button>
          <button type="button" className="research-chip" onClick={() => setConfirming(false)} disabled={pending}>
            Cancel
          </button>
          <small className="dim">Buys back every leg now. The trade is flagged MANUAL EXIT.</small>
        </>
      ) : (
        <button type="button" className="research-chip td-exit-btn" onClick={() => setConfirming(true)}>
          <Icon name="exit" />
          Exit now
        </button>
      )}
      {error && <small className="export-error">{error}</small>}
    </div>
  )
}

/** The badge for a flagged trade: says what happened when it was the user's own action (a manual entry or exit),
 * INVALID otherwise (a late exit and the like). */
function flagLabel(reason: string): { text: string; manual: boolean } {
  const entry = reason.includes('entered manually')
  const exit = reason.includes('exited manually')
  if (entry && exit) return { text: 'MANUAL ENTRY + EXIT', manual: true }
  if (entry) return { text: 'MANUAL ENTRY', manual: true }
  if (exit) return { text: 'MANUAL EXIT', manual: true }
  return { text: 'INVALID', manual: false }
}

/** The open position's P&L from the live quotes - the same close-at prices the "Live now" columns use (a short at
 * the ask, a long at the bid). The engine only records MTM marks from 09:30, so an overnight S5 position still held
 * between 09:15 and its 09:30 exit would otherwise show no MTM despite live prices. Undefined if a leg has no quote. */
function liveMtm(trade: LabDay, live: LabLive | undefined): number | undefined {
  if (!live || !trade.legs?.length) return undefined
  let total = 0
  for (const leg of trade.legs) {
    const quote = live.quotes[leg.instrument_key]
    const closeAt = quote ? (leg.side === 'SELL' ? quote.close_short : quote.close_long) : null
    if (closeAt == null) return undefined
    const entry = typeof leg.entry === 'number' ? leg.entry : leg.entry.fill
    total += (leg.side === 'SELL' ? entry - closeAt : closeAt - entry) * trade.qty
  }
  return total
}

const S5: LabStrategy = 'straddle_sell_overnight'
const S8: LabStrategy = 'straddle_sell_overnight_skip_friday'
const WEEKDAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
const DAY_START = 9 * 60 + 15 // the timeline runs from the 09:15 open to the 15:30 close
const DAY_END = 15 * 60 + 30
const MARK_FROM = 9 * 60 + 30 // MTM marks (and so the sparklines) run 09:30 to the exit
const MARK_TO = 15 * 60 + 29
const SHOW_OTHERS_KEY = 'today.showOthers'
const RECORD_BARS = 20 // the S5 track record draws this many most recent paper trades

/** Seconds since midnight of an HH:MM[:SS] IST clock. */
const secondsOf = (clock: string): number => {
  const [h, m, s] = clock.split(':').map(Number)
  return h * 3600 + m * 60 + (s || 0)
}

const clockAt = (seconds: number): string => {
  const whole = Math.max(0, Math.floor(seconds))
  return [Math.floor(whole / 3600), Math.floor((whole % 3600) / 60), whole % 60].map((part) => String(part).padStart(2, '0')).join(':')
}


const utcDay = (day: string) => new Date(`${day}T00:00:00Z`)
/** Epoch ms of an IST wall-clock moment on `day`. */
const istEpoch = (day: string, clock: string): number => Date.parse(`${day}T${clock}+05:30`)
const weekdayShort = (day: string): string => utcDay(day).toLocaleDateString('en-IN', { weekday: 'short', timeZone: 'UTC' })
const dayShortLabel = (day: string): string => utcDay(day).toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })
const dayLabel = (day: string): string => utcDay(day).toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })
const shortDay = (day: string): string => utcDay(day).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', timeZone: 'UTC' })
const isFriday = (day: string): boolean => utcDay(day).getUTCDay() === 5
const entryPrice = (leg: LabLeg): number => (typeof leg.entry === 'number' ? leg.entry : leg.entry.fill)
const exitPrice = (leg: LabLeg): number | null => (leg.exit == null ? null : typeof leg.exit === 'number' ? leg.exit : leg.exit.fill)

function readShowOthers(): boolean {
  try {
    return window.localStorage.getItem(SHOW_OTHERS_KEY) === '1'
  } catch {
    return false
  }
}

function writeShowOthers(value: boolean) {
  try {
    window.localStorage.setItem(SHOW_OTHERS_KEY, value ? '1' : '0')
  } catch {
    // storage unavailable (private window): the choice simply is not remembered
  }
}

/** What the engine does next today, from the server's clock: the fixed paper schedule (see paper.py). */
function nextEvent(today: Today, nowSeconds: number): { label: string; at: string; seconds: number | null; from: number } {
  if (today.market_day) {
    const exit = secondsOf(clockOf(today.exit_at))
    const events: [number, string][] = [
      [secondsOf('09:15'), 'market opens'],
      [secondsOf('09:20'), 'health check'],
      [secondsOf('09:25'), 'pre-subscribe'],
      [secondsOf('09:30'), 'S5 exit & entries'],
      [exit, 'same-day exits'],
      [secondsOf('15:30'), 'market closes'],
      [exit + 11 * 60, 'reconcile'],
    ]
    const next = events.find(([at]) => at > nowSeconds)
    const previous = [...events].reverse().find(([at]) => at <= nowSeconds)
    if (next) return { label: next[1], at: clockAt(next[0]).slice(0, 5), seconds: next[0] - nowSeconds, from: previous?.[0] ?? 0 }
    return { label: 'S5 exit & entries', at: '09:30', seconds: null, from: events[events.length - 1][0] }
  }
  return { label: 'S5 exit & entries', at: '09:30', seconds: null, from: 0 }
}

/** A live clock face for the server's IST time. The angles keep growing through the day, so every hand only ever
 * moves forward (the second hand ticks; CSS eases each step). */
function AnalogClock({ seconds }: { seconds: number }) {
  return (
    <svg className="td-analog" viewBox="0 0 40 40" aria-hidden="true">
      <circle className="face" cx="20" cy="20" r="18" />
      {Array.from({ length: 12 }, (_, hour) => (
        <line key={hour} className={hour % 3 ? 'tick' : 'tick major'} x1="20" y1="3.6" x2="20" y2={hour % 3 ? 5.8 : 7.2} transform={`rotate(${hour * 30} 20 20)`} />
      ))}
      <line className="hand hour" x1="20" y1="20" x2="20" y2="10.5" style={{ transform: `rotate(${seconds / 120}deg)` }} />
      <line className="hand minute" x1="20" y1="20" x2="20" y2="6.5" style={{ transform: `rotate(${seconds / 10}deg)` }} />
      <line className="hand second" x1="20" y1="23.5" x2="20" y2="5" style={{ transform: `rotate(${seconds * 6}deg)` }} />
      <circle className="pin" cx="20" cy="20" r="1.7" />
    </svg>
  )
}

/** How much of the wait for the next scheduled event is left, as a ring that empties second by second. */
function CountdownRing({ left }: { left: number | null }) {
  const radius = 17
  const length = 2 * Math.PI * radius
  const share = left == null ? 1 : Math.max(0, Math.min(1, left))
  return (
    <div className="td-ring" aria-hidden="true">
      <svg viewBox="0 0 40 40">
        <circle className="track" cx="20" cy="20" r={radius} />
        <circle className="left" cx="20" cy="20" r={radius} strokeDasharray={length} strokeDashoffset={length * (1 - share)} />
      </svg>
      <Icon name="timer" />
    </div>
  )
}

/** Which way `value` last moved (compared at `digits` decimals), with a counter that changes on every move - used as a
 * key, it replays a CSS flash. */
function useMoveFlash(value: number | null | undefined, digits = 0): { dir: 'up' | 'down'; n: number } | null {
  const previous = useRef(value)
  const [flash, setFlash] = useState<{ dir: 'up' | 'down'; n: number } | null>(null)
  useEffect(() => {
    const before = previous.current
    previous.current = value
    if (value == null || before == null || value.toFixed(digits) === before.toFixed(digits)) return
    setFlash((current) => ({ dir: value > before ? 'up' : 'down', n: (current?.n ?? 0) + 1 }))
  }, [value, digits])
  return flash
}

/** NIFTY's tick, asked for every second while the market is live (null otherwise, or until the first answer): the
 * index tile moves with the market rather than with the 2-second Today poll. One request at a time; paused while the
 * tab is hidden; dropped when it belongs to another session than the one shown. */
function useLiveSpot(live: boolean, session: string | undefined): LabSpot | null {
  const [spot, setSpot] = useState<LabSpot | null>(null)
  useEffect(() => {
    if (!live) {
      setSpot(null)
      return
    }
    let stopped = false
    let busy = false
    const ask = async () => {
      if (busy || document.hidden) return
      busy = true
      try {
        const next = await fetchLabSpot()
        if (!stopped) setSpot(next)
      } catch {
        // the tile falls back to the Today poll's own spot
      } finally {
        busy = false
      }
    }
    void ask()
    const timer = window.setInterval(ask, 1000)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [live])
  return spot && spot.trading_date === session ? spot : null
}

/** NIFTY's minutes so far in the session shown: loaded once, then every 30 s while the market is live. */
function useSpotMinutes(session: string | undefined, live: boolean): LabSpotMinutes | null {
  const [data, setData] = useState<LabSpotMinutes | null>(null)
  useEffect(() => {
    if (!session) return
    let stopped = false
    const load = () => {
      if (document.hidden) return
      fetchLabSpotMinutes()
        .then((next) => !stopped && setData(next))
        .catch(() => undefined) // the sparkline is context: the tile works without it
    }
    load()
    const timer = live ? window.setInterval(load, 30_000) : 0
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [session, live])
  return data && data.trading_date === session ? data : null
}

const SPARK_FROM = 9 * 60 + 15 // the session's 09:15 open ...
const SPARK_SPAN = 375 // ... to its 15:30 close, in minutes

/** NIFTY through the session so far, minute by minute, on the whole session's axis: the previous close dashed, the
 * 09:30 entry as S5's pink dot, the live end glowing. A stretch the feed missed is left as a faint dotted bridge. */
function NiftySpark({ minutes, spot, liveMinute, prevClose, entrySpot }: {
  minutes: { minute: string; ltp: number }[]
  spot: number | null
  liveMinute: string | null
  prevClose: number | null
  entrySpot: number | null
}) {
  const width = 116
  const height = 46
  const points = minutes.map((item) => ({ t: Number(item.minute.slice(0, 2)) * 60 + Number(item.minute.slice(3, 5)) - SPARK_FROM, v: item.ltp })).filter((point) => point.t >= 0 && point.t <= SPARK_SPAN)
  if (spot != null && liveMinute) {
    const t = Number(liveMinute.slice(0, 2)) * 60 + Number(liveMinute.slice(3, 5)) - SPARK_FROM
    if (t >= 0 && t <= SPARK_SPAN) {
      if (points.length && points[points.length - 1].t === t) points[points.length - 1] = { t, v: spot }
      else if (!points.length || points[points.length - 1].t < t) points.push({ t, v: spot })
    }
  }
  if (points.length < 2) return null
  const values = [...points.map((point) => point.v), ...(prevClose != null ? [prevClose] : []), ...(entrySpot != null ? [entrySpot] : [])]
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const pad = (hi - lo || 1) * 0.08
  const x = (t: number) => (t / SPARK_SPAN) * width
  const y = (v: number) => 3 + ((hi + pad - v) / (hi - lo + 2 * pad)) * (height - 6)
  let line = ''
  let bridges = ''
  points.forEach((point, index) => {
    const gap = index > 0 && point.t - points[index - 1].t > 2
    if (gap) bridges += `M${x(points[index - 1].t).toFixed(1)} ${y(points[index - 1].v).toFixed(1)}L${x(point.t).toFixed(1)} ${y(point.v).toFixed(1)}`
    line += `${index === 0 || gap ? 'M' : 'L'}${x(point.t).toFixed(1)} ${y(point.v).toFixed(1)}`
  })
  const last = points[points.length - 1]
  const down = prevClose != null && last.v < prevClose
  const entryT = 15 // 09:30
  return (
    <div className={`td-nifty-spark ${down ? 'down' : 'up'}`} title="NIFTY today, minute by minute (09:15-15:30): dashed - the previous close; pink dot - the 09:30 entry; a dotted stretch - minutes the live feed missed">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
        {prevClose != null && <line className="pc" x1={0} x2={width} y1={y(prevClose)} y2={y(prevClose)} />}
        {bridges && <path className="bridge" d={bridges} />}
        <path className="line" d={line} />
        {entrySpot != null && <circle className="entry" cx={x(entryT)} cy={y(entrySpot)} r={2.6} />}
      </svg>
      <i className="end" style={{ left: x(last.t), top: y(last.v) }} />
    </div>
  )
}

/** S5 today as a diverging bar around ₹0: what added to it to the right (green), what took from it to the left (red) -
 * the trade booked this morning solid, the open position hatched - and a marker on the net they come to. */
function S5Split({ booked, open }: { booked: number; open: number | undefined }) {
  const parts = [
    { kind: 'booked', value: booked },
    { kind: 'open', value: open ?? 0 },
  ].filter((part) => part.value !== 0)
  const gains = parts.filter((part) => part.value > 0).reduce((sum, part) => sum + part.value, 0)
  const losses = parts.filter((part) => part.value < 0).reduce((sum, part) => sum + part.value, 0)
  const span = gains - losses || 1
  const at = (value: number) => ((value - losses) / span) * 100
  let up = 0
  let down = 0
  const segments = parts.map((part) => {
    const from = part.value > 0 ? up : down
    const to = from + part.value
    if (part.value > 0) up = to
    else down = to
    return { ...part, left: at(Math.min(from, to)), width: Math.abs(at(to) - at(from)) }
  })
  const net = booked + (open ?? 0)
  return (
    <div className="td-s5-split" title={`Booked ${signedRs(booked)} (bought back this morning) + open ${open != null ? signedRs(open) : '—'} (today's position, marked live) = ${signedRs(net)}`}>
      {segments.map((segment) => (
        <i key={segment.kind} className={`seg ${segment.kind} ${segment.value > 0 ? 'up' : 'down'}`} style={{ left: `${segment.left}%`, width: `${Math.max(1.5, segment.width)}%` }} />
      ))}
      <i className="zero" style={{ left: `${at(0)}%` }} />
      <i className="net" style={{ left: `${at(net)}%` }} />
    </div>
  )
}

const priceFormat = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const formatPrice = (value: number | null | undefined) => (value == null ? '—' : priceFormat.format(value))

/** A live price: glides to each new tick (from where it was - never counting up from zero) and flashes the way it moved. */
function FlashPrice({ value }: { value: number | null }) {
  const flash = useMoveFlash(value, 2)
  const ref = useGlide<HTMLElement>(value, formatPrice, { ms: 450, fromZero: false })
  return (
    <b ref={ref} key={flash?.n ?? 0} className={`td-price${flash ? ` td-flash-${flash.dir}` : ''}`}>
      {formatPrice(value)}
    </b>
  )
}

/** The S5 hero figure: glides to each new value, glows the way it moved, and lets the change itself float up and away. */
function HeroValue({ value }: { value: number | null | undefined }) {
  const ref = useGlide<HTMLElement>(value, signedRs)
  const previous = useRef(value)
  const [moves, setMoves] = useState<{ id: number; delta: number }[]>([])
  useEffect(() => {
    const before = previous.current
    previous.current = value
    if (value == null || before == null) return
    const delta = Math.round(value) - Math.round(before)
    if (!delta) return
    const id = performance.now()
    setMoves((list) => [...list.slice(-1), { id, delta }])
    window.setTimeout(() => setMoves((list) => list.filter((move) => move.id !== id)), 1500)
  }, [value])
  const last = moves.at(-1)
  return (
    <div className="td-hero-figure">
      <b ref={ref} key={last?.id ?? 0} className={`${pnlClass(value)}${last ? ` td-glow-${last.delta > 0 ? 'up' : 'down'}` : ''}`}>
        {signedRs(value)}
      </b>
      {moves.map((move) => (
        <span key={move.id} className={`td-delta ${move.delta > 0 ? 'up' : 'down'}`} aria-hidden="true">
          {move.delta > 0 ? '+' : '−'}₹{Math.abs(move.delta).toLocaleString('en-IN')}
        </span>
      ))}
    </div>
  )
}

/** Share of the credit collected at entry that is kept (MTM) or was kept (closed), as a small ring that fills in. */
function KeptRing({ share }: { share: number }) {
  const radius = 7
  const length = 2 * Math.PI * radius
  const filled = Math.min(1, Math.abs(share) / 100)
  return (
    <span className={`td-kept ${share >= 0 ? 'up' : 'down'}`} title="Share of the premium collected at entry that is kept (MTM) or was kept (closed)">
      <svg viewBox="0 0 18 18" aria-hidden="true" style={{ '--len': length } as CSSProperties}>
        <circle className="track" cx="9" cy="9" r={radius} />
        <circle className="fill" cx="9" cy="9" r={radius} strokeDasharray={length} strokeDashoffset={length * (1 - filled)} />
      </svg>
      {share.toFixed(0)}% of credit kept
    </span>
  )
}

/** A leg's P&L cell: glides to each new value and flashes the way it moved. */
const formatPnl = (value: number | null | undefined) => (value == null ? '—' : signedRs(value))

function PnlCell({ value }: { value: number | null }) {
  const ref = useGlide<HTMLTableCellElement>(value, formatPnl, { ms: 500 })
  const flash = useMoveFlash(value)
  return (
    <td ref={ref} key={flash?.n ?? 0} className={`${pnlClass(value)}${flash ? ` td-cell-${flash.dir}` : ''}`}>
      {formatPnl(value)}
    </td>
  )
}

/** A live price cell that flashes the way it moved. */
function PriceCell({ value }: { value: number | null }) {
  const flash = useMoveFlash(value, 2)
  return (
    <td key={flash?.n ?? 0} className={flash ? `td-cell-${flash.dir}` : undefined}>
      {value == null ? '—' : value.toFixed(2)}
    </td>
  )
}

/** A P&L figure that glides to each new value and briefly flashes green or red whenever it moves. */
function FlashValue({ value, className = '' }: { value: number | null | undefined; className?: string }) {
  const flash = useMoveFlash(value)
  const ref = useGlide<HTMLElement>(value, signedRs)
  return (
    <b ref={ref} key={flash?.n ?? 0} className={`${className} ${pnlClass(value)}${flash ? ` td-flash-${flash.dir}` : ''}`}>
      {signedRs(value)}
    </b>
  )
}


const HOURS = ['09:15', '10:00', '11:00', '12:00', '13:00', '14:00', '15:00', '15:30'] // the scale above today's session
const SEG_DAY = 72 // % of the bar: today's 09:15-15:30 session
const SEG_NIGHT = 14 // % of the bar: from the close to the next session's 09:15 (however many days that is)
const PREV_DAY = 20 // % of the bar: an earlier session whose S5 is carried into today, compressed, left of today
const PREV_NIGHT = 8 // % of the bar: that session's night
const CARRY = PREV_DAY + PREV_NIGHT // how far the window sits left of today while it carries an earlier position in

/** One strip of time, of which the bar is a window: an earlier session and its night (compressed), today's session, then
 * tonight and the next session up to its 09:30 - the span an S5 position is held over. The fixed schedule is marked, the
 * elapsed part filled, and "now" moves along it.
 *
 * While an S5 from an earlier session is still held (from 09:15 until its 09:30 exit lands), the window shows that
 * session, its night and today: its lane runs from its entry across the night to this morning's exit, and today's own
 * progress fills today's session beside it. When the exit lands, the hand-over plays: the exit point bursts with the
 * result, the window pans on to today, tonight and the next morning, and the day's two events (bought back, entered)
 * settle in under it. Clicking the bought-back chip replays it. */
type TimelineEvents = {
  closed: { day: string; net: number | null; at: string; status: string; trade: LabDay } | null // an earlier session's S5, bought back today
  opened: { status: string; at: string; strike: number | null; trade: LabDay; mtm: number | undefined } | null // today's own S5
}

type TrackPoint = { x: number; value: number; minute: string; day: string; t: number }

/** A minute of the bar's session ("HH:MM", or past 24:00 for the next session's morning) as seconds of the clock. */
const clockSeconds = (minute: string): number => Number(minute.slice(0, 2)) * 3600 + Number(minute.slice(3, 5)) * 60

function SessionTimeline({ today, nowMs, s5Open, carried, events, live, s5Marks, spotMinutes, alerts }: {
  today: Today
  nowMs: number
  s5Open: boolean
  /** An S5 position from an earlier session that is still open (until its 09:30 exit lands). */
  carried: LabDay | null
  events: TimelineEvents | null
  live: boolean
  s5Marks: LabMark[]
  spotMinutes: LabSpotMinutes | null
  alerts: Today['alerts']
}) {
  const day = today.trading_date
  const marketDay = today.market_day
  const carry = !!carried && marketDay && carried.trading_date < day
  // the earlier session on the strip: the one an S5 is carried in from, or this morning's bought-back one
  const prevTrade = carry ? carried : (events?.closed?.trade ?? null)
  const prevDay = prevTrade && prevTrade.trading_date < day ? prevTrade.trading_date : null
  const next = today.next_session !== day ? today.next_session : null
  const start = istEpoch(day, '09:15:00')
  const end = istEpoch(day, '15:30:00')
  const nextOpen = next ? istEpoch(next, '09:15:00') : null
  const nextExit = next ? istEpoch(next, '09:30:00') : null
  const morning = 100 - SEG_DAY - SEG_NIGHT
  const onDay = (clock: string) => ((istEpoch(day, clock) - start) / (end - start)) * SEG_DAY
  const position = (ms: number): number => {
    if (marketDay && ms <= start) return 0
    if (marketDay && ms <= end) return ((ms - start) / (end - start)) * SEG_DAY
    if (!nextOpen || !nextExit) return SEG_DAY
    const from = marketDay ? end : istEpoch(day, '00:00:00')
    if (ms <= nextOpen) return SEG_DAY + SEG_NIGHT * Math.max(0, Math.min(1, (ms - from) / (nextOpen - from)))
    return SEG_DAY + SEG_NIGHT + morning * Math.min(1, (ms - nextOpen) / (nextExit - nextOpen))
  }
  const now = Math.min(100, Math.max(0, position(nowMs)))
  const fill = (from: number, width: number) => `${Math.min(100, Math.max(0, ((now - from) / width) * 100))}%`
  const exitClock = clockOf(today.exit_at)
  const sessionX = (seconds: number) => Math.min(SEG_DAY, Math.max(0, ((seconds - (9 * 3600 + 15 * 60)) / (375 * 60)) * SEG_DAY))
  const prevX = (seconds: number) => -CARRY + (sessionX(seconds) / SEG_DAY) * PREV_DAY
  // a mark's place on the strip: today's session or (past 24:00) the next morning; for the earlier session's position,
  // its own session (left of today) or (past 24:00) this morning, on today's session
  const xOf = (markDay: string, minute: string): number | null => {
    const seconds = clockSeconds(minute)
    if (markDay === day) {
      if (seconds < 24 * 3600) return sessionX(seconds)
      return next ? SEG_DAY + SEG_NIGHT + morning * Math.min(1, Math.max(0, (seconds - 24 * 3600 - (9 * 3600 + 15 * 60)) / (15 * 60))) : null
    }
    if (markDay === prevDay) return seconds < 24 * 3600 ? prevX(seconds) : sessionX(seconds - 24 * 3600)
    return null
  }

  // ---- the hand-over: the carried position's exit landing pans the window on (the state-from-props pattern, so the very
  // commit that moves the strip also carries the class that animates it). A replay rewinds, then plays it again.
  const [wasCarrying, setWasCarrying] = useState(carry)
  const [handoverAt, setHandoverAt] = useState<number | null>(null)
  if (wasCarrying !== carry) {
    setWasCarrying(carry)
    if (wasCarrying && !carry) setHandoverAt(nowMs)
  }
  const [replay, setReplay] = useState<'back' | 'go' | null>(null)
  useEffect(() => {
    if (!replay) return
    const timer = window.setTimeout(() => setReplay(replay === 'back' ? 'go' : null), replay === 'back' ? 700 : 3600)
    return () => window.clearTimeout(timer)
  }, [replay])
  const exiting = replay === 'go' || (replay == null && handoverAt != null && nowMs - handoverAt < 3600)
  const offset = carry || replay === 'back' ? CARRY : 0

  // ---- what the track carries: the S5 positions' MTM (the carried one, then today's), the feed's gaps, the alerts
  const points: TrackPoint[] = s5Marks
    .filter((mark) => (mark.trading_date === day || mark.trading_date === prevDay) && mark.mtm_rs != null)
    .flatMap((mark) => {
      const x = xOf(mark.trading_date, mark.minute)
      return x == null ? [] : [{ x, value: mark.mtm_rs as number, minute: mark.minute, day: mark.trading_date, t: istEpoch(mark.trading_date, '00:00:00') + clockSeconds(mark.minute) * 1000 }]
    })
    .sort((a, b) => a.t - b.t)
  const biggest = Math.max(500, ...points.map((point) => Math.abs(point.value)))
  const yOf = (value: number) => 12 - (value / biggest) * 10.5
  const runs: TrackPoint[][] = []
  for (const point of points) {
    const run = runs[runs.length - 1]
    const previous = run?.[run.length - 1]
    // a break of more than two minutes (the feed was down, or the night), or the next trade, starts a new stretch of line
    if (!run || point.t - previous.t > 120_000 || point.day !== previous.day) runs.push([point])
    else run.push(point)
  }
  const line = runs.map((run) => run.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(3)} ${yOf(point.value).toFixed(2)}`).join('')).join('')
  const area = runs
    .map((run) => `M${run[0].x.toFixed(3)} 12${run.map((point) => `L${point.x.toFixed(3)} ${yOf(point.value).toFixed(2)}`).join('')}L${run[run.length - 1].x.toFixed(3)} 12Z`)
    .join('')
  const spot = spotMinutes && spotMinutes.trading_date === day ? spotMinutes.minutes : null
  const spotBy = new Map((spot ?? []).map((item) => [item.minute, item.ltp]))
  const nowClock = new Date(nowMs + 330 * 60_000).toISOString().slice(11, 16)
  const gaps: { from: string; to: string; minutes: number }[] = []
  if (spot && spot.length) {
    const until = Math.min(clockSeconds(nowClock) - 120, clockSeconds('15:30'))
    let runFrom: number | null = null
    for (let seconds = clockSeconds('09:16'); seconds <= until; seconds += 60) {
      const minute = `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String((seconds / 60) % 60).padStart(2, '0')}`
      if (!spotBy.has(minute)) runFrom ??= seconds
      else if (runFrom != null) {
        if (seconds - runFrom >= 180) gaps.push({ from: clockLabelOf(runFrom), to: clockLabelOf(seconds), minutes: (seconds - runFrom) / 60 })
        runFrom = null
      }
    }
    if (runFrom != null && until - runFrom >= 120) gaps.push({ from: clockLabelOf(runFrom), to: clockLabelOf(until + 120), minutes: Math.round((until + 120 - runFrom) / 60) })
  }
  const pins = alerts
    .filter((alert) => alert.trading_date === day && alert.ts?.slice(0, 10) === day)
    .map((alert) => ({ ...alert, x: xOf(day, alert.ts.slice(11, 16)) ?? 0 }))

  // ---- the day's own S5 events, once nothing from an earlier session is held any more
  const shown = !carry && marketDay && events && (events.closed || events.opened) ? events : null
  const closedAt = shown?.closed ? onDay(shown.closed.at) : null
  const openedAt = shown?.opened ? onDay(shown.opened.at) : null
  const eventsAt = Math.min(closedAt ?? Infinity, openedAt ?? Infinity)
  const exitAt = onDay('09:30:00')
  // where the earlier session's position was entered, on its compressed session
  const prevEntryTs = prevTrade ? ((prevTrade.legs ?? []).map((leg) => fillTs(leg.entry)).filter((ts): ts is string => !!ts).sort()[0] ?? null) : null
  const prevEntryX = prevDay ? prevX(clockSeconds(prevEntryTs ? clockOf(prevEntryTs) : '09:30')) : null
  const marks: { at: number; label?: string; title: string; align?: 'start' | 'end'; s5?: boolean }[] = marketDay
    ? [
        { at: onDay('09:20:00'), title: '09:20 health check' },
        { at: onDay('09:25:00'), title: '09:25 pre-subscribe' },
        {
          at: exitAt,
          label: shown ? undefined : carry ? '09:30 S5 exit · entries' : '09:30 entries',
          title: "09:30: the overnight S5 exits, then today's entries",
          align: 'start',
          s5: carry,
        },
        { at: onDay(exitClock), label: `${exitClock.slice(0, 5)} exits`, title: `${exitClock} same-day exits (S1-S3; S5 stays open overnight)`, align: 'end' },
      ]
    : []

  // ---- the scrubber: hover anywhere on the bar for that minute's S5 and NIFTY (the earlier session's too, while in view)
  const [scrub, setScrub] = useState<{ x: number; text: string; tone: string } | null>(null)
  // the trade in the spotlight: hovered by its chip or lane, or by its stretch of the track under the scrubber
  const [hovered, setHovered] = useState<'closed' | 'opened' | null>(null)
  const underScrub: 'closed' | 'opened' | null =
    scrub == null || !shown || offset ? null : shown.closed && closedAt != null && scrub.x <= closedAt ? 'closed' : shown.opened && openedAt != null && scrub.x >= openedAt ? 'opened' : null
  const focus = replay ? null : (hovered ?? underScrub)
  const hover = (kind: 'closed' | 'opened') => ({ onMouseEnter: () => setHovered(kind), onMouseLeave: () => setHovered(null) })
  const onScrub = (event: React.MouseEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const x = Math.min(100, Math.max(0, ((event.clientX - rect.left) / rect.width) * 100))
    const along = x - offset // the same point on the strip
    let minute: string | null = null
    let onSession = day
    if (along >= 0 && along <= SEG_DAY && marketDay) minute = clockLabelOf(9 * 3600 + 15 * 60 + Math.round((along / SEG_DAY) * 375) * 60)
    else if (prevDay && along >= -CARRY && along <= -PREV_NIGHT) {
      minute = clockLabelOf(9 * 3600 + 15 * 60 + Math.round(((along + CARRY) / PREV_DAY) * 375) * 60)
      onSession = prevDay
    }
    if (!minute) return setScrub({ x, text: along < SEG_DAY + SEG_NIGHT ? 'night · market closed' : `${next ? weekdayShort(next) : 'next'} morning`, tone: '' })
    const seconds = clockSeconds(minute)
    const t = istEpoch(onSession, '00:00:00') + seconds * 1000
    const at = [...points].reverse().find((point) => point.t <= t && t - point.t <= 120_000)
    const isToday = onSession === day
    const nifty = isToday ? spotBy.get(minute) : undefined
    const gap = isToday ? gaps.find((item) => seconds >= clockSeconds(item.from) && seconds < clockSeconds(item.to)) : undefined
    const future = isToday && seconds > clockSeconds(nowClock) + 30
    const parts = [isToday ? markClock(minute) : `${weekdayShort(onSession)} ${minute}`]
    const step = isToday ? ({ '09:20': 'health check', '09:25': 'pre-subscribe', '09:30': carry ? 'S5 exit · entries' : 'S5 exit & entries' }[minute] ?? (minute === exitClock.slice(0, 5) ? 'same-day exits' : null)) : null
    if (step) parts.push(step)
    const fired = isToday ? pins.filter((pin) => pin.ts.slice(11, 16) === minute) : []
    if (fired.length) parts.push(`⚠ ${fired[0].message.replace(/^PAPER /, '').split(' - ')[0].split(' (')[0]}`)
    if (future) parts.push('later today')
    else if (gap) parts.push(`no feed ${gap.from}-${gap.to}`)
    else {
      // whose line it is: an earlier session's position says so (it is not today's trade)
      if (at) parts.push(`${at.day !== day ? `${weekdayShort(at.day)} ` : ''}S5 ${signedRs(at.value)}`)
      if (nifty != null) parts.push(`NIFTY ${nifty.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`)
    }
    setScrub({ x, text: parts.join(' · '), tone: future || gap ? '' : pnlClass(at?.value) })
  }
  const lastPoint = points[points.length - 1]

  return (
    <div
      className={`td-timeline v2 v3${live ? ' live' : ''}${s5Open ? ' held' : ''}${carry ? ' carrying' : ''}${exiting ? ' exiting' : ''}${replay === 'back' ? ' rewinding' : ''}${focus ? ` focusing focus-${focus}` : ''}`}
      aria-label={carry && prevDay ? `Session timeline: ${weekdayShort(prevDay)}'s S5 held into today until 09:30, and today's session` : 'Session timeline: today, overnight, next session'}
    >
      <div className="td-strip" style={{ transform: `translateX(${offset}%)` }}>
        {prevDay && (
          <>
            <div className="td-seg day prev" style={{ left: `${-CARRY}%`, width: `calc(${PREV_DAY}% - 3px)` }}>
              <i style={{ width: '100%' }} />
            </div>
            <div className="td-seg night prev" style={{ left: `calc(${-PREV_NIGHT}% + 3px)`, width: `calc(${PREV_NIGHT}% - 6px)` }}>
              <i style={{ width: '100%' }} />
            </div>
            <span className="td-hour start prev" style={{ left: `${-CARRY}%` }}>
              {weekdayShort(prevDay)} 09:15
            </span>
            <span className="td-hour end prev" style={{ left: `calc(${-PREV_NIGHT}% - 3px)` }}>
              15:30
            </span>
            <span className="td-seg-note night prev" style={{ left: `${-PREV_NIGHT / 2}%` }}>
              <Icon name="moon" />
            </span>
          </>
        )}
        <div className={`td-seg day${marketDay ? '' : ' off'}`} style={{ left: 0, width: `calc(${SEG_DAY}% - 3px)` }}>
          <i style={{ width: fill(0, SEG_DAY) }} />
        </div>
        <div className="td-seg night" style={{ left: `calc(${SEG_DAY}% + 3px)`, width: `calc(${SEG_NIGHT}% - 6px)` }}>
          <i style={{ width: fill(SEG_DAY, SEG_NIGHT) }} />
        </div>
        <div className="td-seg next" style={{ left: `calc(${SEG_DAY + SEG_NIGHT}% + 3px)`, width: `calc(${morning}% - 3px)` }}>
          <i style={{ width: fill(SEG_DAY + SEG_NIGHT, morning) }} />
        </div>
        {marketDay && HOURS.slice(1, -1).map((hour) => <i key={`grid-${hour}`} className="td-gridline" style={{ left: `${onDay(`${hour}:00`)}%` }} />)}
        {gaps.map((gap) => (
          <i
            key={gap.from}
            className="td-gap"
            style={{ left: `${xOf(day, gap.from)}%`, width: `${Math.max(0.4, (xOf(day, gap.to) ?? 0) - (xOf(day, gap.from) ?? 0))}%` }}
            aria-label={`No live feed ${gap.from}-${gap.to} (${gap.minutes} min)`}
          />
        ))}
        {points.length > 1 && (
          <svg className="td-track-chart" viewBox={`${-CARRY} 0 ${100 + CARRY} 24`} preserveAspectRatio="none" aria-hidden="true" style={{ left: `${-CARRY}%`, width: `${100 + CARRY}%` }}>
            <defs>
              <clipPath id="td-track-up">
                <rect x={-CARRY} y="0" width={100 + CARRY} height="12" />
              </clipPath>
              <clipPath id="td-track-down">
                <rect x={-CARRY} y="12" width={100 + CARRY} height="12" />
              </clipPath>
            </defs>
            <line className="zero" x1="0" x2={SEG_DAY} y1="12" y2="12" />
            {prevDay && <line className="zero" x1={-CARRY} x2={-PREV_NIGHT} y1="12" y2="12" />}
            <path className="area up" d={area} clipPath="url(#td-track-up)" />
            <path className="area down" d={area} clipPath="url(#td-track-down)" />
            <path className="line up" d={line} clipPath="url(#td-track-up)" />
            <path className="line down" d={line} clipPath="url(#td-track-down)" />
          </svg>
        )}
        {pins.map((pin) => (
          <i key={`${pin.ts}${pin.message}`} className={`td-pin ${pin.level}`} style={{ left: `${pin.x}%` }} aria-label={`${clockOf(pin.ts)} · ${pin.message}`} />
        ))}
        {!marketDay && <span className="td-seg-note" style={{ left: 0 }}>no session today</span>}
        <span className="td-seg-note night" style={{ left: `${SEG_DAY + SEG_NIGHT / 2}%` }}>
          <Icon name="moon" />
          {s5Open && !carry ? 'S5 held overnight' : 'overnight'}
        </span>
        {marketDay &&
          HOURS.map((hour) => (
            <span key={hour} className={`td-hour${hour === '09:15' ? ' start' : hour === '15:30' ? ' end' : ''}`} style={{ left: `${onDay(`${hour}:00`)}%` }}>
              {/* with an earlier session in the strip (or after midnight), the session's day is named */}
              {hour === '09:15' && (prevDay || day !== today.calendar_date) ? `${weekdayShort(day)} ${hour}` : hour}
            </span>
          ))}
        {/* the carried position: in from its entry, across the night, to this morning's 09:30 exit */}
        {carry && prevEntryX != null && (
          <>
            <i className="td-lane open carry" style={{ left: `${prevEntryX}%`, width: `${Math.max(0, Math.min(now, exitAt) - prevEntryX)}%` }} />
            <i className="td-lane plan carry" style={{ left: `${Math.min(now, exitAt)}%`, width: `${Math.max(0, exitAt - now)}%` }} />
            <div className="td-events carry" style={{ left: `${prevEntryX}%` }}>
              <span className="td-event carry" aria-label={`${weekdayShort(prevDay!)}'s S5, entered ${prevEntryTs ? clockOf(prevEntryTs) : '09:30'}, is held until today's 09:30 exit`}>
                <Icon name="enter" />
                {weekdayShort(prevDay!)} S5 held
                {carried?.atm_strike != null && <small>{carried.atm_strike}</small>}
                <em>→ 09:30</em>
              </span>
            </div>
          </>
        )}
        {shown?.closed && closedAt != null && (
          <>
            <i className={`td-lane closed ${pnlClass(shown.closed.net)}${focus === 'closed' ? ' active' : ''}`} style={{ left: `${prevEntryX ?? 0}%`, width: `${closedAt - (prevEntryX ?? 0)}%` }} />
            <i className="td-lane-hit" style={{ left: 0, width: `${Math.max(closedAt, 1.5)}%` }} {...hover('closed')} />
          </>
        )}
        {shown?.opened?.status === 'open' && openedAt != null && (
          <>
            <i className={`td-lane open${focus === 'opened' ? ' active' : ''}`} style={{ left: `${openedAt}%`, width: `${Math.max(0, now - openedAt)}%` }} />
            <i className={`td-lane plan${focus === 'opened' ? ' active' : ''}`} style={{ left: `${Math.max(now, openedAt)}%`, width: `${Math.max(0, 100 - Math.max(now, openedAt))}%` }} />
            <i className="td-lane-hit" style={{ left: `${openedAt}%`, width: `${100 - openedAt}%` }} {...hover('opened')} />
          </>
        )}
        {shown && (
          <div className="td-events" style={{ left: `${eventsAt}%` }}>
            {shown.closed && (
              <span
                {...hover('closed')}
                role="button"
                tabIndex={0}
                onClick={() => setReplay('back')}
                onKeyDown={(event) => (event.key === 'Enter' || event.key === ' ') && setReplay('back')}
                className={`td-event closed replayable ${pnlClass(shown.closed.net)}${focus === 'closed' ? ' active' : ''}`}
                aria-label={`${weekdayShort(shown.closed.day)}'s S5 position, held overnight, was bought back at ${shown.closed.at}: ${signedRs(shown.closed.net)} net. Click to replay the hand-over`}
              >
                <Icon name="check" />
                {weekdayShort(shown.closed.day)} S5 {shown.closed.status === 'closed' ? 'closed' : STATUS_LABEL[shown.closed.status]?.toLowerCase() ?? shown.closed.status}
                <b>{signedRs(shown.closed.net)}</b>
              </span>
            )}
            {shown.opened && (
              <span
                {...hover('opened')}
                className={`td-event opened ${shown.opened.status}${focus === 'opened' ? ' active' : ''}`}
                aria-label={
                  shown.opened.status === 'open'
                    ? `Today's S5 entered at ${shown.opened.at}${shown.opened.strike != null ? ` (${shown.opened.strike} straddle)` : ''}, held until ${next ? weekdayShort(next) : 'the next session'} 09:30`
                    : `Today's S5: ${STATUS_LABEL[shown.opened.status] ?? shown.opened.status}`
                }
              >
                <Icon name={shown.opened.status === 'open' || shown.opened.status === 'closed' ? 'enter' : 'alert'} />
                S5 {shown.opened.status === 'open' ? 'entered' : shown.opened.status === 'closed' ? 'entered · closed' : STATUS_LABEL[shown.opened.status]?.toLowerCase() ?? shown.opened.status}
                {shown.opened.status === 'open' && shown.opened.strike != null && <small>{shown.opened.strike}</small>}
              </span>
            )}
          </div>
        )}
        {marks.map((mark) => (
          <div key={mark.title} className={`td-mark ${mark.align ?? ''}${mark.s5 ? ' s5' : ''}${now >= mark.at ? ' done' : ''}`} style={{ left: `${mark.at}%` }} aria-label={mark.title}>
            <b />
            {mark.label && (
              <span>
                {mark.s5 && <Icon name="flag" />}
                {mark.label}
              </span>
            )}
          </div>
        ))}
        {next && (
          <div className={`td-mark end s5${now >= 100 ? ' done' : ''}`} style={{ left: '100%' }} aria-label={`${dayShortLabel(next)} 09:30: the S5 position is bought back, then the next entries`}>
            <b />
            <span>
              <Icon name="flag" />
              {weekdayShort(next)} 09:30 {s5Open && !carry ? 'S5 exit' : 'entries'}
            </span>
          </div>
        )}
        {/* the moment the carried position's exit lands: rings and its result rise from the exit point */}
        {exiting && shown?.closed && closedAt != null && (
          <div key={handoverAt ?? 'replay'} className={`td-exit-burst ${pnlClass(shown.closed.net)}`} style={{ left: `${closedAt}%` }}>
            <i />
            <i />
            <b>
              {weekdayShort(shown.closed.day)} S5 {signedRs(shown.closed.net)}
            </b>
          </div>
        )}
        <i className="td-now-line" style={{ left: `${now}%` }} />
        {s5Open && lastPoint && Math.abs(lastPoint.x - now) < 1.5 && (
          <b className={`td-now-value ${pnlClass(lastPoint.value)}${now + offset > 88 ? ' left' : ''}`} style={{ left: `${now}%` }}>
            {signedRs(lastPoint.value)}
          </b>
        )}
        <div className="td-now" style={{ left: `${now}%` }}>
          <i />
        </div>
      </div>
      {focus && shown && (
        <TradeSpotlight
          kind={focus}
          events={shown}
          closedAt={closedAt}
          openedAt={openedAt}
          now={now}
          nowMs={nowMs}
          nextExit={nextExit}
          next={next}
          flags={hovered != null}
          at={hovered == null && underScrub ? scrub : null}
        />
      )}
      <div className="td-scrub" onMouseMove={onScrub} onMouseLeave={() => setScrub(null)} />
      {scrub && (
        <>
          <i className="td-scrub-line" style={{ left: `${scrub.x}%` }} />
          {!underScrub && (
            <span className={`td-scrub-tip ${scrub.tone}${scrub.x < 8 ? ' start' : scrub.x > 92 ? ' end' : ''}`} style={{ left: `${scrub.x}%` }}>
              {scrub.text}
            </span>
          )}
        </>
      )}
    </div>
  )
}

/** One S5 trade in the spotlight: its span on the track banded with brackets (yesterday's carried in from the left edge;
 * today's from its entry to the next 09:30 exit, the part already held filled), its entry and exit flagged above the
 * track (when hovered by its chip or lane - the scrubber has the top then), and a card with its story below. */
function TradeSpotlight({ kind, events, closedAt, openedAt, now, nowMs, nextExit, next, flags, at }: {
  kind: 'closed' | 'opened'
  events: TimelineEvents
  closedAt: number | null
  openedAt: number | null
  now: number
  nowMs: number
  nextExit: number | null
  next: string | null
  flags: boolean
  at: { text: string; tone: string } | null // the scrubbed minute, when the trade is hovered on the track
}) {
  const event = kind === 'closed' ? events.closed : events.opened
  if (!event) return null
  const trade = event.trade
  const legs = trade.legs ?? []
  const entryTs = legs.map((leg) => fillTs(leg.entry)).filter((ts): ts is string => !!ts).sort()[0] ?? null
  const exitTs = legs.map((leg) => fillTs(leg.exit)).filter((ts): ts is string => !!ts).sort().slice(-1)[0] ?? null
  const credit = legs.length ? creditPoints(legs, 'entry') : null
  const left = kind === 'closed' ? 0 : (openedAt ?? 0)
  const right = kind === 'closed' ? (closedAt ?? 0) : 100
  const tone = kind === 'closed' ? pnlClass(events.closed?.net) : ''
  const cardLeft = `clamp(0px, ${left}%, calc(100% - 340px))`
  const atRow = at && (
    <div className={`td-trade-at ${at.tone}`}>
      <Icon name="target" /> {at.text}
    </div>
  )
  if (kind === 'closed' && events.closed) {
    const closed = events.closed
    return (
      <>
        <i className={`td-trade-band closed carried ${tone}`} style={{ left: `${left}%`, width: `${Math.max(1.2, right - left)}%` }} />
        {flags && (
          // its stretch of today's bar is only the 09:15-09:30 morning: one flag carries both ends
          <span className={`td-trade-flag ${tone}`} style={{ left: 0 }}>
            <Icon name="enter" /> in from {weekdayShort(closed.day)} {entryTs ? clockOf(entryTs).slice(0, 5) : '09:30'} → <Icon name="check" /> bought back {closed.at}
          </span>
        )}
        <div className={`td-trade-card ${tone}`} style={{ left: cardLeft }}>
          {atRow}
          <header>
            <span>
              <Icon name="check" /> {weekdayShort(closed.day)} S5 · closed {exitTs?.slice(0, 10) === entryTs?.slice(0, 10) ? 'same day' : 'this morning'}
            </span>
            <b className={pnlClass(closed.net)}>{signedRs(closed.net)}</b>
          </header>
          <div className="when">
            {entryTs ? `${weekdayShort(entryTs.slice(0, 10))} ${clockOf(entryTs)}` : '—'}
            <i />
            {exitTs ? `${weekdayShort(exitTs.slice(0, 10))} ${clockOf(exitTs)}` : '—'}
            <small>{heldFor(entryTs, exitTs) ?? ''}</small>
          </div>
          <table>
            <tbody>
              {legs.map((leg) => {
                const out = exitPrice(leg)
                const pts = out == null ? null : leg.side === 'SELL' ? entryPrice(leg) - out : out - entryPrice(leg)
                return (
                  <tr key={`${leg.type}${leg.strike}${leg.side}`}>
                    <td>
                      {leg.type} {leg.strike}
                    </td>
                    <td>
                      {entryPrice(leg).toFixed(2)} → {out == null ? '—' : out.toFixed(2)}
                    </td>
                    <td className={pnlClass(pts)}>{pts == null ? '—' : signedRs(pts * trade.qty)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div className="facts">
            {trade.gross_rs != null && trade.charges_rs != null ? (
              <span>
                <b className={pnlClass(trade.gross_rs)}>{signedRs(trade.gross_rs)}</b> − ₹{Math.round(trade.charges_rs).toLocaleString('en-IN')} charges = <b className={pnlClass(closed.net)}>{signedRs(closed.net)}</b>
              </span>
            ) : (
              <span>
                net <b className={pnlClass(closed.net)}>{signedRs(closed.net)}</b>
              </span>
            )}
          </div>
        </div>
      </>
    )
  }
  const opened = events.opened!
  const toExit = nextExit != null ? Math.max(0, Math.round((nextExit - nowMs) / 1000)) : null
  return (
    <>
      <i className="td-trade-band opened" style={{ left: `${left}%`, width: `${right - left}%` }}>
        <i style={{ width: `${Math.max(0, Math.min(100, ((now - left) / (right - left || 1)) * 100))}%` }} />
      </i>
      {flags && (
        <>
          <span className="td-trade-flag" style={{ left: `${left}%` }}>
            <Icon name="enter" /> sold {opened.at}
            {opened.strike != null && ` · ${opened.strike}`}
          </span>
          {next && (
            <span className="td-trade-flag exit end" style={{ left: '100%' }}>
              <Icon name="flag" /> buy back {weekdayShort(next)} 09:30
            </span>
          )}
        </>
      )}
      <div className="td-trade-card" style={{ left: cardLeft }}>
        {atRow}
        <header>
          <span>
            <Icon name="enter" /> S5 · {opened.status === 'open' ? 'open' : (STATUS_LABEL[opened.status] ?? opened.status).toLowerCase()} since {opened.at.slice(0, 5)}
          </span>
          {opened.mtm != null && <b className={pnlClass(opened.mtm)}>{signedRs(opened.mtm)}</b>}
        </header>
        <div className="when">
          {weekdayShort(trade.trading_date)} {opened.at}
          <i />
          {next ? `${weekdayShort(next)} 09:30` : '—'}
          <small>{toExit != null && opened.status === 'open' ? `in ${formatCountdown(toExit)}` : ''}</small>
        </div>
        <table>
          <tbody>
            {legs.map((leg) => (
              <tr key={`${leg.type}${leg.strike}${leg.side}`}>
                <td>
                  {leg.type} {leg.strike}
                </td>
                <td>
                  {leg.side === 'SELL' ? 'sold' : 'bought'} {entryPrice(leg).toFixed(2)}
                </td>
                <td>{trade.expiry ? `exp ${utcDay(trade.expiry).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', timeZone: 'UTC' })}` : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="facts">
          {credit != null && (
            <span>
              credit <b>{credit.toFixed(2)} pts</b> · <b>{formatRs(credit * trade.qty)}</b>
            </span>
          )}
          {entryTs && opened.status === 'open' && <span>held {heldFor(entryTs, new Date(nowMs).toISOString()) ?? '0h 00m'}</span>}
        </div>
      </div>
    </>
  )
}

/** Seconds of the clock as "HH:MM" (past 24:00 for the next session's morning). */
const clockLabelOf = (seconds: number): string => `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}`

/** One strategy's MTM through the day, on the same 09:30-15:29 axis as the chart, so it fills in as the day goes. */
function Sparkline({ marks, color }: { marks: LabMark[]; color: string }) {
  if (marks.length < 2) return <div className="td-spark empty">line after a few minutes</div>
  const width = 160
  const height = 40
  const values = marks.map((mark) => mark.mtm_rs)
  const low = Math.min(0, ...values)
  const high = Math.max(0, ...values)
  const range = high - low || 1
  const x = (minute: string) => ((secondsOf(minute) / 60 - MARK_FROM) / (MARK_TO - MARK_FROM)) * width
  const y = (value: number) => height - 3 - ((value - low) / range) * (height - 6)
  const line = marks.map((mark) => `${x(mark.minute).toFixed(1)},${y(mark.mtm_rs).toFixed(1)}`).join(' ')
  const first = x(marks[0].minute).toFixed(1)
  const last = x(marks[marks.length - 1].minute).toFixed(1)
  const zero = y(0).toFixed(1)
  return (
    <svg className="td-spark" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      <polygon points={`${first},${zero} ${line} ${last},${zero}`} style={{ fill: color }} opacity="0.16" />
      <line x1="0" x2={width} y1={zero} y2={zero} className="td-spark-zero" vectorEffect="non-scaling-stroke" />
      <polyline points={line} fill="none" style={{ stroke: color }} strokeWidth="1.8" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
    </svg>
  )
}

/** A trade's full legs table, in a dialog so it never pushes the dashboard into scrolling. */
function LegsDialog({ trade, live, onClose }: { trade: LabDay; live: LabLive | undefined; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="td-dialog-backdrop" onClick={onClose}>
      <div className="td-dialog" role="dialog" aria-modal="true" aria-label="Legs and fills" onClick={(event) => event.stopPropagation()}>
        <div className="td-dialog-head">
          <strong>
            {STRATEGY_SHORT[trade.strategy]} · {shortDay(trade.trading_date)} · legs &amp; fills
          </strong>
          <PaperBadge />
          <button type="button" className="research-chip" onClick={onClose}>
            <Icon name="close" />
            Close
          </button>
        </div>
        {trade.legs?.length ? <LegsTable legs={trade.legs} live={trade.status === 'open' ? live : undefined} qty={trade.qty} /> : <p className="dim">No legs.</p>}
        {trade.invalid_reason && <small className="lab-note">{trade.invalid_reason}</small>}
        {trade.notes && <small className="lab-note">{trade.notes}</small>}
      </div>
    </div>
  )
}

/** An S5 paper trade against the candles: each leg's fill at its real minute on the entry day and on the exit day. */
function ChartDialog({ trade, theme, onClose }: { trade: LabDay; theme: ThemeName; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="td-dialog-backdrop" onClick={onClose}>
      <div className="td-dialog wide fit" role="dialog" aria-modal="true" aria-label="Verify against the chart" onClick={(event) => event.stopPropagation()}>
        <LabDayChart date={trade.trading_date} strategy={trade.strategy} source="paper" theme={theme} onClose={onClose} fit trade={trade} />
      </div>
    </div>
  )
}

/** A trade's status pill. An OPEN one carries two layers for its light - a conic ring that turns round its border and a
 * band that sweeps across it - both moved by transform alone, so the always-on pill costs no repaints. */
function StatusPill({ status }: { status: string }) {
  return (
    <span className={`lab-status ${status}`}>
      {status === 'open' && (
        <>
          <i className="lab-status-ring" aria-hidden="true" />
          <i className="lab-status-sweep" aria-hidden="true" />
        </>
      )}
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

function FlagChip({ trade }: { trade: LabDay }) {
  if (!trade.invalid_reason) return null
  const flag = flagLabel(trade.invalid_reason)
  return (
    <span className={flag.manual ? 'lab-manual' : 'lab-invalid'} title={`${trade.invalid_reason}. Its timing is not the strategy's own, so it does not compare cleanly with the backtest.`}>
      {flag.text}
    </span>
  )
}

/** S5's paper history as bars (newest right), with S8's view of the same trades (no Friday entries). */
function TrackRecord({ rows }: { rows: LabDay[] }) {
  const done = rows.filter((row) => (row.status === 'closed' || row.status === 'missed_exit') && row.net_rs != null).sort((a, b) => a.trading_date.localeCompare(b.trading_date))
  if (!done.length) return <div className="td-record empty">S5's paper record starts with its first closed trade.</div>
  const stats = (list: LabDay[]) => {
    const total = list.reduce((sum, row) => sum + (row.net_rs ?? 0), 0)
    return { total, count: list.length, wins: list.filter((row) => (row.net_rs ?? 0) > 0).length, avg: list.length ? total / list.length : 0 }
  }
  const s5 = stats(done)
  const s8 = stats(done.filter((row) => !isFriday(row.trading_date)))
  const recent = done.slice(-RECORD_BARS)
  const biggest = Math.max(1, ...recent.map((row) => Math.abs(row.net_rs ?? 0)))
  return (
    <div className="td-record">
      <div className="td-record-head">
        <span>
          <Icon name="trophy" /> S5 paper record
        </span>
        <TweenRs value={s5.total} />
        <small>
          {s5.count} trades · {Math.round((100 * s5.wins) / s5.count)}% won · avg {signedRs(s5.avg)}
        </small>
      </div>
      <div className="td-bars" aria-label="Recent S5 paper trades, newest on the right">
        {recent.map((row, index) => {
          const value = row.net_rs ?? 0
          return (
            <span key={row.trading_date} style={{ '--i': index } as CSSProperties} title={`${dayLabel(row.trading_date)}: ${formatRs(value)}${isFriday(row.trading_date) ? ' · Friday entry (S8 skips it)' : ''}${row.invalid_reason ? ' · flagged' : ''}`}>
              <i
                className={`${value >= 0 ? 'up' : 'down'}${row.invalid_reason ? ' flagged' : ''}${isFriday(row.trading_date) ? ' friday' : ''}`}
                style={{ height: `${Math.max(4, (Math.abs(value) / biggest) * 50)}%` }}
              />
            </span>
          )
        })}
      </div>
      <div className="td-record-foot">
        <span>S8 view · no Friday</span>
        <TweenRs value={s8.total} />
        <small className="dim">
          {s8.count} trades · hollow bar = Friday entry · striped = flagged
        </small>
      </div>
    </div>
  )
}

const fillTs = (value: LabLeg['entry'] | LabLeg['exit']): string | null => (value && typeof value === 'object' ? value.ts : null)

const heldFor = (from: string | null, to: string | null): string | null => {
  if (!from || !to) return null
  const minutes = Math.round((Date.parse(to) - Date.parse(from)) / 60_000)
  return minutes > 0 ? `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m` : null
}

/** The open position's MTM range so far today: its high and low (with when), and how far it now sits off the high. */
function IntradayRange({ marks }: { marks: LabMark[] }) {
  const high = marks.reduce((best, mark) => (mark.mtm_rs > best.mtm_rs ? mark : best))
  const low = marks.reduce((worst, mark) => (mark.mtm_rs < worst.mtm_rs ? mark : worst))
  const now = marks[marks.length - 1]
  const span = high.mtm_rs - low.mtm_rs || 1
  const at = (value: number) => Math.min(100, Math.max(0, ((value - low.mtm_rs) / span) * 100))
  const zero = low.mtm_rs < 0 && high.mtm_rs > 0 ? at(0) : null
  const nowAt = at(now.mtm_rs)
  const base = zero ?? (now.mtm_rs >= 0 ? 0 : 100) // the bar fills from ₹0 (or the nearer end) to now
  return (
    <div className="td-range" title="Mark-to-market range of this position so far today (shorts marked at the ask)">
      <div className="td-range-head">
        <span>
          <Icon name="range" /> Today's range
        </span>
        <small>
          off the high <b className={pnlClass(now.mtm_rs - high.mtm_rs)}>{signedRs(now.mtm_rs - high.mtm_rs)}</b>
        </small>
      </div>
      <div className="td-range-body">
        <span className="td-range-end">
          <b className={pnlClass(low.mtm_rs)}>{signedRs(low.mtm_rs)}</b> {markClock(low.minute)}
        </span>
        <div className="td-range-track">
          <i className={`fill ${now.mtm_rs >= 0 ? 'up' : 'down'}`} style={{ left: `${Math.min(base, nowAt)}%`, width: `${Math.abs(nowAt - base)}%` }} />
          {zero != null && <i className="zero" style={{ left: `${zero}%` }} />}
          <em style={{ left: `${nowAt}%` }} title={`Now ${signedRs(now.mtm_rs)}`} />
        </div>
        <span className="td-range-end high">
          <b className={pnlClass(high.mtm_rs)}>{signedRs(high.mtm_rs)}</b> {markClock(high.minute)}
        </span>
      </div>
    </div>
  )
}

type Yard = { count: number; avg: number; won: number }

function yardOf(rows: LabDay[]): Yard | null {
  const done = rows.filter((row) => row.net_rs != null)
  if (!done.length) return null
  const total = done.reduce((sum, row) => sum + (row.net_rs ?? 0), 0)
  return { count: done.length, avg: total / done.length, won: (100 * done.filter((row) => (row.net_rs ?? 0) > 0).length) / done.length }
}

/** Where NIFTY sits against the straddle's expiry breakevens (strike -/+ the credit collected): the room left on each
 * side as numbers - the breakeven values live in those side boxes, so nothing is clipped at the bar's ends - and the
 * strike, NIFTY at entry and NIFTY now on one bar. Before expiry the position still holds time value, so this is room
 * left, not a stop. */
function PositionMap({ strike, credit, spot, entrySpot }: { strike: number; credit: number; spot: number | null; entrySpot: number | null | undefined }) {
  const lower = strike - credit
  const upper = strike + credit
  const pad = credit * 0.35
  const from = lower - pad
  const to = upper + pad
  const at = (price: number) => Math.min(100, Math.max(0, ((price - from) / (to - from)) * 100))
  const fmt = (price: number) => price.toLocaleString('en-IN', { maximumFractionDigits: 0 })
  const roomDown = spot == null ? null : spot - lower
  const roomUp = spot == null ? null : upper - spot
  const tone = (room: number | null) => (room == null ? '' : room <= 0 ? 'out' : room < credit * 0.25 ? 'edge' : 'safe')
  const nowAt = spot == null ? null : at(spot)
  const nearest = roomDown == null || roomUp == null ? null : Math.min(roomDown, roomUp)
  return (
    <div className="td-map" title="Breakevens at expiry: the strike minus/plus the credit collected. Before expiry the position still holds time value, so this is the room left, not a stop.">
      <div className="td-map-head">
        <span>
          <Icon name="target" /> NIFTY vs breakevens
        </span>
        <small>at expiry · strike ± {fmt(credit)} credit</small>
      </div>
      <div className="td-map-body">
        <div className={`td-map-room ${tone(roomDown)}`}>
          <b>
            {roomDown == null ? '—' : roomDown > 0 ? (
              <>
                <Icon name="down" className="tri" /> {fmt(roomDown)}
              </>
            ) : (
              `${fmt(-roomDown)} past`
            )}
          </b>
          <small>pts to {fmt(lower)}</small>
        </div>
        <div className="td-map-bar">
          {spot != null && nowAt != null && (
            <span className={`td-map-now-label${nowAt < 14 ? ' start' : nowAt > 86 ? ' end' : ''}`} style={{ left: `${nowAt}%` }}>
              NIFTY {fmt(spot)}
            </span>
          )}
          <div className="td-map-track">
            <i className="zone" style={{ left: `${at(lower)}%`, width: `${at(upper) - at(lower)}%` }} />
            <b className="be" style={{ left: `${at(lower)}%` }} />
            <b className="be" style={{ left: `${at(upper)}%` }} />
            <b className="strike" style={{ left: `${at(strike)}%` }} />
            {entrySpot != null && <em className="entry" style={{ left: `${at(entrySpot)}%` }} title={`NIFTY at entry ${entrySpot.toFixed(2)}`} />}
            {nowAt != null && <em className={`now ${tone(nearest)}`} style={{ left: `${nowAt}%` }} />}
          </div>
          <span className="td-map-strike" style={{ left: `${at(strike)}%` }}>
            strike {fmt(strike)}
            {entrySpot != null && <> · ◇ entry {fmt(entrySpot)}</>}
          </span>
        </div>
        <div className={`td-map-room right ${tone(roomUp)}`}>
          <b>
            {roomUp == null ? '—' : roomUp > 0 ? (
              <>
                {fmt(roomUp)} <Icon name="up" className="tri" />
              </>
            ) : (
              `${fmt(-roomUp)} past`
            )}
          </b>
          <small>pts to {fmt(upper)}</small>
        </div>
      </div>
    </div>
  )
}

/** Today's S5 against its own backtest history: the same weekday, the same calendar month, and S8 overall. */
function Yardstick({ day, s5, s8 }: { day: string; s5: LabDay[]; s8: LabDay[] }) {
  const weekday = utcDay(day).getUTCDay()
  const month = day.slice(5, 7)
  const sameWeekday = yardOf(s5.filter((row) => utcDay(row.trading_date).getUTCDay() === weekday))
  const sameMonth = yardOf(s5.filter((row) => row.trading_date.slice(5, 7) === month))
  const s8All = yardOf(s8)
  if (!sameWeekday && !s8All) return null
  const card = (label: string, yard: Yard | null, hint: string) =>
    yard && (
      <div className="td-yard" title={`${hint}: ${yard.count} trades, ${yard.won.toFixed(0)}% won`}>
        <small>{label}</small>
        <b className={pnlClass(yard.avg)}>{signedRs(yard.avg)}</b>
        <em>
          {yard.won.toFixed(0)}% won · {yard.count}
        </em>
        <i className="td-yard-won" style={{ width: `${yard.won}%` }} />
      </div>
    )
  return (
    <div className="td-yardstick" aria-label="Backtest averages per trade">
      <span className="td-yardstick-label">
        <Icon name="history" /> Backtest avg / trade
      </span>
      <div className="td-yards">
        {card(`S5 · ${WEEKDAY_NAMES[weekday]}s`, sameWeekday, `S5 backtest trades entered on a ${WEEKDAY_NAMES[weekday]}`)}
        {card(`S5 · ${utcDay(day).toLocaleDateString('en-IN', { month: 'long', timeZone: 'UTC' })}`, sameMonth, 'S5 backtest trades entered in this calendar month, every year')}
        {card('S8 · all', s8All, 'S8 (S5 without Friday entries), every backtest trade')}
      </div>
    </div>
  )
}

/** An S5 trade that already closed - typically the one bought back at 09:30 this morning - in full: when it was
 * entered and closed, each leg's sale and buy-back, and how the net came out. */
function ClosedS5({ trade, today, backtest, onLegs, onChart }: { trade: LabDay; today: string; backtest: LabDay | null | undefined; onLegs: (trade: LabDay) => void; onChart: (trade: LabDay) => void }) {
  const legs = trade.legs ?? []
  const entryTs = legs.map((leg) => fillTs(leg.entry)).filter(Boolean).sort()[0] ?? null
  const exitTs = legs.map((leg) => fillTs(leg.exit)).filter(Boolean).sort().slice(-1)[0] ?? null
  const credit = legs.length ? creditPoints(legs, 'entry') : null
  const kept = trade.net_pts != null && credit ? (trade.net_pts / credit) * 100 : null
  const closedToday = exitTs?.slice(0, 10) === today
  const held = heldFor(entryTs, exitTs)
  const gap = backtest?.net_rs != null ? (trade.net_rs ?? 0) - backtest.net_rs : null
  const strikeDiffers = backtest?.atm_strike != null && trade.atm_strike != null && backtest.atm_strike !== trade.atm_strike
  return (
    <section className="td-closed" aria-label="Closed S5 trade">
      <div className="td-closed-when">
        <span>
          <Icon name="check" className="td-closed-icon" /> {closedToday ? 'Closed this morning' : exitTs ? `Closed ${dayShortLabel(exitTs.slice(0, 10))}` : 'Closed'}
        </span>
        <div className="td-held" title={`Entered ${entryTs ? `${entryTs.slice(0, 10)} ${clockOf(entryTs)}` : '—'} · exited ${exitTs ? `${exitTs.slice(0, 10)} ${clockOf(exitTs)}` : '—'} (IST)`}>
          <span className="td-held-end">
            <b>{entryTs ? clockOf(entryTs).slice(0, 5) : '—'}</b>
            <small>{dayShortLabel(entryTs?.slice(0, 10) ?? trade.trading_date)}</small>
          </span>
          <span className="td-held-line">
            <i />
            {held && <em>held {held}</em>}
          </span>
          <span className="td-held-end exit">
            <b>{exitTs ? clockOf(exitTs).slice(0, 5) : '—'}</b>
            <small>{closedToday ? 'today' : exitTs ? dayShortLabel(exitTs.slice(0, 10)) : ''}</small>
          </span>
        </div>
        <div className="td-tile-tags">
          <StatusPill status={trade.status} />
          <FlagChip trade={trade} />
        </div>
      </div>
      <div className="td-closed-net">
        <span>Net P&amp;L</span>
        <TweenRs value={trade.net_rs} />
        <small>
          <em className={pnlClass(trade.net_pts)}>{formatPts(trade.net_pts)} pts</em>
          {kept != null && ` · ${kept.toFixed(0)}% of credit kept`}
        </small>
        <div className="td-vs-bt">
          {backtest === undefined ? (
            <span className="dim">backtest loading…</span>
          ) : backtest === null || backtest.net_rs == null ? (
            <span className="dim" title="The backtest row for this trade is not built yet: it needs the exit day's 09:30 candles (the S5 candle build downloads them).">
              backtest pending
            </span>
          ) : (
            <>
              <span title="The candle backtest for the same entry day and rule (sell at the 09:30 open, buy back at the next trading day's 09:30 close), at the same size">
                backtest <b className={pnlClass(backtest.net_rs)}>{signedRs(backtest.net_rs)}</b>
              </span>
              <span title="Paper minus backtest: slippage plus any timing difference">
                gap <b className={pnlClass(gap)}>{signedRs(gap)}</b>
              </span>
              {strikeDiffers && (
                <span
                  className="td-vs-warn"
                  title={`Not the same contracts - backtest strike ${backtest.atm_strike}, paper strike ${trade.atm_strike}: the gap is mostly the different strike and entry time, not slippage`}
                >
                  <Icon name="alert" /> strike {backtest.atm_strike} ≠ {trade.atm_strike}
                </span>
              )}
            </>
          )}
        </div>
      </div>
      <div className="td-closed-legs">
        <table className="td-legs compact">
          <thead>
            <tr>
              <th>Leg</th>
              <th>Sold</th>
              <th>Bought</th>
              <th>P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {legs.map((leg) => {
              const out = exitPrice(leg)
              const pts = out == null ? null : leg.side === 'SELL' ? entryPrice(leg) - out : out - entryPrice(leg)
              return (
                <tr key={`${leg.type}${leg.strike}${leg.side}`}>
                  <th>
                    <span className={leg.type === 'CE' ? 'td-ce' : 'td-pe'}>{leg.type}</span> {leg.strike}
                  </th>
                  <td>{entryPrice(leg).toFixed(2)}</td>
                  <td>{out == null ? '—' : out.toFixed(2)}</td>
                  <td className={pnlClass(pts)}>{pts == null ? '—' : signedRs(pts * trade.qty)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <div className="td-closed-math">
          {/* in rupees, so it adds up from the leg rows above: the legs' total, less the round trip's charges, is the net */}
          <span
            title={`The legs' P&L (${formatPts(trade.gross_pts)} pts) less charges (${formatPts(trade.charges_pts)} pts: brokerage ₹20 an order, STT 0.1% of the sell premium, exchange, SEBI, stamp duty and GST) = net ${formatPts(trade.net_pts)} pts a unit, × ${trade.qty} qty. The leg rows are rounded to the rupee.`}
          >
            {trade.gross_rs != null && trade.charges_rs != null ? (
              <>
                <em className={pnlClass(trade.gross_rs)}>{signedRs(trade.gross_rs)}</em> − <em className="negative">₹{Math.round(trade.charges_rs).toLocaleString('en-IN')}</em> charges ={' '}
                <b className={pnlClass(trade.net_rs)}>{signedRs(trade.net_rs)}</b>
              </>
            ) : (
              <>
                {formatPts(trade.gross_pts)} − {formatPts(trade.charges_pts)} = <b className={pnlClass(trade.net_pts)}>{formatPts(trade.net_pts)} pts</b>
              </>
            )}
          </span>
          <button type="button" className="research-chip icon-only" onClick={() => onChart(trade)} aria-label="Chart" title="Chart: each leg's fill on the candle chart of its minute, entry day and exit day">
            <Icon name="chart" />
          </button>
          <button type="button" className="research-chip icon-only" onClick={() => onLegs(trade)} aria-label="Fills" title="Fills: every leg's entry and exit quotes">
            <Icon name="list" />
          </button>
        </div>
      </div>
    </section>
  )
}

function S5Feature({
  trade, previous, today, live, marks, record, backtest, nowSeconds, theme, onChanged, onLegs, onChart,
}: {
  trade: LabDay | undefined; previous: LabDay | undefined; today: Today; live: LabLive | undefined; marks: LabMark[]; record: LabDay[]
  backtest: { s5: LabDay[]; s8: LabDay[] } | null; nowSeconds: number; theme: ThemeName; onChanged: () => void; onLegs: (trade: LabDay) => void
  onChart: (trade: LabDay) => void
}) {
  const accent = { '--accent': STRATEGY_COLOR[S5] } as CSSProperties
  const open = trade?.status === 'open'
  const missed = trade?.status === 'missed_entry'
  const mtm = trade && open ? (today.latest_mtm_rs[S5] ?? liveMtm(trade, live)) : undefined
  const value = open ? mtm : trade?.net_rs
  const credit = trade?.legs?.length ? creditPoints(trade.legs, 'entry') : null
  const points = trade ? (open ? (mtm != null && trade.qty ? mtm / trade.qty : null) : trade.net_pts) : null
  const captured = points != null && credit ? (points / credit) * 100 : null
  const spot = live?.spot?.ltp ?? null
  const enteredToday = trade?.trading_date === today.trading_date
  let exitText = '—'
  if (trade && open) {
    if (enteredToday) exitText = `${dayShortLabel(today.next_session)} · 09:30`
    else exitText = nowSeconds < secondsOf('09:30') ? `today 09:30 · in ${formatCountdown(secondsOf('09:30') - nowSeconds)}` : 'today 09:30 · due now'
  } else if (trade && !missed) {
    const fill = trade.legs?.find((leg) => leg.exit && typeof leg.exit === 'object')?.exit
    exitText = fill && typeof fill === 'object' ? `${shortDay(fill.ts.slice(0, 10))} ${clockOf(fill.ts)}` : '—'
  }
  return (
    <section className={`td-feature ${trade?.status ?? 'none'}`} style={accent}>
      <div className="td-feature-info">
        <header className="td-feature-head">
          <span className={`td-code big${open ? ' live' : ''}`}>S5</span>
          <div className="td-name">
            <strong title={STRATEGY_LABEL[S5]}>Straddle overnight</strong>
            <div className="td-tile-tags">
              <PaperBadge />
              {trade && <span className="td-tag">entered {trade.trading_date === (today.calendar_date ?? today.trading_date) ? 'today' : shortDay(trade.trading_date)}</span>}
              {trade && <FlagChip trade={trade} />}
              {trade && (
                <span
                  className={`td-tag ${isFriday(trade.trading_date) ? 'bad' : 'good'}`}
                  title={isFriday(trade.trading_date) ? 'S8 skips this trade: S8 is S5 that never enters on a Friday' : 'S8 takes this trade too: S8 is S5 that never enters on a Friday'}
                >
                  {isFriday(trade.trading_date) ? 'S8 skips it (Fri)' : 'S8 takes it too'}
                </span>
              )}
            </div>
          </div>
          {trade && <StatusPill status={trade.status} />}
        </header>

        {!trade ? (
          <div className="td-feature-empty">{phaseMessage(today)}</div>
        ) : (
          <>
            <div className="td-hero-number">
              <div className="td-hero-row">
                <div className="td-hero-value">
                  <span title="Shorts marked at the ask, longs at the bid">{open ? 'Live MTM' : missed ? 'Not entered' : 'Net P&L'}</span>
                  {missed ? <b>—</b> : <HeroValue value={value} />}
                  {points != null && (
                    <small>
                      <em className={pnlClass(points)}>{formatPts(points)} pts / unit</em>
                      {captured != null && <KeptRing share={captured} />}
                    </small>
                  )}
                </div>
                <div className="td-hero-actions">
                  {trade.legs?.length ? (
                    <>
                      <button type="button" className="research-chip" onClick={() => onLegs(trade)}>
                        <Icon name="list" />
                        Legs &amp; fills
                      </button>
                      <button type="button" className="research-chip" onClick={() => onChart(trade)} title="The fills on the candle chart of their minute">
                        <Icon name="chart" />
                        Chart
                      </button>
                    </>
                  ) : null}
                  {missed && <RetryEntryButton strategy={S5} onRetried={onChanged} />}
                  {open && <ExitNowButton trade={trade} onExited={onChanged} />}
                </div>
              </div>
            </div>
            {backtest && <Yardstick day={trade.trading_date} s5={backtest.s5} s8={backtest.s8} />}
            {open && marks.length > 2 && <IntradayRange marks={marks} />}

            {trade.legs?.length ? (
              <table className="td-legs">
                <thead>
                  <tr>
                    <th>Leg</th>
                    <th>Sold</th>
                    <th>{open ? 'Now' : 'Bought'}</th>
                    <th>P&amp;L</th>
                  </tr>
                </thead>
                <tbody>
                  {trade.legs.map((leg) => {
                    const quote = live?.quotes[leg.instrument_key] ?? null
                    const now = open ? (quote ? (leg.side === 'SELL' ? quote.close_short : quote.close_long) : null) : exitPrice(leg)
                    const pts = now == null ? null : leg.side === 'SELL' ? entryPrice(leg) - now : now - entryPrice(leg)
                    return (
                      <tr key={`${leg.type}${leg.strike}${leg.side}`}>
                        <th>
                          <span className={leg.type === 'CE' ? 'td-ce' : 'td-pe'}>{leg.type}</span> {leg.strike}
                        </th>
                        <td>{entryPrice(leg).toFixed(2)}</td>
                        <PriceCell value={now} />
                        <PnlCell value={pts == null ? null : pts * trade.qty} />
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            ) : null}

            {!missed && (
              <dl className="td-facts-strip">
                <div>
                  <dt>
                    <Icon name="calendar" />
                    Expiry
                  </dt>
                  <dd>{trade.expiry ? shortDay(trade.expiry) : '—'}</dd>
                </div>
                <div>
                  <dt>
                    <Icon name="coins" />
                    Credit
                  </dt>
                  <dd>{credit != null ? `${formatPts(credit)} pts · ${formatRs(credit * trade.qty)}` : '—'}</dd>
                </div>
                <div>
                  <dt>
                    <Icon name="clock" />
                    {open ? 'Exits' : 'Exited'}
                  </dt>
                  <dd>{exitText}</dd>
                </div>
              </dl>
            )}
            {open && credit != null && trade.atm_strike != null && <PositionMap strike={trade.atm_strike} credit={credit} spot={spot} entrySpot={trade.spot_at_entry} />}

          </>
        )}

      </div>

      <div className="td-feature-right">
        {previous && (
          <ClosedS5
            trade={previous}
            today={today.calendar_date ?? today.trading_date}
            backtest={backtest ? (backtest.s5.find((row) => row.trading_date === previous.trading_date) ?? null) : undefined}
            onLegs={onLegs}
            onChart={onChart}
          />
        )}
        <div className="td-feature-chart">
          <div className="td-chart-head">
            <h3>
              <Icon name="chart" className="td-chart-icon" />
              S5 mark-to-market <PaperBadge />
            </h3>
            <span className="dim">{marks.length ? `${marks.length} minutes marked · shorts at the ask` : 'marks start at 09:30'}</span>
          </div>
          {marks.length > 0 ? (
            <MtmChart marks={marks} theme={theme} fill />
          ) : (
            <div className="td-chart-empty">The S5 line draws here from 09:30.</div>
          )}
          <TrackRecord rows={record} />
        </div>
      </div>
    </section>
  )
}

function OtherTile({ trade, mtm: markedMtm, live, marks, today, onChanged, onLegs }: { trade: LabDay; mtm: number | undefined; live: LabLive | undefined; marks: LabMark[]; today: string; onChanged: () => void; onLegs: (trade: LabDay) => void }) {
  const open = trade.status === 'open'
  const missed = trade.status === 'missed_entry'
  const mtm = open ? (markedMtm ?? liveMtm(trade, live)) : undefined
  const value = open ? mtm : trade.net_rs
  const [code, ...rest] = STRATEGY_SHORT[trade.strategy].split(' ')
  const accent = { '--accent': STRATEGY_COLOR[trade.strategy] } as CSSProperties
  return (
    <article className={`td-mini ${trade.status}`} style={accent}>
      <header className="td-tile-head">
        <span className="td-code">{code}</span>
        <div className="td-name">
          <strong>{rest.join(' ')}</strong>
          <small>
            {trade.derived_from ? derivedTag(trade) : strikesLabel(trade)}
            {trade.trading_date !== today ? ` · ${shortDay(trade.trading_date)}` : ''}
          </small>
        </div>
        <StatusPill status={trade.status} />
      </header>
      <div className="td-mini-main">
        <div className="td-pnl">
          <span>{open ? 'MTM' : missed ? 'Missed' : 'Net'}</span>
          <b className={pnlClass(value)}>{missed ? '—' : signedRs(value)}</b>
        </div>
        {marks.length > 1 ? <Sparkline marks={marks} color={STRATEGY_COLOR[trade.strategy]} /> : null}
      </div>
      <footer className="td-tile-foot">
        <FlagChip trade={trade} />
        {trade.legs?.length ? (
          <button type="button" className="research-chip" onClick={() => onLegs(trade)}>
            <Icon name="list" />
            Legs
          </button>
        ) : null}
        {missed && !trade.derived_from && <RetryEntryButton strategy={trade.strategy} onRetried={onChanged} />}
        {open && !trade.derived_from && <ExitNowButton trade={trade} onExited={onChanged} />}
      </footer>
    </article>
  )
}

function AlertsButton({ alerts, today }: { alerts: Today['alerts']; today: string }) {
  const [open, setOpen] = useState(false)
  // Counted and coloured by TODAY's alerts only: an older error stays listed but does not keep the button alarmed.
  const todays = alerts.filter((alert) => alert.trading_date === today)
  const errors = todays.filter((alert) => alert.level === 'error').length
  return (
    <div className="td-alerts-wrap">
      <button type="button" className={`td-alerts-btn${errors ? ' error' : todays.length ? ' warn' : ''}${open ? ' open' : ''}`} aria-expanded={open} onClick={() => setOpen((shown) => !shown)}>
        <Icon name="bell" className="td-bell" />
        <span className="td-alerts-word">Alerts</span> <b key={todays.length}>{todays.length}</b>
        <Icon name="chevronDown" className="td-chevron" />
      </button>
      {open && (
        <div className="td-alerts-pop" role="dialog" aria-label="Alerts">
          <div className="td-alerts-pop-head">
            {todays.length} today{alerts.length > todays.length ? ` · ${alerts.length - todays.length} earlier error${alerts.length - todays.length === 1 ? '' : 's'}` : ''}
          </div>
          {alerts.length === 0 && <p className="dim">No alerts.</p>}
          {alerts.map((alert) => (
            <div key={`${alert.ts}${alert.message}`} className={`td-alert ${alert.level}${alert.trading_date === today ? '' : ' old'}`}>
              <i />
              <span>{alert.trading_date === today ? alert.ts.slice(11, 19) : `${shortDay(alert.ts.slice(0, 10))} ${alert.ts.slice(11, 16)}`}</span>
              <p>{alert.message}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** Today's PAPER trading as a one-screen dashboard centred on S5 (the strategy being proven), with S1-S4 one switch away. */
export function LabToday({ theme, size }: { theme: ThemeName; size: LabSize }) {
  const [today, setToday] = useState<Today | null>(null)
  const [record, setRecord] = useState<LabDay[]>([])
  const [backtest, setBacktest] = useState<{ s5: LabDay[]; s8: LabDay[] } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const [showOthers, setShowOthers] = useState(readShowOthers)
  const [legsOf, setLegsOf] = useState<LabDay | null>(null)
  const [chartOf, setChartOf] = useState<LabDay | null>(null)
  const [marks, setMarks] = useState<LabMark[]>([])
  // The server's clock, as an offset from this browser's: taken from the lowest-latency response seen (each response's
  // time is corrected by half its round trip), so one slow response never drags the clocks and countdowns around.
  const clockSync = useRef<{ offset: number; rtt: number; at: number } | null>(null)
  const fetchedAt = useRef(0)
  const inFlight = useRef(false)
  // Every mark of the day, merged across polls: after the first full load only the newest minutes are requested.
  const marksStore = useRef<{ day: string; size: string; byKey: Map<string, LabMark>; latest: string; fullAt: number } | null>(null)
  const mounted = useRef(true)
  const { ref: screenRef, height } = useFitToWindow()
  useEffect(() => {
    // StrictMode mounts every effect, cleans it up, then mounts it again (dev-only) - resetting mounted.current
    // to true here (not just via the useRef() initializer) means that replay leaves it true, not stuck false.
    mounted.current = true
    return () => void (mounted.current = false)
  }, [])

  // One request at a time (a poll never overlaps the previous one). Also called directly after a manual retry or exit
  // succeeds, so the change shows without waiting for the next poll.
  const load = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    const sizeId = sizeKey(size)
    const store = marksStore.current
    // incremental once a full day is held; a full reload every 5 minutes keeps it honest
    const since = store && store.size === sizeId && Date.now() - store.fullAt < 300_000 ? store.latest || '00:00' : undefined
    const sentAt = performance.now()
    const sentWall = Date.now()
    try {
      const result = await fetchLabToday(size, since)
      if (!mounted.current) return
      const rtt = performance.now() - sentAt
      const offset = Date.parse(result.now) - (sentWall + rtt / 2)
      const sync = clockSync.current
      if (!sync || rtt < sync.rtt || Math.abs(offset - sync.offset) > 1000 || Date.now() - sync.at > 300_000) {
        clockSync.current = { offset, rtt, at: Date.now() }
      }
      fetchedAt.current = Date.now()

      let next = store
      let changed = false
      if (!next || next.day !== result.trading_date || next.size !== sizeId || !result.marks_since) {
        // an incremental answer for a NEW session has none of the earlier days' marks: load in full on the next poll
        next = { day: result.trading_date, size: sizeId, byKey: new Map(), latest: '', fullAt: result.marks_since ? 0 : Date.now() }
        changed = true
      }
      for (const mark of result.marks) {
        const key = `${mark.strategy}|${mark.trading_date}|${mark.minute}`
        if (next.byKey.get(key)?.mtm_rs !== mark.mtm_rs) {
          next.byKey.set(key, mark)
          changed = true
        }
        // only the session's own minutes set the bookmark - an open position's earlier-day marks are fixed
        if (mark.trading_date === result.trading_date && mark.minute > next.latest) next.latest = mark.minute
      }
      marksStore.current = next
      if (changed) setMarks([...next.byKey.values()]) // a new array only when a mark actually changed
      setToday(result)
      setError(null)
    } catch (caught) {
      if (mounted.current) setError(caught instanceof Error ? caught.message : 'Backend not reachable')
    } finally {
      inFlight.current = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sizeKey(size)])
  const refresh = useCallback(() => void load(), [load])

  // Poll every 2 s while the tab is visible - each poll starts only after the previous one finished - and every 30 s
  // while it is hidden; coming back to the tab refreshes at once.
  useEffect(() => {
    let timer: number | undefined
    let stopped = false
    const run = async () => {
      await load()
      if (!stopped) timer = window.setTimeout(run, document.hidden ? HIDDEN_POLL_MS : POLL_MS)
    }
    void run()
    const onVisibility = () => {
      if (!document.hidden && !inFlight.current) {
        window.clearTimeout(timer)
        void run()
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stopped = true
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [load])

  // Re-render on every new second of the SERVER's clock (a few ms after it starts), so a countdown steps exactly one
  // second at a time instead of drifting against the poll.
  useEffect(() => {
    let timer: number
    const schedule = () => {
      const now = Date.now() + (clockSync.current?.offset ?? 0)
      timer = window.setTimeout(() => {
        setTick((value) => value + 1)
        schedule()
      }, 1000 - (now % 1000) + 8)
    }
    schedule()
    return () => window.clearTimeout(timer)
  }, [])

  const s5Closed = today?.trades.filter((trade) => trade.strategy === S5 && trade.status !== 'open').length ?? 0
  useEffect(() => {
    fetchLabPaper(size, S5)
      .then((result) => mounted.current && setRecord(result.rows))
      .catch(() => undefined) // the record is a summary; Today still works without it
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sizeKey(size), s5Closed])

  // the Calendar opens on the whole backtest (~4 MB, ~0.6 s for the server to build): fetch it in the background
  // once Today has settled, so switching to it finds it already here (it stays cached for 10 minutes - see api.ts).
  useEffect(() => {
    const timer = window.setTimeout(() => void fetchLabBacktest(size).catch(() => undefined), 6000)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sizeKey(size)])

  // The S5/S8 backtest, at the same size as paper: the yardstick and the closed trade's paper-vs-backtest gap.
  const tradingDay = today?.trading_date
  useEffect(() => {
    Promise.all([fetchLabBacktest(size, S5), fetchLabBacktest(size, S8)])
      .then(([s5, s8]) => mounted.current && setBacktest({ s5: s5.rows, s8: s8.rows }))
      .catch(() => undefined) // context only; Today works without it
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sizeKey(size), tradingDay, s5Closed])

  const marksBy = useMemo(() => {
    const by = new Map<string, LabMark[]>()
    for (const mark of [...marks].sort((a, b) => a.minute.localeCompare(b.minute))) {
      const key = `${mark.strategy}|${mark.trading_date}`
      const list = by.get(key)
      if (list) list.push(mark)
      else by.set(key, [mark])
    }
    return by
  }, [marks])

  void tick // the state change re-renders every second, which is what moves the clocks
  // The server's IST wall clock: shifting the epoch by +05:30 and reading the UTC digits gives IST whatever the browser's zone.
  const offset = clockSync.current?.offset ?? 0
  const nowIst = today ? new Date(Date.now() + offset + 330 * 60_000).toISOString().slice(11, 19) : '00:00:00'
  const nowSeconds = secondsOf(nowIst)
  const screenAge = fetchedAt.current ? Math.round((Date.now() - fetchedAt.current) / 1000) : 0
  const feedAge = today?.engine.feed_age_seconds
  const alerts = (today?.alerts ?? []).filter((alert) => alert.trading_date === today?.trading_date || alert.level === 'error')

  const trades = today?.trades ?? []
  const s5Rows = trades.filter((trade) => trade.strategy === S5).sort((a, b) => b.trading_date.localeCompare(a.trading_date))
  const featured = s5Rows.find((trade) => trade.status === 'open') ?? s5Rows.find((trade) => trade.trading_date === today?.trading_date) ?? s5Rows[0]
  const previous = s5Rows.find((trade) => trade !== featured && trade.status !== 'open')
  const others = trades
    .filter((trade) => trade.strategy !== S5)
    .sort((a, b) => LAB_STRATEGIES.indexOf(a.strategy) - LAB_STRATEGIES.indexOf(b.strategy))
  const s5Booked = s5Rows
    .filter((trade) => trade.status !== 'open' && trade.net_rs != null && trade.legs?.some((leg) => typeof leg.exit === 'object' && leg.exit?.ts.slice(0, 10) === today?.trading_date))
    .reduce((sum, trade) => sum + (trade.net_rs ?? 0), 0)
  const s5Open = featured?.status === 'open' ? (today?.latest_mtm_rs[S5] ?? liveMtm(featured, today?.live)) : undefined
  const s5Today = s5Booked + (s5Open ?? 0)
  const nowMs = Date.now() + offset
  // The next scheduled thing: later today if there is one, else the next session's 09:30 (holidays already skipped server-side).
  const sessionIsToday = !!today && today.trading_date === today.calendar_date
  const todayNext = today ? (sessionIsToday ? nextEvent(today, nowSeconds) : { ...nextEvent({ ...today, market_day: false }, nowSeconds), from: secondsOf(clockOf(today.exit_at)) + 11 * 60 }) : null
  const nextUp: { label: string; seconds: number | null; when: string; left: number | null } =
    todayNext?.seconds != null
      ? {
          label: todayNext.label,
          seconds: todayNext.seconds,
          when: `today ${todayNext.at} IST`,
          left: todayNext.seconds / Math.max(1, nowSeconds + todayNext.seconds - todayNext.from),
        }
      : today && todayNext
        ? (() => {
            const target = istEpoch(today.next_session, '09:30:00')
            const since = istEpoch(today.trading_date, clockAt(todayNext.from))
            return {
              label: featured?.status === 'open' ? 'S5 exit & entries' : 'entries',
              seconds: Math.max(0, (target - nowMs) / 1000),
              when: `${dayShortLabel(today.next_session)} · 09:30 IST`,
              left: (target - nowMs) / Math.max(1, target - since),
            }
          })()
        : { label: '—', seconds: null, when: '', left: null }
  const inSession = sessionIsToday && !!today?.market_day && nowSeconds >= DAY_START * 60 && nowSeconds <= DAY_END * 60
  const preOpen = !!today?.calendar_market_day && nowSeconds >= 9 * 3600 && nowSeconds < DAY_START * 60
  const engineState = !today ? 'connecting' : !today.engine.running || today.engine.last_error ? 'off' : today.engine.feed_stalled ? 'stalled' : inSession ? 'live' : 'idle'
  // Today's two S5 events for the timeline: an earlier session's position bought back today, and today's own entry
  const fillsOf = (trade: LabDay, side: 'entry' | 'exit') => (trade.legs ?? []).map((leg) => fillTs(leg[side])).filter((ts): ts is string => !!ts).sort()
  const closedToday = s5Rows.find((trade) => trade.trading_date !== today?.trading_date && trade.status !== 'open' && fillsOf(trade, 'exit').slice(-1)[0]?.slice(0, 10) === today?.trading_date)
  const openedToday = s5Rows.find((trade) => trade.trading_date === today?.trading_date)
  // an earlier session's S5 still held (until its 09:30 exit lands), and the earlier session the timeline's strip shows
  const carried = (today && s5Rows.find((trade) => trade.status === 'open' && trade.trading_date < today.trading_date)) || null
  const stripPrevDay = carried?.trading_date ?? closedToday?.trading_date ?? null
  const timelineEvents: TimelineEvents | null = today
    ? {
        closed: closedToday
          ? { day: closedToday.trading_date, net: closedToday.net_rs, at: clockOf(fillsOf(closedToday, 'exit').slice(-1)[0]), status: closedToday.status, trade: closedToday }
          : null,
        opened: openedToday
          ? {
              status: openedToday.status,
              at: fillsOf(openedToday, 'entry')[0] ? clockOf(fillsOf(openedToday, 'entry')[0]) : '09:30:00',
              strike: openedToday.atm_strike,
              trade: openedToday,
              mtm: openedToday.status === 'open' && featured === openedToday ? s5Open : undefined,
            }
          : null,
      }
    : null
  // NIFTY: the per-second tick while the market is live, else the Today poll's own
  const liveSpot = useLiveSpot(engineState === 'live', today?.trading_date)
  const spotMinutes = useSpotMinutes(today?.trading_date, engineState === 'live')
  const spot = liveSpot?.spot?.ltp ?? today?.live.spot?.ltp ?? null
  const spotDay = liveSpot?.spot_day ?? today?.spot_day ?? null
  const dayMove = spot != null && spotDay?.prev_close != null ? spot - spotDay.prev_close : null
  const entrySpot = trades.find((trade) => trade.trading_date === today?.trading_date && trade.spot_at_entry != null)?.spot_at_entry ?? null
  const sinceEntry = spot != null && entrySpot != null ? spot - entrySpot : null
  const engineText = {
    connecting: 'Connecting…',
    off: today?.engine.running ? 'Engine error' : 'Engine not running',
    stalled: `Feed stalled · ${feedAge != null ? Math.round(feedAge) : '?'}s`,
    live: `Market live · feed ${feedAge != null ? (feedAge < 1 ? '<1' : Math.round(feedAge)) : '—'}s`,
    idle: preOpen ? 'Pre-open · opens 09:15' : `Market closed · opens ${today ? weekdayShort(today.next_session) : ''} 09:15`,
  }[engineState]
  const toggleOthers = () =>
    setShowOthers((shown) => {
      writeShowOthers(!shown)
      return !shown
    })

  return (
    <div ref={screenRef} className={`td-screen${height ? ' fit' : ''}${showOthers && others.length ? ' with-others' : ''}`} style={height ? { height } : undefined}>
      <div className={`td-bar${engineState === 'stalled' || engineState === 'off' ? ' alarm' : ''}`}>
        <div className="td-bar-main">
          <div
            className={`td-clock ${engineState}`}
            role={engineState === 'stalled' || engineState === 'off' ? 'alert' : undefined}
            title={today?.engine.last_tick ? `Server clock (IST). Paper engine last ticked ${clockOf(today.engine.last_tick)}` : 'Server clock (IST)'}
          >
            <AnalogClock seconds={nowSeconds} />
            <div className="td-clock-text">
              <span>{today ? dayLabel(today.calendar_date ?? today.trading_date) : '…'}</span>
              <b>
                {today ? nowIst : '--:--:--'}
                <small>IST</small>
              </b>
              <small className="td-clock-state">
                <i />
                {engineText}
              </small>
            </div>
          </div>
          <div className="td-bar-stat td-bar-s5" title="S5's result for today: the trade bought back at 09:30 plus the open position's MTM">
            <span>
              <Icon name={s5Today < 0 ? 'trendDown' : 'trendUp'} /> S5 {sessionIsToday || !today ? 'today' : weekdayShort(today.trading_date)} <PaperBadge />
            </span>
            <FlashValue value={s5Today} />
            {(s5Booked !== 0 || s5Open != null) && <S5Split booked={s5Booked} open={s5Open} />}
            <small>
              booked <em className={pnlClass(s5Booked)}>{signedRs(s5Booked)}</em> · open <em className={pnlClass(s5Open)}>{s5Open != null ? signedRs(s5Open) : '—'}</em>
            </small>
          </div>
          <div className="td-bar-stat td-bar-nifty" title={spotDay ? `Previous close ${spotDay.prev_close ?? '—'} · open ${spotDay.open ?? '—'} · high ${spotDay.high ?? '—'} · low ${spotDay.low ?? '—'}` : undefined}>
            <span>
              <Icon name="candles" /> NIFTY 50
              {liveSpot && <i className="td-live-dot" title={`Live: the newest tick, asked for every second (${liveSpot.spot?.age_s ?? '?'}s old)`} />}
            </span>
            <FlashPrice value={spot} />
            <small>
              {dayMove != null && spotDay?.prev_close ? (
                <em className={pnlClass(dayMove)}>
                  <Icon name={dayMove >= 0 ? 'up' : 'down'} className="tri" /> {formatPts(Math.abs(dayMove))} ({((dayMove / spotDay.prev_close) * 100).toFixed(2)}%)
                </em>
              ) : (
                'live spot'
              )}
              {sinceEntry != null && (
                <>
                  {' '}
                  · <Icon name={sinceEntry >= 0 ? 'up' : 'down'} className="tri" /> {formatPts(Math.abs(sinceEntry))} since 09:30
                </>
              )}
            </small>
            {spot != null && spotDay?.low != null && spotDay.high != null && spotDay.high > spotDay.low && (
              <div className="td-nifty-day" title={`Where NIFTY is within today's range: low ${spotDay.low} - high ${spotDay.high}`}>
                <em>L {Math.round(spotDay.low).toLocaleString('en-IN')}</em>
                <div className="td-nifty-range">
                  <i style={{ left: `${Math.min(100, Math.max(0, ((spot - spotDay.low) / (spotDay.high - spotDay.low)) * 100))}%` }} />
                </div>
                <em>H {Math.round(spotDay.high).toLocaleString('en-IN')}</em>
              </div>
            )}
            {spotMinutes && (
              <NiftySpark minutes={spotMinutes.minutes} spot={spot} liveMinute={engineState === 'live' ? nowIst.slice(0, 5) : null} prevClose={spotDay?.prev_close ?? null} entrySpot={entrySpot} />
            )}
          </div>
          <div className="td-bar-stat td-bar-next">
            <CountdownRing left={nextUp.left} />
            <div>
              <span>Next · {nextUp.label}</span>
              <b>{nextUp.seconds != null ? formatCountdown(nextUp.seconds) : '—'}</b>
              <small>{nextUp.when}</small>
            </div>
          </div>
          <div className="td-bar-right">
            <AlertsButton alerts={alerts} today={today?.trading_date ?? ''} />
            {others.length > 0 && (
              <button type="button" className={`td-switch${showOthers ? ' on' : ''}`} role="switch" aria-checked={showOthers} onClick={toggleOthers} title="Show S1-S4 below S5">
                <i />
                Others ({others.length})
              </button>
            )}
          </div>
        </div>
        {today && (
          <SessionTimeline
            today={today}
            nowMs={nowMs}
            s5Open={featured?.status === 'open'}
            carried={carried}
            events={timelineEvents}
            live={engineState === 'live'}
            s5Marks={[...(marksBy.get(`${S5}|${today.trading_date}`) ?? []), ...(stripPrevDay ? (marksBy.get(`${S5}|${stripPrevDay}`) ?? []) : [])]}
            spotMinutes={spotMinutes}
            alerts={alerts}
          />
        )}
      </div>

      {(screenAge > 8 || error || today?.engine.last_error) && (
        <div className="td-warn">{error ?? today?.engine.last_error ?? `Not hearing from the backend - showing figures from ${screenAge}s ago.`}</div>
      )}

      {today && (
        <S5Feature
          trade={featured}
          previous={previous}
          today={today}
          live={today.live}
          marks={featured ? (marksBy.get(`${S5}|${featured.trading_date}`) ?? []) : []}
          record={record}
          backtest={backtest}
          nowSeconds={nowSeconds}
          theme={theme}
          onChanged={refresh}
          onLegs={setLegsOf}
          onChart={setChartOf}
        />
      )}

      {showOthers && others.length > 0 && (
        <section className="td-others" aria-label="Other strategies">
          {others.map((trade) => (
            <OtherTile
              key={`${trade.strategy}-${trade.trading_date}`}
              trade={trade}
              mtm={today?.latest_mtm_rs[trade.strategy]}
              live={today?.live}
              marks={marksBy.get(`${trade.strategy}|${trade.trading_date}`) ?? []}
              today={today?.trading_date ?? ''}
              onChanged={refresh}
              onLegs={setLegsOf}
            />
          ))}
        </section>
      )}

      {legsOf && <LegsDialog trade={legsOf} live={today?.live} onClose={() => setLegsOf(null)} />}
      {chartOf && <ChartDialog trade={chartOf} theme={theme} onClose={() => setChartOf(null)} />}
    </div>
  )
}
