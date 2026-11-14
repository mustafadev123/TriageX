import { memo, useMemo } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
  ReferenceLine,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { useElementSize } from '../hooks/useElementSize'
import {
  activeRequest,
  axisMaxima,
  chartRows,
  clockAt,
  niceTicks,
  recommendedProposal,
  revealedDivergence,
  windowRange,
  type ChartRow,
} from '../lib/demo'
import type { DashboardState } from '../types/dashboard'

const C = {
  shared: '#cbd5e1',
  baseline: '#fb923c',
  triagex: '#2dd4bf',
  grid: '#15213a',
  axis: '#5b6d8e',
}

interface ChartMarker {
  tick: number
  label: string
  color: string
  emphasis?: boolean
  dim?: boolean
}

/** Markers the audience has reached; the scheduled shift start is shown dimmed. */
function markers(state: DashboardState, playhead: number): ChartMarker[] {
  const out: ChartMarker[] = []
  const onset = state.run.scenario.surge_onset_tick
  if (playhead >= onset) out.push({ tick: onset, label: 'Surge', color: '#f43f5e' })
  const card = recommendedProposal(state)
  if (card && playhead >= card.tick) out.push({ tick: card.tick, label: 'Forecast warning', color: '#fbbf24' })
  const req = activeRequest(state)
  if (req && playhead >= req.approved_tick) out.push({ tick: req.approved_tick, label: 'Approved', color: '#94a3b8' })
  if (req && req.confirmed_staff > 0) {
    const active = playhead >= req.effective_tick
    out.push({ tick: req.effective_tick, label: active ? 'Shift active' : 'Shift scheduled', color: C.triagex, emphasis: active, dim: !active })
  }
  const div = revealedDivergence(state, playhead)
  if (div !== null && div !== req?.effective_tick) out.push({ tick: div, label: 'Futures diverge', color: C.triagex })
  return out
}

function hourTicks(start: number, last: number): number[] {
  const ticks: number[] = []
  for (let t = start; t <= last; t += 4) ticks.push(t)
  return ticks
}

interface TooltipProps {
  active?: boolean
  label?: number
  payload?: { dataKey?: string; value?: number | number[] | null }[]
  state: DashboardState
  unit: string
}

function ChartTooltip({ active, label, payload, state, unit }: TooltipProps) {
  if (!active || label === undefined || !payload?.length) return null
  const names: Record<string, [string, string]> = {
    sharedSat: ['Both futures', C.shared],
    baselineSat: ['Without TriageX', C.baseline],
    triagexSat: ['With TriageX', C.triagex],
    forecastSat: ['2-h forecast', '#99f6e4'],
    sharedHours: ['Both futures', C.shared],
    baselineHours: ['Without TriageX', C.baseline],
    triagexHours: ['With TriageX', C.triagex],
  }
  const rows = payload.filter((p) => p.dataKey && names[p.dataKey] && typeof p.value === 'number')
  if (!rows.length) return null
  return (
    <div className="rounded-lg border border-line bg-panel/95 px-3 py-2 text-[12px] shadow-xl">
      <div className="mb-1 font-semibold">{clockAt(state, label)}</div>
      {rows.map((p) => (
        <div key={p.dataKey} className="flex items-center justify-between gap-4">
          <span style={{ color: names[p.dataKey!][1] }}>{names[p.dataKey!][0]}</span>
          <span className="tabular">
            {(p.value as number).toFixed(unit === '%' ? 0 : 2)}
            {unit}
          </span>
        </div>
      ))}
    </div>
  )
}

interface ChartProps {
  state: DashboardState
  rows: ChartRow[]
  playhead: number
  kind: 'saturation' | 'hours'
  yMax: number
}

