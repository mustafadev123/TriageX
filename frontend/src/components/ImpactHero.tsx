import { memo } from 'react'
import {
  activeRequest,
  atWindowEnd,
  clockAt,
  curvePointAt,
  formatHours,
  revealedDivergence,
  savedSoFar,
} from '../lib/demo'
import type { DashboardState } from '../types/dashboard'

/**
 * The hero counter follows the playhead using the API's running value
 * (curves.points[t].ambulance_hours_saved_so_far). Final figures from `impact` appear only
 * once playback reaches the end of the demo window, so nothing is revealed early.
 */
export const ImpactHero = memo(function ImpactHero({ state, playhead }: { state: DashboardState; playhead: number }) {
  const saved = savedSoFar(state, playhead)
  const point = curvePointAt(state, playhead)
  const diverged = revealedDivergence(state, playhead) !== null
  const done = atWindowEnd(state, playhead)
  const { primary, secondary, recovery } = state.impact
  const req = activeRequest(state)
  const wait = secondary.find((m) => m.key === 'mean_wait_minutes')
  const focus = state.hospitals.find((h) => h.id === recovery.hospital_id)?.short_name ?? ''

  let caption: string
  if (diverged) caption = `vs. without TriageX · ${state.timeline.demo_window.start_clock}–${clockAt(state, playhead)}`
  else if (req && req.confirmed_staff > 0) caption = `Futures identical until the shift starts at ${req.effective_clock}`
  else caption = 'Futures identical · no intervention active'

  const triRecovered = recovery.triagex_recovered_tick !== null && playhead >= recovery.triagex_recovered_tick
  const baseRecovered = recovery.baseline_recovered_tick !== null && playhead >= recovery.baseline_recovered_tick

  return (
    <section
      className={`panel flex items-center gap-5 px-5 py-3 ${diverged && saved > 0 ? 'border-triagex/50' : ''}`}
      aria-label="Impact"
    >
      <div className="min-w-[210px]">
        <div className="eyebrow">Ambulance-hours saved</div>
        <div className="flex items-baseline gap-2">
          <span
            className={`tabular text-[52px] leading-none font-bold transition-colors ${
              saved > 0 ? 'text-triagex-strong' : 'text-text'
            }`}
            data-testid="hours-saved"
          >
            {formatHours(saved)}
          </span>
          <span className="text-[16px] text-muted">h</span>
        </div>
        <div className="mt-1 text-[12px] text-muted" data-testid="hero-caption">
          {caption}
        </div>
      </div>

      <div className="grid flex-1 grid-cols-3 gap-3">
        <Figure
          label="Lost without TriageX"
          value={point ? `${formatHours(point.baseline_ambulance_hours_cumulative)} h` : '—'}
          tone="baseline"
        />
        <Figure
          label="Lost with TriageX"
          value={point ? `${formatHours(point.triagex_ambulance_hours_cumulative)} h` : '—'}
          tone="triagex"
        />
        {done ? (
          <Figure
            label="Final result"
            value={primary.pct_change !== null && primary.improved ? `${Math.abs(primary.pct_change).toFixed(0)}% fewer` : 'No change'}
            sub={wait && wait.improved ? `avg wait −${Math.abs(wait.delta).toFixed(1)} min` : undefined}
            tone="triagex"
            testId="final-result"
          />
        ) : triRecovered ? (
          <Figure
            label={`${focus} back to normal`}
            value={baseRecovered && recovery.minutes_sooner ? `${recovery.minutes_sooner} min sooner` : recovery.triagex_recovered_clock ?? ''}
            sub={baseRecovered ? `Without TriageX: ${recovery.baseline_recovered_clock}` : 'Without TriageX: still overloaded'}
            tone="triagex"
          />
        ) : (
          <Figure
            label="Patients waiting now"
            value={point ? `${point.triagex_patients_waiting}` : '—'}
            sub={point && diverged ? `${point.baseline_patients_waiting} without TriageX` : 'network-wide'}
          />
        )}
      </div>
    </section>
  )
})

function Figure({
  label,
  value,
  sub,
  tone,
  testId,
}: {
  label: string
  value: string
  sub?: string
  tone?: 'baseline' | 'triagex'
  testId?: string
}) {
  const color = tone === 'baseline' ? 'text-baseline' : tone === 'triagex' ? 'text-triagex-strong' : 'text-text'
  return (
    <div className="rounded-xl bg-ink/50 px-3 py-2" data-testid={testId}>
      <div className="text-[11px] uppercase tracking-wider text-faint">{label}</div>
      <div className={`tabular text-[22px] font-semibold ${color}`}>{value}</div>
      {sub && <div className="truncate text-[12px] text-muted">{sub}</div>}
    </div>
  )
}
