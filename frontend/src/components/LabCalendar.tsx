import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { fetchLabBacktest, fetchLabMonthly, fetchLabPaper, LAB_STRATEGIES, type LabDay, type LabMonth, type LabSize, type LabStrategy } from '../api'
import {
  dayHasEstimatedFill,
  derivedTag,
  formatPts,
  formatRs,
  isEstimated,
  legPoints,
  monthGrid,
  monthLabel,
  pnlClass,
  priceOf,
  signedRs,
  sizeKey,
  sizeLabel,
  STATUS_LABEL,
  STRATEGY_COLOR,
  STRATEGY_LABEL,
  STRATEGY_SHORT,
  strikesLabel,
  tradeSizeLabel,
  WEEKDAYS,
} from '../lab'
import type { ThemeName } from '../charts/theme'
import { Icon, type IconName } from './Icons'
import { LabDayChart } from './LabDayChart'
import { TweenRs } from './Motion'
import { PaperBadge } from './PaperBadge'
import { useFitToWindow } from './useFitToWindow'
import { useLabData } from './useLabData'

type Source = 'backtest' | 'paper' | 'both'
type Kind = 'backtest' | 'paper'

// a day cell is narrow: the short form of a paper trade's status (the full one is in its label and the day dialog)
const CELL_STATUS: Record<string, string> = { open: 'open', closed: 'closed', missed_entry: 'missed', missed_exit: 'late exit', error: 'error', backtest: 'backtest' }
// a day the strategy has no row for, in a word or two (the backend's own reason is the cell's tooltip)
const SKIP_LABEL: Record<string, string> = {
  not_entered_friday: 'skips Fri',
  not_entered_december: 'skips Dec',
  no_next_day: 'awaiting exit',
  no_0930_next_day_candle: 'exit pending',
  no_0930_entry_candle: 'no entry candle',
}
const SKIP_ICON: Record<string, IconName> = {
  not_entered_friday: 'pause',
  not_entered_december: 'pause',
  no_next_day: 'hourglass',
  no_0930_next_day_candle: 'clock',
  no_0930_entry_candle: 'alert',
}
// trades still waiting to close (those cells get the "pending" look), as against days the rules leave out
const PENDING_SKIPS = new Set(['no_next_day', 'no_0930_next_day_candle'])
/** A day's or month's shading against the biggest one: faint for the ordinary ones, so only the big results stand out. */
const heatOf = (share: number): string => `${(3 + 24 * share ** 1.2).toFixed(1)}%`
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** Compact amounts: 1.2k, −34k, 2.1L (with ₹ unless `bare`). */
function shortRs(value: number, bare = false): string {
  const abs = Math.abs(value)
  const text = abs >= 100_000 ? `${(abs / 100_000).toFixed(abs >= 1_000_000 ? 0 : 1)}L` : abs >= 1000 ? `${(abs / 1000).toFixed(abs >= 10_000 ? 0 : 1)}k` : String(Math.round(abs))
  return `${value < 0 ? '−' : Math.round(value) > 0 ? '+' : ''}${bare ? '' : '₹'}${text}`
}

/** "Wed, 7 Oct 2026" / "Wed, 7 Oct" for an ISO date. */
const longDay = (day: string): string =>
  new Date(`${day}T00:00:00Z`).toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })
const dayNoYear = (day: string): string => new Date(`${day}T00:00:00Z`).toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })
const shortMonth = (month: string): string => `${MONTH_NAMES[Number(month.slice(5, 7)) - 1]} ${month.slice(0, 4)}`
const todayIst = (): string => new Date(Date.now() + 330 * 60_000).toISOString().slice(0, 10)
const strategyCode = (id: LabStrategy): [string, string] => {
  const [code, ...rest] = STRATEGY_SHORT[id].split(' ')
  return [code, rest.join(' ')]
}

type Stats = { total: number; days: number; winning: number; best: LabDay | null; worst: LabDay | null }

function statsOf(rows: LabDay[]): Stats {
  const settled = rows.filter((row) => row.net_rs != null)
  const byNet = [...settled].sort((a, b) => (a.net_rs as number) - (b.net_rs as number))
  return {
    total: settled.reduce((sum, row) => sum + (row.net_rs as number), 0),
    days: settled.length,
    winning: settled.filter((row) => (row.net_rs as number) > 0).length,
    best: byNet.at(-1) ?? null,
    worst: byNet[0] ?? null,
  }
}

/** A share (0..1) as a small ring with the percentage inside. */
function ShareRing({ share, label }: { share: number | null; label: string }) {
  const radius = 15
  const length = 2 * Math.PI * radius
  const filled = share == null ? 0 : Math.max(0, Math.min(1, share))
  return (
    <div className="cv-ring" aria-label={label}>
      <svg viewBox="0 0 36 36" aria-hidden="true" style={{ '--len': length } as CSSProperties}>
        <circle className="track" cx="18" cy="18" r={radius} />
        <circle className="fill" cx="18" cy="18" r={radius} strokeDasharray={length} strokeDashoffset={length * (1 - filled)} />
      </svg>
      <b>{share == null ? '—' : `${Math.round(filled * 100)}%`}</b>
    </div>
  )
}

