/**
 * Pure presentation logic. Every value is read from the API's DashboardState; nothing here
 * computes outcomes. The playhead only decides how much of the (already computed) data is
 * revealed, so the futures can never appear to differ before the API says they do.
 */
import type {
  CurvePoint,
  DashboardState,
  FeedEvent,
  HospitalPoint,
  ProposalCard,
  StaffingRequest,
  TrackName,
} from '../types/dashboard'

export const FORECAST_LEAD_TICKS = 8 // the backend forecasts 2 hours (8 x 15 min) ahead

export function windowRange(state: DashboardState): { start: number; last: number } {
  const w = state.timeline.demo_window
  return { start: w.start_tick, last: w.end_tick - 1 }
}

export function clampTick(state: DashboardState, tick: number): number {
  const { start, last } = windowRange(state)
  return Math.min(last, Math.max(start, Math.round(tick)))
}

export function clockAt(state: DashboardState, tick: number): string {
  return state.timeline.clocks[tick] ?? ''
}

/** Divergence is revealed only once the playhead reaches it. */
export function revealedDivergence(state: DashboardState, playhead: number): number | null {
  const d = state.divergence?.tick
  return d !== undefined && d !== null && playhead >= d ? d : null
}

export interface ChartRow {
  tick: number
  clock: string
  // Saturation chart (focus hospital, % of treatment spaces)
  sharedSat: number | null
  baselineSat: number | null
  triagexSat: number | null
  forecastSat: number | null // 2-hour forecast made 8 ticks earlier (dotted)
  // Ambulance-hours chart (network, cumulative within the demo window)
  sharedHours: number | null
  baselineHours: number | null
  triagexHours: number | null
  savedBand: [number, number] | null
}

/**
 * Rows for both charts. Values after the playhead are null, so lines draw progressively.
 * Before the revealed divergence there is a single "shared" line (both tracks are equal
 * there, as the API guarantees); from the divergence tick the two futures split.
 */
export function chartRows(state: DashboardState, playhead: number): ChartRow[] {
  const divergence = revealedDivergence(state, playhead)
  const focus = state.series.find((s) => s.hospital_id === state.curves.focus_hospital_id)
  const forecastSource = focus?.triagex ?? []
  return state.curves.points.map((p: CurvePoint) => {
    const shown = p.tick <= playhead
    const split = divergence !== null && p.tick >= divergence - 1
    const shared = divergence === null || p.tick <= divergence - 1
    const made = forecastSource[p.tick - FORECAST_LEAD_TICKS]
    // The dotted forecast runs from the cursor 2 hours ahead: each point was forecast
    // 8 ticks earlier, so every value shown was already known at the playhead.
    const forecastVisible = made !== undefined && made.tick <= playhead && p.tick >= playhead
    return {
      tick: p.tick,
      clock: p.clock,
      sharedSat: shown && shared ? p.baseline_focus_saturation_pct : null,
      baselineSat: shown && split ? p.baseline_focus_saturation_pct : null,
      triagexSat: shown && split ? p.triagex_focus_saturation_pct : null,
      forecastSat: forecastVisible ? made.forecast_saturation_pct : null,
      sharedHours: shown && shared ? p.baseline_ambulance_hours_cumulative : null,
      baselineHours: shown && split ? p.baseline_ambulance_hours_cumulative : null,
      triagexHours: shown && split ? p.triagex_ambulance_hours_cumulative : null,
      savedBand:
        shown && split && divergence !== null && p.tick >= divergence
          ? [p.triagex_ambulance_hours_cumulative, p.baseline_ambulance_hours_cumulative]
          : null,
    }
  })
}

/**
 * Fixed y-axis maxima for the run, so axes never jump while the playhead moves. Scaled to
 * the observed values; forecast spikes above it are clipped visually (tooltips keep the
 * exact value).
 */
export function axisMaxima(state: DashboardState): { saturation: number; hours: number } {
  const { start, last } = windowRange(state)
  let sat = 100
  for (const p of state.curves.points) {
    if (p.tick >= start && p.tick <= last) {
      sat = Math.max(sat, p.baseline_focus_saturation_pct, p.triagex_focus_saturation_pct)
    }
  }
  const hours = Math.max(1, ...state.curves.points.map((p) => p.baseline_ambulance_hours_cumulative))
  return { saturation: Math.ceil((sat + 10) / 25) * 25, hours: Math.ceil((hours * 1.1) / 5) * 5 }
}

export function niceTicks(max: number): number[] {
  const step = max <= 20 ? 5 : max <= 60 ? 10 : max <= 160 ? 25 : 50
  const ticks: number[] = []
  for (let v = 0; v <= max; v += step) ticks.push(v)
  return ticks
}

export function curvePointAt(state: DashboardState, playhead: number): CurvePoint | undefined {
  return state.curves.points.find((p) => p.tick === playhead)
}

/** Ambulance-hours saved so far at the playhead (the API's running counter). */
export function savedSoFar(state: DashboardState, playhead: number): number {
  return curvePointAt(state, playhead)?.ambulance_hours_saved_so_far ?? 0
}

export function atWindowEnd(state: DashboardState, playhead: number): boolean {
  return playhead >= windowRange(state).last
}

