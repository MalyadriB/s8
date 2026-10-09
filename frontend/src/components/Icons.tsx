import type { ReactNode } from 'react'

/** The app's icon set: small stroke icons drawn inline (currentColor, 24-unit grid), sized by the surrounding text
 * (1.1em) unless `size` is given. Decorative by default - the text next to an icon carries the meaning. */
const PATHS = {
  today: <path d="M22 12h-4l-3 9L9 3l-3 9H2" />,
  calendar: (
    <>
      <rect x="3" y="4.5" width="18" height="16.5" rx="2.5" />
      <path d="M16 2.5v4M8 2.5v4M3 10h18" />
    </>
  ),
  download: <path d="M12 3v12m0 0-4.5-4.5M12 15l4.5-4.5M4 20h16" />,
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4" />
    </>
  ),
  moon: <path d="M20.5 13.2A8.5 8.5 0 1 1 10.8 3.5a6.6 6.6 0 0 0 9.7 9.7Z" />,
  shield: (
    <>
      <path d="M12 21.5s7.5-3.6 7.5-9.6V5.3L12 2.5 4.5 5.3v6.6c0 6 7.5 9.6 7.5 9.6Z" />
      <path d="m9 12 2.2 2.2L15.5 10" />
    </>
  ),
  trendUp: <path d="M3 17l6-6 4 4 8-8M15 7h6v6" />,
  trendDown: <path d="M3 7l6 6 4-4 8 8M15 17h6v-6" />,
  candles: (
    <>
      <path d="M7 3v4M7 17v4M17 4v4M17 16v4" />
      <rect x="4.5" y="7" width="5" height="10" rx="1.2" />
      <rect x="14.5" y="8" width="5" height="8" rx="1.2" />
    </>
  ),
  timer: (
    <>
      <path d="M10 2.5h4M12 14l3.2-3.2" />
      <circle cx="12" cy="14" r="7.5" />
    </>
  ),
  bell: (
    <>
      <path d="M6 8.5a6 6 0 0 1 12 0c0 6.5 2.5 8.5 2.5 8.5h-17S6 15 6 8.5Z" />
      <path d="M10.2 20.5a2 2 0 0 0 3.6 0" />
    </>
  ),
  layers: <path d="m12 3 9 4.5-9 4.5-9-4.5L12 3Zm-9 9 9 4.5 9-4.5M3 16.5 12 21l9-4.5" />,
  chevronDown: <path d="m6 9 6 6 6-6" />,
  chevronRight: <path d="m9 6 6 6-6 6" />,
  list: <path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01" />,
  chart: <path d="M3 3v18h18M7 15l4-4.5 3 3L19.5 7" />,
  exit: <path d="M9.5 20.5H5.5a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2h4M16 16.5l4.5-4.5L16 7.5M20.5 12H9" />,
  enter: <path d="M14.5 3.5h4a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2h-4M10 16.5l4.5-4.5L10 7.5M14.5 12H3.5" />,
  check: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="m8.3 12.3 2.6 2.6 4.8-5" />
    </>
  ),
  target: (
    <>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="5" />
      <circle cx="12" cy="12" r="1" />
    </>
  ),
  history: <path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1L3.5 8.5M3.5 3.5v5h5M12 7.5V12l3 2" />,
  trophy: <path d="M8 21h8M12 16.5V21M7 3.5h10V9a5 5 0 0 1-10 0V3.5ZM17 5h3v1.5A3.5 3.5 0 0 1 16.8 10M7 5H4v1.5A3.5 3.5 0 0 0 7.2 10" />,
  flag: <path d="M5 21.5V3.5M5 4h11.5l-2.2 4.2L16.5 12.5H5" />,
  coins: (
    <>
      <ellipse cx="12" cy="6" rx="7" ry="3" />
      <path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3.2 2" />
    </>
  ),
  range: <path d="m7 15 5 5 5-5M7 9l5-5 5 5" />,
  close: <path d="M18 6 6 18M6 6l12 12" />,
  minus: <path d="M5.5 12h13" />,
  plus: <path d="M12 5.5v13M5.5 12h13" />,
  retry: <path d="M20.5 12a8.5 8.5 0 1 1-2.5-6M20.5 3.5V9H15" />,
  alert: <path d="M12 3.5 2.5 20.5h19L12 3.5ZM12 10v4.5M12 17.6h.01" />,
  grid: (
    <>
      <rect x="3.5" y="3.5" width="7" height="7" rx="1.6" />
      <rect x="13.5" y="3.5" width="7" height="7" rx="1.6" />
      <rect x="3.5" y="13.5" width="7" height="7" rx="1.6" />
      <rect x="13.5" y="13.5" width="7" height="7" rx="1.6" />
    </>
  ),
  pause: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M10 9.5v5M14 9.5v5" />
    </>
  ),
  hourglass: <path d="M6.5 3.5h11M6.5 20.5h11M8 3.5v2.8a4.4 4.4 0 0 0 2.3 3.8L12 11l1.7-.9A4.4 4.4 0 0 0 16 6.3V3.5M8 20.5v-2.8a4.4 4.4 0 0 1 2.3-3.8L12 13l1.7.9a4.4 4.4 0 0 1 2.3 3.8v2.8" />,
  drawdown: (
    <>
      <path d="M3 6.5h18" strokeDasharray="1.5 3" />
      <path d="M3 6.5c3.5 0 4.5 11 9 11s5.5-7 9-7" />
    </>
  ),
  sigma: <path d="M17.5 5h-11l6 7-6 7h11" />,
  skipEnd: <path d="m6 6 6 6-6 6M17.5 5.5v13" />,
  paper: (
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" />
      <path d="M14 3v5h5M9 13h6M9 17h4" />
    </>
  ),
  up: <path d="M12 6.5 19 17.5H5Z" fill="currentColor" stroke="none" />,
  down: <path d="M12 17.5 19 6.5H5Z" fill="currentColor" stroke="none" />,
} satisfies Record<string, ReactNode>

export type IconName = keyof typeof PATHS

export function Icon({ name, size, className, title }: { name: IconName; size?: number; className?: string; title?: string }) {
  return (
    <svg
      className={`icon icon-${name}${className ? ` ${className}` : ''}`}
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.9}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : undefined}
    >
      {title && <title>{title}</title>}
      {PATHS[name]}
    </svg>
  )
}
