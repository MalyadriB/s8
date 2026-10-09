import { useEffect, useLayoutEffect, useRef, useState } from 'react'

/** Fills the window below the element's own top edge, so a screen never scrolls on a desktop-sized window: the height
 * to give it (at least `min`), or null on a small window, where the page flows normally instead. Measured after every
 * render (the content above it can change height, and the element itself may only appear once data has loaded) and
 * on every resize; an unchanged height does not re-render. */
export function useFitToWindow<T extends HTMLElement = HTMLDivElement>(min = 480) {
  const ref = useRef<T>(null)
  const [height, setHeight] = useState<number | null>(null)
  const fit = useRef(() => {})
  fit.current = () => {
    const element = ref.current
    if (!element) return
    const roomy = window.innerWidth >= 1000 && window.innerHeight >= 620
    setHeight(roomy ? Math.max(min, Math.floor(window.innerHeight - element.getBoundingClientRect().top - window.scrollY - 14)) : null)
  }
  useLayoutEffect(() => fit.current())
  useEffect(() => {
    const onResize = () => fit.current()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  return { ref, height }
}
