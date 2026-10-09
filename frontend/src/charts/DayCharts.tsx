import { useEffect, useRef } from 'react'
import {
  AreaSeries,
  BaselineSeries,
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type SeriesType,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import type { LabDayChart, LabDayChartBar, LabDayChartLeg } from '../api'
import { buildChartOptions, type ThemeName } from './theme'

const CE = '#2f9bff'
const PE = '#ff8a32'
const UP = '#20c997'
const DOWN = '#ff4d5a'
/** Entry and exit have their own colours on every chart (never the leg's CE/PE colour), so they stand out from the candles. */
export const ENTRY_COLOR = '#eab308'
export const EXIT_COLOR = '#a855f7'
const ENTRY_COLUMN = 'rgba(234, 179, 8, 0.15)'
const EXIT_COLUMN = 'rgba(168, 85, 247, 0.17)'

/** A chart's height: pixels, or 'fill' to take its container's (which then sets the size, e.g. a grid cell). */
export type ChartHeight = number | 'fill'
const heightStyle = (height: ChartHeight) => ({ height: height === 'fill' ? '100%' : height })

/** Unix seconds for HH:MM on `date` in IST, so every chart shares one time axis and the axis reads as Indian time. */
export const minuteTime = (date: string, hhmm: string): UTCTimestamp => Math.floor(new Date(`${date}T${hhmm}:00+05:30`).getTime() / 1000) as UTCTimestamp

const TAIL_MINUTES = 6

/** A few empty minutes after the last candle, on every chart alike (so their time axes still line up): room for the
 * exit marker's label, which is centred on its candle and would otherwise be cut off at the right edge. */
function withTail<T extends { time: UTCTimestamp }>(points: T[]): (T | { time: UTCTimestamp })[] {
  const ordered = inOrder(points)
  if (ordered.length === 0) return ordered
  const last = ordered[ordered.length - 1].time
  return [...ordered, ...Array.from({ length: TAIL_MINUTES }, (_, i) => ({ time: (last + 60 * (i + 1)) as UTCTimestamp }))]
}

/** One point per minute, in time order. The chart library throws on a repeated or backward time (and the throw takes
 * the whole page down), and a stored day can hold a minute twice - 8 Oct 2026's NIFTY bars carry a copy of the 12:55
 * candle filed again under 12:56, after a feed reconnect. The first bar of a minute wins: there, the real one. */
function inOrder<T extends { time: UTCTimestamp }>(points: T[]): T[] {
  const byTime = new Map<number, T>()
  for (const point of points) if (!byTime.has(point.time)) byTime.set(point.time, point)
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

/** A fill moment to highlight: the minute of the entry or the exit. */
export type DayEvent = { time: string; kind: 'entry' | 'exit' }

/** Full-height translucent columns at the entry/exit minutes, drawn behind everything (so add this first): zoomed in they
 * highlight the whole candle the fill came from, over the whole day they read as vertical lines. */
function addEventColumns(chart: IChartApi, date: string, events: DayEvent[]) {
  if (events.length === 0) return
  const columns = chart.addSeries(HistogramSeries, {
    priceScaleId: 'events',
    lastValueVisible: false,
    priceLineVisible: false,
    autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 1 } }),
  })
  chart.priceScale('events').applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false })
  const byTime = new Map(events.map((event) => [minuteTime(date, event.time) as number, event.kind]))
  columns.setData([...byTime.entries()].sort((a, b) => a[0] - b[0]).map(([time, kind]) => ({ time: time as UTCTimestamp, value: 1, color: kind === 'entry' ? ENTRY_COLUMN : EXIT_COLUMN })))
}

type Member = { chart: IChartApi; series: ISeriesApi<SeriesType>; valueAt: (time: number) => number | undefined }

/** Keeps the day's charts moving together: one time range, and one crosshair (hover a minute on the call and the put shows the same minute). */
export class DayChartLink {
  private members = new Set<Member>()
  private busy = false
  private range: { from: UTCTimestamp; to: UTCTimestamp }
  onHover: (time: number | null) => void = () => {}

  /** `from`/`to`: the whole trading day. Every chart opens on it, whatever data it holds, so they line up minute for minute. */
  constructor(from: UTCTimestamp, to: UTCTimestamp) {
    this.range = { from, to }
  }

  /** The window every chart shows (the whole day, or a zoom set by show()); charts created later open on it too. */
  currentRange() {
    return this.range
  }

