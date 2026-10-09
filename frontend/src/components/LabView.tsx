import { useMemo, useState, type ReactNode } from 'react'
import { labExportUrl, type LabSize } from '../api'
import type { ThemeName } from '../charts/theme'
import { LabCalendar } from './LabCalendar'
import { LabToday } from './LabToday'
import { Icon, type IconName } from './Icons'
import { sizeKey, sizeLabel } from '../lab'
import { PaperBadge } from './PaperBadge'

type Tab = 'today' | 'calendar'

const TABS: { id: Tab; label: string; icon: IconName }[] = [
  { id: 'today', label: 'Today', icon: 'today' },
  { id: 'calendar', label: 'Calendar', icon: 'calendar' },
]

// Same limit as the backend (strategy_lab.MAX_LOTS), which enforces it.
const MAX_LOTS = 10_000
const SIZE_STORAGE_KEY = 'zero-lab-size'
// the paper/backtest schedule in one line; Today draws the same schedule as its session timeline
const RULES = 'NIFTY · nearest expiry · entry 09:30 · same-day exit 15:29 (paper from 15:29:50) · S5 exits the next trading day at 09:30'

const validLots = (value: unknown): value is number => typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= MAX_LOTS

/** The stored size (an older saved quantity is dropped: the lab sizes in lots only). */
function readStoredLots(): number {
  try {
    const stored = JSON.parse(localStorage.getItem(SIZE_STORAGE_KEY) ?? 'null') as { lots?: unknown } | null
    if (stored && validLots(stored.lots)) return stored.lots
  } catch {
    /* storage unavailable or unreadable: one lot */
  }
  return 1
}

/** Trade size for every rupee figure in the lab, in lots - each day at its own contract lot size. A stepper: − and +,
 * or type the number (the arrow keys step it too). */
function SizeControl({ lots, onChange }: { lots: number; onChange: (lots: number) => void }) {
  const [draft, setDraft] = useState<string | null>(null)
  const text = draft ?? String(lots)
  const valid = text.trim() !== '' && validLots(Number(text))
  const shown = valid ? Number(text) : lots
  const step = (by: number) => {
    setDraft(null)
    onChange(Math.min(MAX_LOTS, Math.max(1, lots + by)))
  }

  return (
    <div
      className={`lab-size${valid ? '' : ' invalid'}`}
      title="Every ₹ figure in the lab is shown at this many lots. Each day uses that day's own contract lot size (it has changed over time). Paper fills are per-unit prices, so paper results scale the same way."
    >
      <span className="lab-size-label">Size</span>
      <button type="button" className="lab-step" onClick={() => step(-1)} disabled={lots <= 1} aria-label="One lot fewer">
        <Icon name="minus" />
      </button>
      <label className="lab-size-field">
        <input
          type="number"
          min={1}
          max={MAX_LOTS}
          step={1}
          inputMode="numeric"
          aria-label="Number of lots"
          aria-invalid={!valid}
          value={text}
          style={{ width: `${Math.max(1, text.length) + 0.4}ch` }}
          onChange={(event) => {
            const next = event.target.value
            setDraft(next)
            const parsed = Number(next)
            if (next.trim() !== '' && validLots(parsed)) onChange(parsed)
          }}
          onBlur={() => setDraft(null)}
        />
        <span>{shown === 1 ? 'lot' : 'lots'}</span>
      </label>
      <button type="button" className="lab-step" onClick={() => step(1)} disabled={lots >= MAX_LOTS} aria-label="One lot more">
        <Icon name="plus" />
      </button>
      {!valid && <small className="lab-size-error">a whole number of lots, 1 to {MAX_LOTS.toLocaleString('en-IN')}</small>}
    </div>
  )
}

/** Strategy lab: candle backtests of the straddle and iron-fly strategies, and their live PAPER trades. */
export function LabView({ theme, renderHeader }: { theme: ThemeName; renderHeader: (nav: ReactNode) => ReactNode }) {
  const [tab, setTab] = useState<Tab>('today')
  const [lots, setLots] = useState<number>(readStoredLots)
  const size: LabSize = useMemo(() => ({ mode: 'lots', value: lots }), [lots])

  const changeLots = (next: number) => {
    setLots(next)
    try {
      localStorage.setItem(SIZE_STORAGE_KEY, JSON.stringify({ lots: next }))
    } catch {
      /* the choice still applies for this session */
    }
  }

  const nav = (
    <>
      <div className="segmented lab-tabs" role="tablist" aria-label="Strategy lab">
        {TABS.map(({ id, label, icon }) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>
            <Icon name={icon} />
            {label}
          </button>
        ))}
      </div>
      <div className="lab-tools">
        <SizeControl lots={lots} onChange={changeLots} />
        <a
          key={sizeKey(size)}
          className="export-button lab-download"
          href={labExportUrl(size)}
          download
          title={`Download one CSV with every day's profit and loss for every strategy: the candle backtest and the PAPER trades, at ${sizeLabel(size)}. Points are per unit; rupees are points x qty. Includes legs, charges and the running total.`}
        >
          <Icon name="download" />
          P&amp;L CSV
        </a>
        <span className="lab-paper-note" title={`${RULES}. PAPER trades are simulated - no real order is ever placed.`}>
          <PaperBadge />
          <Icon name="shield" className="lab-paper-shield" />
          <span className="lab-paper-text">no real orders</span>
        </span>
      </div>
    </>
  )

  return (
    <>
      {renderHeader(nav)}
      <section className="research-view lab-view">
        {tab === 'today' && <LabToday theme={theme} size={size} />}
        {tab === 'calendar' && <LabCalendar size={size} theme={theme} />}
      </section>
    </>
  )
}
