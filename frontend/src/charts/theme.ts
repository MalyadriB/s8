import { CrosshairMode, LineStyle, TickMarkType, type Time } from 'lightweight-charts'

export type ThemeName = 'dark' | 'light'

const istTickFormatter = new Intl.DateTimeFormat('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' })

/** A unix-seconds timestamp as an IST HH:MM tick, whatever the viewer's own time zone. */
const formatIstTick = (unixSeconds: number): string => istTickFormatter.format(new Date(unixSeconds * 1000))

// Only the chart surface, grid, border and text change with the theme; CE/PE and candle colours stay the same in both.
const CHART_PALETTES: Record<ThemeName, { background: string; grid: string; border: string; text: string }> = {
  dark: { background: '#1a1d22', grid: '#2c3139', border: '#383e48', text: '#b5ada2' }, // the app's graphite panel and warm grey text
  light: { background: '#ffffff', grid: '#e3e7ee', border: '#cbd3dc', text: '#5b6472' },
}

// A widened, translucent vertical line, so the hovered candle reads as highlighted rather than a stray tick.
const CROSSHAIR = {
  mode: CrosshairMode.Normal,
  vertLine: { width: 4 as const, color: 'rgba(47, 155, 255, 0.18)', style: LineStyle.Solid, labelBackgroundColor: '#2f9bff' },
  horzLine: { width: 1 as const, color: 'rgba(47, 155, 255, 0.6)', style: LineStyle.Solid, labelBackgroundColor: '#2f9bff' },
}

const axisDateFormatter = new Intl.DateTimeFormat('en-IN', { day: '2-digit', month: 'short', timeZone: 'Asia/Kolkata' })

/** Intraday ticks show IST time; the first tick of a new day shows its date instead, so a session start is visible. */
function formatAxisTickMark(time: Time, tickMarkType: TickMarkType): string {
  if (tickMarkType === TickMarkType.Time || tickMarkType === TickMarkType.TimeWithSeconds) return formatIstTick(time as number)
  return axisDateFormatter.format(new Date((time as number) * 1000))
}

/** The shared layout, grid, axis and crosshair options for one theme. */
export function buildChartOptions(theme: ThemeName) {
  const palette = CHART_PALETTES[theme]
  return {
    layout: { background: { color: palette.background }, textColor: palette.text, fontFamily: 'inherit', attributionLogo: false },
    grid: { vertLines: { color: palette.grid }, horzLines: { color: palette.grid } },
    rightPriceScale: { borderColor: palette.border },
    localization: { timeFormatter: formatIstTick },
    timeScale: { borderColor: palette.border, timeVisible: true, secondsVisible: true, tickMarkFormatter: formatAxisTickMark },
    crosshair: CROSSHAIR,
    autoSize: true,
  }
}