export function formatHours(value: number): string {
  return Math.abs(value).toFixed(2)
}

/** Feed events already "happened" at the playhead, newest first. */
export function visibleFeed(state: DashboardState, playhead: number): FeedEvent[] {
  return state.feed
    .filter((e) => e.in_demo_window && e.tick <= playhead)
    .map((e, i) => ({ e, i }))
    .sort((a, b) => b.e.tick - a.e.tick || b.i - a.i)
    .map(({ e }) => e)
}

export function recommendedProposal(state: DashboardState): ProposalCard | undefined {
  return state.proposals.find((p) => p.recommended)
}

export function activeRequest(state: DashboardState): StaffingRequest | undefined {
  const rec = recommendedProposal(state)
  return (
    state.staffing_requests.find((r) => r.proposal_id === rec?.id) ?? state.staffing_requests[0]
  )
}

export function pointAt(
  state: DashboardState,
  hospitalId: string,
  track: TrackName,
  tick: number,
): HospitalPoint | undefined {
  return state.series.find((s) => s.hospital_id === hospitalId)?.[track][tick]
}

export type MapStatus = 'normal' | 'approaching' | 'overloaded' | 'diverting' | 'recovering'

export const STATUS_COLOR: Record<MapStatus, string> = {
  normal: 'var(--color-st-normal)',
  approaching: 'var(--color-st-approaching)',
  overloaded: 'var(--color-st-overloaded)',
  diverting: 'var(--color-st-diverting)',
  recovering: 'var(--color-st-recovering)',
}

export const MAP_STATUS_LABEL: Record<MapStatus, string> = {
  normal: 'Normal',
  approaching: 'Approaching capacity',
  overloaded: 'Overloaded',
  diverting: 'Diverting',
  recovering: 'Recovering',
}

/**
 * Map status from the API point at the playhead. "Recovering" is a display state:
 * not in breach now, but in breach at some point in the previous 2 hours.
 */
export function mapStatus(
  state: DashboardState,
  hospitalId: string,
  track: TrackName,
  tick: number,
): MapStatus {
  const p = pointAt(state, hospitalId, track, tick)
  if (!p) return 'normal'
  if (p.diverting) return 'diverting'
  if (p.status === 'breach') return 'overloaded'
  const series = state.series.find((s) => s.hospital_id === hospitalId)?.[track] ?? []
  const recent = series.slice(Math.max(0, tick - FORECAST_LEAD_TICKS), tick)
  if (recent.some((r) => r.status === 'breach')) return 'recovering'
  return p.status === 'pressure' ? 'approaching' : 'normal'
}

// --- Presentation mode -------------------------------------------------------------------

export type StepId = 'start' | 'predict' | 'approve' | 'confirm' | 'activate' | 'reveal'

export interface PresentationStep {
  id: StepId
  label: string
  tick: number
  /** Something only the presenter may do. Next Event never does it for them. */
  needs?: 'approval' | 'confirmation'
}

export function presentationSteps(state: DashboardState): PresentationStep[] {
  const { last } = windowRange(state)
  const rec = recommendedProposal(state)
  const req = activeRequest(state)
  const proposalTick = rec?.tick ?? state.run.scenario.surge_onset_tick
  const approvalTick = req?.approved_tick ?? clampTick(state, proposalTick + 1)
  return [
    { id: 'start', label: 'Start Surge', tick: state.run.scenario.surge_onset_tick },
    { id: 'predict', label: 'Show Prediction', tick: proposalTick },
    { id: 'approve', label: 'Approve Outreach', tick: approvalTick, needs: 'approval' },
    { id: 'confirm', label: 'Receive Confirmations', tick: approvalTick, needs: 'confirmation' },
    { id: 'activate', label: 'Activate Shift', tick: req?.effective_tick ?? approvalTick + 3 },
    { id: 'reveal', label: 'Reveal Impact', tick: last },
  ]
}

export function stepSatisfied(state: DashboardState, step: PresentationStep, playhead: number) {
  if (playhead < step.tick) return false
  const req = activeRequest(state)
  if (step.needs === 'approval') return req !== undefined
  if (step.needs === 'confirmation') return (req?.confirmed_staff ?? 0) > 0
  return true
}

/** Index of the next unfinished step, or null when the walkthrough is complete. */
export function nextStepIndex(state: DashboardState, playhead: number): number | null {
  const steps = presentationSteps(state)
  const i = steps.findIndex((s) => !stepSatisfied(state, s, playhead))
  return i === -1 ? null : i
}

export type NextAction =
  | { kind: 'advance'; toTick: number; step: PresentationStep }
  | { kind: 'await'; step: PresentationStep; reason: string }
  | { kind: 'done' }

/** What the Next Event control does. It only ever moves the playhead. */
export function nextAction(state: DashboardState, playhead: number): NextAction {
  const i = nextStepIndex(state, playhead)
  if (i === null) return { kind: 'done' }
  const step = presentationSteps(state)[i]
  if (playhead < step.tick) return { kind: 'advance', toTick: step.tick, step }
  if (step.needs === 'approval') {
    return { kind: 'await', step, reason: 'Approve the outreach to continue' }
  }
  return { kind: 'await', step, reason: 'Waiting for a staff reply (use Simulate Staff Reply)' }
}
