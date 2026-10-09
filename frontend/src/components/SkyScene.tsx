import { useEffect, useMemo, useRef, type CSSProperties } from 'react'
import type { ThemeName } from '../charts/theme'

/** The Sky backdrop's living scene, fixed behind the app (the body's gradient is the sky itself; this is what's in it).
    Night: drifting nebulae, the Milky Way with its dust lane, stars twinkling in groups, a few bright sparkling stars, a
    slowly turning spiral galaxy, a satellite and a shooting star now and then. Day: the sun's slowly turning rays and drifting clouds.
    Only transform and opacity animate, and only on HTML boxes - an animated <svg> is repainted every frame, a <div>
    holding one is moved by the compositor - so the sky costs no repaints; prefers-reduced-motion stills all of it. */

// the star chart is drawn on a 1920x1080 sky, scaled to cover the window
const W = 1920
const H = 1080

/** A small seeded generator, so the sky is the same on every load. */
function seeded(seed: number) {
  let a = seed
  return () => {
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

type Star = { x: number; y: number; r: number; o: number; c: string }
// mostly white, some blue-white, a few warm and rose - real stars' colours, kept pale
const TINTS = ['#ffffff', '#ffffff', '#ffffff', '#e2eaff', '#cddcff', '#fff0d9', '#ffe1e8']

function scatter(seed: number, count: number, r: [number, number], o: [number, number]): Star[] {
  const rand = seeded(seed)
  return Array.from({ length: count }, () => ({
    x: rand() * W,
    y: rand() * H,
    r: r[0] + rand() ** 2 * (r[1] - r[0]),
    o: o[0] + rand() * (o[1] - o[0]),
    c: TINTS[Math.floor(rand() * TINTS.length)],
  }))
}

/** The Milky Way's own stars: dense along the band's centre line, thinning out to either side. */
function band(seed: number, count: number): Star[] {
  const rand = seeded(seed)
  return Array.from({ length: count }, () => {
    const spread = Math.sqrt(-2 * Math.log(1 - rand())) * Math.cos(2 * Math.PI * rand())
    return { x: -200 + rand() * (W + 400), y: H / 2 + spread * 92, r: 0.55 + rand() ** 3 * 1.1, o: 0.25 + rand() * 0.6, c: TINTS[Math.floor(rand() * TINTS.length)] }
  })
}

function Stars({ stars }: { stars: Star[] }) {
  return stars.map((star, index) => <circle key={index} cx={star.x.toFixed(1)} cy={star.y.toFixed(1)} r={star.r.toFixed(2)} fill={star.c} opacity={star.o.toFixed(2)} />)
}

/** Two logarithmic arms round a bright core. */
function spiralArm(offset: number) {
  const points: string[] = []
  for (let theta = 0.4; theta < 10.6; theta += 0.12) {
    const radius = 6 * Math.exp(0.28 * theta)
    points.push(`${(radius * Math.cos(theta + offset)).toFixed(1)} ${(radius * Math.sin(theta + offset)).toFixed(1)}`)
  }
  return `M ${points.join(' L ')}`
}

const ARMS = [spiralArm(0), spiralArm(Math.PI)]

// stars strewn along the arms, thicker toward the core
const ARM_STARS = (() => {
  const rand = seeded(31)
  return Array.from({ length: 150 }, () => {
    const theta = 0.6 + rand() ** 0.7 * 9.6
    const offset = rand() < 0.5 ? 0 : Math.PI
    const radius = 6 * Math.exp(0.28 * theta) + (rand() - 0.5) * 12
    return { x: radius * Math.cos(theta + offset), y: radius * Math.sin(theta + offset), r: 0.5 + rand() * 1.1, o: 0.35 + rand() * 0.6 }
  })
})()

function SpiralGalaxy() {
  return (
    <div className="sky-galaxy" aria-hidden="true">
      <div className="sky-galaxy-disc">
      <svg viewBox="-130 -130 260 260">
        <defs>
          <radialGradient id="sky-gx-halo">
            <stop offset="0" stopColor="#e9e2ff" stopOpacity="0.5" />
            <stop offset="0.35" stopColor="#a99cff" stopOpacity="0.2" />
            <stop offset="1" stopColor="#7c6cff" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="sky-gx-core">
            <stop offset="0" stopColor="#fffaf0" stopOpacity="1" />
            <stop offset="0.4" stopColor="#ffe2c4" stopOpacity="0.55" />
            <stop offset="1" stopColor="#ffd2b0" stopOpacity="0" />
          </radialGradient>
          <filter id="sky-gx-soft" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="7" />
          </filter>
        </defs>
        <circle r="125" fill="url(#sky-gx-halo)" />
        {ARMS.map((arm, index) => (
          <g key={index} fill="none" strokeLinecap="round">
            <path d={arm} stroke="#b4b8ff" strokeOpacity="0.34" strokeWidth="18" filter="url(#sky-gx-soft)" />
            <path d={arm} stroke="#e8e4ff" strokeOpacity="0.22" strokeWidth="5" filter="url(#sky-gx-soft)" />
          </g>
        ))}
        {ARM_STARS.map((star, index) => <circle key={index} cx={star.x.toFixed(1)} cy={star.y.toFixed(1)} r={star.r.toFixed(2)} fill="#f2f0ff" opacity={star.o.toFixed(2)} />)}
        <circle r="34" fill="url(#sky-gx-core)" />
      </svg>
      </div>
    </div>
  )
}

/** Meteors, one at a time: a single streak waits a few seconds after the last one has gone, then crosses from a new
    random spot. They all fall the same way - from the upper left toward the lower right, within a narrow band of angles,
    like a shower from one radiant - so the sky never shows two meteors crossing each other's paths. */
function ShootingStar() {
  const streak = useRef<HTMLElement>(null)
  useEffect(() => {
    const element = streak.current
    if (!element || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    let timer = 0
    let animation: Animation | null = null
    const fire = () => {
      const angle = 24 + Math.random() * 14
      element.style.left = `${2 + Math.random() * 60}vw`
      element.style.top = `${Math.random() * 55}vh`
      element.style.width = `${110 + Math.random() * 150}px`
      const travel = 280 + Math.random() * 360
      animation = element.animate(
        [
          { transform: `rotate(${angle}deg) translateX(0) scaleX(0.15)`, opacity: 0 },
          { transform: `rotate(${angle}deg) translateX(${travel * 0.18}px) scaleX(0.8)`, opacity: 1, offset: 0.18 },
          { transform: `rotate(${angle}deg) translateX(${travel}px) scaleX(1)`, opacity: 0 },
        ],
        { duration: 800 + Math.random() * 600, easing: 'cubic-bezier(0.3, 0.1, 0.55, 1)' },
      )
      animation.onfinish = () => {
        timer = window.setTimeout(fire, 3500 + Math.random() * 7000)
      }
    }
    timer = window.setTimeout(fire, 2000 + Math.random() * 2000)
    return () => {
      window.clearTimeout(timer)
      animation?.cancel()
    }
  }, [])
  return <i ref={streak} className="sky-shoot" aria-hidden="true" />
}

/** Bright stars that sparkle: a soft glow and a four-point glint that swell and fade, each on its own beat. */
const SPARKS = (() => {
  const rand = seeded(77)
  return Array.from({ length: 11 }, () => ({ x: 3 + rand() * 94, y: 2 + rand() * 94, d: 2.6 + rand() * 4, delay: -rand() * 6, s: 0.7 + rand() * 0.6 }))
})()

// stars that twinkle together in a group fade on different beats from the other groups
const TWINKLE_GROUPS = [
  { seed: 11, count: 140, duration: 5.2, delay: 0, low: 0.2 },
  { seed: 12, count: 120, duration: 7.6, delay: -2.4, low: 0.3 },
  { seed: 13, count: 90, duration: 3.8, delay: -1.1, low: 0.15 },
]

function NightSky() {
  const sky = useMemo(
    () => ({
      faint: scatter(10, 520, [0.55, 1.15], [0.25, 0.65]),
      milkyWay: band(20, 1100),
      groups: TWINKLE_GROUPS.map((group) => ({ ...group, stars: scatter(group.seed, group.count, [0.8, 2], [0.6, 1]) })),
    }),
    [],
  )
  return (
    <>
      <i className="sky-nebula n1" />
      <i className="sky-nebula n2" />
      <i className="sky-nebula n3" />
      {/* the fixed sky: the Milky Way (a soft band, brighter knots, a dark dust lane through it, dense stars) and the faint stars */}
      <svg className="sky-layer" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid slice">
        <defs>
          <radialGradient id="sky-mw-glow">
            <stop offset="0" stopColor="#c4ccff" stopOpacity="0.2" />
            <stop offset="0.5" stopColor="#a3aef2" stopOpacity="0.09" />
            <stop offset="1" stopColor="#8090e0" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="sky-mw-knot">
            <stop offset="0" stopColor="#fff2e6" stopOpacity="0.22" />
            <stop offset="0.6" stopColor="#ffe0cc" stopOpacity="0.07" />
            <stop offset="1" stopColor="#ffd9c0" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="sky-mw-rose">
            <stop offset="0" stopColor="#f4b8ff" stopOpacity="0.14" />
            <stop offset="1" stopColor="#f4b8ff" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="sky-mw-dust">
            <stop offset="0" stopColor="#060918" stopOpacity="0.32" />
            <stop offset="1" stopColor="#060918" stopOpacity="0" />
          </radialGradient>
          <filter id="sky-mw-soft" x="-10%" y="-60%" width="120%" height="220%">
            <feGaussianBlur stdDeviation="10" />
          </filter>
        </defs>
        <g transform={`rotate(-21 ${W / 2} ${H / 2})`}>
          <ellipse cx={W / 2} cy={H / 2} rx="1400" ry="300" fill="url(#sky-mw-glow)" />
          <ellipse cx={W / 2 - 100} cy={H / 2 + 10} rx="1000" ry="170" fill="url(#sky-mw-glow)" />
          <ellipse cx={W / 2 - 330} cy={H / 2 - 30} rx="340" ry="130" fill="url(#sky-mw-knot)" />
          <ellipse cx={W / 2 + 120} cy={H / 2 + 20} rx="300" ry="120" fill="url(#sky-mw-knot)" />
          <ellipse cx={W / 2 + 520} cy={H / 2 - 10} rx="260" ry="100" fill="url(#sky-mw-knot)" />
          <ellipse cx={W / 2 - 640} cy={H / 2 + 30} rx="300" ry="120" fill="url(#sky-mw-rose)" />
          <ellipse cx={W / 2 + 300} cy={H / 2 - 40} rx="260" ry="110" fill="url(#sky-mw-rose)" />
          <g filter="url(#sky-mw-soft)">
            <ellipse cx={W / 2 - 360} cy={H / 2 - 8} rx="380" ry="26" fill="url(#sky-mw-dust)" />
            <ellipse cx={W / 2 + 80} cy={H / 2 + 14} rx="330" ry="22" fill="url(#sky-mw-dust)" />
            <ellipse cx={W / 2 + 470} cy={H / 2 + 2} rx="240" ry="18" fill="url(#sky-mw-dust)" />
          </g>
          <Stars stars={sky.milkyWay} />
        </g>
        <Stars stars={sky.faint} />
      </svg>
      {sky.groups.map((group) => (
        <div key={group.seed} className="sky-layer sky-twinkle" style={{ '--d': `${group.duration}s`, '--delay': `${group.delay}s`, '--low': group.low } as CSSProperties}>
          <svg className="sky-layer" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid slice">
            <Stars stars={group.stars} />
          </svg>
        </div>
      ))}
      <SpiralGalaxy />
      <i className="sky-galaxy-far" />
      {SPARKS.map((spark, index) => (
        <i
          key={index}
          className="sky-spark"
          style={{ left: `${spark.x}%`, top: `${spark.y}%`, '--d': `${spark.d}s`, '--delay': `${spark.delay}s`, '--s': spark.s } as CSSProperties}
        />
      ))}
      <i className="sky-satellite" />
      <ShootingStar />
    </>
  )
}

const CLOUDS = [
  { top: 6, width: 34, duration: 210, delay: -40, opacity: 0.95 },
  { top: 22, width: 22, duration: 160, delay: -120, opacity: 0.8 },
  { top: 44, width: 40, duration: 260, delay: -170, opacity: 0.85 },
  { top: 64, width: 26, duration: 190, delay: -20, opacity: 0.75 },
  { top: 80, width: 36, duration: 240, delay: -95, opacity: 0.9 },
]

function DaySky() {
  return (
    <>
      <i className="sky-sun" />
      {CLOUDS.map((cloud, index) => (
        <i
          key={index}
          className="sky-cloud"
          style={{ top: `${cloud.top}vh`, width: `${cloud.width}vw`, opacity: cloud.opacity, '--d': `${cloud.duration}s`, '--delay': `${cloud.delay}s` } as CSSProperties}
        />
      ))}
    </>
  )
}

export function SkyScene({ theme }: { theme: ThemeName }) {
  return (
    <div className={`sky-scene ${theme}`} aria-hidden="true">
      {theme === 'dark' ? <NightSky /> : <DaySky />}
    </div>
  )
}