  /** Moves every linked chart to one window at once - e.g. a few minutes around the entry candle. */
  show(from: UTCTimestamp, to: UTCTimestamp) {
    this.range = { from, to }
    this.busy = true
    for (const member of this.members) member.chart.timeScale().setVisibleRange(this.range)
    this.busy = false
  }

  register(member: Member): () => void {
    this.members.add(member)
    member.chart.timeScale().setVisibleRange(this.currentRange())
    const onRange = (next: { from: Time; to: Time } | null) => {
      if (this.busy || !next) return
      this.busy = true
      for (const other of this.members) if (other !== member) other.chart.timeScale().setVisibleRange(next)
      this.busy = false
    }
    const onCross = (param: { time?: Time }) => {
      if (this.busy) return
      this.busy = true
      const time = param.time as number | undefined
      for (const other of this.members) {
        if (other === member) continue
        const value = time === undefined ? undefined : other.valueAt(time)
        if (time === undefined || value === undefined) other.chart.clearCrosshairPosition()
        else other.chart.setCrosshairPosition(value, time as UTCTimestamp, other.series)
      }
      this.busy = false
      this.onHover(time ?? null)
    }
    member.chart.timeScale().subscribeVisibleTimeRangeChange(onRange)
    member.chart.subscribeCrosshairMove(onCross)
    return () => {
      this.members.delete(member)
      member.chart.timeScale().unsubscribeVisibleTimeRangeChange(onRange)
      member.chart.unsubscribeCrosshairMove(onCross)
    }
  }
}

/** The day charts' look: a transparent surface (the chart sits on its panel), faint horizontal rules only, no axis
 * borders - the candles and the fill marks carry the picture. */
function baseOptions(theme: ThemeName, height: ChartHeight, container: HTMLElement) {
  const base = buildChartOptions(theme)
  const rule = theme === 'dark' ? 'rgba(255, 255, 255, 0.05)' : 'rgba(15, 23, 42, 0.06)'
  return {
    ...base,
    height: height === 'fill' ? container.clientHeight || 200 : height,
    width: container.clientWidth,
    layout: { ...base.layout, background: { type: ColorType.Solid, color: 'transparent' }, fontSize: 11 },
    grid: { vertLines: { visible: false }, horzLines: { color: rule } },
    rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.14, bottom: 0.12 } },
    // rightOffset leaves room after the last candle so the exit marker's label is not cut off at the edge
    timeScale: { ...base.timeScale, borderVisible: false, secondsVisible: false, rightOffset: 6 },
    autoSize: true,
  }
}

/** A chart made before its container has a width keeps the bar spacing it computed then; re-fit the linked window once the real width is known (and if the panel is resized). */
function fitOnResize(chart: IChartApi, container: HTMLElement, link: DayChartLink): () => void {
  let width = container.clientWidth
  const observer = new ResizeObserver(() => {
    if (Math.abs(container.clientWidth - width) > 1) {
      width = container.clientWidth
      chart.timeScale().setVisibleRange(link.currentRange())
    }
  })
  observer.observe(container)
  return () => observer.disconnect()
}

const price = (value: number | null | undefined) => (value == null ? '—' : value.toFixed(2))

