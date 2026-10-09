import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { ThemeName } from '../charts/theme'
import { Icon } from './Icons'

/** The component themes: the sky behind the app stays; these restyle everything on it - the header, the cards, their
    tiles, the chips, the dropdowns and dialogs - each in a dark and a light version. Text and the P&L colours never change. */
export const SKINS = [
  { id: 'glass', label: 'Glass', note: 'navy glass, soft white edges' },
  { id: 'nebula', label: 'Nebula', note: 'violet-tinted glass' },
  { id: 'ocean', label: 'Ocean', note: 'deep teal glass, cyan edges' },
  { id: 'ember', label: 'Ember', note: 'warm amber glass' },
  { id: 'midnight', label: 'Midnight', note: 'solid cards, calm and crisp' },
  { id: 'frost', label: 'Frost', note: 'clear glass, bright edges' },
] as const
export type Skin = (typeof SKINS)[number]['id']

/** A swatch button that opens the component themes. */
function SkinPicker({ value, onChange }: { value: Skin; onChange: (value: Skin) => void }) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onDown = (event: PointerEvent) => {
      if (!box.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('pointerdown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('pointerdown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])
  const current = SKINS.find((item) => item.id === value) ?? SKINS[0]
  return (
    <div className="skin-picker" ref={box}>
      <button type="button" className={`skin-button${open ? ' open' : ''}`} onClick={() => setOpen((shown) => !shown)} aria-expanded={open} aria-label={`Theme: ${current.label}`}>
        <i className={`skin-swatch ${current.id}`} />
      </button>
      {open && (
        <div className="skin-menu" role="menu" aria-label="Theme">
          <span className="skin-menu-title">Theme</span>
          {SKINS.map((item) => (
            <button
              key={item.id}
              type="button"
              role="menuitemradio"
              aria-checked={item.id === value}
              className={`skin-option${item.id === value ? ' active' : ''}`}
              onClick={() => onChange(item.id)}
            >
              <i className={`skin-swatch ${item.id}`} />
              <span>
                <b>{item.label}</b>
                <small>{item.note}</small>
              </span>
              {item.id === value && <Icon name="check" />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

type Props = {
  theme: ThemeName
  onThemeChange: (theme: ThemeName) => void
  skin: Skin
  onSkinChange: (skin: Skin) => void
  tokenConfigured: boolean
  tokenExpired: boolean | null
  onUpstoxLogin: () => void
  /** The page's own navigation and tools, between the brand and the theme/connection controls. */
  children?: ReactNode
}

export function Header(props: Props) {
  return (
    <header className="app-header">
      <div className="header-row">
        <div className="brand-block">
          {/* the S8 mark: the letters alone, set in Space Grotesk */}
          <span className="brand-logo" aria-hidden="true">
            S8
          </span>
          <div className="brand-text">
            <span className="brand-name">NIFTY 50</span>
            <span className="brand-subtitle">Strategy Lab</span>
          </div>
        </div>

        {props.children && <div className="header-main">{props.children}</div>}

        <div className="header-right">
          <SkinPicker value={props.skin} onChange={props.onSkinChange} />
          <div className="segmented theme-switch" role="group" aria-label="Theme">
            <button type="button" className={props.theme === 'dark' ? 'active' : ''} onClick={() => props.onThemeChange('dark')} aria-label="Dark theme" title="Dark theme">
              <Icon name="moon" />
            </button>
            <button type="button" className={props.theme === 'light' ? 'active' : ''} onClick={() => props.onThemeChange('light')} aria-label="Light theme" title="Light theme">
              <Icon name="sun" />
            </button>
          </div>
          {props.tokenConfigured && !props.tokenExpired ? (
            <span className="upstox-connected">
              <i />
              Connected
            </span>
          ) : (
            <button className="login-button" onClick={props.onUpstoxLogin}>
              {props.tokenConfigured ? 'Reconnect Upstox' : 'Login to Upstox'}
            </button>
          )}
        </div>
      </div>
    </header>
  )
}
