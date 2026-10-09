import { Fragment, useEffect, useMemo, useState, type CSSProperties } from 'react'
import { fetchLabDayChart, type LabDay, type LabDayChartBar, type LabDayChartLeg, type LabStrategy } from '../api'
import { DayChartLink, ENTRY_COLOR, EXIT_COLOR, LegChart, minuteTime, OverlayChart, PositionChart, SpotChart, type DayEvent } from '../charts/DayCharts'
import type { ThemeName } from '../charts/theme'
import { formatPts, pnlClass, signedRs, STRATEGY_COLOR, STRATEGY_LABEL, tradeSizeLabel } from '../lab'
import { Icon } from './Icons'
import { PaperBadge } from './PaperBadge'
import { useLabData } from './useLabData'

type View = 'side' | 'overlay'
type Zoom = 'day' | 'entry' | 'exit'
const istClock = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false })
const legName = (leg: LabDayChartLeg) => `${leg.type} ${leg.strike}${leg.role === 'wing' ? ' wing' : ''}`
const legColor = (leg: LabDayChartLeg) => (leg.type === 'CE' ? '#2f9bff' : '#ff8a32')
const sorted = (legs: LabDayChartLeg[]) => [...legs].sort((a, b) => (a.type === b.type ? 0 : a.type === 'CE' ? -1 : 1))

/** HH:MM:SS of an ISO timestamp that already carries +05:30 (IST). */
const clockFromIso = (iso: string | null | undefined): string => (iso ? iso.slice(11, 19) : '—')

const dayFormat = new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })
/** "Wed 7 Oct" for an ISO date. */
const shortDate = (iso: string | null | undefined): string => (iso ? dayFormat.format(new Date(`${iso}T00:00:00Z`)).replace(/,/g, '') : '—')
const niftyFormat = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

/** How long the position was held, from its entry fill to its exit fill: "24h 00m". */
function heldFor(from: string | null | undefined, to: string | null | undefined): string | null {
  if (!from || !to) return null
  const minutes = Math.round((Date.parse(to) - Date.parse(from)) / 60000)
  return minutes > 0 ? `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m` : null
}

/** NIFTY's close in the given minute, or the last one before it. */
function spotAt(bars: LabDayChartBar[] | undefined, minute: string): number | null {
  let value: number | null = null
  for (const bar of bars ?? []) if (bar.t <= minute) value = bar.c
  return value
}

/** The spacing of the index bars, in minutes - so the NIFTY label says what is really drawn (1-minute or coarser). */
function barMinutes(bars: LabDayChartBar[] | undefined): number | null {
  const minutes = (bars ?? []).map((bar) => Number(bar.t.slice(0, 2)) * 60 + Number(bar.t.slice(3, 5)))
  const gaps = minutes.slice(1).map((m, i) => m - minutes[i]).filter((gap) => gap > 0)
  return gaps.length ? Math.min(...gaps) : null
}

/** "09:30" moved by `delta` minutes. */
function shiftMinute(hhmm: string, delta: number): string {
  const total = Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5)) + delta
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
}


type Fill = { when: string; time: string; price: number | null; candle: LabDayChartBar | undefined; field: 'o' | 'c'; estimated?: boolean }
type FillRow = { key: string; leg: LabDayChartLeg; entry: Fill; exit: Fill; points: number | null }

function fillOf(leg: LabDayChartLeg, side: 'entry' | 'exit', when?: string): Fill {
  const time = side === 'entry' ? leg.entry_time : leg.exit_time
  return { when: when ?? time, time, price: side === 'entry' ? leg.entry : leg.exit, candle: leg.bars.find((bar) => bar.t === time), field: side === 'entry' ? 'o' : 'c', estimated: leg.estimated }
}

/** The recorded fill next to the candle it should come from. Backtest fills must equal the candle exactly (✓/✗);
 * paper fills are real bid/ask prices, so they only show how far they sat from the candle. */
function FillCell({ fill, exact }: { fill: Fill; exact: boolean }) {
  if (fill.price == null) return <td className="dim">—</td>
  if (!fill.candle) return <td className="dc-note">no candle</td>
  const diff = fill.price - fill.candle[fill.field]
  if (!exact) return <td className="dim">Δ {formatPts(diff)}</td>
  return Math.abs(diff) < 0.005 ? <td className="positive">✓ match</td> : <td className="negative">✗ {formatPts(diff)}</td>
}