function FutureChart({ state, rows, playhead, kind, yMax }: ChartProps) {
  const [ref, { width, height }] = useElementSize<HTMLDivElement>()
  const { start, last } = windowRange(state)
  const req = activeRequest(state)
  const ms = markers(state, playhead)
  const keys =
    kind === 'saturation'
      ? { shared: 'sharedSat', base: 'baselineSat', tri: 'triagexSat' }
      : { shared: 'sharedHours', base: 'baselineHours', tri: 'triagexHours' }
  const unit = kind === 'saturation' ? '%' : ' h'
  const diverged = revealedDivergence(state, playhead) !== null

  return (
    <div ref={ref} className="min-h-0 flex-1" data-testid={`chart-${kind}`}>
      {width > 0 && height > 0 && (
        <ComposedChart width={width} height={height} data={rows} margin={{ top: 8, right: 18, bottom: 0, left: 0 }}>
          <defs>
            <filter id={`glow-${kind}`} x="-20%" y="-50%" width="140%" height="200%">
              <feGaussianBlur stdDeviation="3.5" result="b" />
              <feMerge>
                <feMergeNode in="b" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>
          <CartesianGrid stroke={C.grid} vertical={false} />
          <XAxis
            dataKey="tick"
            type="number"
            domain={[start, last]}
            ticks={hourTicks(start, last)}
            tickFormatter={(t: number) => clockAt(state, t)}
            stroke={C.axis}
            tick={{ fontSize: 12 }}
            allowDataOverflow
          />
          <YAxis
            domain={[0, yMax]}
            ticks={niceTicks(yMax)}
            allowDataOverflow
            stroke={C.axis}
            tick={{ fontSize: 12 }}
            width={44}
            tickFormatter={(v: number) => (kind === 'saturation' ? `${v}%` : `${v}`)}
          />
          {req && (
            <ReferenceArea
              x1={req.approved_tick}
              x2={req.effective_tick}
              fill={C.triagex}
              fillOpacity={0.08}
              stroke={C.triagex}
              strokeOpacity={0.25}
              strokeDasharray="3 3"
              label={kind === 'saturation' ? { value: 'Staff en route · 45 min', position: 'insideBottom', fill: '#5eead4', fontSize: 11 } : undefined}
            />
          )}
          {kind === 'saturation' && (
            <ReferenceLine
              y={100}
              stroke="#f43f5e"
              strokeOpacity={0.5}
              strokeDasharray="6 4"
              label={{ value: 'All treatment spaces full', position: 'insideBottomRight', fill: '#fb7185', fontSize: 11 }}
            />
          )}
          {ms.map((m, i) => (
            <ReferenceLine
              key={`${m.label}-${m.tick}`}
              x={m.tick}
              stroke={m.color}
              strokeWidth={m.emphasis ? 2.5 : 1.25}
              strokeOpacity={m.dim ? 0.4 : 0.9}
              strokeDasharray={m.dim ? '4 4' : undefined}
              label={
                kind === 'saturation'
                  ? (props: { viewBox?: { x?: number; y?: number } }) => (
                      // Stack labels so close markers (16:00, 16:15, 16:30) stay readable.
                      <text
                        x={(props.viewBox?.x ?? 0) + 4}
                        y={(props.viewBox?.y ?? 0) + 11 + (i % 4) * 13}
                        fill={m.color}
                        fontSize={11}
                        fontWeight={m.emphasis ? 700 : 500}
                        opacity={m.dim ? 0.6 : 1}
                      >
                        {m.label}
                      </text>
                    )
                  : undefined
              }
            />
          ))}
          {kind === 'hours' && (
            <Area
              dataKey="savedBand"
              stroke="none"
              fill={C.triagex}
              fillOpacity={0.16}
              isAnimationActive={false}
              connectNulls={false}
            />
          )}
          <Line dataKey={keys.shared} stroke={C.shared} strokeWidth={2.5} dot={false} isAnimationActive={false} connectNulls={false} />
          <Line dataKey={keys.base} stroke={C.baseline} strokeWidth={2.5} dot={false} isAnimationActive={false} connectNulls={false} />
          <Line
            dataKey={keys.tri}
            stroke={C.triagex}
            strokeWidth={diverged ? 3.5 : 2.5}
            dot={false}
            isAnimationActive={false}
            connectNulls={false}
            filter={diverged ? `url(#glow-${kind})` : undefined}
          />
          {kind === 'saturation' && (
            <Line
              dataKey="forecastSat"
              stroke="#99f6e4"
              strokeOpacity={0.75}
              strokeWidth={2}
              strokeDasharray="2 5"
              strokeLinecap="round"
              dot={false}
              isAnimationActive={false}
              connectNulls={false}
            />
          )}
          <ReferenceLine x={playhead} stroke="#e8eef8" strokeOpacity={0.6} strokeWidth={1} />
          <Tooltip
            content={<ChartTooltip state={state} unit={unit} />}
            cursor={{ stroke: '#e8eef8', strokeOpacity: 0.25 }}
            isAnimationActive={false}
          />
        </ComposedChart>
      )}
    </div>
  )
}

