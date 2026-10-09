import { useCallback, useEffect, useRef, useState } from 'react'
import {
  BaselineSeries,
  ColorType,
  createChart,
  CrosshairMode,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts'
import type { LabMark } from '../api'
import { formatRs, signedRs } from '../lab'
import { buildChartOptions, type ThemeName } from './theme'
import { reducedMotion } from '../components/Motion'

// Minutes are plotted on one fixed dummy day so the axis reads as an HH:MM clock; the timestamp is never shown as a date.
const DUMMY_DAY_SECONDS = Date.UTC(2000, 0, 3) / 1000
const dummyTimestamp = (hhmm: string): UTCTimestamp => {
  const [hours, minutes] = hhmm.split(':').map(Number)
  return (DUMMY_DAY_SECONDS + hours * 3600 + minutes * 60) as UTCTimestamp
}
const clockLabel = (timestamp: number): string => new Date(timestamp * 1000).toISOString().slice(11, 16)
const tone = (value: number | null | undefined): string => (value == null || Math.round(value) === 0 ? '' : value > 0 ? 'positive' : 'negative')

const rsFormat = { type: 'custom' as const, formatter: (price: number) => formatRs(price), minMove: 1 }

// Profit / loss colours per theme - the same values as the CSS --positive / --negative tokens.
const PNL_COLORS: Record<ThemeName, { up: string; down: string }> = { dark: { up: '#20c997', down: '#ff4d5a' }, light: { up: '#12925f', down: '#d6304a' } }
// The crosshair's axis labels, in the dashboard's violet.
const CROSSHAIR_LABEL: Record<ThemeName, string> = { dark: '#7c5cf0', light: '#6d28d9' }
const withAlpha = (hex: string, alpha: number) => `${hex}${Math.round(alpha * 255).toString(16).padStart(2, '0')}`

// The session the line fills in toward; a position still marked after it (S5, held overnight) stretches the axis.
const WINDOW_START = '09:30'
const WINDOW_END = '15:29'
// An overnight position's next-morning marks (minutes past 24:00, from the next session's 09:15 open) follow its own
// session after a short gap - the night - and the axis holds room up to their 09:30 exit.
const NEXT_SESSION = '24:00'
const NEXT_EXIT = '33:30'
const NIGHT_SLOTS = 6
const REVEAL_MS = 900
const RIGHT_AIR = 4 // minutes of space after the last point, so the "now" dot and its ripple clear the price axis

type Point = { time: UTCTimestamp; value: number }
type Flag = { x: number; y: number; value: number; minute: string; align: 'start' | 'center' | 'end' }
type Overlay = { high: Flag | null; low: Flag | null; now: { x: number; y: number } | null; night: number | null }
type Hover = { x: number; y: number; width: number; value: number }


/** Every minute from `from` to `to` (both inclusive) as whitespace: the time axis always spans the whole session, so it
 * never rescales as the line grows (and the draw-in animation can grow the line inside a fixed frame). */
function minutesBetween(from: number, to: number): WhitespaceData<Time>[] {
  const out: WhitespaceData<Time>[] = []
  for (let time = from; time <= to; time += 60) out.push({ time: time as UTCTimestamp })
  return out
}

/** One position's running mark-to-market through the day (one point per minute), as profit above ₹0 and loss below.
 *
 * Built once per theme and updated in place as marks arrive (rebuilding on every 2-second poll blocked the page). The
 * whole session is always on the axis; the first data draws itself in from the left; the day's high and low and the
 * latest minute are marked with HTML labels that stay inside the chart; and the crosshair snaps to the line, with the
 * hovered minute's MTM in a small pill above the point (its time is on the axis tag). `fill` makes the chart take its
 * parent's full height instead of a fixed 320px. */
export function MtmChart({ marks, theme, fill = false }: { marks: LabMark[]; theme: ThemeName; fill?: boolean }) {
  const hostRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<{ chart: IChartApi; series: ISeriesApi<'Baseline'> } | null>(null)
  const pointsRef = useRef<Point[]>([])
  const slotsRef = useRef(0) // minutes on the axis (points plus the whitespace around them)
  const nightRef = useRef<UTCTimestamp | null>(null) // the middle of the night gap, when the line runs into a next morning
  const revealedRef = useRef(false)
  const [revealing, setRevealing] = useState(false)
  const [overlay, setOverlay] = useState<Overlay | null>(null)
  const [hover, setHover] = useState<Hover | null>(null)

  // The labels follow the chart's own coordinates: after data changes and after every resize.
  const place = useCallback(() => {
    const current = chartRef.current
    const points = pointsRef.current
    if (!current || !points.length) return setOverlay(null)
    const width = current.chart.timeScale().width()
    const at = (point: Point) => {
      const x = current.chart.timeScale().timeToCoordinate(point.time)
      const y = current.series.priceToCoordinate(point.value)
      return x == null || y == null ? null : { x, y }
    }
    const flag = (point: Point): Flag | null => {
      const spot = at(point)
      if (!spot) return null
      return { ...spot, value: point.value, minute: clockLabel(point.time), align: spot.x < 64 ? 'start' : spot.x > width - 64 ? 'end' : 'center' }
    }
    const high = points.reduce((best, point) => (point.value > best.value ? point : best))
    const low = points.reduce((worst, point) => (point.value < worst.value ? point : worst))
    const many = points.length > 2
    const night = nightRef.current == null ? null : current.chart.timeScale().timeToCoordinate(nightRef.current)
    setOverlay({ high: many ? flag(high) : null, low: many && low !== high ? flag(low) : null, now: at(points[points.length - 1]), night })
  }, [])

  // ---- the chart itself, once per theme
  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const base = buildChartOptions(theme)
    const { up, down } = PNL_COLORS[theme]
    const guide = { width: 1 as const, style: LineStyle.Dashed, color: withAlpha(base.layout.textColor, 0.55), labelBackgroundColor: CROSSHAIR_LABEL[theme] }
    // the hover card carries the value, so the horizontal guide has no price tag of its own
    const clock = (time: Time) => clockLabel(Number(time))
    const chart = createChart(host, {
      ...base,
      layout: { ...base.layout, background: { type: ColorType.Solid, color: 'transparent' } },
      grid: { vertLines: { visible: false }, horzLines: { color: base.grid.horzLines.color, style: LineStyle.Dotted } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.2, bottom: 0.16 } },
      // The chart's time is an HH:MM clock on a dummy date, so the shared IST tick formatting does not apply.
      localization: { timeFormatter: clock, priceFormatter: (price: number) => formatRs(price) },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, tickMarkFormatter: clock, shiftVisibleRangeOnNewBar: false },
      crosshair: { mode: CrosshairMode.Magnet, vertLine: guide, horzLine: { ...guide, labelVisible: false } },
      // a dashboard view of one whole day: nothing to pan or zoom (the Chart dialog has the full candles)
      handleScroll: false,
      handleScale: false,
    })
    const series = chart.addSeries(BaselineSeries, {
      baseValue: { type: 'price', price: 0 },
      topLineColor: up,
      topFillColor1: withAlpha(up, 0.28),
      topFillColor2: withAlpha(up, 0.02),
      bottomLineColor: down,
      bottomFillColor1: withAlpha(down, 0.02),
      bottomFillColor2: withAlpha(down, 0.28),
      lineWidth: 2,
      priceFormat: rsFormat,
      lastValueVisible: true,
      priceLineVisible: false,
      crosshairMarkerRadius: 5,
      crosshairMarkerBorderWidth: 2,
      crosshairMarkerBorderColor: theme === 'dark' ? '#0b1620' : '#ffffff',
    })
    series.createPriceLine({ price: 0, color: withAlpha(base.layout.textColor, 0.45), lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: '' })
    chartRef.current = { chart, series }

    const onMove = (param: MouseEventParams<Time>) => {
      const data = param.seriesData.get(series) as { value?: number } | undefined
      if (!param.point || param.time == null || data?.value == null) return setHover(null)
      setHover({
        x: chart.timeScale().timeToCoordinate(param.time) ?? param.point.x,
        y: series.priceToCoordinate(data.value) ?? param.point.y,
        width: chart.timeScale().width(),
        value: data.value,
      })
    }
    chart.subscribeCrosshairMove(onMove)
    let frame = 0
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame)
      // after the chart's own resize: fit the day to the new width again, then move the labels
      frame = requestAnimationFrame(() => {
        if (slotsRef.current) chart.timeScale().setVisibleLogicalRange({ from: -0.5, to: slotsRef.current - 1 + RIGHT_AIR })
        frame = requestAnimationFrame(place)
      })
    })
    observer.observe(host)
    return () => {
      observer.disconnect()
      cancelAnimationFrame(frame)
      chart.unsubscribeCrosshairMove(onMove)
      chart.remove()
      chartRef.current = null
    }
  }, [theme, place])

  // ---- the data, updated in place
  useEffect(() => {
    const current = chartRef.current
    if (!current) return
    const points: Point[] = marks.map((mark) => ({ time: dummyTimestamp(mark.minute), value: mark.mtm_rs })).sort((a, b) => a.time - b.time)
    pointsRef.current = points
    if (!points.length) {
      current.series.setData([])
      setOverlay(null)
      return
    }
    const values = points.map((point) => point.value)
    const low = Math.min(0, ...values)
    const high = Math.max(0, ...values)
    // the price scale always fits the whole day (and ₹0), so it holds still while the line draws in
    current.series.applyOptions({ autoscaleInfoProvider: () => ({ priceRange: { minValue: low, maxValue: high } }) })
    const split = points.findIndex((point) => point.time >= dummyTimestamp(NEXT_SESSION))
    const morning = split > 0
    const start = Math.min(dummyTimestamp(WINDOW_START), points[0].time)
    const end = Math.max(dummyTimestamp(morning ? NEXT_EXIT : WINDOW_END), points[points.length - 1].time)
    const lead = minutesBetween(start, points[0].time - 60)
    const tail = minutesBetween(points[points.length - 1].time + 60, end)
    // the night: a few empty slots a second apart (no clock boundary falls inside them, so the axis labels none)
    const night: WhitespaceData<Time>[] = morning ? Array.from({ length: NIGHT_SLOTS }, (_, i) => ({ time: (points[split - 1].time + i + 1) as UTCTimestamp })) : []
    nightRef.current = morning ? (night[Math.floor(NIGHT_SLOTS / 2)].time as UTCTimestamp) : null
    const layout = (shown: number) => {
      const line = points.map((point, index) => (index < shown ? point : { time: point.time }))
      return morning ? [...lead, ...line.slice(0, split), ...night, ...line.slice(split), ...tail] : [...lead, ...line, ...tail]
    }
    slotsRef.current = lead.length + points.length + night.length + tail.length
    // the frame is the whole day by index - whitespace included (fitContent stops at the last real point)
    const frameDay = () => current.chart.timeScale().setVisibleLogicalRange({ from: -0.5, to: slotsRef.current - 1 + RIGHT_AIR })
    const finish = () => {
      current.series.setData(layout(points.length))
      frameDay()
      setRevealing(false)
      requestAnimationFrame(place)
    }
    if (revealedRef.current || reducedMotion()) {
      finish()
      return
    }
    // first data: draw the line in from the left (the rest of the day stays on the axis as whitespace)
    revealedRef.current = true
    setRevealing(true)
    setOverlay(null)
    current.series.setData(layout(1))
    frameDay()
    const began = performance.now()
    let frame = 0
    let done = false
    const step = (now: number) => {
      const progress = Math.min(1, (now - began) / REVEAL_MS)
      current.series.setData(layout(Math.max(1, Math.ceil(points.length * (1 - (1 - progress) ** 3)))))
      frameDay()
      if (progress < 1) frame = requestAnimationFrame(step)
      else {
        done = true
        finish()
      }
    }
    frame = requestAnimationFrame(step)
    return () => {
      cancelAnimationFrame(frame)
      // interrupted (new marks, or React's dev-mode double run): the next run draws it in again
      if (!done) revealedRef.current = false
      setRevealing(false)
    }
  }, [marks, theme, place])

  const tipAlign = !hover ? '' : hover.x < 44 ? ' start' : hover.x > hover.width - 44 ? ' end' : ''
  return (
    <div className={`mtm-chart lab-chart${fill ? ' fill' : ''}${hover ? ' hovering' : ''}`} onMouseLeave={() => setHover(null)}>
      <div className="mtm-host" ref={hostRef} />
      {!revealing && overlay?.high && <FlagLabel kind="high" flag={overlay.high} />}
      {!revealing && overlay?.low && <FlagLabel kind="low" flag={overlay.low} />}
      {!revealing && overlay?.now && <span className="mtm-now" style={{ left: overlay.now.x, top: overlay.now.y }} aria-hidden="true" />}
      {overlay?.night != null && (
        <span className="mtm-night" style={{ left: overlay.night }} title="Overnight: the position was held through the close; the line goes on from the next session's 09:15 open">
          night
        </span>
      )}
      {hover && (
        <b className={`mtm-tip ${tone(hover.value)}${tipAlign}${hover.y < 30 ? ' below' : ''}`} style={{ left: hover.x, top: hover.y }} aria-hidden="true">
          {signedRs(hover.value)}
        </b>
      )}
    </div>
  )
}

/** The day's high or low: a ring on the point, a short stem, and a label that turns inward near the chart's edges. */
function FlagLabel({ kind, flag }: { kind: 'high' | 'low'; flag: Flag }) {
  return (
    <div className={`mtm-flag ${kind} ${flag.align} ${flag.value >= 0 ? 'up' : 'down'}`} style={{ left: flag.x, top: flag.y }}>
      <span className="mtm-flag-chip">
        {kind === 'high' ? 'High' : 'Low'} <b>{signedRs(flag.value)}</b> <small>{flag.minute}</small>
      </span>
    </div>
  )
}
