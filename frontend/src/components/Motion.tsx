import { useLayoutEffect, useRef } from 'react'
import { pnlClass, signedRs } from '../lab'

/** True when the viewer asked the system for less motion - every animation in the lab checks this or a CSS media query. */
export const reducedMotion = () => typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

/** A number shown as text that glides to each new `value` over `ms` (ease-out), counting up from 0 the first time
 * (unless `fromZero` is off). Put the returned ref on the element that renders `format(value)` as its only child: React
 * only ever renders that final text, and while the number moves the in-between values are written straight into the
 * element's text node, at most ~30 times a second. (Setting React state on every animation frame instead - the old
 * way - cost a render, a style recalculation and a layout ~60 times a second for each moving number on the live
 * screen, and with NIFTY ticking every second and P&L every two, something was nearly always moving.) Display only:
 * every comparison and colour still uses the real value. */
export function useGlide<T extends HTMLElement = HTMLElement>(
  value: number | null | undefined,
  format: (value: number | null | undefined) => string,
  { ms = 650, fromZero = true }: { ms?: number; fromZero?: boolean } = {},
) {
  const ref = useRef<T>(null)
  const at = useRef<number | null | undefined>(fromZero && value != null ? 0 : value) // where the shown number is now
  const gliding = useRef(false)
  const formatRef = useRef(format)
  formatRef.current = format
  const write = useRef((shown: number | null | undefined) => {
    const text = ref.current?.firstChild
    if (text && text.nodeType === Node.TEXT_NODE) text.nodeValue = formatRef.current(shown)
  }).current
  // after every commit, before paint: an element re-created mid-glide (a flash re-keys it) carries on from the glide,
  // not from the final text React gave it
  useLayoutEffect(() => {
    if (gliding.current) write(at.current)
  })
  useLayoutEffect(() => {
    const origin = at.current
    if (value == null || origin == null || origin === value || reducedMotion()) {
      at.current = value
      return
    }
    gliding.current = true
    write(origin)
    const start = performance.now()
    let frame = 0
    let wrote = 0
    const step = (now: number) => {
      const progress = Math.min(1, (now - start) / ms)
      if (progress >= 1) {
        at.current = value
        gliding.current = false
        write(value) // exactly the text React rendered
        return
      }
      at.current = origin + (value - origin) * (1 - (1 - progress) ** 3)
      if (now - wrote >= 32) {
        wrote = now
        write(at.current)
      }
      frame = requestAnimationFrame(step)
    }
    frame = requestAnimationFrame(step)
    return () => {
      cancelAnimationFrame(frame)
      gliding.current = false
    }
  }, [value, ms, write])
  return ref
}

/** Signed rupees that count up into place. */
export function TweenRs({ value, className = '' }: { value: number | null | undefined; className?: string }) {
  const ref = useGlide<HTMLElement>(value, signedRs)
  return (
    <b ref={ref} className={`${className} ${pnlClass(value)}`}>
      {signedRs(value)}
    </b>
  )
}