/** The strategy's running total, month by month, as a small curve in its colour (its latest point glows). */
function RunningTotal({ rows }: { rows: LabMonth[] }) {
  if (rows.length < 2) return null
  const sorted = [...rows].sort((a, b) => a.month.localeCompare(b.month))
  let sum = 0
  const points = [0, ...sorted.map((row) => (sum += row.net_rs))]
  const low = Math.min(...points)
  const high = Math.max(...points)
  const span = high - low || 1
  const width = 100
  const height = 30
  const yOf = (value: number) => height - 3 - ((value - low) / span) * (height - 6)
  const xy = points.map((value, i) => [(i / (points.length - 1)) * width, yOf(value)])
  const line = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`).join(' ')
  const [endX, endY] = xy[xy.length - 1]
  return (
    <div className="cv-curve" aria-label={`Running total over ${sorted.length} months: ${signedRs(sum)}`}>
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <linearGradient id="cv-curve-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" />
            <stop offset="100%" />
          </linearGradient>
        </defs>
        <path className="area" d={`${line} L${width} ${height} L0 ${height} Z`} />
        {low < 0 && <path className="zero" d={`M0 ${yOf(0).toFixed(2)} H${width}`} />}
        <path className="line" d={line} />
      </svg>
      <i style={{ left: `${endX}%`, top: `${(endY / height) * 100}%` }} />
    </div>
  )
}

/** The lab's strategies as picks: colour and code, plus the name of the picked one (every name on hover). */
function StrategyChips({ value, onChange }: { value: LabStrategy; onChange: (id: LabStrategy) => void }) {
  return (
    <div className="cv-strategies" role="radiogroup" aria-label="Strategy">
      {LAB_STRATEGIES.map((id) => {
        const [code, name] = strategyCode(id)
        return (
          <button
            key={id}
            type="button"
            role="radio"
            aria-checked={id === value}
            className={`cv-strategy${id === value ? ' active' : ''}`}
            style={{ '--chip': STRATEGY_COLOR[id] } as CSSProperties}
            onClick={() => onChange(id)}
            title={STRATEGY_LABEL[id]}
          >
            <i />
            <b>{code}</b>
            <span>{name}</span>
          </button>
        )
      })}
    </div>
  )
}

function DayCell({ date, index, backtest, paper, source, biggest, flag, skip, today, selected, onSelect }: {
  date: string
  index: number
  skip: string | undefined
  backtest: LabDay | undefined
  paper: LabDay | undefined
  source: Source
  biggest: number
  flag: 'best' | 'worst' | null
  today: string
  selected: boolean
  onSelect: () => void
}) {
  const primary = source === 'paper' ? paper : backtest
  const showPaper = source === 'both' ? paper : undefined
  const traded = Boolean(primary || showPaper)
  const value = primary?.net_rs
  const share = value != null && biggest > 0 ? Math.min(1, Math.abs(value) / biggest) : 0
  const state = traded ? (value == null ? 'status' : value > 0 ? 'up' : value < 0 ? 'down' : 'flat') : skip ? 'skipped' : date > today ? 'upcoming' : 'none'
  const label = [
    primary ? `${primary.source} ${primary.net_rs == null ? STATUS_LABEL[primary.status] : formatRs(primary.net_rs)}` : null,
    showPaper ? `paper ${showPaper.net_rs == null ? STATUS_LABEL[showPaper.status] : formatRs(showPaper.net_rs)}` : null,
  ]
    .filter(Boolean)
    .join(', ')
  return (
    <button
      type="button"
      disabled={!traded}
      className={`cv-day ${state}${selected ? ' selected' : ''}${flag ? ` ${flag}` : ''}${date === today ? ' today' : ''}${!traded && skip && PENDING_SKIPS.has(skip) ? ' pending' : ''}`}
      style={{ '--heat': heatOf(share), '--i': index } as CSSProperties}
      onClick={onSelect}
      aria-label={`${longDay(date)}${label ? `: ${label}` : skip ? `: ${SKIP_LABEL[skip] ?? skip}` : state === 'upcoming' ? ': upcoming' : ': not traded'}`}
      aria-pressed={selected}
    >
      <span className="cv-day-in">
      <span className="cv-day-top">
        <span className="cv-date">{Number(date.slice(8))}</span>
        {flag && (
          <i className="cv-flag">
            <Icon name={flag === 'best' ? 'trophy' : 'trendDown'} />
            <span>{flag}</span>
          </i>
        )}
        {date === today && !flag && (
          <i className="cv-flag today">
            <span>today</span>
          </i>
        )}
      </span>
      {primary && (
        <b className={`cv-value ${pnlClass(value)}`}>
          {value == null ? (CELL_STATUS[primary.status] ?? primary.status) : shortRs(value)}
          {source === 'paper' && value != null && dayHasEstimatedFill(primary) && <em className="lab-estimated">est.</em>}
        </b>
      )}
      {!traded && skip && (
        <span className="cv-skip">
          <Icon name={SKIP_ICON[skip] ?? 'pause'} />
          {SKIP_LABEL[skip] ?? skip.replace(/_/g, ' ')}
        </span>
      )}
      {showPaper && (
        <span className={`cv-paper ${pnlClass(showPaper.net_rs)}`}>
          <PaperBadge title={null} />
          <span>{showPaper.net_rs == null ? (CELL_STATUS[showPaper.status] ?? showPaper.status) : shortRs(showPaper.net_rs)}</span>
        </span>
      )}
      </span>
      {value != null && value !== 0 && <i className="cv-mag" style={{ width: `${Math.max(6, share * 100)}%` }} />}
    </button>
  )
}

/** The month's days as a calendar - weekdays only unless the month traded on a weekend - with each week's total. */
function MonthGrid({ month, source, get, skipOf, slide, biggest, best, worst, selected, onSelect }: {
  month: string
  skipOf: (date: string) => string | undefined
  slide: '' | 'next' | 'prev'
  source: Source
  get: (kind: Kind, date: string) => LabDay | undefined
  biggest: number
  best: string | null
  worst: string | null
  selected: string | null
  onSelect: (date: string) => void
}) {
  const today = todayIst()
  const weeks = monthGrid(month)
  const primaryKind: Kind = source === 'paper' ? 'paper' : 'backtest'
  const tradedOn = (date: string) => Boolean(get(primaryKind, date) || (source === 'both' && get('paper', date)))
  const weekend = weeks.some((week) => week.slice(5).some((date) => date && tradedOn(date)))
  const columns = weekend ? [0, 1, 2, 3, 4, 5, 6] : [0, 1, 2, 3, 4]
  let cell = 0
  return (
    <div className={`cv-grid${slide ? ` slide-${slide}` : ''}`} role="grid" aria-label={monthLabel(month)} style={{ '--cols': columns.length, '--weeks': weeks.length } as CSSProperties}>
      {columns.map((position) => (
        <div key={WEEKDAYS[position]} className={`cv-weekday${position > 4 ? ' weekend' : ''}`}>
          {WEEKDAYS[position]}
        </div>
      ))}
      <div className="cv-weekday week">
        <Icon name="sigma" /> Week
      </div>
      {weeks.map((week, row) => {
        const rows = week.map((date) => (date ? get(primaryKind, date) : undefined)).filter((day): day is LabDay => !!day && day.net_rs != null)
        const total = rows.reduce((sum, day) => sum + (day.net_rs ?? 0), 0)
        const won = rows.filter((day) => (day.net_rs ?? 0) > 0).length
        return [
          ...columns.map((position) => {
            const date = week[position]
            if (!date) return <div key={`pad-${row}-${position}`} className="cv-pad" />
            return (
              <DayCell
                key={date}
                date={date}
                index={cell++}
                backtest={get('backtest', date)}
                paper={get('paper', date)}
                source={source}
                biggest={biggest}
                flag={date === best ? 'best' : date === worst ? 'worst' : null}
                skip={skipOf(date)}
                today={today}
                selected={date === selected}
                onSelect={() => onSelect(date)}
              />
            )
          }),
          <div key={`week-${row}`} className={`cv-week${rows.length ? '' : ' empty'}`} style={{ '--i': cell++ } as CSSProperties}>
            {rows.length ? (
              <>
                <b className={pnlClass(total)}>{shortRs(total)}</b>
                <small>
                  {won}/{rows.length} won
                </small>
              </>
            ) : (
              <small>—</small>
            )}
          </div>,
        ]
      })}
    </div>
  )
}

type MonthHover = { key: string; left: number; top: number }

/** Every month at once: a row per year, a column per calendar month (quarters a little apart), the year's total, and
 * a hover card. Each month shows its net, shaded and barred by its size;
 * hovering one opens its card, a click opens the month. */
function YearGrid({ rows, paper, kind, month, pickable, daily, onPick }: {
  rows: LabMonth[]
  paper: LabMonth[] | null
  kind: Kind
  month: string
  pickable: string[]
  daily: Map<string, LabDay[]>
  onPick: (month: string) => void
}) {
  const byMonth = new Map(rows.map((row) => [row.month, row]))
  const paperBy = new Map((paper ?? []).map((row) => [row.month, row]))
  const years = [...new Set([...rows, ...(paper ?? [])].map((row) => row.month.slice(0, 4)))].sort()
  const biggest = Math.max(1, ...rows.map((row) => Math.abs(row.net_rs)))
  const nowMonth = todayIst().slice(0, 7)
  const [hover, setHover] = useState<MonthHover | null>(null)
  const frame = useRef<HTMLDivElement>(null)
  // a month just pressed: its card stays shut until the pointer leaves it (no flash back while the click opens it)
  const pressed = useRef<string | null>(null)
  const show = (key: string, target: HTMLElement) => {
    const box = frame.current
    if (!box) return
    const width = 300
    const left = Math.min(Math.max(0, target.offsetLeft + target.offsetWidth / 2 - width / 2), box.clientWidth - width)
    setHover({ key, left, top: target.offsetTop + target.offsetHeight + 6 })
  }
  const shownHover = hover && hover.key !== month ? hover : null
  const hoverYear = shownHover?.key.slice(0, 4)
  const hoverIndex = shownHover ? Number(shownHover.key.slice(5, 7)) - 1 : -1
  let cell = 0
  return (
    <div
      ref={frame}
      className={`cv-years${shownHover ? ' hovering' : ''}`}
      role="grid"
      aria-label="Net per month, every year"
      style={{ '--years': years.length } as CSSProperties}
      onMouseLeave={() => setHover(null)}
    >
      <span className="cv-y-head" />
      {MONTH_NAMES.map((name, index) => (
        <span key={name} className={`cv-y-head${index % 3 === 0 && index ? ' q' : ''}${index === hoverIndex ? ' on' : ''}`}>
          {name}
        </span>
      ))}
      <span className="cv-y-head total">Year</span>
      {years.map((year) => {
        const yearRows = rows.filter((row) => row.month.startsWith(year))
        const total = yearRows.reduce((sum, row) => sum + row.net_rs, 0)
        const up = yearRows.filter((row) => row.net_rs > 0).length
        return [
          <span key={`${year}-label`} className={`cv-y-label${year === hoverYear ? ' on' : ''}`}>
            {year}
          </span>,
          ...MONTH_NAMES.map((_, index) => {
            const key = `${year}-${String(index + 1).padStart(2, '0')}`
            const row = byMonth.get(key)
            const paperRow = paperBy.get(key)
            const q = index % 3 === 0 && index ? ' q' : ''
            if (!row && !paperRow) return <span key={key} className={`cv-y-cell none${q}${key > nowMonth ? ' later' : ''}`} />
            const share = row ? Math.min(1, Math.abs(row.net_rs) / biggest) : 0
            const cross = shownHover && (year === hoverYear || index === hoverIndex) ? ' cross' : ''
            return (
              <button
                key={key}
                type="button"
                disabled={!pickable.includes(key)}
                className={`cv-y-cell ${row ? (row.net_rs >= 0 ? 'up' : 'down') : 'only-paper'}${q}${cross}${shownHover?.key === key ? ' hovered' : ''}${key === month ? ' active' : ''}${key === nowMonth ? ' current' : ''}`}
                style={{ '--heat': heatOf(share), '--i': cell++ } as CSSProperties}
                // the card shuts the moment the month is pressed - before the (heavier) click opens it below
                onPointerDown={() => {
                  pressed.current = key
                  setHover(null)
                }}
                onClick={() => {
                  setHover(null)
                  onPick(key)
                }}
                // the open month's details are already on screen below: no card for it
                onMouseEnter={(event) => (key === month || pressed.current === key ? setHover(null) : show(key, event.currentTarget))}
                onMouseLeave={() => {
                  if (pressed.current === key) pressed.current = null
                }}
                // a keyboard focus opens the card too; a mouse press focusing the button does not
                onFocus={(event) => (key === month || !event.currentTarget.matches(':focus-visible') ? undefined : show(key, event.currentTarget))}
                onBlur={() => setHover(null)}
                aria-label={`${monthLabel(key)}: ${row ? signedRs(row.net_rs) : 'no backtest'}${paperRow ? `, paper ${signedRs(paperRow.net_rs)}` : ''}`}
              >
                {row && <b className={pnlClass(row.net_rs)}>{shortRs(row.net_rs)}</b>}
                {paperRow && (
                  <small className={pnlClass(paperRow.net_rs)}>
                    <i>P</i>
                    {shortRs(paperRow.net_rs)}
                  </small>
                )}
                {row && <em className="cv-y-mag" style={{ width: `${Math.max(8, share * 100)}%` }} />}
              </button>
            )
          }),
          <span key={`${year}-total`} className={`cv-y-total${year === hoverYear ? ' on' : ''}`}>
            <b className={pnlClass(total)}>{yearRows.length ? shortRs(total) : '—'}</b>
            <small>
              {up} of {yearRows.length} up
            </small>
          </span>,
        ]
      })}
      {/* never for the open month - also when it became the open one while hovered (a click, or ← / →) */}
      {shownHover && (
        <MonthCard
          key={shownHover.key}
          month={shownHover.key}
          row={byMonth.get(shownHover.key) ?? null}
          paperRow={paperBy.get(shownHover.key) ?? null}
          kind={kind}
          days={daily.get(shownHover.key) ?? []}
          style={{ left: shownHover.left, top: shownHover.top }}
        />
      )}
    </div>
  )
}

/** A month in full, on hover: its net, how many days it won, its average day, best and worst day, the largest fall
 * inside it, and the paper trades' month beside it. */
function MonthCard({ month, row, paperRow, kind, days, style }: {
  month: string
  row: LabMonth | null
  paperRow: LabMonth | null
  kind: Kind
  days: LabDay[]
  style: CSSProperties
}) {
  const settled = days.filter((day) => day.net_rs != null)
  const best = settled.reduce<LabDay | null>((top, day) => (!top || (day.net_rs ?? 0) > (top.net_rs ?? 0) ? day : top), null)
  const worst = settled.reduce<LabDay | null>((low, day) => (!low || (day.net_rs ?? 0) < (low.net_rs ?? 0) ? day : low), null)
  const tone = row ? pnlClass(row.net_rs) : paperRow ? pnlClass(paperRow.net_rs) : ''
  return (
    <div className={`cv-month-card ${tone}`} style={style} role="tooltip">
      <header>
        <span>
          <Icon name="calendar" /> {monthLabel(month)} <em>{kind}</em>
        </span>
        <b className={pnlClass(row?.net_rs ?? paperRow?.net_rs)}>{signedRs(row?.net_rs ?? paperRow?.net_rs)}</b>
      </header>
      {row && (
        <dl>
          <div>
            <dt>Won</dt>
            <dd>
              {row.winning_days}/{row.days} <small>{row.days ? `${Math.round((row.winning_days / row.days) * 100)}%` : ''}</small>
            </dd>
          </div>
          <div>
            <dt>Avg / day</dt>
            <dd className={pnlClass(row.net_rs)}>{row.days ? signedRs(row.net_rs / row.days) : '—'}</dd>
          </div>
          <div>
            <dt>Best day</dt>
            <dd className={pnlClass(best?.net_rs)}>
              {best ? signedRs(best.net_rs) : '—'} <small>{best ? dayNoYear(best.trading_date) : ''}</small>
            </dd>
          </div>
          <div>
            <dt>Worst day</dt>
            <dd className={pnlClass(worst?.net_rs)}>
              {worst ? signedRs(worst.net_rs) : '—'} <small>{worst ? dayNoYear(worst.trading_date) : ''}</small>
            </dd>
          </div>
          <div>
            <dt>Max drawdown</dt>
            <dd className={row.max_drawdown_rs ? 'negative' : ''}>{row.max_drawdown_rs ? signedRs(-row.max_drawdown_rs) : '₹0'}</dd>
          </div>
        </dl>
      )}
      {paperRow && (
        <div className="cv-month-card-paper">
          <PaperBadge /> <b className={pnlClass(paperRow.net_rs)}>{signedRs(paperRow.net_rs)}</b>
          <small>
            {paperRow.winning_days}/{paperRow.days} days won
          </small>
        </div>
      )}
      <footer>Click to open the month</footer>
    </div>
  )
}

/** One result for the chosen day: its net, its facts, its legs, and the candle charts to verify it. */
function LegsDetail({ day, theme, chartable }: { day: LabDay; theme: ThemeName; chartable: boolean }) {
  const paper = day.source === 'paper'
  const [chartOpen, setChartOpen] = useState(false)
  return (
    <div className={`cv-result${paper ? ' paper' : ''}`}>
      <div className="cv-result-head">
        {paper ? <PaperBadge /> : <span className="cv-source">BACKTEST</span>}
        {day.derived_from && <span className="lab-derived">{derivedTag(day)}</span>}
        {paper && day.status !== 'closed' && <span className={`lab-status ${day.status}`}>{STATUS_LABEL[day.status] ?? day.status}</span>}
        <span className="cv-result-net">
          <small>Net</small>
          <b className={pnlClass(day.net_rs)}>{signedRs(day.net_rs)}</b>
        </span>
      </div>
      <div className="cv-facts">
        <span>
          <em>Strikes</em> {strikesLabel(day)}
        </span>
        <span>
          <em>Size</em> {tradeSizeLabel(day)}
        </span>
        <span>
          <em>Expiry</em> {day.expiry ?? '—'}
        </span>
        {day.net_pts != null && (
          <span title="Gross points minus charges = net points per unit">
            <em>Points</em> {formatPts(day.gross_pts)} − {formatPts(day.charges_pts)} = <b className={pnlClass(day.net_pts)}>{formatPts(day.net_pts)}</b>
          </span>
        )}
      </div>
      {day.legs?.length ? (
        <table className="td-legs compact cv-legs">
          <thead>
            <tr>
              <th>Leg</th>
              <th>Side</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>Points</th>
            </tr>
          </thead>
          <tbody>
            {day.legs.map((leg) => {
              const points = legPoints(leg)
              return (
                <tr key={`${leg.type}${leg.strike}${leg.side}${leg.role}`}>
                  <th>
                    <span className={leg.type === 'CE' ? 'td-ce' : 'td-pe'}>{leg.type}</span> {leg.strike} {leg.role === 'wing' && <span className="dim">wing</span>}
                  </th>
                  <td className={leg.side === 'SELL' ? 'negative' : 'positive'}>{leg.side}</td>
                  <td>
                    {priceOf(leg.entry)?.toFixed(2) ?? '—'}
                    {isEstimated(leg.entry) && <em className="lab-estimated">est.</em>}
                  </td>
                  <td>
                    {priceOf(leg.exit)?.toFixed(2) ?? '—'}
                    {isEstimated(leg.exit) && <em className="lab-estimated">est.</em>}
                  </td>
                  <td className={pnlClass(points)}>{formatPts(points)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      ) : (
        <small className="lab-note">{day.notes ?? 'No legs were traded.'}</small>
      )}
      {day.notes && day.legs?.length ? <small className="lab-note">{day.notes}</small> : null}
      {chartable && day.legs?.length ? (
        <>
          <div>
            <button type="button" className="research-chip" onClick={() => setChartOpen((open) => !open)} aria-expanded={chartOpen}>
              <Icon name="chart" />
              {chartOpen ? 'Hide minute charts' : 'Verify on the minute charts'}
            </button>
          </div>
          {chartOpen && <LabDayChart date={day.trading_date} strategy={day.strategy} source={day.source} theme={theme} trade={day} />}
        </>
      ) : null}
    </div>
  )
}

/** The chosen day in a dialog (so the screen itself never grows): every shown result, then every strategy that day. */
function DayDialog({ date, strategy, rows, others, theme, chartable, onPickStrategy, onClose }: {
  date: string
  strategy: LabStrategy
  rows: LabDay[]
  others: { id: LabStrategy; kind: Kind; row: LabDay }[]
  theme: ThemeName
  chartable: boolean
  onPickStrategy: (id: LabStrategy) => void
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  const [code, name] = strategyCode(strategy)
  return (
    <div className="td-dialog-backdrop" onClick={onClose}>
      <div
        className="td-dialog cv-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={`${code} on ${longDay(date)}`}
        style={{ '--accent': STRATEGY_COLOR[strategy] } as CSSProperties}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="cv-detail-head">
          <div>
            <span>
              <i className="cv-dot" /> {code} {name}
            </span>
            <b>{longDay(date)}</b>
          </div>
          <button type="button" className="research-chip" onClick={onClose}>
            <Icon name="close" />
            Close
          </button>
        </header>
        <div className="cv-results">
          {rows.map((row) => (
            <LegsDetail key={`${row.source}${row.strategy}${row.trading_date}`} day={row} theme={theme} chartable={chartable} />
          ))}
        </div>
        <div className="cv-others">
          <span>Same day, every strategy</span>
          {others.map(({ id, kind, row }) => (
            <button
              key={`${id}${kind}`}
              type="button"
              className={`cv-other${id === strategy ? ' current' : ''}`}
              style={{ '--chip': STRATEGY_COLOR[id] } as CSSProperties}
              onClick={() => onPickStrategy(id)}
              title={`${STRATEGY_LABEL[id]} (${kind}) - show this strategy`}
            >
              <i />
              <b>{strategyCode(id)[0]}</b>
              {kind === 'paper' ? <PaperBadge /> : null}
              <em className={pnlClass(row.net_rs)}>{row.net_rs == null ? (STATUS_LABEL[row.status] ?? row.status) : signedRs(row.net_rs)}</em>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

/** One strategy's P&L on one screen: every month of its history on top (a year-by-month grid with the all-time
 * summary - click a month to open it), and below it the month that is open: its numbers, then its calendar (each
 * trading day's net ₹ and each week's total) filling the rest of the window. A day opens its legs in a dialog; the
 * arrow keys step through the months. */
export function LabCalendar({ size, theme }: { size: LabSize; theme: ThemeName }) {
  const [strategy, setStrategy] = useState<LabStrategy>('straddle_sell_overnight_skip_friday')
  const [source, setSource] = useState<Source>('both')
  const [chosenMonth, setChosenMonth] = useState<string | null>(null)
  const [slide, setSlide] = useState<'' | 'next' | 'prev'>('')
  const [selected, setSelected] = useState<string | null>(null)
  const fit = useFitToWindow<HTMLElement>(220)
  const backtest = useLabData(() => fetchLabBacktest(size), [sizeKey(size)])
  const paper = useLabData(() => fetchLabPaper(size), [sizeKey(size)])
  const monthlyBacktest = useLabData(() => fetchLabMonthly('backtest', size, strategy), [strategy, sizeKey(size)])
  const monthlyPaper = useLabData(() => fetchLabMonthly('paper', size, strategy), [strategy, sizeKey(size)])

  const all = useMemo(() => [...(backtest.data?.rows ?? []), ...(paper.data?.rows ?? [])], [backtest.data, paper.data])
  const byKey = useMemo(() => new Map(all.map((row) => [`${row.source}|${row.strategy}|${row.trading_date}`, row])), [all])
  const shownMonths = useMemo(
    () => [...new Set(all.filter((row) => row.strategy === strategy && (source === 'both' || row.source === source)).map((row) => row.trading_date.slice(0, 7)))].sort(),
    [all, strategy, source],
  )

  const month = chosenMonth && shownMonths.includes(chosenMonth) ? chosenMonth : (shownMonths.at(-1) ?? null)
  const index = month ? shownMonths.indexOf(month) : -1
  const get = (kind: Kind, date: string) => byKey.get(`${kind}|${strategy}|${date}`)

  const monthRows = (kind: Kind) => (month ? all.filter((row) => row.source === kind && row.strategy === strategy && row.trading_date.startsWith(month)) : [])
  const primaryKind: Kind = source === 'paper' ? 'paper' : 'backtest'
  const primary = statsOf(monthRows(primaryKind))
  const paperStats = source === 'both' ? statsOf(monthRows('paper')) : null
  const biggest = Math.max(0, ...monthRows(primaryKind).map((row) => Math.abs(row.net_rs ?? 0)))
  const monthlyPrimary = (source === 'paper' ? monthlyPaper.data?.rows : monthlyBacktest.data?.rows) ?? []
  const thisMonth = monthlyPrimary.find((row) => row.month === month)
  const avgMonth = monthlyPrimary.length ? monthlyPrimary.reduce((sum, row) => sum + row.net_rs, 0) / monthlyPrimary.length : null
  const allTotal = monthlyPrimary.reduce((sum, row) => sum + row.net_rs, 0)
  const upMonths = monthlyPrimary.filter((row) => row.net_rs > 0).length
  const bestMonth = monthlyPrimary.reduce<LabMonth | null>((best, row) => (!best || row.net_rs > best.net_rs ? row : best), null)
  const worstMonth = monthlyPrimary.reduce<LabMonth | null>((worst, row) => (!worst || row.net_rs < worst.net_rs ? row : worst), null)

  const error = backtest.error ?? paper.error ?? monthlyBacktest.error ?? monthlyPaper.error
  const loading = !error && (!backtest.data || !paper.data)
  const monthsLoading = !error && (!monthlyBacktest.data || !monthlyPaper.data)
  const detailDate = selected && selected.startsWith(month ?? '#') ? selected : null
  const detailRows = detailDate ? all.filter((row) => row.trading_date === detailDate && row.strategy === strategy && (source === 'both' || row.source === source)) : []
  const others = detailDate
    ? LAB_STRATEGIES.flatMap((id) =>
        (['backtest', 'paper'] as const).flatMap((kind) => {
          const row = byKey.get(`${kind}|${id}|${detailDate}`)
          return row ? [{ id, kind, row }] : []
        }),
      )
    : []
  const [code, name] = strategyCode(strategy)
  const latest = shownMonths.at(-1) ?? null
  const pickDay = (date: string | null | undefined) => date && setSelected(date === detailDate ? null : date)
  // every way of changing the month comes through here, so the calendar slides in from the side it came from
  const goTo = (next: string | null | undefined) => {
    if (!next || next === month) return
    setSlide(month && next < month ? 'prev' : 'next')
    setChosenMonth(next)
  }
  // the shown source's days of this strategy, month by month in date order (the band's sparklines and month cards)
  const dailyByMonth = useMemo(() => {
    const out = new Map<string, LabDay[]>()
    for (const row of all) {
      if (row.strategy !== strategy || row.source !== primaryKind || row.net_rs == null) continue
      const key = row.trading_date.slice(0, 7)
      const list = out.get(key)
      if (list) list.push(row)
      else out.set(key, [row])
    }
    for (const list of out.values()) list.sort((a, b) => a.trading_date.localeCompare(b.trading_date))
    return out
  }, [all, strategy, primaryKind])
  const skips = useMemo(
    () => new Map((backtest.data?.derived_skips?.[strategy]?.[source === 'paper' ? 'paper' : 'backtest'] ?? []).map((skip) => [skip.trading_date, skip.reason])),
    [backtest.data, strategy, source],
  )

  // ← / → step through the months (not while a dialog is open or a field has the keyboard)
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (detailDate || event.altKey || event.ctrlKey || event.metaKey) return
      if ((event.target as HTMLElement | null)?.closest?.('input, select, textarea')) return
      if (event.key === 'ArrowLeft' && index > 0) goTo(shownMonths[index - 1])
      else if (event.key === 'ArrowRight' && index >= 0 && index < shownMonths.length - 1) goTo(shownMonths[index + 1])
      else return
      event.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailDate, index, shownMonths, month])

  return (
    <div className="cv-root" style={{ '--accent': STRATEGY_COLOR[strategy] } as CSSProperties}>
      <div className="cv-controls">
        <StrategyChips value={strategy} onChange={setStrategy} />
        <div className="segmented cv-source" role="group" aria-label="Source">
          {(['backtest', 'paper', 'both'] as const).map((id) => (
            <button key={id} type="button" className={source === id ? 'active' : ''} onClick={() => setSource(id)}>
              <Icon name={id === 'backtest' ? 'history' : id === 'paper' ? 'paper' : 'layers'} />
              {id === 'backtest' ? 'Backtest' : id === 'paper' ? 'Paper' : 'Both'}
            </button>
          ))}
        </div>
      </div>

      {error && <small className="export-error">{error}</small>}
      {loading && <div className="research-card research-empty">Loading…</div>}
      {!loading && !error && !month && (
        <div className="research-card research-empty cv-empty">
          {source === 'paper' ? (
            <>
              <span>
                No paper trades for {code} {name}
                {all.some((row) => row.source === 'paper') ? ' - only some strategies are paper-traded.' : ' yet. The first is taken at 09:30 on the next market day.'}
              </span>
              <button type="button" className="research-chip" onClick={() => setSource('both')}>
                Show its backtest
              </button>
            </>
          ) : (
            'No rows.'
          )}
        </div>
      )}

      {!loading && !error && month && (
        <>
          <section className="cv-band" aria-label="Every month">
            <header className="cv-band-head">
              <span className="cv-band-title">
                <Icon name="grid" /> Every month
                <em>
                  <i className="cv-dot" /> {code} {name}
                  {source === 'both' ? ' · backtest' : source === 'paper' ? ' · paper' : ''}
                </em>
                {source !== 'backtest' && <PaperBadge />}
              </span>
              {!monthsLoading && <RunningTotal key={`${strategy}|${source}`} rows={monthlyPrimary} />}
              <div className="cv-band-facts">
                <span>
                  <Icon name="coins" className="hue-accent" />
                  {monthlyPrimary.length} months <b className={pnlClass(allTotal)}>{signedRs(allTotal)}</b>
                </span>
                <span>
                  <Icon name="target" className="hue-cyan" />
                  avg month <b className={pnlClass(avgMonth)}>{avgMonth != null ? signedRs(avgMonth) : '—'}</b>
                </span>
                <span>
                  <Icon name="up" className="tri positive" />
                  <b className="positive">{upMonths}</b> up <Icon name="down" className="tri negative" />
                  <b className="negative">{monthlyPrimary.length - upMonths}</b> down
                </span>
                {bestMonth && (
                  <button type="button" className="roomy" disabled={!shownMonths.includes(bestMonth.month)} onClick={() => goTo(bestMonth.month)} aria-label={`Best month: ${monthLabel(bestMonth.month)} - open it`}>
                    <Icon name="trophy" className="hue-gold" />
                    {shortMonth(bestMonth.month)} <b className={pnlClass(bestMonth.net_rs)}>{signedRs(bestMonth.net_rs)}</b>
                  </button>
                )}
                {worstMonth && (
                  <button type="button" className="roomy" disabled={!shownMonths.includes(worstMonth.month)} onClick={() => goTo(worstMonth.month)} aria-label={`Worst month: ${monthLabel(worstMonth.month)} - open it`}>
                    <Icon name="trendDown" className="hue-rose" />
                    {shortMonth(worstMonth.month)} <b className={pnlClass(worstMonth.net_rs)}>{signedRs(worstMonth.net_rs)}</b>
                  </button>
                )}
                {source === 'both' && (
                  <span>
                    <i className="cv-p">P</i> paper
                  </span>
                )}
              </div>
            </header>
            {monthsLoading ? (
              <div className="research-empty">Loading months…</div>
            ) : (
              <YearGrid
                key={`${strategy}|${source}`}
                rows={monthlyPrimary}
                paper={source === 'both' ? (monthlyPaper.data?.rows ?? []) : null}
                kind={primaryKind}
                month={month}
                pickable={shownMonths}
                daily={dailyByMonth}
                onPick={goTo}
              />
            )}
          </section>

          <section ref={fit.ref} className={`cv-month${fit.height ? ' fit' : ''}`} style={fit.height ? { height: fit.height } : undefined} aria-label={monthLabel(month)}>
            <header className="cv-month-head">
              <div className="cv-hero-nav">
                <button type="button" className="cv-nav" disabled={index <= 0} onClick={() => goTo(shownMonths[index - 1])} aria-label="Previous month" title="Previous month (←)">
                  <Icon name="chevronRight" className="flip" />
                </button>
                <div className="cv-hero-title">
                  <span>
                    <Icon name="calendar" /> {sizeLabel(size)} · net ₹ per day
                  </span>
                  <label className="cv-month-pick">
                    <b key={month}>{monthLabel(month)}</b>
                    <Icon name="chevronDown" />
                    <select value={month} onChange={(event) => goTo(event.target.value)} aria-label="Month">
                      {[...shownMonths].reverse().map((value) => (
                        <option key={value} value={value}>
                          {monthLabel(value)}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <button type="button" className="cv-nav" disabled={index >= shownMonths.length - 1} onClick={() => goTo(shownMonths[index + 1])} aria-label="Next month" title="Next month (→)">
                  <Icon name="chevronRight" />
                </button>
                {latest && month !== latest && (
                  <button type="button" className="research-chip cv-latest" onClick={() => goTo(latest)} title={`Back to ${monthLabel(latest)}`}>
                    <Icon name="skipEnd" />
                    Latest
                  </button>
                )}
              </div>
              <div className="cv-stats">
                <div className="cv-stat main">
                  <span>
                    <Icon name="coins" className="hue-accent" /> Month net{source === 'both' ? ' · backtest' : ''}
                  </span>
                  <TweenRs value={primary.total} />
                  <small>{avgMonth != null ? `avg month ${signedRs(avgMonth)}` : '—'}</small>
                </div>
                <div className="cv-stat ring">
                  <ShareRing share={primary.days ? primary.winning / primary.days : null} label="Share of winning days" />
                  <div className="cv-stat-text">
                    <span>
                      <Icon name="check" className="hue-violet" /> Won
                    </span>
                    <b>
                      {primary.winning}/{primary.days}
                    </b>
                    <small>days</small>
                  </div>
                </div>
                <div className="cv-stat roomy">
                  <span>
                    <Icon name="target" className="hue-cyan" /> Avg / day
                  </span>
                  <TweenRs value={primary.days ? primary.total / primary.days : null} />
                  <small>{primary.days} trading days</small>
                </div>
                <button type="button" className="cv-stat pick" disabled={!primary.best} onClick={() => pickDay(primary.best?.trading_date)} aria-label="Open the month's best day">
                  <span>
                    <Icon name="trophy" className="hue-gold" /> Best day
                  </span>
                  <TweenRs value={primary.best?.net_rs} />
                  <small>{primary.best ? dayNoYear(primary.best.trading_date) : '—'}</small>
                </button>
                <button type="button" className="cv-stat pick" disabled={!primary.worst} onClick={() => pickDay(primary.worst?.trading_date)} aria-label="Open the month's worst day">
                  <span>
                    <Icon name="trendDown" className="hue-rose" /> Worst day
                  </span>
                  <TweenRs value={primary.worst?.net_rs} />
                  <small>{primary.worst ? dayNoYear(primary.worst.trading_date) : '—'}</small>
                </button>
                <div className="cv-stat">
                  <span>
                    <Icon name="drawdown" className="hue-rose" /> Max drawdown
                  </span>
                  <TweenRs value={thisMonth ? -thisMonth.max_drawdown_rs : null} />
                  <small>inside the month</small>
                </div>
                {paperStats && (
                  <div className="cv-stat paper">
                    <span>
                      <PaperBadge title={null} /> paper
                    </span>
                    <TweenRs value={paperStats.days ? paperStats.total : null} />
                    <small>{paperStats.days ? `${paperStats.winning}/${paperStats.days} won` : 'no paper days'}</small>
                  </div>
                )}
              </div>
            </header>
            <MonthGrid
              key={`${strategy}|${source}|${month}`}
              month={month}
              source={source}
              get={get}
              skipOf={(date) => skips.get(date)}
              slide={slide}
              biggest={biggest}
              // the tags only mark a real win / a real loss: a month of all-green days has no "worst" cell
              best={(primary.best?.net_rs ?? 0) > 0 ? primary.best!.trading_date : null}
              worst={(primary.worst?.net_rs ?? 0) < 0 ? primary.worst!.trading_date : null}
              selected={detailDate}
              onSelect={(date) => pickDay(date)}
            />
          </section>

          {detailDate && (
            <DayDialog
              date={detailDate}
              strategy={strategy}
              rows={detailRows}
              others={others}
              theme={theme}
              chartable
              onPickStrategy={setStrategy}
              onClose={() => setSelected(null)}
            />
          )}
        </>
      )}
    </div>
  )
}
