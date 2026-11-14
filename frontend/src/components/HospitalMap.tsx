import { memo, useMemo } from 'react'
import { useElementSize } from '../hooks/useElementSize'
import { MAP_STATUS_LABEL, STATUS_COLOR, mapStatus, pointAt, type MapStatus } from '../lib/demo'
import type { DashboardState, HospitalInfo, TrackName } from '../types/dashboard'


interface Placed {
  h: HospitalInfo
  x: number
  y: number
  labelSide: 'left' | 'right'
  /** Outside the metro core: drawn at the panel edge in its true direction. */
  edge?: { km: number; bearing: string }
}

const EARTH_KM = 6371
const CORE_RADIUS_KM = 15

function distanceKm(a: { lat: number; lon: number }, b: { lat: number; lon: number }) {
  const rad = Math.PI / 180
  const dLat = (b.lat - a.lat) * rad
  const dLon = (b.lon - a.lon) * rad
  const h =
    Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2
  return 2 * EARTH_KM * Math.asin(Math.sqrt(h))
}

function compass(dx: number, dy: number): string {
  const names = ['E', 'NE', 'N', 'NW', 'W', 'SW', 'S', 'SE']
  const angle = (Math.atan2(-dy, dx) * 180) / Math.PI
  return names[Math.round(((angle + 360) % 360) / 45) % 8]
}

/**
 * Equirectangular projection of the API's real coordinates, fitted to the metro core so
 * the in-town hospitals are readable. A hospital far from the core (Kennestone, ~27 km)
 * is pinned at the panel edge along its true bearing and labelled with its distance.
 */
function project(hospitals: HospitalInfo[], width: number, height: number, pad: number) {
  const median = (xs: number[]) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)]
  const center = { lat: median(hospitals.map((h) => h.lat)), lon: median(hospitals.map((h) => h.lon)) }
  const core = hospitals.filter((h) => distanceKm(h, center) <= CORE_RADIUS_KM)
  const fitSet = core.length >= 2 ? core : hospitals
  const lat0 = (fitSet.reduce((a, h) => a + h.lat, 0) / fitSet.length) * (Math.PI / 180)
  const px = (h: HospitalInfo) => h.lon * Math.cos(lat0)
  const py = (h: HospitalInfo) => -h.lat
  const xs = fitSet.map(px)
  const ys = fitSet.map(py)
  const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)]
  const scale = Math.min((width - 2 * pad) / (maxX - minX || 1), (height - 2 * pad) / (maxY - minY || 1))
  const offX = (width - (maxX - minX) * scale) / 2
  const offY = (height - (maxY - minY) * scale) / 2
  const cx = width / 2
  const cy = height / 2
  const edgeMargin = 30

  const placed: Placed[] = hospitals.map((h) => {
    let x = offX + (px(h) - minX) * scale
    let y = offY + (py(h) - minY) * scale
    let edge: Placed['edge']
    if (!fitSet.includes(h)) {
      const dx = x - cx
      const dy = y - cy
      const t = Math.min(
        dx ? (Math.sign(dx) * (cx - edgeMargin)) / dx : Infinity,
        dy ? (Math.sign(dy) * (cy - edgeMargin)) / dy : Infinity,
      )
      edge = { km: Math.round(distanceKm(h, center)), bearing: compass(dx, dy) }
      x = cx + dx * Math.min(1, t)
      y = cy + dy * Math.min(1, t)
    }
    return { h, x, y, labelSide: 'right' as const, edge }
  })
  for (const p of placed) {
    const nearest = placed
      .filter((q) => q !== p)
      .sort((a, b) => Math.hypot(a.x - p.x, a.y - p.y) - Math.hypot(b.x - p.x, b.y - p.y))[0]
    p.labelSide = nearest && nearest.x > p.x + 4 ? 'left' : 'right'
    if (p.x > width - 120) p.labelSide = 'left'
    if (p.x < 120) p.labelSide = 'right'
  }
  const pxPerKm = scale / 111.32
  return { placed, pxPerKm }
}

interface Props {
  state: DashboardState
  playhead: number
  track: TrackName
  onTrackChange: (track: TrackName) => void
  selected: string
  onSelect: (id: string) => void
}