/** The fill against the candle it should come from, as one short line: backtest fills must equal it, paper fills show how far they sat. */
function fillCheck(fill: Fill, exact: boolean): { text: string; tone: string } {
  if (fill.price == null) return { text: '', tone: 'dim' }
  if (!fill.candle) return { text: 'no candle for this minute', tone: 'warn' }
  const candle = fill.candle[fill.field]
  const label = `candle ${fill.field === 'o' ? 'open' : 'close'} ${candle.toFixed(2)}`
  const diff = fill.price - candle
  if (!exact) return { text: `${label} · Δ ${formatPts(diff)}`, tone: 'dim' }
  return Math.abs(diff) < 0.005 ? { text: `${label} · ✓ match`, tone: 'positive' } : { text: `${label} · ✗ ${formatPts(diff)}`, tone: 'negative' }
}

function FillCheck({ rows, exact, onJump }: { rows: FillRow[]; exact: boolean; onJump?: (kind: 'entry' | 'exit') => void }) {
  const when = (fill: Fill, kind: 'entry' | 'exit') =>
    onJump ? (
      <button type="button" className="dc-jump" onClick={() => onJump(kind)} title="Zoom every chart to this candle">
        {fill.when}
      </button>
    ) : (
      fill.when
    )
  const candleTitle = (fill: Fill) => (fill.candle ? `O ${fill.candle.o}  H ${fill.candle.h}  L ${fill.candle.l}  C ${fill.candle.c}` : undefined)
  return (
    <div className="research-table-scroll cmp-fit">
      <table className="research-table dc-fills">
        <thead>
          <tr>
            <th>Leg</th>
            <th className="dc-entry-head">Entry at</th>
            <th className="dc-entry-head">Fill</th>
            <th className="dc-entry-head">Candle open</th>
            <th className="dc-entry-head">Check</th>
            <th className="dc-exit-head">Exit at</th>
            <th className="dc-exit-head">Fill</th>
            <th className="dc-exit-head">Candle close</th>
            <th className="dc-exit-head">Check</th>
            <th>Points</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ key, leg, entry, exit, points }) => (
            <tr key={key}>
              <th style={{ color: legColor(leg) }}>
                {leg.side} {legName(leg)}
              </th>
              <td>{when(entry, 'entry')}</td>
              <td style={{ color: ENTRY_COLOR }}>
                {entry.price?.toFixed(2) ?? '—'}
                {entry.estimated && <em className="lab-estimated">est.</em>}
              </td>
              <td title={candleTitle(entry)}>{entry.candle?.o.toFixed(2) ?? '—'}</td>
              <FillCell fill={entry} exact={exact} />
              <td>{when(exit, 'exit')}</td>
              <td style={{ color: EXIT_COLOR }}>
                {exit.price?.toFixed(2) ?? '—'}
                {exit.estimated && <em className="lab-estimated">est.</em>}
              </td>
              <td title={candleTitle(exit)}>{exit.candle?.c.toFixed(2) ?? '—'}</td>
              <FillCell fill={exit} exact={exact} />
              <td className={pnlClass(points)}>{points == null ? '—' : formatPts(points)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function legPoints(leg: LabDayChartLeg): number | null {
  if (leg.entry == null || leg.exit == null) return null
  return leg.side === 'SELL' ? leg.entry - leg.exit : leg.exit - leg.entry
}

/** [min, max] over the legs' candles and entry prices (with a margin), so two charts can share one price scale and be compared by eye. */
function sharedRange(legs: LabDayChartLeg[]): [number, number] | null {
  const values = legs.flatMap((leg) => [...leg.bars.flatMap((bar) => [bar.h, bar.l]), ...(leg.entry != null ? [leg.entry] : [])])
  if (values.length === 0) return null
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const pad = (hi - lo) * 0.06 || 1
  return [Math.max(0, lo - pad), hi + pad]
}

/** Verify a day: each strike's 1-minute candles with the recorded entry and exit marked, side by side (or overlaid), the whole position's price and profit, and NIFTY.
 * S5 (held overnight, sold on `date` and bought back the NEXT trading day) has no such single day, so it renders a
 * different, two-panel body instead - the entry day's legs and the exit day's legs, each against its own candles. */
export function LabDayChart({ date, strategy, source, theme, onClose, fit = false, trade }: {
  date: string
  strategy: LabStrategy
  source: 'backtest' | 'paper'
  theme: ThemeName
  onClose?: () => void
  /** Fill the dialog it sits in (a definite height) instead of fixed chart heights - the S5 page then never scrolls. */
  fit?: boolean
  /** The trade's own row, when the caller has it: its rupee result and size for the S5 header. */
  trade?: LabDay
}) {
  const chart = useLabData(() => fetchLabDayChart(date, strategy, source), [date, strategy, source])
  const [view, setView] = useState<View>('side')
  const [candles, setCandles] = useState(true)
  const [sameScale, setSameScale] = useState(false)
  const [hover, setHover] = useState<number | null>(null)
  const data = chart.data
  const isS5 = strategy === 'straddle_sell_overnight' || strategy === 'straddle_sell_overnight_skip_friday' || strategy === 'straddle_sell_overnight_skip_friday_december'
  const link = useMemo(() => {
    const next = new DayChartLink(minuteTime(date, '09:15'), minuteTime(date, '15:42'))
    next.onHover = setHover
    return next
    // a fresh link per day: charts from an earlier day must never sync with this one
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data])
  // S5 only: its own pair of links, one per day, since the entry day and the exit day are not the same time axis. Each
  // opens on about ten minutes before its fill to 15 past it (a manual 10:04 entry moves it along) - the 09:15 opening candle,
  // often a spike, stays just out of view so it does not flatten the minutes around the fill; drag to see it.
  const [entryHover, setEntryHover] = useState<number | null>(null)
  const [exitHover, setExitHover] = useState<number | null>(null)
  const entryLink = useMemo(() => {
    const from = data?.entry_bar ? shiftMinute(data.entry_bar, -11) : '09:19'
    const next = new DayChartLink(minuteTime(date, from < '09:15' ? '09:15' : from), minuteTime(date, data?.entry_window_end ? shiftMinute(data.entry_window_end, 2) : '10:00'))
    next.onHover = setEntryHover
    return next
  }, [data]) // eslint-disable-line react-hooks/exhaustive-deps
  const exitLink = useMemo(() => {
    const day = data?.exit_date ?? date
    const from = data?.exit_bar ? shiftMinute(data.exit_bar, -11) : '09:19'
    const next = new DayChartLink(minuteTime(day, from < '09:15' ? '09:15' : from), minuteTime(day, data?.exit_window_end ? shiftMinute(data.exit_window_end, 2) : '10:00'))
    next.onHover = setExitHover
    return next
  }, [data]) // eslint-disable-line react-hooks/exhaustive-deps

  const bodies = useMemo(() => sorted((data?.legs ?? []).filter((leg) => leg.role === 'body')), [data])
  const wings = useMemo(() => sorted((data?.legs ?? []).filter((leg) => leg.role === 'wing')), [data])
  const bodyRange = useMemo(() => (sameScale ? sharedRange(bodies) : null), [sameScale, bodies])
  const wingRange = useMemo(() => (sameScale ? sharedRange(wings) : null), [sameScale, wings])
  const entryLevel = useMemo(() => (data?.legs ? data.legs.reduce((sum, leg) => sum + (leg.entry ?? 0) * (leg.side === 'SELL' ? 1 : -1), 0) : null), [data])
  const positionPrice = useMemo(() => (data?.position ?? []).map((p) => ({ t: p.t, v: p.price })), [data])
  const positionPnl = useMemo(() => (data?.pnl ?? []).map((p) => ({ t: p.t, v: p.pts })), [data])
  const drawn = useMemo(() => (data?.legs ?? []).filter((leg) => leg.bars.length > 0), [data])
  const entryLegs = useMemo(() => sorted(data?.entry_legs ?? []), [data])
  const exitLegs = useMemo(() => sorted(data?.exit_legs ?? []), [data])

  // Zoom: the whole day squeezes ~375 one-minute candles into one chart, far too thin to check a single fill; the
  // entry/exit windows show ~30 minutes around that candle on every linked chart at once.
  const [zoom, setZoom] = useState<Zoom>('day')
  const entryTime = bodies[0]?.entry_time ?? '09:30'
  const exitTime = bodies[0]?.exit_time ?? data?.exit_bar ?? '15:29'
  const dayEvents = useMemo<DayEvent[]>(() => [{ time: entryTime, kind: 'entry' }, { time: exitTime, kind: 'exit' }], [entryTime, exitTime])
  useEffect(() => {
    if (!data?.available || isS5) return
    const [from, to] = zoom === 'entry' ? [shiftMinute(entryTime, -10), shiftMinute(entryTime, 20)] : zoom === 'exit' ? [shiftMinute(exitTime, -25), shiftMinute(exitTime, 6)] : ['09:15', '15:42']
    link.show(minuteTime(date, from), minuteTime(date, to))
  }, [zoom, link, data, isS5, date, entryTime, exitTime])

  const fillRows = useMemo<FillRow[]>(
    () =>
      sorted(data?.legs ?? []).map((leg) => ({
        key: `${leg.type}${leg.strike}${leg.side}${leg.role}`,
        leg,
        entry: fillOf(leg, 'entry'),
        exit: fillOf(leg, 'exit'),
        points: legPoints(leg),
      })),
    [data],
  )
  const hoverMinute = hover == null ? null : istClock.format(new Date(hover * 1000))
  const at = <T extends { t: string }>(rows: T[] | undefined): T | undefined => (hoverMinute ? rows?.find((row) => row.t === hoverMinute) : undefined)

  if (chart.error) return <small className="export-error">{chart.error}</small>
  if (!data) return <div className="research-empty">Loading the day&apos;s candles…</div>
  if (!data.available) return <div className="research-empty">{data.reason}</div>

  // S5 on one page: the entry day on the left and the exit day on the right; the call, the put and NIFTY as rows, each
  // row's result at its end; the trade's summary (when, how long, what it made) in the header.
  if (isS5) {
    const paperS5 = data.source === 'paper'
    const checksOkS5 = data.s5_checks ? data.s5_checks.entry_matches_0930_open && data.s5_checks.exit_matches_0930_close : false
    const entryMinute = entryLegs[0]?.entry_time ?? data.entry_bar ?? '09:30'
    const exitMinute = exitLegs[0]?.exit_time ?? data.exit_bar ?? '09:30'
    const entryEvents: DayEvent[] = [{ time: entryMinute, kind: 'entry' }]
    const exitEvents: DayEvent[] = [{ time: exitMinute, kind: 'exit' }]
    const exitDay = data.exit_date ?? null
    // each leg's row: the same contract on its entry day and on its exit day, and what it made between the two
    const rows = entryLegs.map((leg) => {
      const out = exitLegs.find((other) => other.type === leg.type && other.strike === leg.strike && other.side === leg.side)
      const entry = fillOf(leg, 'entry')
      const exit = out ? fillOf(out, 'exit') : null
      const points = entry.price != null && exit?.price != null ? (leg.side === 'SELL' ? entry.price - exit.price : exit.price - entry.price) : null
      return { key: `${leg.type}${leg.strike}${leg.side}`, leg, out, entry, exit, points }
    })
    const niftyIn = spotAt(data.entry_spot, entryMinute)
    const niftyOut = spotAt(data.exit_spot, exitMinute)
    const niftyMove = niftyIn != null && niftyOut != null ? niftyOut - niftyIn : null
    const held = heldFor(data.entry_ts, data.exit_ts)
    const netRs = trade?.net_rs ?? null
    const step = barMinutes(data.entry_spot) ?? barMinutes(data.exit_spot)
    const spotLabel = step == null ? '' : step === 1 ? '1-minute' : `${step}-minute bars`
    // hovering a chart: its column's heading reads that minute across the call, the put and NIFTY
    const readout = (time: number | null, legs: LabDayChartLeg[], spot: LabDayChartBar[] | undefined) => {
      if (time == null) return null
      const minute = istClock.format(new Date(time * 1000))
      const parts = legs.map((leg) => {
        const bar = leg.bars.find((b) => b.t === minute)
        return `${leg.type} ${bar ? bar.c.toFixed(2) : '—'}`
      })
      const index = spot?.find((bar) => bar.t === minute)
      return `${minute} · ${parts.join(' · ')}${index ? ` · NIFTY ${niftyFormat.format(index.c)}` : ''}`
    }
    const entryReadout = readout(entryHover, entryLegs, data.entry_spot)
    const exitReadout = readout(exitHover, exitLegs, data.exit_spot)
    const legCell = (leg: LabDayChartLeg | undefined, fill: Fill | null, side: 'entry' | 'exit') => {
      if (!leg || !fill) {
        return (
          <div className="dc-s5-cell">
            <div className="dc-s5-empty">{exitDay ? 'No candles for the exit day yet' : 'Still open - bought back at the next 09:30'}</div>
          </div>
        )
      }
      const sold = leg.side === 'SELL'
      const check = fillCheck(fill, !paperS5)
      const verb = side === 'entry' ? (sold ? 'sold' : 'bought') : sold ? 'bought back' : 'sold'
      return (
        <div className="dc-s5-cell">
          <div className="dc-s5-cellhead">
            <b className={`dc-s5-leg ${leg.type.toLowerCase()}`}>
              {leg.side} {legName(leg)}
            </b>
            <span className={`fill ${side}`}>
              {verb} <b>{fill.price?.toFixed(2) ?? '—'}</b>
              {fill.estimated && <em className="lab-estimated">est.</em>}
            </span>
            <em className={check.tone}>{check.text}</em>
          </div>
          <div className="dc-s5-chart">
            {leg.bars.length ? (
              <LegChart date={side === 'entry' ? date : (exitDay ?? date)} leg={leg} theme={theme} candles={candles} link={side === 'entry' ? entryLink : exitLink} sharedRange={null} height="fill" markerText={false} />
            ) : (
              <div className="dc-s5-empty">No candles stored for this contract.</div>
            )}
          </div>
        </div>
      )
    }
    const spotCell = (bars: LabDayChartBar[] | undefined, day: string | null, events: DayEvent[], chartLink: DayChartLink, minute: string, value: number | null) => (
      <div className="dc-s5-cell spot">
        <div className="dc-s5-cellhead">
          <b className="dc-s5-leg nifty">NIFTY</b>
          <span className="fill">{value == null ? '—' : `${niftyFormat.format(value)} at ${minute}`}</span>
          <em className="dim">{spotLabel}</em>
        </div>
        <div className="dc-s5-chart">
          {bars?.length && day ? <SpotChart date={day} bars={bars} theme={theme} link={chartLink} events={events} height="fill" markerText={false} /> : <div className="dc-s5-empty">No index bars for this day.</div>}
        </div>
      </div>
    )
    return (
      <div className={`dc-panel s5${fit ? ' fit' : ''}`} style={{ '--accent': STRATEGY_COLOR[strategy] } as CSSProperties}>
        <header className="dc-s5-head">
          <div className="dc-s5-title">
            <span className="dc-s5-code">S5</span>
            <div>
              <div className="dc-s5-name">
                <strong>{STRATEGY_LABEL[strategy]}</strong>
                {paperS5 ? <PaperBadge /> : <span className="lab-status">BACKTEST</span>}
                {data.rolled_to_next_expiry && <span className="lab-derived">rolled to next expiry</span>}
              </div>
              <small>
                Expiry {shortDate(data.expiry)} · ATM {data.atm_strike ?? '—'}
                {trade ? ` · ${tradeSizeLabel(trade)}` : ''}
              </small>
            </div>
          </div>
          <div className="dc-s5-span">
            <span className="end">
              <b>{entryMinute}</b>
              <small>{shortDate(date)}</small>
            </span>
            <span className="line">
              <i />
              <em>{held ? `held ${held}` : exitDay ? '' : 'open overnight'}</em>
            </span>
            <span className="end exit">
              <b>{exitDay ? exitMinute : '—'}</b>
              <small>{exitDay ? shortDate(exitDay) : 'next 09:30'}</small>
            </span>
          </div>
          <div className="dc-s5-total">
            <span>{netRs != null ? 'Net P&L' : 'Net per unit'}</span>
            <b className={pnlClass(netRs ?? data.trade.net_pts)}>{netRs != null ? signedRs(netRs) : `${formatPts(data.trade.net_pts)} pts`}</b>
            <small>
              {netRs != null && `${formatPts(data.trade.net_pts)} pts net · `}gross {formatPts(data.trade.gross_pts)} pts
            </small>
          </div>
          {onClose && (
            <button type="button" className="research-chip" onClick={onClose}>
              <Icon name="close" />
              Close
            </button>
          )}
        </header>
        {data.s5_checks && (
          <div className={`dc-check ${checksOkS5 ? 'ok' : 'bad'}`}>
            {checksOkS5 ? "✓ Each leg's entry equals its 09:30 open on the entry day, and its exit its 09:30 close on the exit day." : '✗ A recorded entry or exit does not match the candle it should come from - check this trade.'}
          </div>
        )}
        {paperS5 && (
          <p className="dc-s5-note">
            Paper fills are the real bid (sells, {clockFromIso(data.entry_ts)}) and ask (buy-backs, {clockFromIso(data.exit_ts)}), so they sit near - not exactly on - that minute&apos;s candle; Δ is how far. Each day is its own window: the
            candles do not join across the night.
            {data.invalid_reason && <b className="flag"> Flag: {data.invalid_reason}.</b>}
          </p>
        )}
        <div className="dc-s5-grid">
          <div className={`dc-s5-colhead entry${entryReadout ? ' reading' : ''}`}>
            <i />
            {entryReadout ?? (
              <span>
                <b>Entry</b> {shortDate(date)} · sold at {entryMinute}
              </span>
            )}
          </div>
          <div className={`dc-s5-colhead exit${exitReadout ? ' reading' : ''}`}>
            <i />
            {exitReadout ?? (
              <span>
                <b>Exit</b> {exitDay ? `${shortDate(exitDay)} · bought back at ${exitMinute}` : 'the next trading day at 09:30'}
              </span>
            )}
          </div>
          <div className="dc-s5-colhead result">Points</div>
          {rows.map((row) => (
            <Fragment key={row.key}>
              {legCell(row.leg, row.entry, 'entry')}
              {legCell(row.out, row.exit, 'exit')}
              <div className="dc-s5-result">
                <span>{row.leg.type}</span>
                <b className={pnlClass(row.points)}>{row.points == null ? '—' : formatPts(row.points)}</b>
                <small>
                  {row.points == null
                    ? 'open'
                    : row.leg.side === 'SELL'
                      ? `${row.entry.price?.toFixed(2)} − ${row.exit?.price?.toFixed(2)}`
                      : `${row.exit?.price?.toFixed(2)} − ${row.entry.price?.toFixed(2)}`}
                </small>
              </div>
            </Fragment>
          ))}
          {spotCell(data.entry_spot, date, entryEvents, entryLink, entryMinute, niftyIn)}
          {spotCell(data.exit_spot, exitDay, exitEvents, exitLink, exitMinute, niftyOut)}
          <div className="dc-s5-result spot">
            <span>NIFTY</span>
            <b>{niftyMove == null ? '—' : `${niftyMove > 0 ? '▲' : niftyMove < 0 ? '▼' : ''} ${Math.abs(niftyMove).toFixed(2)}`}</b>
            <small>{niftyMove == null || niftyIn == null ? 'fill to fill' : `${((niftyMove / niftyIn) * 100).toFixed(2)}% fill to fill`}</small>
          </div>
        </div>
      </div>
    )
  }

  const partial = data.legs.filter((leg) => leg.bars.length === 0)
  const fromTicks = data.legs.some((leg) => leg.bars_source === 'ticks')
  const isPaper = data.source === 'paper'
  const checksOk = data.checks ? data.checks.entry_matches_0930_open && data.checks.exit_matches_1529_close : false
  const worst = at(data.pnl)
  const position = at(data.position)
  const spotNow = at(data.spot)

  const legGrid = (legs: LabDayChartLeg[], range: [number, number] | null, label: string, chartDate = date, chartLink = link) =>
    legs.length > 0 && (
      <div className="dc-group">
        <div className="dc-group-title">{label}</div>
        <div className="dc-grid" style={{ gridTemplateColumns: `repeat(${legs.length}, minmax(0, 1fr))` }}>
          {legs.map((leg) => {
            const points = legPoints(leg)
            return (
              <div key={`${leg.type}${leg.strike}${leg.side}`} className="dc-cell">
                <div className="dc-cell-head" style={{ color: legColor(leg) }}>
                  <b>
                    {leg.side} {legName(leg)}
                  </b>
                  <span>
                    {leg.entry?.toFixed(2) ?? '—'} → {leg.exit?.toFixed(2) ?? '—'} <em className={pnlClass(points)}>{points == null ? '' : `${formatPts(points)} pts`}</em>
                  </span>
                </div>
                {leg.bars.length ? <LegChart date={chartDate} leg={leg} theme={theme} candles={candles} link={chartLink} sharedRange={range} /> : <div className="research-empty">No candles stored for this contract.</div>}
              </div>
            )
          })}
        </div>
      </div>
    )

  return (
    <div className="dc-panel">
      <div className="dc-head">
        <div>
          <strong>
            {STRATEGY_LABEL[strategy]} · {date}
          </strong>{' '}
          {isPaper ? <PaperBadge /> : <span className="lab-status">BACKTEST</span>}
          {data.derived_from && <span className="lab-derived">via {STRATEGY_LABEL[data.derived_from]}</span>}
          <div className="dc-sub">
            Expiry {data.expiry ?? '—'} · ATM {data.atm_strike ?? '—'}
            {data.wing_width ? ` · wings ±${data.wing_width}` : ''} · gross <b className={pnlClass(data.trade.gross_pts)}>{formatPts(data.trade.gross_pts)} pts</b> · net{' '}
            <b className={pnlClass(data.trade.net_pts)}>{formatPts(data.trade.net_pts)} pts</b> per unit
          </div>
        </div>
        {onClose && (
          <button type="button" className="research-chip" onClick={onClose}>
            Close ✕
          </button>
        )}
      </div>

      {data.checks && (
        <div className={`dc-check ${checksOk ? 'ok' : 'bad'}`}>
          {checksOk ? "✓ Each leg's recorded entry equals its 09:30 candle open and its exit equals its 15:29 candle close." : '✗ A recorded entry or exit does not match the candle it should come from - check this day.'}
        </div>
      )}
      {isPaper && <div className="dc-note">PAPER: entries and exits are the recorded fills (sells at the bid, buys at the ask), so they can sit a little away from the candle prices.</div>}
      {fromTicks && <div className="dc-note">Some candles are built from the recorded live ticks, because no downloaded candles exist for this day yet.</div>}
      {partial.length > 0 && <div className="dc-note">No candles for {partial.map(legName).join(', ')}: the position charts are not drawn.</div>}

      <FillCheck rows={fillRows} exact={!isPaper} onJump={setZoom} />

      <div className="dc-controls">
        <div className="paths-choice">
          <span>Zoom</span>
          <div className="segmented" role="group" aria-label="Zoom">
            <button type="button" className={zoom === 'day' ? 'active' : ''} onClick={() => setZoom('day')}>
              Whole day
            </button>
            <button type="button" className={`dc-zoom-entry${zoom === 'entry' ? ' active' : ''}`} onClick={() => setZoom('entry')}>
              Entry {entryTime}
            </button>
            <button type="button" className={`dc-zoom-exit${zoom === 'exit' ? ' active' : ''}`} onClick={() => setZoom('exit')}>
              Exit {exitTime}
            </button>
          </div>
        </div>
        <div className="paths-choice">
          <span>Layout</span>
          <div className="segmented" role="group" aria-label="Layout">
            <button type="button" className={view === 'side' ? 'active' : ''} onClick={() => setView('side')}>
              Side by side
            </button>
            <button type="button" className={view === 'overlay' ? 'active' : ''} onClick={() => setView('overlay')}>
              Overlay
            </button>
          </div>
        </div>
        {view === 'side' && (
          <>
            <div className="paths-choice">
              <span>Style</span>
              <div className="segmented" role="group" aria-label="Style">
                <button type="button" className={candles ? 'active' : ''} onClick={() => setCandles(true)}>
                  Candles
                </button>
                <button type="button" className={!candles ? 'active' : ''} onClick={() => setCandles(false)}>
                  Line
                </button>
              </div>
            </div>
            <div className="paths-choice">
              <span>Price scale</span>
              <div className="segmented" role="group" aria-label="Price scale">
                <button type="button" className={!sameScale ? 'active' : ''} onClick={() => setSameScale(false)}>
                  Each its own
                </button>
                <button type="button" className={sameScale ? 'active' : ''} onClick={() => setSameScale(true)}>
                  Same for both
                </button>
              </div>
            </div>
          </>
        )}
      </div>

      <div className="dc-readout" aria-live="polite">
        {hoverMinute ? (
          <>
            <b>{hoverMinute}</b>
            {data.legs.map((leg) => {
              const bar = leg.bars.find((b) => b.t === hoverMinute)
              return (
                <span key={`${leg.type}${leg.strike}${leg.side}`} style={{ color: legColor(leg) }}>
                  {legName(leg)}: {bar ? `O ${bar.o} H ${bar.h} L ${bar.l} C ${bar.c}` : '—'}
                </span>
              )
            })}
            {position && <span>close-out cost {position.price.toFixed(2)}</span>}
            {worst && <span className={pnlClass(worst.pts)}>profit {formatPts(worst.pts)} pts</span>}
            {spotNow && <span>NIFTY {spotNow.c}</span>}
          </>
        ) : (
          <span className="dim">Move over any chart: the others follow to the same minute.</span>
        )}
      </div>

      {view === 'side' ? (
        <>
          {legGrid(bodies, bodyRange, wings.length ? 'Sold legs (the straddle)' : 'The two strikes sold')}
          {legGrid(wings, wingRange, 'Bought wings (protection)')}
        </>
      ) : (
        <div className="dc-group">
          <div className="dc-group-title">Every leg on one scale (dashed = entry price)</div>
          <OverlayChart date={date} legs={drawn} theme={theme} link={link} events={dayEvents} />
        </div>
      )}

      {data.complete && (
        <div className="dc-group">
          <div className="dc-group-title">The whole position</div>
          <div className="dc-grid" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
            <div className="dc-cell">
              <div className="dc-cell-head">
                <b>Cost to close everything now (points)</b>
                <span>entry {entryLevel?.toFixed(2)}: lower is better for the seller</span>
              </div>
              <PositionChart date={date} series={positionPrice} kind="price" entryLevel={entryLevel} theme={theme} link={link} events={dayEvents} />
            </div>
            <div className="dc-cell">
              <div className="dc-cell-head">
                <b>Profit so far (points per unit)</b>
                <span>
                  worst {data.stats.worst ? `${formatPts(data.stats.worst.pts)} at ${data.stats.worst.t}` : '—'} · best {data.stats.best ? `${formatPts(data.stats.best.pts)} at ${data.stats.best.t}` : '—'}
                </span>
              </div>
              <PositionChart date={date} series={positionPnl} kind="profit" entryLevel={null} theme={theme} link={link} events={dayEvents} />
            </div>
          </div>
        </div>
      )}

      {data.spot.length > 0 && (
        <div className="dc-group">
          <div className="dc-group-title">
            NIFTY
            {data.stats.spot && ` · 09:30 ${data.stats.spot.open_0930.toFixed(2)} → close ${data.stats.spot.close.toFixed(2)} (range ${data.stats.spot.low.toFixed(0)}-${data.stats.spot.high.toFixed(0)})`}
            {data.spot_source !== 'nifty_1min.csv' && ` · ${data.spot_source} (coarser than 1 minute)`}
          </div>
          <SpotChart date={date} bars={data.spot} theme={theme} link={link} events={dayEvents} />
        </div>
      )}
      <small className="research-hint">
        Amber marks the entry and violet the exit on every chart: a shaded column on that minute&apos;s candle, a dashed line at the fill price, and an arrow with the price and time. Use Zoom (or click a time in the table) to see the
        fill candle up close. Candles are the stored 1-minute candles of each contract from 09:15 to {data.exit_bar}; the trade is open from 09:30 to {data.exit_bar} ({data.source === 'paper' ? 'the paper exit is the last minute the options trade' : 'the backtest exits at 15:29'}). The position charts mark every leg at each minute&apos;s close, and a minute with no trade repeats
        the previous price.
      </small>
    </div>
  )
}