/** One leg as candles (or a line) with the trade's entry and exit marked and an entry price line, so what the strike did between the two is visible. */
export function LegChart({ date, leg, theme, candles, link, sharedRange, height = 280, markerText = true }: {
  date: string
  leg: LabDayChartLeg
  theme: ThemeName
  candles: boolean
  link: DayChartLink
  /** When both legs use one price scale, the [min, max] to force on this one. */
  sharedRange: [number, number] | null
  height?: ChartHeight
  /** Off: the fill arrows carry no text - for a page whose headings already give the price and the minute. */
  markerText?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const container = ref.current
    if (!container || leg.bars.length === 0) return
    const chart = createChart(container, baseOptions(theme, height, container))
    const times = leg.bars.map((bar) => minuteTime(date, bar.t))
    const provider = sharedRange ? { autoscaleInfoProvider: () => ({ priceRange: { minValue: sharedRange[0], maxValue: sharedRange[1] } }) } : {}
    const sold = leg.side === 'SELL'
    addEventColumns(chart, date, [
      ...(leg.entry != null ? [{ time: leg.entry_time, kind: 'entry' as const }] : []),
      ...(leg.exit != null ? [{ time: leg.exit_time, kind: 'exit' as const }] : []),
    ])
    let series: ISeriesApi<SeriesType>
    if (candles) {
      const s = chart.addSeries(CandlestickSeries, { upColor: UP, downColor: DOWN, borderVisible: false, wickUpColor: UP, wickDownColor: DOWN, priceLineVisible: false, lastValueVisible: false, ...provider })
      s.setData(withTail(leg.bars.map((bar, i) => ({ time: times[i], open: bar.o, high: bar.h, low: bar.l, close: bar.c }))))
      series = s
    } else {
      // Shaded against the entry price: green where this leg is in profit, red where it is losing (a sold leg profits below its entry).
      const good = { line: UP, near: 'rgba(32,201,151,0.30)', far: 'rgba(32,201,151,0.03)' }
      const bad = { line: DOWN, near: 'rgba(255,77,90,0.30)', far: 'rgba(255,77,90,0.03)' }
      const above = sold ? bad : good
      const below = sold ? good : bad
      const s = chart.addSeries(BaselineSeries, {
        baseValue: { type: 'price', price: leg.entry ?? leg.exit ?? leg.bars[0].c },
        topLineColor: above.line,
        topFillColor1: above.near,
        topFillColor2: above.far,
        bottomLineColor: below.line,
        bottomFillColor1: below.far,
        bottomFillColor2: below.near,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        ...provider,
      })
      s.setData(withTail(leg.bars.map((bar, i) => ({ time: times[i], value: bar.c }))))
      series = s
    }
    // the price itself is on the axis label; the line's own tag only says which fill it is
    if (leg.entry != null) series.createPriceLine({ price: leg.entry, color: ENTRY_COLOR, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: sold ? 'sold' : 'bought' })
    if (leg.exit != null) series.createPriceLine({ price: leg.exit, color: EXIT_COLOR, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: sold ? 'bought back' : 'sold' })
    const markers: SeriesMarker<Time>[] = []
    if (leg.entry != null) markers.push({ time: minuteTime(date, leg.entry_time), position: sold ? 'aboveBar' : 'belowBar', color: ENTRY_COLOR, shape: sold ? 'arrowDown' : 'arrowUp', size: 1, text: markerText ? `${sold ? 'SELL' : 'BUY'} ${price(leg.entry)} · ${leg.entry_time}` : undefined })
    if (leg.exit != null) markers.push({ time: minuteTime(date, leg.exit_time), position: sold ? 'belowBar' : 'aboveBar', color: EXIT_COLOR, shape: sold ? 'arrowUp' : 'arrowDown', size: 1, text: markerText ? `${sold ? 'BUY BACK' : 'SELL'} ${price(leg.exit)} · ${leg.exit_time}` : undefined })
    createSeriesMarkers(series, markers)
    chart.timeScale().fitContent()
    const byTime = new Map(leg.bars.map((bar, i) => [times[i] as number, bar.c]))
    const stopFit = fitOnResize(chart, container, link)
    const unlink = link.register({ chart, series, valueAt: (time) => byTime.get(time) })
    return () => {
      stopFit()
      unlink()
      chart.remove()
    }
  }, [date, leg, theme, candles, link, sharedRange, height, markerText])
  return <div className="dc-chart" ref={ref} style={heightStyle(height)} />
}

/** Both (or all four) legs' closes on one chart and one scale: the quickest way to see one leg's loss paid for by the other. */
export function OverlayChart({ date, legs, theme, link, events = [], height = 320 }: { date: string; legs: LabDayChartLeg[]; theme: ThemeName; link: DayChartLink; events?: DayEvent[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const container = ref.current
    if (!container) return
    const chart = createChart(container, baseOptions(theme, height, container))
    addEventColumns(chart, date, events)
    let anchor: ISeriesApi<SeriesType> | null = null
    let anchorMap = new Map<number, number>()
    legs.forEach((leg) => {
      if (leg.bars.length === 0) return
      const shade = leg.role === 'wing' ? 0.55 : 1
      const series = chart.addSeries(LineSeries, {
        color: leg.type === 'CE' ? CE : PE,
        lineWidth: leg.role === 'wing' ? 1 : 2,
        lineStyle: leg.role === 'wing' ? LineStyle.Dotted : LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: true,
        title: `${leg.type} ${leg.strike}${leg.role === 'wing' ? ' wing' : ''}`,
        opacity: shade,
      } as never)
      const times = leg.bars.map((bar) => minuteTime(date, bar.t))
      series.setData(withTail(leg.bars.map((bar, i) => ({ time: times[i], value: bar.c }))))
      if (leg.entry != null) series.createPriceLine({ price: leg.entry, color: leg.type === 'CE' ? CE : PE, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: `${leg.side === 'SELL' ? 'sold' : 'bought'} ${price(leg.entry)}` })
      if (!anchor) {
        anchor = series
        anchorMap = new Map(leg.bars.map((bar, i) => [times[i] as number, bar.c]))
      }
    })
    chart.timeScale().fitContent()
    const stopFit = fitOnResize(chart, container, link)
    const unlink = anchor ? link.register({ chart, series: anchor, valueAt: (time) => anchorMap.get(time) }) : () => {}
    return () => {
      stopFit()
      unlink()
      chart.remove()
    }
  }, [date, legs, theme, link, events, height])
  return <div className="dc-chart" ref={ref} style={{ height }} />
}