export const HospitalMap = memo(function HospitalMap({
  state,
  playhead,
  track,
  onTrackChange,
  selected,
  onSelect,
}: Props) {
  const [ref, size] = useElementSize<HTMLDivElement>()
  const { width, height } = size
  const layout = useMemo(
    () => (width > 0 && height > 0 ? project(state.hospitals, width, height, 60) : null),
    [state.hospitals, width, height],
  )
  const byId = new Map(layout?.placed.map((p) => [p.h.id, p]) ?? [])
  const flows = state.flows.filter((f) => f.tick === playhead && f.track === track)
  const scaleKm = layout && layout.pxPerKm * 5 > width * 0.45 ? 1 : layout && layout.pxPerKm * 2 > width * 0.35 ? 1 : 2

  return (
    <section className="panel flex min-h-0 flex-1 flex-col p-3" aria-label="Atlanta hospital network">
      <header className="mb-2 flex items-center justify-between gap-2">
        <div>
          <div className="eyebrow">Atlanta network</div>
          <div className="text-[12px] text-muted">5 EDs · synthetic data</div>
        </div>
        <div className="flex rounded-lg border border-line p-0.5 text-[12px]" role="group" aria-label="Map future">
          {(['triagex', 'baseline'] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => onTrackChange(t)}
              aria-pressed={track === t}
              className={`rounded-md px-2 py-1 font-medium transition ${
                track === t
                  ? t === 'triagex'
                    ? 'bg-triagex/15 text-triagex-strong'
                    : 'bg-baseline/15 text-baseline'
                  : 'text-muted hover:text-text'
              }`}
            >
              {t === 'triagex' ? 'With TriageX' : 'Without'}
            </button>
          ))}
        </div>
      </header>
      <div ref={ref} className="relative min-h-0 flex-1 overflow-hidden rounded-xl bg-ink/60">
        {layout && (
          <svg width={width} height={height} role="img" aria-label="Hospital status map">
            <defs>
              <radialGradient id="map-bg" cx="65%" cy="70%" r="70%">
                <stop offset="0%" stopColor="#12233f" stopOpacity="0.9" />
                <stop offset="100%" stopColor="#050a14" stopOpacity="0" />
              </radialGradient>
              <filter id="soft-glow" x="-50%" y="-50%" width="200%" height="200%">
                <feGaussianBlur stdDeviation="5" />
              </filter>
            </defs>
            <rect width={width} height={height} fill="url(#map-bg)" />
            {Array.from({ length: 9 }, (_, i) => (
              <line key={`v${i}`} x1={(i * width) / 8} x2={(i * width) / 8} y1={0} y2={height} stroke="#13203a" />
            ))}
            {Array.from({ length: 9 }, (_, i) => (
              <line key={`h${i}`} y1={(i * height) / 8} y2={(i * height) / 8} x1={0} x2={width} stroke="#13203a" />
            ))}

            {flows.map((f) => {
              const a = byId.get(f.from_hospital_id)
              const b = byId.get(f.to_hospital_id)
              if (!a || !b) return null
              const mx = (a.x + b.x) / 2 + (b.y - a.y) * 0.25
              const my = (a.y + b.y) / 2 - (b.x - a.x) * 0.25
              const color = f.kind === 'diversion' ? 'var(--color-st-overloaded)' : 'var(--color-triagex)'
              return (
                <path
                  key={`${f.from_hospital_id}-${f.to_hospital_id}-${f.kind}`}
                  d={`M${a.x},${a.y} Q${mx},${my} ${b.x},${b.y}`}
                  fill="none"
                  stroke={color}
                  strokeWidth={2 + Math.min(4, f.count)}
                  strokeLinecap="round"
                  className="flow-line"
                  opacity={0.85}
                >
                  <title>{`${f.count} ambulance${f.count > 1 ? 's' : ''} ${f.kind === 'diversion' ? 'diverted' : 'level-loaded'}`}</title>
                </path>
              )
            })}

            {layout.placed.map(({ h, x, y, labelSide, edge }) => {
              const status = mapStatus(state, h.id, track, playhead)
              const point = pointAt(state, h.id, track, playhead)
              const color = STATUS_COLOR[status]
              const hot = status === 'overloaded' || status === 'diverting'
              const r = 9 + Math.min(9, ((point?.saturation_pct ?? 0) / 100) * 6)
              const isSelected = selected === h.id
              const anchor = labelSide === 'right' ? 'start' : 'end'
              const lx = labelSide === 'right' ? x + r + 8 : x - r - 8
              return (
                <g
                  key={h.id}
                  role="button"
                  tabIndex={0}
                  aria-label={`${h.name}: ${MAP_STATUS_LABEL[status]}`}
                  data-testid={`hospital-${h.id}`}
                  data-status={status}
                  onClick={() => onSelect(h.id)}
                  onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onSelect(h.id)}
                  className="cursor-pointer outline-none"
                >
                  <circle cx={x} cy={y} r={r + 10} fill={color} opacity={0.22} filter="url(#soft-glow)" />
                  {hot && <circle cx={x} cy={y} r={r} fill="none" stroke={color} strokeWidth={2} className="pulse-ring" />}
                  <circle
                    cx={x}
                    cy={y}
                    r={r}
                    fill={color}
                    fillOpacity={0.9}
                    stroke={isSelected ? '#ffffff' : '#0b1322'}
                    strokeWidth={isSelected ? 2.5 : 2}
                    style={{ transition: 'r 300ms ease, fill 300ms ease' }}
                  />
                  {status === 'diverting' && (
                    <circle cx={x} cy={y} r={r + 5} fill="none" stroke={color} strokeWidth={1.5} strokeDasharray="3 3" />
                  )}
                  <text x={lx} y={y - 3} textAnchor={anchor} className="fill-text" fontSize={13} fontWeight={600}>
                    {h.short_name}
                    {edge && (
                      <tspan fill="#5b6d8e" fontWeight={400} fontSize={11}>
                        {` · ${edge.km} km ${edge.bearing}`}
                      </tspan>
                    )}
                  </text>
                  <text x={lx} y={y + 13} textAnchor={anchor} fontSize={12} className="tabular" fill={color}>
                    {point ? `${Math.round(point.saturation_pct)}% · ${MAP_STATUS_LABEL[status]}` : ''}
                  </text>
                </g>
              )
            })}

            <g transform={`translate(${14}, ${height - 18})`}>
              <line x1={0} x2={layout.pxPerKm * scaleKm} y1={0} y2={0} stroke="#5b6d8e" strokeWidth={2} />
              <text x={0} y={-6} fontSize={10} fill="#5b6d8e">
                {scaleKm} km
              </text>
            </g>
            <text x={width - 12} y={18} textAnchor="end" fontSize={11} fill="#5b6d8e">
              N ↑
            </text>
          </svg>
        )}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted" aria-label="Status legend">
        {(Object.keys(MAP_STATUS_LABEL) as MapStatus[]).map((s) => (
          <li key={s} className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full" style={{ background: STATUS_COLOR[s] }} />
            {MAP_STATUS_LABEL[s]}
          </li>
        ))}
      </ul>
    </section>
  )
})

