import { useCallback, useEffect, useState, type ReactNode } from 'react'
import './App.css'
import { API, fetchHealth, type TokenStatus } from './api'
import type { ThemeName } from './charts/theme'
import { Header, SKINS, type Skin } from './components/Header'
import { LabView } from './components/LabView'
import { SkyScene } from './components/SkyScene'

const THEME_STORAGE_KEY = 'zero-theme'
// the component theme (one choice; each theme has a dark and a light version) - index.html applies it before paint too
const SKIN_STORAGE_KEY = 'zero-skin'

function readStoredSkin(): Skin {
  try {
    const stored = localStorage.getItem(SKIN_STORAGE_KEY)
    return SKINS.some((item) => item.id === stored) ? (stored as Skin) : 'glass'
  } catch {
    return 'glass'
  }
}

function readStoredTheme(): ThemeName {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

function App() {
  const [theme, setTheme] = useState<ThemeName>(readStoredTheme)
  const [skin, setSkin] = useState<Skin>(readStoredSkin)
  const [tokenStatus, setTokenStatus] = useState<TokenStatus | null>(null)
  const [authNotice, setAuthNotice] = useState<{ type: 'success' | 'error'; message?: string } | null>(null)

  // Drives the [data-theme] CSS overrides and, through the theme prop, the canvas-drawn charts. Persisted across reloads.
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme)
    } catch {
      /* storage unavailable (private browsing): the theme still applies for this session */
    }
  }, [theme])

  // The background is always the sky ([data-bg] 'sky'); the component theme is [data-skin] CSS overrides. Persisted.
  useEffect(() => {
    document.documentElement.setAttribute('data-bg', 'sky')
    document.documentElement.setAttribute('data-skin', skin)
    try {
      localStorage.setItem(SKIN_STORAGE_KEY, skin)
    } catch {
      /* the theme still applies for this session */
    }
  }, [skin])

  const refreshHealth = useCallback(() => {
    return fetchHealth()
      .then((health) => setTokenStatus(health.token))
      .catch(() => setTokenStatus(null))
  }, [])

  useEffect(() => {
    void refreshHealth()
    const timer = window.setInterval(() => void refreshHealth(), 30_000)
    return () => window.clearInterval(timer)
  }, [refreshHealth])

  // The Upstox login redirects back here with ?auth=success|error.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const auth = params.get('auth')
    if (!auth) return
    setAuthNotice({ type: auth === 'success' ? 'success' : 'error', message: params.get('message') ?? undefined })
    window.history.replaceState({}, '', window.location.pathname)
    void refreshHealth()
  }, [refreshHealth])

  // One bar for the whole app: the lab hands in its tabs and tools, the header adds the brand, theme and connection.
  const renderHeader = (nav: ReactNode) => (
    <>
      <Header
        theme={theme}
        onThemeChange={setTheme}
        skin={skin}
        onSkinChange={setSkin}
        tokenConfigured={tokenStatus?.configured ?? false}
        tokenExpired={tokenStatus?.expired ?? null}
        onUpstoxLogin={() => {
          window.location.href = `${API}/api/upstox/login`
        }}
      >
        {nav}
      </Header>

      {authNotice && (
        <div className={`auth-notice ${authNotice.type}`}>
          {authNotice.type === 'success' ? '✓ Upstox connected successfully.' : `Upstox login failed${authNotice.message ? `: ${authNotice.message}` : '.'}`}
          <button onClick={() => setAuthNotice(null)}>✕</button>
        </div>
      )}
    </>
  )

  return (
    <main className="app-shell">
      <SkyScene theme={theme} />
      <LabView theme={theme} renderHeader={renderHeader} />
    </main>
  )
}

export default App