function Legend({ diverged, forecast }: { diverged: boolean; forecast?: boolean }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted">
      {!diverged && <Key color={C.shared} label="Both futures (identical)" />}
      <Key color={C.baseline} label="Without TriageX" dim={!diverged} />
      <Key color={C.triagex} label="With TriageX" dim={!diverged} />
      {forecast && <Key color="#99f6e4" label="2-h forecast" dashed />}
    </div>
  )
}

function Key({ color, label, dashed, dim }: { color: string; label: string; dashed?: boolean; dim?: boolean }) {
  return (
    <span className={`flex items-center gap-1.5 ${dim ? 'opacity-45' : ''}`}>
      <span
        className="inline-block h-0 w-5 border-t-[3px]"
        style={{ borderColor: color, borderStyle: dashed ? 'dotted' : 'solid' }}
      />
      {label}
    </span>
  )
}

export const FuturesCharts = memo(function FuturesCharts({ state, playhead }: { state: DashboardState; playhead: number }) {
  const rows = useMemo(() => chartRows(state, playhead), [state, playhead])
  const max = useMemo(() => axisMaxima(state), [state])
  const diverged = revealedDivergence(state, playhead) !== null
  const focus = state.hospitals.find((h) => h.id === state.curves.focus_hospital_id)
  const req = activeRequest(state)
  const shiftActive = !!req && req.confirmed_staff > 0 && playhead >= req.effective_tick

  return (
    <section className="panel flex min-h-0 flex-1 flex-col p-3" aria-label="Two futures comparison">
      <header className="flex items-center justify-between gap-3">
        <div>
          <div className="eyebrow">One surge · Two futures</div>
          <div className="text-[13px] text-muted">Same emergency. Same patient arrivals. A different response.</div>
        </div>
        {shiftActive ? (
          <div
            className="glow-teal rounded-lg border border-triagex/60 bg-triagex/10 px-3 py-1.5 text-[14px] font-bold tracking-wide text-triagex-strong"
            data-testid="shift-active-banner"
          >
            SHIFT ACTIVE — {focus?.short_name.toUpperCase()}
          </div>
        ) : req && req.confirmed_staff > 0 ? (
          <div className="rounded-lg border border-line px-3 py-1.5 text-[13px] text-muted">
            Shift scheduled {req.effective_clock}
          </div>
        ) : null}
      </header>

      <div className="mt-2 flex min-h-0 flex-[1.15] flex-col">
        <div className="flex items-center justify-between">
          <h2 className="text-[14px] font-semibold">
            {focus?.short_name} saturation <span className="font-normal text-muted">· % of treatment spaces</span>
          </h2>
          <Legend diverged={diverged} forecast />
        </div>
        <FutureChart state={state} rows={rows} playhead={playhead} kind="saturation" yMax={max.saturation} />
      </div>

      <div className="mt-1 flex min-h-0 flex-1 flex-col">
        <div className="flex items-center justify-between">
          <h2 className="text-[14px] font-semibold">
            Ambulance-hours lost to handoff delays <span className="font-normal text-muted">· network, cumulative</span>
          </h2>
          {diverged && <span className="text-[12px] text-triagex-strong">Shaded gap = ambulance-hours saved</span>}
        </div>
        <FutureChart state={state} rows={rows} playhead={playhead} kind="hours" yMax={max.hours} />
      </div>
    </section>
  )
})
