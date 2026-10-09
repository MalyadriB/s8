import { useEffect, useState } from 'react'

/** Loads once (and again when `deps` change, dropping the previous result so it is never shown under new settings); `error` is the failure text. */
export function useLabData<T>(load: () => Promise<T>, deps: unknown[]): { data: T | null; error: string | null } {
  const [state, setState] = useState<{ data: T | null; error: string | null }>({ data: null, error: null })
  useEffect(() => {
    let cancelled = false
    setState({ data: null, error: null })
    load()
      .then((data) => !cancelled && setState({ data, error: null }))
      .catch((caught) => !cancelled && setState({ data: null, error: caught instanceof Error ? caught.message : 'Backend not reachable' }))
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return state
}
