import type { LabDay, LabFill, LabLeg, LabSize, LabStrategy } from './api'

export const STRATEGY_LABEL: Record<LabStrategy, string> = {
  straddle_sell: 'Straddle sell',
  iron_fly_w1: 'Iron fly · W = 1× premium',
  iron_fly_w2: 'Iron fly · W = 2× premium',
  straddle_expiry_wings: 'Straddle, wings on expiry day',
  straddle_sell_overnight: 'Straddle sell, overnight to next 09:30',
  straddle_sell_two_overnight: 'Straddle sell, two nights to day-after-next 09:30',
  straddle_sell_three_overnight: 'Straddle sell, three nights to the third trading day 09:30',
  straddle_sell_overnight_skip_friday: 'Straddle sell, overnight - skip Friday entries',
  straddle_sell_overnight_skip_friday_december: 'Straddle sell, overnight - skip Friday and December entries',
}

export const STRATEGY_SHORT: Record<LabStrategy, string> = {
  straddle_sell: 'S1 straddle',
  iron_fly_w1: 'S2 iron fly W1',
  iron_fly_w2: 'S3 iron fly W2',
  straddle_expiry_wings: 'S4 straddle + expiry wings',
  straddle_sell_overnight: 'S5 straddle overnight',
  straddle_sell_two_overnight: 'S6 straddle two overnights',
  straddle_sell_three_overnight: 'S7 straddle three overnights',
  straddle_sell_overnight_skip_friday: 'S8 overnight (no Friday)',
  straddle_sell_overnight_skip_friday_december: 'S10 overnight (no Fri, no Dec)',
}

export const STRATEGY_COLOR: Record<LabStrategy, string> = {
  straddle_sell: '#2f9bff',
  iron_fly_w1: '#20c997',
  iron_fly_w2: '#f5b942',
  straddle_expiry_wings: '#a78bfa',
  straddle_sell_overnight: '#f472b6',
  straddle_sell_two_overnight: '#fb923c',
  straddle_sell_three_overnight: '#22d3ee',
  straddle_sell_overnight_skip_friday: 'var(--s8)', // white on the dark theme, ink on the light one (App.css)
  straddle_sell_overnight_skip_friday_december: '#6366f1',
}

const rupees = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 })

/** ₹1,23,456 with a real minus sign; blank for a missing value. */
export function formatRs(value: number | null | undefined): string {
  if (value == null) return '—'
  const text = rupees.format(Math.abs(Math.round(value)))
  return `${value < 0 && Math.round(value) !== 0 ? '−' : ''}₹${text}`
}

export function formatPts(value: number | null | undefined, digits = 2): string {
  if (value == null) return '—'
  return `${value < 0 ? '−' : ''}${Math.abs(value).toFixed(digits)}`
}

/** ₹ with an explicit + on a profit, so a P&L reads as one at a glance. */
export const signedRs = (value: number | null | undefined): string => (value != null && Math.round(value) > 0 ? `+${formatRs(value)}` : formatRs(value))

export const pnlClass = (value: number | null | undefined): string => (value == null || value === 0 ? '' : value > 0 ? 'positive' : 'negative')

const isFill = (value: LabFill | number | null | undefined): value is LabFill => typeof value === 'object' && value !== null

/** A leg's entry/exit price: the candle price on backtest rows, the fill on paper rows. */
export const priceOf = (value: LabFill | number | null | undefined): number | null => (isFill(value) ? value.fill : typeof value === 'number' ? value : null)

export const isEstimated = (value: LabFill | number | null | undefined): boolean => isFill(value) && value.source.startsWith('estimated')

export const dayHasEstimatedFill = (day: LabDay): boolean => (day.legs ?? []).some((leg) => isEstimated(leg.entry) || isEstimated(leg.exit) || leg.entry_estimated || leg.exit_estimated)

/** Net credit in points: what was collected (sold minus bought) at entry, and what closing costs (bought back minus sold) at exit. */
export function creditPoints(legs: LabLeg[] | null, phase: 'entry' | 'exit'): number | null {
  if (!legs?.length) return null
  let total = 0
  for (const leg of legs) {
    const price = priceOf(leg[phase])
    if (price == null) return null
    // Entry: SELL collects, BUY pays. Exit: the opposite trade happens, so a short pays and a long collects.
    const collects = (leg.side === 'SELL') === (phase === 'entry')
    total += collects ? price : -price
  }
  return total
}