export function HospitalDetail({
  state,
  playhead,
  hospitalId,
}: {
  state: DashboardState
  playhead: number
  hospitalId: string
}) {
  const h = state.hospitals.find((x) => x.id === hospitalId)
  const tri = pointAt(state, hospitalId, 'triagex', playhead)
  const base = pointAt(state, hospitalId, 'baseline', playhead)
  if (!h || !tri || !base) return null
  const status = mapStatus(state, hospitalId, 'triagex', playhead)
  const identical = tri.saturation_pct === base.saturation_pct && tri.queue_length === base.queue_length
  return (
    <section className="panel mt-3 p-3" aria-label={`${h.name} details`} data-testid="hospital-detail">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-[15px] font-semibold">{h.name}</div>
          <div className="text-[12px] text-muted">
            {h.treatment_spaces} treatment spaces · base staff {h.base_staff}
          </div>
        </div>
        <span
          className="shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold"
          style={{ color: STATUS_COLOR[status], background: 'rgb(255 255 255 / 0.05)' }}
        >
          {MAP_STATUS_LABEL[status]}
        </span>
      </div>
      <dl className="mt-2 grid grid-cols-4 gap-2 text-center">
        <Stat label="Saturation" value={`${Math.round(tri.saturation_pct)}%`} />
        <Stat label="Waiting" value={tri.queue_length} />
        <Stat label="Ambulances" value={tri.ambulances_waiting} />
        <Stat label="Staff" value={tri.staff_on_duty} />
      </dl>
      <div className="mt-2 flex items-center justify-between text-[12px] text-muted">
        <span>
          2-h forecast <span className="tabular text-text">{Math.round(tri.forecast_saturation_pct)}%</span>
        </span>
        <span className="tabular">
          {identical ? (
            'Both futures identical'
          ) : (
            <>
              <span className="text-baseline">{Math.round(base.saturation_pct)}%</span> without ·{' '}
              <span className="text-triagex-strong">{Math.round(tri.saturation_pct)}%</span> with
            </>
          )}
        </span>
      </div>
    </section>
  )
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg bg-ink/50 py-1.5">
      <dt className="text-[10px] uppercase tracking-wider text-faint">{label}</dt>
      <dd className="tabular text-[17px] font-semibold">{value}</dd>
    </div>
  )
}