/** The position as a whole through the day: what it would cost to close everything (a line, with the entry level marked) or the profit so far. */
export function PositionChart({ date, series: points, kind, entryLevel, theme, link, events = [], height = 220 }: {
  date: string
  series: { t: string; v: number }[]
  kind: 'price' | 'profit'
  entryLevel: number | null
  theme: ThemeName
  link: DayChartLink
  events?: DayEvent[]
  height?: number
}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const container = ref.current
    if (!container || points.length === 0) return
    const chart = createChart(container, baseOptions(theme, height, container))
    addEventColumns(chart, date, events)
    let series: ISeriesApi<SeriesType>
    if (kind === 'profit') {
      const s = chart.addSeries(BaselineSeries, {
        baseValue: { type: 'price', price: 0 },
        topLineColor: UP,
        topFillColor1: 'rgba(32,201,151,0.35)',
        topFillColor2: 'rgba(32,201,151,0.03)',
        bottomLineColor: DOWN,
        bottomFillColor1: 'rgba(255,77,90,0.03)',
        bottomFillColor2: 'rgba(255,77,90,0.35)',
        lineWidth: 2,
        priceLineVisible: false,
      })
      s.setData(withTail(points.map((p) => ({ time: minuteTime(date, p.t), value: p.v }))))
      series = s
    } else {
      const s = chart.addSeries(LineSeries, { color: '#a78bfa', lineWidth: 2, priceLineVisible: false })
      s.setData(withTail(points.map((p) => ({ time: minuteTime(date, p.t), value: p.v }))))
      if (entryLevel != null) s.createPriceLine({ price: entryLevel, color: ENTRY_COLOR, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: `entry ${price(entryLevel)}` })
      series = s
    }
    chart.timeScale().fitContent()
    const byTime = new Map(points.map((p) => [minuteTime(date, p.t) as number, p.v]))
    const stopFit = fitOnResize(chart, container, link)
    const unlink = link.register({ chart, series, valueAt: (time) => byTime.get(time) })
    return () => {
      stopFit()
      unlink()
      chart.remove()
    }
  }, [date, points, kind, entryLevel, theme, link, events, height])
  return <div className="dc-chart" ref={ref} style={{ height }} />
}

/** NIFTY through the day, with the entry and exit minutes marked, for the "why did it move" question. */
export function SpotChart({ date, bars, theme, link, events, height = 200, markerText = true }: {
  date: string
  bars: LabDayChartBar[]
  theme: ThemeName
  link: DayChartLink
  events: DayEvent[]
  height?: ChartHeight
  markerText?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const container = ref.current
    if (!container || bars.length === 0) return
    const chart = createChart(container, baseOptions(theme, height, container))
    addEventColumns(chart, date, events)
    const ink = theme === 'dark' ? '148, 170, 210' : '71, 98, 145'
    const series = chart.addSeries(AreaSeries, {
      lineColor: `rgb(${ink})`,
      topColor: `rgba(${ink}, 0.26)`,
      bottomColor: `rgba(${ink}, 0.02)`,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
    })
    const times = bars.map((bar) => minuteTime(date, bar.t))
    series.setData(withTail(bars.map((bar, i) => ({ time: times[i], value: bar.c }))))
    createSeriesMarkers(
      series,
      events
        .map((event): SeriesMarker<Time> => ({ time: minuteTime(date, event.time), position: 'belowBar', color: event.kind === 'entry' ? ENTRY_COLOR : EXIT_COLOR, shape: 'arrowUp', size: 1, text: markerText ? `${event.kind} ${event.time}` : undefined }))
        .filter((marker) => times.some((t) => t === marker.time)),
    )
    chart.timeScale().fitContent()
    const byTime = new Map(bars.map((bar, i) => [times[i] as number, bar.c]))
    const stopFit = fitOnResize(chart, container, link)
    const unlink = link.register({ chart, series, valueAt: (time) => byTime.get(time) })
    return () => {
      stopFit()
      unlink()
      chart.remove()
    }
  }, [date, bars, theme, link, events, height, markerText])
  return <div className="dc-chart" ref={ref} style={heightStyle(height)} />
}