/** "25000" for a straddle, "25000 ± 300" for an iron fly. */
export function strikesLabel(day: LabDay): string {
  if (day.atm_strike == null) return '—'
  return day.wing_width ? `${day.atm_strike} ± ${day.wing_width}` : String(day.atm_strike)
}

export function formatCountdown(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60
  return `${hours > 0 ? `${hours}h ` : ''}${String(minutes).padStart(2, '0')}m ${String(rest).padStart(2, '0')}s`
}

export const STATUS_LABEL: Record<string, string> = {
  open: 'OPEN',
  closed: 'CLOSED',
  missed_entry: 'MISSED ENTRY',
  missed_exit: 'EXITED LATE', // closed, just after its exit time - not left open
  error: 'ERROR',
  backtest: 'BACKTEST',
}

/** IST clock time (HH:MM:SS) of an ISO timestamp that already carries the +05:30 offset. */
export const clockOf = (iso: string | null | undefined): string => (iso ? iso.slice(11, 19) : '—')

export const sizeKey = (size: LabSize): string => `${size.mode}:${size.value}`

/** "2 lots" or "130 qty". */
/** A mark's minute as a clock. Minutes past 24:00 are the next session's morning: an overnight position is marked from
 * that session's 09:15 open until its 09:30 exit under its own entry day, so "33:20" reads as 09:20. */
export const markClock = (minute: string): string => {
  const hour = Number(minute.slice(0, 2))
  return hour >= 24 ? `${String(hour - 24).padStart(2, '0')}${minute.slice(2)}` : minute
}

export const sizeLabel = (size: LabSize): string => (size.mode === 'lots' ? `${size.value} lot${size.value === 1 ? '' : 's'}` : `${size.value} qty`)

/** "65 units (1 lot)" for a row; a quantity that is not a whole number of lots says so. */
export function tradeSizeLabel(day: Pick<LabDay, 'qty' | 'lots' | 'whole_lots'>): string {
  return day.whole_lots ? `${day.qty} units (${day.lots} lot${day.lots === 1 ? '' : 's'})` : `${day.qty} units (${day.lots} lots - not a whole number of lots)`
}

/** Points made on one unit of a leg. */
export function legPoints(leg: LabLeg): number | null {
  const entry = priceOf(leg.entry)
  const exit = priceOf(leg.exit)
  if (entry == null || exit == null) return null
  return leg.side === 'SELL' ? entry - exit : exit - entry
}

export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/** The weeks of a 'YYYY-MM' month as rows of 7 (Monday first): an ISO date per day, null for the padding around it. */
export function monthGrid(month: string): (string | null)[][] {
  const [year, mon] = month.split('-').map(Number)
  const lead = (new Date(Date.UTC(year, mon - 1, 1)).getUTCDay() + 6) % 7
  const days = new Date(Date.UTC(year, mon, 0)).getUTCDate()
  const cells: (string | null)[] = [...Array<null>(lead).fill(null)]
  for (let day = 1; day <= days; day += 1) cells.push(`${month}-${String(day).padStart(2, '0')}`)
  while (cells.length % 7 !== 0) cells.push(null)
  const weeks: (string | null)[][] = []
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7))
  return weeks
}

export const monthLabel = (month: string): string =>
  new Date(`${month}-01T00:00:00Z`).toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' })

/** The result's shade: green for a gain, red for a loss, stronger the bigger it is against the month's biggest move. */
export function heat(value: number | null | undefined, biggest: number): string | undefined {
  if (value == null || value === 0 || biggest <= 0) return undefined
  const alpha = 0.1 + 0.42 * Math.min(1, Math.abs(value) / biggest)
  return value > 0 ? `rgba(32, 201, 151, ${alpha.toFixed(2)})` : `rgba(255, 77, 90, ${alpha.toFixed(2)})`
}

const SOURCE_TAG: Partial<Record<LabStrategy, string>> = { straddle_sell: 'S1', iron_fly_w2: 'S3' }

/** "via S3" for a derived row (which strategy's result that day is), else empty. */
export const derivedTag = (day: Pick<LabDay, 'derived_from'>): string => (day.derived_from ? `via ${SOURCE_TAG[day.derived_from] ?? day.derived_from}` : '')
