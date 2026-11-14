// Mirrors backend/app/models/dashboard.py (see docs/api.md). Keep in sync with the API.

export type TrackName = 'baseline' | 'triagex'
export type StatusName = 'normal' | 'pressure' | 'breach'
export type Origin = 'simulation' | 'operator' | 'simulated_reply' | 'webhook'
export type Severity = 'info' | 'warning' | 'critical' | 'success'
export type CurvePhase = 'before_intervention' | 'staff_en_route' | 'shift_active' | 'after_shift'

export interface TimeWindow {
  start_tick: number
  end_tick: number
  start_clock: string
  end_clock: string
}

export interface RunInfo {
  run_id: string
  version: number
  scenario: {
    id: string
    name: string
    description: string
    focus_hospital_id: string
    surge_onset_tick: number
  }
  seed: number
  created_at: string
  notice: string
}

export interface Timeline {
  tick_minutes: number
  horizon_ticks: number
  clocks: string[]
  demo_window: TimeWindow
  suggested_ms_per_tick: number
}

export interface HospitalInfo {
  id: string
  name: string
  short_name: string
  lat: number
  lon: number
  treatment_spaces: number
  base_staff: number
}

export interface HospitalPoint {
  tick: number
  status: StatusName
  saturation_pct: number
  occupied: number
  queue_length: number
  ambulances_waiting: number
  staff_on_duty: number
  staffed_capacity: number
  mean_wait_minutes: number
  forecast_saturation_pct: number
  diverting: boolean
}

export interface HospitalSeries {
  hospital_id: string
  baseline: HospitalPoint[]
  triagex: HospitalPoint[]
}

export interface CurvePoint {
  tick: number
  clock: string
  phase: CurvePhase
  diverged: boolean
  baseline_focus_saturation_pct: number
  triagex_focus_saturation_pct: number
  baseline_patients_waiting: number
  triagex_patients_waiting: number
  baseline_ambulances_waiting: number
  triagex_ambulances_waiting: number
  baseline_ambulance_hours_cumulative: number
  triagex_ambulance_hours_cumulative: number
  ambulance_hours_saved_so_far: number
}

export interface Band {
  kind: 'demo_window' | 'staff_lead_time' | 'shift_active' | 'level_loading_active'
  start_tick: number
  end_tick: number
  label: string
  hospital_id: string | null
}

export interface Marker {
  tick: number
  clock: string
  kind: string
  label: string
  hospital_id: string | null
  in_demo_window: boolean
}

export interface Flow {
  tick: number
  track: TrackName
  from_hospital_id: string
  to_hospital_id: string
  kind: 'diversion' | 'level_loading'
  count: number
  travel_ticks: number
}

export interface FeedEvent {
  id: string
  tick: number
  clock: string
  title: string
  detail: string
  kind: string
  track: 'both' | TrackName
  origin: Origin
  severity: Severity
  hospital_id: string | null
  wall_time: string | null
  in_demo_window: boolean
}

export interface ProposalCard {
  id: string
  kind: 'staffing' | 'level_loading'
  hospital_id: string
  tick: number
  clock: string
  title: string
  rationale: string
  forecast_saturation_pct: number
  current_saturation_pct: number
  recommended_staff: number | null
  status: 'available' | 'approved'
  recommended: boolean
}

export interface Confirmation {
  staff_id: string
  display_name: string
  source: 'simulated' | 'webhook'
  received_at: string
}

export interface StaffingRequest {
  request_id: string
  proposal_id: string
  hospital_id: string
  requested_staff: number
  confirmed_staff: number
  usable_staff: number
  proposal_tick: number
  proposal_clock: string
  status: 'awaiting_replies' | 'partially_confirmed' | 'fully_confirmed' | 'expired'
  approved_tick: number
  approved_clock: string
  effective_tick: number
  effective_clock: string
  end_tick: number
  approved_at: string
  expires_at: string
  confirmations: Confirmation[]
}

export interface HeroMetric {
  key: string
  label: string
  unit: string
  baseline: number
  triagex: number
  delta: number
  pct_change: number | null
  lower_is_better: boolean
  improved: boolean | null
  headline: string
}

export interface RecoveryView {
  hospital_id: string
  baseline_recovered_tick: number | null
  triagex_recovered_tick: number | null
  baseline_recovered_clock: string | null
  triagex_recovered_clock: string | null
  minutes_sooner: number | null
  baseline_breach_hours: number
  triagex_breach_hours: number
  headline: string
}

export interface Impact {
  state: 'awaiting_intervention' | 'intervention_scheduled'
  primary: HeroMetric
  secondary: HeroMetric[]
  recovery: RecoveryView
  window: TimeWindow
}

export interface DashboardState {
  run: RunInfo
  timeline: Timeline
  hospitals: HospitalInfo[]
  series: HospitalSeries[]
  curves: { focus_hospital_id: string; window: TimeWindow; points: CurvePoint[] }
  bands: Band[]
  markers: Marker[]
  transitions: unknown[]
  flows: Flow[]
  feed: FeedEvent[]
  proposals: ProposalCard[]
  staffing_requests: StaffingRequest[]
  impact: Impact
  divergence: { tick: number; clock: string } | null
  meta: { generated_at: string; recompute_ms: number | null; build_ms: number }
}

export interface ScenarioInfo {
  id: string
  name: string
  description: string
  focus_hospital_id: string
  surge_onset_tick: number
  demo_window_start_tick: number
  demo_window_end_tick: number
  start_clock: string
}

export interface IntegrationHealth {
  name: string
  status: 'ok' | 'starting' | 'not_configured' | 'not_implemented' | 'error'
  detail: string
}

export interface IntegrationsResponse {
  simulation: IntegrationHealth
  database: IntegrationHealth
  ai_primary: IntegrationHealth
  ai_fallback: IntegrationHealth
  messaging: IntegrationHealth
  websocket_clients: number
}
